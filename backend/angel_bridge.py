"""
Voice Guard — backend/angel_bridge.py
엔젤시스템 기생형 브리지 API v1.0

[불변 원칙]
  - evidence_ledger / outbox_events 수정 0
  - angel_review_event Append-Only (INSERT만)
  - 상태 전이: DETECTED → REVIEW_REQUIRED → APPROVED_FOR_EXPORT → EXPORTED
  - 원본 증거는 읽기 전용 JOIN으로만 참조

[상태 머신]
  DETECTED            워커 봉인 완료 시 자동 생성 (트리거)
  REVIEW_REQUIRED     관리자가 검수 대기열에 올림 (또는 자동)
  APPROVED_FOR_EXPORT 관리자 승인 → 엔젤 CSV 내보내기 대기
  REJECTED            반영 제외 (사유 필수)
  RECLASSIFIED        분류 정정 후 재검수 대기
  EXPORTED            CSV 내보내기 완료
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger("voice_guard.angel_bridge")

router = APIRouter(prefix="/api/v2/angel", tags=["엔젤 브리지"])


# ── 엔진 주입 (main.py에서 app.include_router 시 설정) ────────────
_engine = None
_redis_pub = None


def init_angel_bridge(engine, redis_pub=None):
    """main.py에서 호출하여 DB 엔진과 Redis 주입"""
    global _engine, _redis_pub
    _engine = engine
    _redis_pub = redis_pub


# ══════════════════════════════════════════════════════════════════
# [1] 반영 대기함 목록 조회 — GET /api/v2/angel/pending
#
# evidence_ledger JOIN angel_review_event 최신 상태
# 관리자 판정 허브의 좌측 리스트 데이터 소스
# ══════════════════════════════════════════════════════════════════

@router.get("/pending")
async def list_pending(
    facility_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(
        None,
        description="DETECTED|REVIEW_REQUIRED|APPROVED_FOR_EXPORT|REJECTED|RECLASSIFIED|EXPORTED"
    ),
    limit: int = Query(100, le=500),
):
    """
    반영 대기함: 판정 대기 건 목록.
    각 ledger_id의 최신 상태만 반환 (DISTINCT ON).
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    where_clauses = []
    params = {"lim": limit}

    if facility_id:
        where_clauses.append("e.facility_id = :fid")
        params["fid"] = facility_id
    if status_filter:
        where_clauses.append("latest.status = :sf")
        params["sf"] = status_filter

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""  # nosec B608 — hardcoded SQL fragments only, user values bound via params dict

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    latest.id           AS review_event_id,
                    latest.ledger_id,
                    latest.status       AS angel_status,
                    latest.reviewer_id,
                    latest.decision_note,
                    latest.reclassified_to,
                    latest.created_at   AS review_ts,
                    e.facility_id,
                    e.beneficiary_id,
                    e.shift_id,
                    e.care_type,
                    e.ingested_at,
                    e.audio_sha256,
                    e.chain_hash,
                    e.transcript_text,
                    e.worm_object_key,
                    e.is_flagged
                FROM (
                    SELECT DISTINCT ON (ledger_id) *
                    FROM angel_review_event
                    ORDER BY ledger_id, created_at DESC
                ) latest
                JOIN v_evidence_sealed e ON e.id = latest.ledger_id
                {where_sql}
                ORDER BY latest.created_at DESC
                LIMIT :lim
            """), params).fetchall()

        return {
            "count": len(rows),
            "items": [dict(r._mapping) for r in rows],
        }
    except Exception as e:
        logger.error(f"[ANGEL] pending 조회 실패: {e}")
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════
# [2] 판정 제출 — POST /api/v2/angel/review
#
# 관리자가 승인/보류/반영제외/분류정정 판정
# angel_review_event에 새 row INSERT (Append-Only)
# ══════════════════════════════════════════════════════════════════

class ReviewDecision(BaseModel):
    ledger_id: str
    decision: str  # APPROVED | REJECTED | RECLASSIFIED
    reviewer_id: str
    note: str = ""
    reclassified_to: Optional[str] = None  # RECLASSIFIED 시 필수


# 유효 상태 전이 맵: {현재 상태: [허용되는 다음 상태]}
VALID_TRANSITIONS = {
    "DETECTED":             ["REVIEW_REQUIRED", "APPROVED_FOR_EXPORT", "REJECTED"],
    "REVIEW_REQUIRED":      ["APPROVED_FOR_EXPORT", "REJECTED", "RECLASSIFIED"],
    "RECLASSIFIED":         ["REVIEW_REQUIRED", "APPROVED_FOR_EXPORT", "REJECTED"],
    "REJECTED":             ["REVIEW_REQUIRED"],   # 재검토 가능
    "APPROVED_FOR_EXPORT":  ["EXPORTED"],           # 내보내기만 가능
    "EXPORTED":             [],                     # 최종 — 전이 불가
}


@router.post("/review", status_code=201)
async def submit_review(body: ReviewDecision):
    """
    판정 이벤트 INSERT.
    상태 전이 유효성 검증 후 angel_review_event에 Append.
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")
    if not body.reviewer_id.strip():
        raise HTTPException(422, "reviewer_id 필수")
    if body.decision == "RECLASSIFIED" and not body.reclassified_to:
        raise HTTPException(422, "RECLASSIFIED 시 reclassified_to 필수")

    # 현재 최신 상태 조회
    try:
        with _engine.connect() as conn:
            current = conn.execute(text("""
                SELECT status FROM angel_review_event
                WHERE ledger_id = :lid
                ORDER BY created_at DESC
                LIMIT 1
            """), {"lid": body.ledger_id}).fetchone()
    except Exception as e:
        raise HTTPException(500, str(e))

    if not current:
        raise HTTPException(
            404, f"ledger_id '{body.ledger_id}' 판정 없음",
        )

    current_status = current.status
    allowed = VALID_TRANSITIONS.get(current_status, [])
    if body.decision not in allowed:
        raise HTTPException(
            409,
            f"상태 전이 불가: {current_status} → {body.decision}. "
            f"허용: {allowed}"
        )

    # Append-Only INSERT
    event_id = str(uuid4())
    now = datetime.now(timezone.utc)

    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO angel_review_event (
                    id, ledger_id, status, reviewer_id,
                    decision_note, reclassified_to, created_at
                ) VALUES (
                    :id, :lid, :status, :reviewer,
                    :note, :reclass, :ts
                )
            """), {
                "id": event_id,
                "lid": body.ledger_id,
                "status": body.decision,
                "reviewer": body.reviewer_id.strip(),
                "note": body.note.strip() if body.note else None,
                "reclass": body.reclassified_to,
                "ts": now,
            })

        logger.info(
            f"[ANGEL] 판정: ledger={body.ledger_id} "
            f"{current_status} → {body.decision} by {body.reviewer_id}"
        )

    except Exception as e:
        logger.error(f"[ANGEL] 판정 INSERT 실패: {e}")
        raise HTTPException(500, str(e))

    # SSE 알림 (선택적)
    if _redis_pub:
        try:
            await _redis_pub.publish(
                "sse:dashboard",
                json.dumps({
                    "event": "angel_review",
                    "data": {
                        "ledger_id": body.ledger_id,
                        "previous": current_status,
                        "new_status": body.decision,
                        "reviewer_id": body.reviewer_id,
                    },
                }, ensure_ascii=False),
            )
        except Exception:
            pass

    return {
        "event_id": event_id,
        "ledger_id": body.ledger_id,
        "transition": f"{current_status} → {body.decision}",
        "reviewed_at": now.isoformat(),
    }


# ══════════════════════════════════════════════════════════════════
# [3] 6대 필수항목 커버리지 — GET /api/v2/angel/coverage
#
# 수급자별 6대 항목 기록 여부 + 누락 경고
# 프론트 좌측 누락 감시 위젯 데이터 소스
# ══════════════════════════════════════════════════════════════════

CARE_6_REQUIRED = [
    "식사 보조", "배변 보조", "체위 변경",
    "구강 위생", "목욕 보조", "이동 보조",
]


@router.get("/coverage")
async def get_coverage(
    facility_id: Optional[str] = Query(None),
):
    """
    6대 필수항목 커버리지.
    evidence_ledger에서 수급자별 기록된 care_type 집계.
    누락 항목이 있으면 missing_items에 표시.
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    # ── [TD-06] WHERE 1=1 안전 동적 빌더 ────────────────────────
    # 기존 코드는 facility_id 미지정 시 `FROM evidence_ledger e AND ...`로
    # SQL 문법 오류 → 500 에러 발생. 조건 배열 + 1=1 패턴으로 차단.
    filters = ["1=1", "e.care_type IS NOT NULL"]
    params: dict = {}
    if facility_id:
        filters.append("e.facility_id = :fid")
        params["fid"] = facility_id
    where_sql = "WHERE " + " AND ".join(filters)  # nosec B608 — hardcoded fragments only

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT e.beneficiary_id, e.care_type
                FROM evidence_ledger e
                {where_sql}
                ORDER BY e.beneficiary_id
            """), params).fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))

    # 집계
    bene_map: dict = {}
    for r in rows:
        bid = r.beneficiary_id or "unknown"
        bene_map.setdefault(bid, set()).add(r.care_type)

    results = []
    total_missing = 0
    for bid, recorded in bene_map.items():
        missing = [item for item in CARE_6_REQUIRED if item not in recorded]
        total_missing += len(missing)
        results.append({
            "beneficiary_id": bid,
            "recorded": list(recorded),
            "missing_items": missing,
            "coverage_rate": round((6 - len(missing)) / 6 * 100, 1),
            "is_complete": len(missing) == 0,
        })

    return {
        "total_beneficiaries": len(results),
        "total_missing_items": total_missing,
        "beneficiaries": results,
    }


# ══════════════════════════════════════════════════════════════════
# [4] 단건 상세 + 이력 — GET /api/v2/angel/detail/{ledger_id}
#
# 우측 패널: 원음 재생 정보 + 해시 증빙 + 전체 판정 이력
# ══════════════════════════════════════════════════════════════════

@router.get("/detail/{ledger_id}")
async def get_detail(ledger_id: str):
    """
    단건 상세: evidence_ledger 증거 + angel_review_event 전체 이력.
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    try:
        with _engine.connect() as conn:
            # 증거 원본 (읽기 전용)
            evidence = conn.execute(text("""
                SELECT id, facility_id, beneficiary_id, shift_id,
                       care_type, recorded_at, ingested_at,
                       audio_sha256, transcript_sha256, chain_hash,
                       transcript_text,
                       worm_bucket, worm_object_key, worm_retain_until,
                       is_flagged, gps_lat, gps_lon,
                       audio_size_kb, is_sealed, sealed_at
                FROM v_evidence_sealed
                WHERE id = :lid
            """), {"lid": ledger_id}).fetchone()

            if not evidence:
                raise HTTPException(404, f"ledger_id '{ledger_id}' 없음")

            # 판정 이력 (전체, 시간순)
            events = conn.execute(text("""
                SELECT id, status, reviewer_id, decision_note,
                       reclassified_to, export_batch_id, created_at
                FROM angel_review_event
                WHERE ledger_id = :lid
                ORDER BY created_at ASC
            """), {"lid": ledger_id}).fetchall()

        return {
            "evidence": dict(evidence._mapping),
            "review_history": [dict(e._mapping) for e in events],
            "current_status": dict(events[-1]._mapping)["status"] if events else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
