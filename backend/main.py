"""
Voice Guard — backend/main.py
FastAPI 핵심 라우터 v2.0

[불변 원칙] DB COMMIT = 증거 봉인 완료
  ① Ingest-First: COMMIT 즉시 202, AI 처리 대기 없음
  ② Atomic Split: evidence_ledger + outbox_events 단일 트랜잭션
  ③ SSE: 워커 이벤트 → 대시보드 실시간 push (WebSocket 불필요)
"""

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from uuid import uuid4

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from notifier import send_alimtalk, ALIMTALK_TPL_NT3, DEFAULT_FACILITY_PHONE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("voice_guard.main")

# ── 설정 ──────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_SSE_CHANNEL = "sse:dashboard"          # Pub/Sub 채널 (워커 → SSE)
REDIS_STREAM  = "voice:ingest"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000"
).split(",") if o.strip()]

# ── DB 엔진 (동기, connection pool) ───────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_size=10, max_overflow=20,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

# ── Redis 클라이언트 ────────────────────────────────────────────────
redis_pub: Optional[aioredis.Redis] = None   # publish 전용
redis_sub: Optional[aioredis.Redis] = None   # subscribe 전용 (SSE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pub, redis_sub
    try:
        redis_pub = aioredis.from_url(REDIS_URL, decode_responses=True)
        redis_sub = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_pub.ping()
        logger.info("[STARTUP] Redis 연결 완료.")
    except Exception as e:
        logger.warning(f"[STARTUP] Redis 연결 실패: {e}")
    yield
    for r in (redis_pub, redis_sub):
        if r: await r.aclose()
    logger.info("[SHUTDOWN] Redis 연결 종료.")


app = FastAPI(
    title="Voice Guard API",
    version="2.0.0",
    description="Anti-Clawback 증거 원장 — Ingest-First / Transactional Outbox / SSE",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Cache-Control"],
)


# ══════════════════════════════════════════════════════════════════
# 유틸리티
# ══════════════════════════════════════════════════════════════════

