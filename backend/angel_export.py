"""
Voice Guard — backend/angel_export.py
엔젤시스템 증거 브리지: CSV Export 엔진 + ZIP 패키징

[기능]
  1. angel_import.csv   — 엔젤시스템 업로드용 (6대 필수항목 매핑)
  2. proof_manifest.csv — 법적 증빙용 (WORM 해시, 오디오 객체 키)
  3. export_receipt.json — 배치 메타데이터 (생성시각, 파일 해시)
  → 3개 파일을 ZIP으로 묶어 단일 다운로드

[불변 원칙]
  - evidence_ledger 수정 0 (SELECT만)
  - angel_review_event Append-Only (EXPORTED 상태 INSERT)
  - bridge_export_batch INSERT로 배치 불변 기록
"""

import csv
import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger("voice_guard.angel_export")

router = APIRouter(
    prefix="/api/v2/angel/export",
    tags=["엔젤 Export"],
)

_engine = None
_redis_pub = None


def init_angel_export(engine, redis_pub=None):
    global _engine, _redis_pub
    _engine = engine
    _redis_pub = redis_pub


# ── 엔젤시스템 CSV 컬럼 매핑 ─────────────────────────────────────
ANGEL_CSV_COLUMNS = [
    "서비스일자",
    "수급자성명",
    "수급자생년월일",
    "장기요양등급",
    "급여유형코드",
    "급여유형명",
    "시작시간",
    "종료시간",
    "제공시간(분)",
    "요양보호사성명",
    "요양보호사자격번호",
    "기관기호",
    "비고",
]

# 내부 care_type → 엔젤 급여유형코드 매핑
CARE_TYPE_MAP = {
    "식사 보조": ("11", "식사도움"),
    "배변 보조": ("12", "배설도움"),
    "체위 변경": ("13", "체위변환"),
    "구강 위생": ("14", "구강관리"),
    "목욕 보조": ("15", "목욕도움"),
    "이동 보조": ("16", "이동도움"),
}