def make_idempotency_key(facility_id: str, beneficiary_id: str, shift_id: str) -> str:
    """
    동일 교대 내 중복 제출 차단용 SHA-256 키.
    DB UNIQUE 제약과 함께 이중 방어.
    """
    raw = f"{facility_id}::{beneficiary_id}::{shift_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def sse_format(event: str, data: dict) -> str:
    """SSE 프로토콜 포맷: event + data 줄 + 빈 줄"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════════════════
# [1] 헬스체크
# ══════════════════════════════════════════════════════════════════

@app.get("/health", tags=["시스템"])
async def health():
    r_ok = False
    if redis_pub:
        try:
            await redis_pub.ping()
            r_ok = True
        except Exception:
            pass
    return {
        "status":   "운영 중",
        "version":  "2.0.0",
        "db":       "연결됨" if engine else "미연결",
        "redis":    "연결됨" if r_ok else "미연결",
        "pipeline": "Ingest-First → Atomic Outbox → Redis → SSE",
    }


# ══════════════════════════════════════════════════════════════════
# [2] 핵심 Ingest 엔드포인트 — POST /api/v2/ingest
#
# 데이터 플로우:
#   수신 → Idempotency 검증 → [Atomic Split COMMIT]
#         → Redis XADD (워커 알림)
#         → Redis PUBLISH (SSE 즉시 알림)
#         → 202 반환
# ══════════════════════════════════════════════════════════════════

@app.post("/api/v2/ingest", status_code=202, tags=["증거 수집"])
async def ingest(
    audio_file:     UploadFile      = File(...),
    facility_id:    str             = Form(...),
    beneficiary_id: str             = Form(...),
    shift_id:       str             = Form(...),
    user_id:        str             = Form(...),
    gps_lat:        Optional[float] = Form(None),
    gps_lon:        Optional[float] = Form(None),
    device_id:      Optional[str]   = Form(None),
    care_type:      Optional[str]   = Form(None),
):
    # ── A. 서버 타임스탬프 (클라이언트 조작 원천 차단) ──────────
    server_ts = datetime.now(timezone.utc)
    ledger_id = str(uuid4())

    # ── B. Idempotency Key ────────────────────────────────────
    idem_key = make_idempotency_key(facility_id, beneficiary_id, shift_id)

    # ── C. 파일 수신 (최대 50MB) ──────────────────────────────
    audio_bytes = await audio_file.read()
    if len(audio_bytes) > 50 * 1024 * 1024:
        raise HTTPException(413, "파일 초과 (최대 50MB)")
    audio_size_kb = len(audio_bytes) // 1024

    # ── D. [핵심] Atomic Split: 단일 DB 트랜잭션 ─────────────
    #
    #   evidence_ledger (INSERT ONLY 불변 원장) +
    #   outbox_events (비동기 처리 큐)
    #
    #   engine.begin() = 자동 COMMIT/ROLLBACK 컨텍스트
    #   둘 중 하나라도 실패 시 전체 ROLLBACK → 불일치 없음
    #
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    outbox_payload = json.dumps({
        "ledger_id":     ledger_id,
        "facility_id":   facility_id,
        "beneficiary_id":beneficiary_id,
        "shift_id":      shift_id,
        "user_id":       user_id,
        "care_type":     care_type,
        "server_ts":     server_ts.isoformat(),
        "audio_size_kb": audio_size_kb,
        "gps_lat":       gps_lat,
        "gps_lon":       gps_lon,
        "device_id":     device_id or "unknown",
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [D-1] evidence_ledger: 불변 원장 INSERT
            conn.execute(text("""
                INSERT INTO evidence_ledger (
                    id, session_id, recorded_at, ingested_at,
                    device_id, facility_id,
                    audio_sha256, transcript_sha256, chain_hash,
                    transcript_text, language_code,
                    case_type, is_flagged,
                    beneficiary_id, shift_id, idempotency_key,
                    care_type, gps_lat, gps_lon,
                    audio_size_kb, worm_bucket, worm_object_key, worm_retain_until
                ) VALUES (
                    :id, :session_id, :recorded_at, :ingested_at,
                    :device_id, :facility_id,
                    'pending', 'pending', 'pending',
                    '', 'ko', :case_type, false,
                    :beneficiary_id, :shift_id, :idempotency_key,
                    :care_type, :gps_lat, :gps_lon,
                    :audio_size_kb, 'pending', 'pending', :recorded_at
                )
            """), {
                "id": ledger_id, "session_id": str(uuid4()),
                "recorded_at": server_ts, "ingested_at": server_ts,
                "device_id": device_id or "unknown",
                "facility_id": facility_id,
                "case_type": care_type,
                "beneficiary_id": beneficiary_id,
                "shift_id": shift_id,
                "idempotency_key": idem_key,
                "care_type": care_type,
                "gps_lat": gps_lat, "gps_lon": gps_lon,
                "audio_size_kb": audio_size_kb,
            })

            # [D-2] outbox_events: 비동기 처리 큐 INSERT (동일 트랜잭션)
            conn.execute(text("""
                INSERT INTO outbox_events (
                    id, ledger_id, status, attempts,
                    payload, created_at
                ) VALUES (
                    :id, :ledger_id, 'pending', 0,
                    :payload::jsonb, :created_at
                )
            """), {
                "id": str(uuid4()),
                "ledger_id": ledger_id,
                "payload": outbox_payload,
                "created_at": server_ts,
            })

        # ← COMMIT 완료. 이 시점부터 증거 봉인 완료.
        logger.info(f"[INGEST] ✅ COMMIT: ledger={ledger_id} facility={facility_id}")

    except IntegrityError as e:
        if "idempotency_key" in str(e).lower():
            raise HTTPException(409, f"중복 요청: shift_id='{shift_id}'는 이미 기록되었습니다.")
        raise HTTPException(500, f"DB 오류: {e}")
    except Exception as e:
        logger.error(f"[INGEST] DB 트랜잭션 실패: {e}")
        raise HTTPException(500, f"저장 실패: {e}")

    # ── E. Redis 알림 (DB 커밋 후) ───────────────────────────
    sse_event_data = {
        "ledger_id":     ledger_id,
        "facility_id":   facility_id,
        "beneficiary_id":beneficiary_id,
        "shift_id":      shift_id,
        "care_type":     care_type,
        "ingested_at":   server_ts.isoformat(),
        "gps_lat":       gps_lat,
        "gps_lon":       gps_lon,
        "is_flagged":    False,
        "sync_status":   "pending",
        "sync_attempts": 0,
        "minutes_elapsed": 0,
    }

    if redis_pub:
        try:
            # [E-1] Stream: 워커 비동기 처리 알림
            await redis_pub.xadd(
                REDIS_STREAM,
                {"ledger_id": ledger_id, "server_ts": server_ts.isoformat()},
                maxlen=10000,
            )
            # [E-2] Pub/Sub: SSE 대시보드 즉시 갱신
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({"event": "new_evidence", "data": sse_event_data}, ensure_ascii=False),
            )
            logger.info(f"[INGEST] Redis XADD + PUBLISH 완료.")
        except Exception as e:
            # Redis 실패는 경고만 — DB 이미 커밋됨, 워커가 outbox 폴링으로 복구
            logger.warning(f"[INGEST] Redis 알림 실패 (outbox 폴링 백업): {e}")

    # ── F. 202 즉시 반환 ──────────────────────────────────────
    return {
        "accepted":        True,
        "ledger_id":       ledger_id,
        "idempotency_key": idem_key,
        "server_ts":       server_ts.isoformat(),
        "message":         "증거 봉인 완료. 음성변환·WORM 봉인은 비동기 처리 중.",
    }


# ══════════════════════════════════════════════════════════════════
# [3] SSE 스트리밍 엔드포인트 — GET /api/sse/stream
#
# 동작:
#   클라이언트(Next.js) 연결 → Redis Pub/Sub 구독
#   워커 또는 Ingest API가 PUBLISH → 즉시 SSE 이벤트 push
#   연결 끊김 시 EventSource 자동 재연결 (Last-Event-ID 지원)
# ══════════════════════════════════════════════════════════════════

@app.get("/api/sse/stream", tags=["실시간"])
async def sse_stream(request: Request):
    """
    SSE 단방향 스트리밍.
    WebSocket 대비 장점:
      - stateless HTTP — 프락시/CDN 통과 용이
      - 자동 재연결 내장
      - 단방향 서버→클라이언트 push에 최적
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        if redis_sub is None:
            yield sse_format("error", {"message": "Redis 미연결"})
            return

        pubsub = redis_sub.pubsub()
        await pubsub.subscribe(REDIS_SSE_CHANNEL)
        logger.info(f"[SSE] 클라이언트 연결: {request.client}")

        # 연결 확인 이벤트
        yield sse_format("connected", {
            "message": "Voice Guard 실시간 스트림 연결 완료",
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        try:
            while True:
                # 클라이언트 연결 끊김 감지
                if await request.is_disconnected():
                    logger.info(f"[SSE] 클라이언트 연결 해제: {request.client}")
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message and message.get("type") == "message":
                    try:
                        payload = json.loads(message["data"])
                        event   = payload.get("event", "update")
                        data    = payload.get("data", {})
                        yield sse_format(event, data)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"[SSE] 메시지 파싱 실패: {e}")
                else:
                    # 30초마다 keep-alive (프락시 타임아웃 방지)
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info(f"[SSE] 스트림 취소: {request.client}")
        finally:
            await pubsub.unsubscribe(REDIS_SSE_CHANNEL)
            await pubsub.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # Nginx 버퍼링 비활성화
            "Connection":        "keep-alive",
        },
    )


# ══════════════════════════════════════════════════════════════════
# [4] 대시보드 데이터 API
# ══════════════════════════════════════════════════════════════════

@app.get("/api/v2/alerts", tags=["대시보드"])
async def get_alerts(minutes: int = 5):
    """Alert View: N분 이내 미처리 건 조회"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT e.id, e.facility_id, e.beneficiary_id,
                       e.shift_id, e.care_type, e.ingested_at,
                       e.gps_lat, e.gps_lon, e.is_flagged,
                       o.status AS sync_status,
                       o.attempts AS sync_attempts,
                       EXTRACT(EPOCH FROM (NOW() - e.ingested_at)) / 60
                           AS minutes_elapsed
                FROM evidence_ledger e
                LEFT JOIN outbox_events o ON o.ledger_id = e.id
                WHERE (o.status IS NULL OR o.status IN ('pending', 'processing'))
                  AND e.ingested_at >= NOW() - (:minutes || ' minutes')::INTERVAL
                ORDER BY e.ingested_at ASC
                LIMIT 200
            """), {"minutes": minutes}).fetchall()
        return {"alerts": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/v2/audit", tags=["대시보드"])
async def get_audit(facility_id: Optional[str] = None, limit: int = 200):
    """Audit-Ready View: 수급자별 해시/WORM/타임스탬프 원장"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    try:
        where = "WHERE e.facility_id = :fid" if facility_id else ""
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT e.id, e.facility_id, e.beneficiary_id,
                       e.shift_id, e.care_type,
                       e.recorded_at, e.ingested_at,
                       e.audio_sha256, e.chain_hash,
                       e.worm_bucket, e.worm_object_key, e.worm_retain_until,
                       e.transcript_text != '' AS has_audio,
                       e.chain_hash != 'pending' AS is_sealed,
                       e.is_flagged,
                       o.status AS outbox_status
                FROM evidence_ledger e
                LEFT JOIN outbox_events o ON o.ledger_id = e.id
                {where}
                ORDER BY e.recorded_at DESC
                LIMIT :lim
            """), {"fid": facility_id, "lim": limit}).fetchall()
        return {"records": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/v2/dlq", tags=["대시보드"])
async def get_dlq():
    """DLQ 관리자 조회"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, ledger_id, failure_reason, detected_at, is_resolved
                FROM dead_letter_queue
                WHERE is_resolved = FALSE
                ORDER BY detected_at DESC LIMIT 100
            """)).fetchall()
        return {"dlq": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


class ResolutionBody(BaseModel):
    cause: str
    memo:  str = ""


@app.patch("/api/v2/evidence/{ledger_id}", tags=["대시보드"])
async def patch_evidence_resolution(ledger_id: str, body: ResolutionBody):
    """
    미기록 건 처리 사유 기록.
    AlertDrawer → 현장 확인 요청 전송 시 호출.
    is_flagged = TRUE 로 전환하고 cause / memo 저장.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    if not body.cause.strip():
        raise HTTPException(422, "cause 는 필수값입니다.")
    try:
        with engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE evidence_ledger
                   SET is_flagged        = TRUE,
                       resolution_cause  = :cause,
                       resolution_memo   = :memo
                 WHERE id = :lid
            """), {"lid": ledger_id, "cause": body.cause.strip(), "memo": body.memo})
        if result.rowcount == 0:
            raise HTTPException(404, f"ledger_id='{ledger_id}' 를 찾을 수 없습니다.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RESOLUTION] DB 오류: {e}")
        raise HTTPException(500, str(e))

    logger.info(f"[RESOLUTION] ✅ 처리 사유 기록: ledger={ledger_id} cause={body.cause!r}")

    if redis_pub:
        try:
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({"event": "evidence_resolved",
                            "data":  {"ledger_id": ledger_id, "cause": body.cause}},
                           ensure_ascii=False),
            )
        except Exception:
            pass

    # ── NT-3: 현장 확인 요청 시 시설 담당자 알림톡 발송 ──────
    # 알림 실패는 로그만 기록, 응답 차단 금지 (notifier 내부 흡수)
    send_alimtalk(
        engine=engine,
        phone=DEFAULT_FACILITY_PHONE,
        template_code=ALIMTALK_TPL_NT3,
        variables={
            "#{원장ID}":   ledger_id[:8] + "…",
            "#{처리사유}": body.cause[:20],
            "#{메모}":     body.memo[:30] if body.memo else "없음",
        },
        trigger_type="NT-3",
        ledger_id=ledger_id,
    )

    return {"resolved": True, "ledger_id": ledger_id}