PROOF_CSV_COLUMNS = [
    "ledger_id",
    "facility_id",
    "beneficiary_id",
    "shift_id",
    "care_type",
    "recorded_at",
    "ingested_at",
    "audio_sha256",
    "transcript_sha256",
    "chain_hash",
    "worm_bucket",
    "worm_object_key",
    "worm_retain_until",
    "angel_status",
    "reviewer_id",
    "reviewed_at",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_angel_csv(rows: list) -> bytes:
    """APPROVED_FOR_EXPORT 건을 엔젤시스템 양식 CSV로 변환."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(ANGEL_CSV_COLUMNS)

    for r in rows:
        care = r.get("care_type") or ""
        code, name = CARE_TYPE_MAP.get(care, ("99", care))
        recorded = r.get("recorded_at", "")
        if recorded:
            dt = datetime.fromisoformat(str(recorded))
            date_str = dt.strftime("%Y-%m-%d")
            start_str = dt.strftime("%H:%M")
            end_str = ""  # 종료시간은 shift 단위로 별도 계산 필요
        else:
            date_str = start_str = end_str = ""

        writer.writerow([
            date_str,
            r.get("beneficiary_id", ""),
            "",  # 생년월일 — 수급자 마스터 연동 시 채움
            "",  # 등급 — 수급자 마스터 연동 시 채움
            code,
            name,
            start_str,
            end_str,
            "",  # 제공시간
            "",  # 요양보호사 — 인사 마스터 연동 시 채움
            "",  # 자격번호
            r.get("facility_id", ""),
            f"VG-{str(r.get('ledger_id', ''))[:8]}",
        ])

    return buf.getvalue().encode("utf-8-sig")  # BOM for Excel


def _build_proof_csv(rows: list) -> bytes:
    """법적 증빙 매니페스트 CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(PROOF_CSV_COLUMNS)

    for r in rows:
        writer.writerow([
            r.get(col, "") for col in PROOF_CSV_COLUMNS
        ])

    return buf.getvalue().encode("utf-8-sig")


def _build_receipt(
    batch_id: str,
    facility_id: str,
    item_count: int,
    ledger_ids: list,
    angel_sha: str,
    proof_sha: str,
    zip_sha: str,
    ts: str,
) -> bytes:
    """배치 메타데이터 JSON."""
    receipt = {
        "export_batch_id": batch_id,
        "facility_id": facility_id,
        "exported_at": ts,
        "item_count": item_count,
        "ledger_ids": ledger_ids,
        "files": {
            "angel_import.csv": {
                "sha256": angel_sha,
                "purpose": "엔젤시스템 업로드용",
            },
            "proof_manifest.csv": {
                "sha256": proof_sha,
                "purpose": "환수 방어 법적 증빙",
            },
        },
        "zip_sha256": zip_sha,
        "integrity_note": (
            "이 파일은 Voice Guard 증거 원장에서 "
            "자동 생성되었으며, bridge_export_batch "
            "테이블에 불변 기록됩니다."
        ),
    }
    return json.dumps(
        receipt, ensure_ascii=False, indent=2,
    ).encode("utf-8")


# ══════════════════════════════════════════════════════════════════
# [1] Export 대상 미리보기 — GET /api/v2/angel/export/preview
# ══════════════════════════════════════════════════════════════════

@router.get("/preview")
async def preview_export(
    facility_id: Optional[str] = Query(None),
):
    """APPROVED_FOR_EXPORT 상태 건 미리보기 (ZIP 생성 전 확인용)."""
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    where = "AND e.facility_id = :fid" if facility_id else ""
    params = {"fid": facility_id} if facility_id else {}

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    latest.ledger_id,
                    latest.reviewer_id,
                    latest.created_at AS reviewed_at,
                    e.facility_id,
                    e.beneficiary_id,
                    e.shift_id,
                    e.care_type,
                    e.recorded_at,
                    e.audio_sha256,
                    e.chain_hash,
                    e.worm_object_key
                FROM (
                    SELECT DISTINCT ON (ledger_id) *
                    FROM angel_review_event
                    ORDER BY ledger_id, created_at DESC
                ) latest
                JOIN evidence_ledger e
                    ON e.id = latest.ledger_id
                WHERE latest.status = 'APPROVED_FOR_EXPORT'
                {where}
                ORDER BY e.recorded_at ASC
            """), params).fetchall()

        items = [dict(r._mapping) for r in rows]
        return {
            "count": len(items),
            "items": items,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════
# [2] ZIP Export — POST /api/v2/angel/export/zip
#
# 1) APPROVED_FOR_EXPORT 건 수집
# 2) angel_import.csv + proof_manifest.csv 생성
# 3) bridge_export_batch INSERT (배치 불변 기록)
# 4) angel_review_event에 EXPORTED INSERT (Append)
# 5) ZIP 스트리밍 반환
# ══════════════════════════════════════════════════════════════════

class ExportRequest(BaseModel):
    facility_id: str
    exported_by: str = "admin"


@router.post("/zip")
async def export_zip(body: ExportRequest):
    """
    증빙 팩 ZIP 생성 + 배치 원장 기록 + 상태 전이.
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    now = datetime.now(timezone.utc)
    batch_id = str(uuid4())

    # ── A. APPROVED_FOR_EXPORT 건 수집 ──────────────────────────
    try:
        with _engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    latest.ledger_id,
                    latest.status AS angel_status,
                    latest.reviewer_id,
                    latest.created_at AS reviewed_at,
                    e.facility_id,
                    e.beneficiary_id,
                    e.shift_id,
                    e.care_type,
                    e.recorded_at,
                    e.ingested_at,
                    e.audio_sha256,
                    e.transcript_sha256,
                    e.chain_hash,
                    e.worm_bucket,
                    e.worm_object_key,
                    e.worm_retain_until
                FROM (
                    SELECT DISTINCT ON (ledger_id) *
                    FROM angel_review_event
                    ORDER BY ledger_id, created_at DESC
                ) latest
                JOIN evidence_ledger e
                    ON e.id = latest.ledger_id
                WHERE latest.status = 'APPROVED_FOR_EXPORT'
                  AND e.facility_id = :fid
                ORDER BY e.recorded_at ASC
            """), {"fid": body.facility_id}).fetchall()
    except Exception as e:
        raise HTTPException(500, f"데이터 조회 실패: {e}")

    if not rows:
        raise HTTPException(
            404, "Export 대상 없음 (APPROVED_FOR_EXPORT 건 0)",
        )

    items = [dict(r._mapping) for r in rows]
    ledger_ids = [str(item["ledger_id"]) for item in items]

    # ── B. CSV 생성 ─────────────────────────────────────────────
    angel_csv_bytes = _build_angel_csv(items)
    proof_csv_bytes = _build_proof_csv(items)

    angel_sha = _sha256_bytes(angel_csv_bytes)
    proof_sha = _sha256_bytes(proof_csv_bytes)

    # ── C. ZIP 패키징 (메모리) ──────────────────────────────────
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("angel_import.csv", angel_csv_bytes)
        zf.writestr("proof_manifest.csv", proof_csv_bytes)
        # receipt는 zip_sha 포함해야 하므로 placeholder
        zf.writestr(
            "export_receipt.json",
            b"{}",  # 임시 — 아래에서 교체
        )

    # ZIP SHA-256 (receipt 제외 내용 기반)
    zip_content = zip_buf.getvalue()
    zip_sha = _sha256_bytes(zip_content)

    # receipt 재생성 (zip_sha 포함) 후 ZIP 재패키징
    receipt_bytes = _build_receipt(
        batch_id=batch_id,
        facility_id=body.facility_id,
        item_count=len(items),
        ledger_ids=ledger_ids,
        angel_sha=angel_sha,
        proof_sha=proof_sha,
        zip_sha=zip_sha,
        ts=now.isoformat(),
    )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("angel_import.csv", angel_csv_bytes)
        zf.writestr("proof_manifest.csv", proof_csv_bytes)
        zf.writestr("export_receipt.json", receipt_bytes)

    final_zip = zip_buf.getvalue()
    final_zip_sha = _sha256_bytes(final_zip)

    # ── D. DB 트랜잭션: 배치 원장 + 상태 전이 ───────────────────
    date_range = _get_date_range(items)

    try:
        with _engine.begin() as conn:
            # [D-1] bridge_export_batch INSERT
            conn.execute(text("""
                INSERT INTO bridge_export_batch (
                    id, facility_id, status,
                    ledger_ids, item_count,
                    zip_sha256, angel_csv_sha256,
                    proof_csv_sha256,
                    export_range_start, export_range_end,
                    exported_by, created_at
                ) VALUES (
                    :id, :fid, 'CREATED',
                    CAST(:lids AS UUID[]), :cnt,
                    :zsha, :asha,
                    :psha,
                    :rs, :re,
                    :by, :ts
                )
            """), {
                "id": batch_id,
                "fid": body.facility_id,
                "lids": ledger_ids,
                "cnt": len(items),
                "zsha": final_zip_sha,
                "asha": angel_sha,
                "psha": proof_sha,
                "rs": date_range[0],
                "re": date_range[1],
                "by": body.exported_by,
                "ts": now,
            })

            # [D-2] angel_review_event EXPORTED INSERT (각 건)
            for lid in ledger_ids:
                conn.execute(text("""
                    INSERT INTO angel_review_event (
                        id, ledger_id, status,
                        reviewer_id, decision_note,
                        export_batch_id, created_at
                    ) VALUES (
                        :id, :lid, 'EXPORTED',
                        :by, :note, :bid, :ts
                    )
                """), {
                    "id": str(uuid4()),
                    "lid": lid,
                    "by": body.exported_by,
                    "note": f"배치 {batch_id[:8]} Export",
                    "bid": batch_id,
                    "ts": now,
                })

        logger.info(
            f"[EXPORT] 배치 생성: {batch_id[:8]} "
            f"건수={len(items)} zip_sha={final_zip_sha[:16]}..."
        )

    except Exception as e:
        logger.error(f"[EXPORT] DB 기록 실패: {e}")
        raise HTTPException(500, f"배치 기록 실패: {e}")

    # ── E. SSE 알림 ─────────────────────────────────────────────
    if _redis_pub:
        try:
            await _redis_pub.publish(
                "sse:dashboard",
                json.dumps({
                    "event": "angel_exported",
                    "data": {
                        "batch_id": batch_id,
                        "item_count": len(items),
                        "facility_id": body.facility_id,
                    },
                }, ensure_ascii=False),
            )
        except Exception:
            pass

    # ── F. ZIP 스트리밍 반환 ─────────────────────────────────────
    filename = (
        f"voiceguard_angel_export_"
        f"{body.facility_id}_"
        f"{now.strftime('%Y%m%d_%H%M%S')}.zip"
    )

    return StreamingResponse(
        io.BytesIO(final_zip),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Batch-Id": batch_id,
            "X-Zip-SHA256": final_zip_sha,
            "X-Item-Count": str(len(items)),
        },
    )


# ══════════════════════════════════════════════════════════════════
# [3] 배치 이력 조회 — GET /api/v2/angel/export/batches
# ══════════════════════════════════════════════════════════════════

@router.get("/batches")
async def list_batches(
    facility_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    """Export 배치 이력 (감사용)."""
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    where = "WHERE facility_id = :fid" if facility_id else ""
    params = {"fid": facility_id, "lim": limit}

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT id, facility_id, status,
                       item_count, zip_sha256,
                       angel_csv_sha256, proof_csv_sha256,
                       export_range_start, export_range_end,
                       exported_by, created_at, downloaded_at
                FROM bridge_export_batch
                {where}
                ORDER BY created_at DESC
                LIMIT :lim
            """), params).fetchall()

        return {
            "batches": [dict(r._mapping) for r in rows],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── 유틸리티 ──────────────────────────────────────────────────────

def _get_date_range(items: list):
    """items에서 recorded_at의 최소/최대 추출."""
    dates = []
    for item in items:
        val = item.get("recorded_at")
        if val:
            if isinstance(val, str):
                dates.append(datetime.fromisoformat(val))
            else:
                dates.append(val)
    if not dates:
        return (None, None)
    return (min(dates), max(dates))