# ══════════════════════════════════════════════════════════════════
# [5] 원장 하향식 지시 API — POST /api/v2/directive
#
# 데이터 플로우:
#   수신 → [Atomic Tx: director_command INSERT + command_outbox INSERT]
#         → Redis PUBLISH (SSE 즉시 알림)
#         → 201 반환
#
# 워커가 command_outbox를 소비하여 알림톡 실발송 (트랜잭션 외부).
# ══════════════════════════════════════════════════════════════════

class DirectiveBody(BaseModel):
    beneficiary_id: str
    action:         str          # 'field_check' | 'freeze' | 'escalate' | 'memo_only'
    reason:         str
    memo:           str = ""
    commanded_by:   Optional[str] = None


@app.post("/api/v2/directive", status_code=201, tags=["지시 원장"])
async def post_directive(body: DirectiveBody):
    """
    원장 하향식 지시 원장 API.

    [불변 원칙]
      - director_command + command_outbox 단일 트랜잭션 원자 적재
      - 알림톡 실발송은 트랜잭션 외부의 아웃박스 워커가 처리
      - 지시 이력은 DELETE/TRUNCATE 트리거로 봉인
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    if not body.reason.strip():
        raise HTTPException(422, "reason 은 필수값입니다.")
    if not body.beneficiary_id.strip():
        raise HTTPException(422, "beneficiary_id 는 필수값입니다.")

    command_id = str(uuid4())
    outbox_id  = str(uuid4())
    now        = datetime.now(timezone.utc)

    outbox_payload = json.dumps({
        "command_id":     command_id,
        "beneficiary_id": body.beneficiary_id,
        "action":         body.action,
        "reason":         body.reason,
        "memo":           body.memo,
        "commanded_at":   now.isoformat(),
        "commanded_by":   body.commanded_by,
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [A] director_command: 지시 불변 원장 INSERT
            conn.execute(text("""
                INSERT INTO director_command (
                    id, beneficiary_id, action, reason, memo,
                    commanded_at, commanded_by
                ) VALUES (
                    :id, :beneficiary_id, :action, :reason, :memo,
                    :commanded_at, :commanded_by
                )
            """), {
                "id":             command_id,
                "beneficiary_id": body.beneficiary_id.strip(),
                "action":         body.action.strip(),
                "reason":         body.reason.strip(),
                "memo":           body.memo.strip() if body.memo else None,
                "commanded_at":   now,
                "commanded_by":   body.commanded_by,
            })

            # [B] command_outbox: 알림 발행 큐 INSERT (동일 트랜잭션)
            #     CAST 사용 — ':p::jsonb' 구문은 SQLAlchemy 파라미터 파서 충돌
            conn.execute(text("""
                INSERT INTO command_outbox (
                    id, command_id, event_type, payload,
                    status, attempts, created_at
                ) VALUES (
                    :id, :command_id, :event_type,
                    CAST(:payload AS jsonb),
                    'pending', 0, :created_at
                )
            """), {
                "id":         outbox_id,
                "command_id": command_id,
                "event_type": f"cmd:{body.action}",
                "payload":    outbox_payload,
                "created_at": now,
            })

        # ← COMMIT 완료. 지시 봉인 완료.
        logger.info(
            f"[DIRECTIVE] ✅ COMMIT: command={command_id} "
            f"beneficiary={body.beneficiary_id} action={body.action!r}"
        )

    except Exception as e:
        logger.error(f"[DIRECTIVE] DB 트랜잭션 실패: {e}")
        raise HTTPException(500, f"지시 적재 실패: {e}")

    # ── SSE: 관리자 대시보드에 즉시 반영 ──────────────────────
    if redis_pub:
        try:
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({
                    "event": "directive_issued",
                    "data": {
                        "command_id":     command_id,
                        "beneficiary_id": body.beneficiary_id,
                        "action":         body.action,
                        "reason":         body.reason,
                        "commanded_at":   now.isoformat(),
                    },
                }, ensure_ascii=False),
            )
        except Exception:
            pass  # Redis 실패는 경고만 — DB 이미 커밋됨

    return {
        "issued":         True,
        "command_id":     command_id,
        "outbox_id":      outbox_id,
        "beneficiary_id": body.beneficiary_id,
        "action":         body.action,
        "commanded_at":   now.isoformat(),
    }


@app.get("/api/v2/directive", tags=["지시 원장"])
async def list_directives(
    beneficiary_id: Optional[str] = None,
    limit: int = 100,
):
    """원장 지시 이력 조회"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    where = "WHERE dc.beneficiary_id = :bid" if beneficiary_id else ""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT dc.id, dc.beneficiary_id, dc.action,
                       dc.reason, dc.memo, dc.commanded_at, dc.commanded_by,
                       co.status AS outbox_status, co.attempts AS outbox_attempts
                FROM director_command dc
                LEFT JOIN command_outbox co ON co.command_id = dc.id
                {where}
                ORDER BY dc.commanded_at DESC
                LIMIT :lim
            """), {"bid": beneficiary_id, "lim": limit}).fetchall()
        return {"directives": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/v2/plan", tags=["대시보드"])
async def get_plan_vs_actual(facility_id: Optional[str] = None):
    """
    급여 계획 대조 매트릭스.
    evidence_ledger 기반으로 수급자별 6대 케어 항목 기록 여부를 집계.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    care_labels = ["식사 보조", "배변 보조", "체위 변경", "구강 위생", "목욕 보조", "이동 보조"]

    where = "WHERE e.facility_id = :fid" if facility_id else ""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT e.beneficiary_id,
                       e.care_type,
                       e.chain_hash != 'pending' AS is_sealed
                FROM evidence_ledger e
                {where}
                ORDER BY e.beneficiary_id, e.ingested_at DESC
            """), {"fid": facility_id}).fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))

    # 수급자별 기록된 care_type 집계
    bene_map: dict = {}
    for r in rows:
        bid = r.beneficiary_id or "unknown"
        if bid not in bene_map:
            bene_map[bid] = set()
        if r.care_type:
            bene_map[bid].add(r.care_type)

    plans = []
    for bid, recorded in bene_map.items():
        care_items = []
        for label in care_labels:
            if label in recorded:
                match = "full"
            else:
                match = "missing"
            care_items.append({"label": label, "match": match})
        plans.append({
            "beneficiary_id":   bid,
            "beneficiary_name": bid,   # 성명 테이블 없으면 ID 그대로
            "care_items":       care_items,
        })

    return {"plans": plans}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
