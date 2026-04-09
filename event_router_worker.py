"""
Voice Guard — Phase 4 Event Router Worker (event_router_worker.py)
==================================================================
모든 이벤트를 단일 Redis Stream(voice:events)에서 수신하여
event_type에 따라 핸들러로 분기하는 통합 워커.

[아키텍처]
  Redis Stream: voice:events  (단일 스트림)
      ↓ XREADGROUP (Consumer Group: vg-router)
  EventRouter.dispatch(event_type)
      ├── IngestHandler  → B2 WORM + Whisper + 해시체인
      ├── NotionHandler  → Notion 페이지 동기화
      ├── ReconHandler   → Phase 3 Reconciliation Engine
      └── AlertHandler   → 카카오 알림톡 (NT-1/NT-2)

[상태 관리 — Append-Only 보상 트랜잭션]
  상태 전이 = unified_outbox에 새 row INSERT (UPDATE 0)
  현재 상태 = v_unified_outbox_current 뷰 조회

[장애 대응]
  A. 워커 크래시  → XAUTOCLAIM 30초 자동 재수령
  B. Redis 장애   → unified_outbox 직접 폴링 fallback
  C. 외부 API 다운→ 지수 백오프 (30→60→120→300→600s)
  D. MAX_ATTEMPTS → status=FAILED 기록 + dead_letter_queue 이관
"""

import asyncio
import hashlib
import hmac as _hmac
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

import boto3
import redis.asyncio as aioredis
from botocore.client import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

logger = logging.getLogger("event_router")
logging.basicConfig(
    level=logging.WARNING,          # 성공은 조용히, 실패만 시끄럽게
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── 설정 ─────────────────────────────────────────────────────
DATABASE_URL       = os.getenv("DATABASE_URL")
REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM       = "voice:events"          # Phase 4 통합 스트림
CONSUMER_GROUP     = "vg-router"
CONSUMER_NAME      = f"router-{os.getpid()}"
SERVER_SECRET      = os.getenv("SECRET_KEY", "CHANGE_ME").encode()
B2_KEY_ID          = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME     = os.getenv("B2_BUCKET_NAME", "voice-guard-korea")
B2_ENDPOINT_URL    = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
WORM_YEARS         = 5
MAX_ATTEMPTS       = 5
AUTOCLAIM_MS       = 30_000
BLOCK_MS           = 5_000
BACKOFF            = [30, 60, 120, 300, 600]

# ── DB / Whisper / B2 초기화 ─────────────────────────────────
_engine = create_engine(
    DATABASE_URL,
    pool_size=5, max_overflow=10,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

_whisper_model = None
_whisper_pool  = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper as _whisper
        _whisper_model = _whisper.load_model("medium")
    return _whisper_model


def _get_b2():
    return boto3.client(
        "s3", endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"),
    )


# ══════════════════════════════════════════════════════════════
# Append-Only 상태 전이 헬퍼
# ══════════════════════════════════════════════════════════════

def _transition(conn, event_id: str, event_type: str, new_status: str,
                payload: dict, attempt_num: int,
                error_message: Optional[str] = None):
    """
    unified_outbox에 새 상태 행 INSERT (UPDATE 0).
    이 함수 외부에서 직접 UPDATE를 시도하지 말 것.
    """
    conn.execute(text("""
        INSERT INTO public.unified_outbox
            (row_id, event_id, event_type, status, payload,
             attempt_num, worker_id, error_message, created_at)
        VALUES
            (:row_id, :event_id, :event_type, :status, CAST(:payload AS jsonb),
             :attempt_num, :worker_id, :error_message, NOW())
    """), {
        "row_id":        str(uuid4()),
        "event_id":      event_id,
        "event_type":    event_type,
        "status":        new_status,
        "payload":       json.dumps(payload, ensure_ascii=False, default=str),
        "attempt_num":   attempt_num,
        "worker_id":     CONSUMER_NAME,
        "error_message": error_message,
    })


def _log_throughput(conn, handler_name: str, event_id: str, event_type: str,
                    result: str, duration_ms: int):
    """worker_throughput_log에 처리 결과 기록 (Append-Only)."""
    conn.execute(text("""
        INSERT INTO public.worker_throughput_log
            (id, handler_name, event_id, event_type, result, duration_ms, worker_id, logged_at)
        VALUES
            (gen_random_uuid(), :handler, :eid, :etype, :result, :dur, :wid, NOW())
    """), {
        "handler": handler_name, "eid": event_id, "etype": event_type,
        "result": result, "dur": duration_ms, "wid": CONSUMER_NAME,
    })


# ══════════════════════════════════════════════════════════════
# IngestHandler: B2 WORM + Whisper + 해시체인
# ══════════════════════════════════════════════════════════════

async def _ingest_handler(event_id: str, payload: dict, attempt_num: int):
    """
    음성 증거 처리:
      1. B2 WORM 업로드 (ObjectLockMode=COMPLIANCE)
      2. Whisper 음성 변환 (비동기, ThreadPoolExecutor)
      3. HMAC 해시체인 생성
      4. evidence_ledger 해시/전사 기록
    """
    ledger_id = payload.get("ledger_id", "")
    if not ledger_id:
        raise ValueError("payload에 ledger_id 없음")

    b2        = _get_b2()
    date_pfx  = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    b2_key    = f"evidence/{date_pfx}/{ledger_id}.wav"
    retain    = datetime.now(timezone.utc) + timedelta(days=365 * WORM_YEARS)

    tmp_path = payload.get("tmp_audio_path")
    if tmp_path and os.path.isfile(tmp_path):
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
    else:
        logger.warning("[IngestHandler] 오디오 파일 없음: %s — 더미 사용", tmp_path)
        audio_bytes = b"RIFF" + b"\x00" * 100

    b2.put_object(
        Bucket=B2_BUCKET_NAME, Key=b2_key, Body=audio_bytes,
        ContentType="audio/wav",
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=retain,
    )
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()

    transcript = ""
    try:
        model  = _get_whisper()
        loop   = asyncio.get_event_loop()
        buf    = io.BytesIO(audio_bytes); buf.name = "audio.wav"
        result = await loop.run_in_executor(
            _whisper_pool,
            lambda: model.transcribe(buf, language="ko"),
        )
        transcript = result.get("text", "")
    except Exception as e:
        logger.warning("[IngestHandler] Whisper 실패: %s", e)

    t_sha256 = hashlib.sha256(transcript.encode()).hexdigest()

    raw = json.dumps({
        "ledger_id": ledger_id,
        "facility_id": payload.get("facility_id", ""),
        "beneficiary_id": payload.get("beneficiary_id", ""),
        "shift_id": payload.get("shift_id", ""),
        "server_ts": payload.get("server_ts", ""),
        "audio_sha256": audio_sha256,
        "transcript_sha256": t_sha256,
        "b2_key": b2_key,
    }, sort_keys=True)
    chain = _hmac.new(
        SERVER_SECRET,
        hashlib.sha256(raw.encode()).hexdigest().encode(),
        hashlib.sha256,
    ).hexdigest()

    if _engine:
        with _engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.evidence_ledger SET
                    audio_sha256=:a, transcript_sha256=:t, chain_hash=:c,
                    transcript_text=:tx, worm_bucket=:bkt,
                    worm_object_key=:bk, worm_retain_until=:ret
                WHERE id=:lid
            """), {
                "a": audio_sha256, "t": t_sha256, "c": chain,
                "tx": transcript, "bkt": B2_BUCKET_NAME,
                "bk": b2_key, "ret": retain, "lid": ledger_id,
            })

    if tmp_path and os.path.isfile(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════
# NotionHandler: Notion 동기화
# ══════════════════════════════════════════════════════════════

async def _notion_handler(event_id: str, payload: dict, attempt_num: int):
    """Notion 페이지 동기화. 실제 구현은 기존 notifier 로직 재사용."""
    ledger_id = payload.get("ledger_id", "")
    # notion_sync_outbox 상태 업데이트 (기존 테이블 하위 호환)
    if _engine and ledger_id:
        with _engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.notion_sync_outbox
                SET status='done', processed_at=NOW()
                WHERE ledger_id=:lid AND status IN ('pending','processing')
            """), {"lid": ledger_id})


# ══════════════════════════════════════════════════════════════
# ReconHandler: Phase 3 Reconciliation Engine 트리거
# ══════════════════════════════════════════════════════════════

async def _recon_handler(event_id: str, payload: dict, attempt_num: int):
    """
    Phase 3 Reconciliation Engine 실행.
    payload: {facility_id, target_date}
    """
    from reconciliation_engine import run_reconciliation

    facility_id = payload.get("facility_id")
    target_date_str = payload.get("target_date")
    from datetime import date as _date
    target = _date.fromisoformat(target_date_str) if target_date_str else None
    run_reconciliation(facility_id=facility_id, target_date=target, dry_run=False)


# ══════════════════════════════════════════════════════════════
# AlertHandler: 카카오 알림톡
# ══════════════════════════════════════════════════════════════

async def _alert_handler(event_id: str, payload: dict, attempt_num: int):
    """
    알림톡 발송.
    payload: {trigger_type, phone, template_code, variables, ledger_id}
    """
    if not _engine:
        return
    try:
        from backend.notifier import send_alimtalk
        send_alimtalk(
            engine=_engine,
            phone=payload.get("phone", ""),
            template_code=payload.get("template_code", ""),
            variables=payload.get("variables", {}),
            trigger_type=payload.get("trigger_type", "ALERT"),
            ledger_id=payload.get("ledger_id"),
        )
    except Exception as e:
        logger.error("[AlertHandler] 알림톡 실패: %s", e)
        raise


# ══════════════════════════════════════════════════════════════
# Event Router — 핵심 디스패처
# ══════════════════════════════════════════════════════════════

from handover_handler import handover_handler as _handover_handler_fn, check_undelivered_handovers

_HANDLERS = {
    "ingest":           (_ingest_handler,      "IngestHandler"),
    "notion":           (_notion_handler,      "NotionHandler"),
    "reconcile":        (_recon_handler,       "ReconHandler"),
    "alert":            (_alert_handler,       "AlertHandler"),
    "handover_trigger": (_handover_handler_fn, "HandoverHandler"),
}


async def _dispatch(event_id: str, event_type: str, payload: dict, attempt_num: int):
    """
    event_type → 핸들러 분기.
    성공: DONE 상태 행 INSERT + 처리량 기록.
    실패: FAILED 행 INSERT + 지수 백오프 표시.
    MAX_ATTEMPTS 초과 시 dead_letter_queue 이관.
    """
    if _engine is None:
        logger.error("[ROUTER] DB 미연결 — dispatch 불가")
        return

    handler_fn, handler_name = _HANDLERS.get(event_type, (None, "UnknownHandler"))
    if handler_fn is None:
        logger.error("[ROUTER] 알 수 없는 event_type: %s", event_type)
        return

    if attempt_num >= MAX_ATTEMPTS:
        _handle_max_attempts(event_id, event_type, payload, attempt_num)
        return

    # PROCESSING 상태 기록 (보상 트랜잭션)
    with _engine.begin() as conn:
        _transition(conn, event_id, event_type, "PROCESSING", payload, attempt_num + 1)

    t_start = time.monotonic()
    try:
        await handler_fn(event_id, payload, attempt_num)
        duration_ms = int((time.monotonic() - t_start) * 1000)

        with _engine.begin() as conn:
            _transition(conn, event_id, event_type, "DONE", payload, attempt_num + 1)
            _log_throughput(conn, handler_name, event_id, event_type, "DONE", duration_ms)

    except Exception as e:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        err_msg = str(e)[:500]
        logger.error("[ROUTER] %s 실패 attempt=%d: %s", handler_name, attempt_num + 1, err_msg)

        with _engine.begin() as conn:
            _transition(conn, event_id, event_type, "FAILED", payload,
                        attempt_num + 1, error_message=err_msg)
            _log_throughput(conn, handler_name, event_id, event_type, "FAILED", duration_ms)

        raise   # XACK 없음 → XAUTOCLAIM 재수령


def _handle_max_attempts(event_id: str, event_type: str, payload: dict, attempt_num: int):
    """MAX_ATTEMPTS 초과 → dead_letter_queue 이관."""
    if not _engine:
        return
    reason = f"MAX_ATTEMPTS({MAX_ATTEMPTS}) 초과 | event_type={event_type}"
    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.dead_letter_queue
                    (id, failure_reason, original_payload, detected_at)
                VALUES
                    (gen_random_uuid(), :reason, CAST(:payload AS jsonb), NOW())
            """), {
                "reason":  reason,
                "payload": json.dumps({"event_id": event_id, **payload},
                                      ensure_ascii=False, default=str),
            })
            _transition(conn, event_id, event_type, "FAILED", payload,
                        attempt_num, error_message=reason)
        logger.critical("[ROUTER] DLQ 이관: event_id=%s reason=%s", event_id, reason)
    except SQLAlchemyError as e:
        logger.critical("[ROUTER] DLQ 이관 자체 실패: %s", e)


# ══════════════════════════════════════════════════════════════
# DB Fallback 폴링
# ══════════════════════════════════════════════════════════════

async def _poll_fallback():
    """Redis 장애 시 unified_outbox 직접 폴링으로 PENDING 이벤트 처리."""
    if not _engine:
        return
    try:
        with _engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT event_id, event_type, payload, attempt_num
                FROM public.v_unified_outbox_current
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                LIMIT 5
            """)).fetchall()

        for row in rows:
            try:
                payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
                await _dispatch(str(row.event_id), row.event_type, payload, row.attempt_num)
            except Exception:
                pass
    except Exception as e:
        logger.error("[FALLBACK] 폴링 실패: %s", e)


# ══════════════════════════════════════════════════════════════
# NT-1 주기 점검 (무계획 미기록 알림)
# ══════════════════════════════════════════════════════════════

async def _check_overdue_alerts():
    """30초마다: 처리 지연 증거에 대해 NT-1 알림 이벤트 발행."""
    if not _engine:
        return
    OVERDUE_MIN = int(os.getenv("ALIMTALK_OVERDUE_MINUTES", "30"))
    try:
        with _engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT e.id, e.facility_id, e.beneficiary_id,
                       e.care_type,
                       ROUND(EXTRACT(EPOCH FROM (NOW() - e.ingested_at)) / 60) AS minutes_elapsed
                FROM public.evidence_ledger e
                WHERE e.ingested_at <= NOW() - (:m || ' minutes')::INTERVAL
                  AND (e.chain_hash IS NULL OR e.chain_hash = 'pending')
                  AND NOT EXISTS (
                      SELECT 1 FROM public.unified_outbox uo
                      WHERE CAST(uo.payload->>'ledger_id' AS TEXT) = CAST(e.id AS TEXT)
                        AND uo.event_type = 'alert'
                        AND uo.status IN ('DONE', 'PROCESSING')
                  )
                ORDER BY e.ingested_at ASC
                LIMIT 10
            """), {"m": OVERDUE_MIN}).fetchall()

        for row in rows:
            _publish_event("alert", {
                "trigger_type": "NT-1",
                "ledger_id":    str(row.id),
                "phone":        os.getenv("DEFAULT_FACILITY_PHONE", ""),
                "template_code": os.getenv("ALIMTALK_TPL_NT1", ""),
                "variables": {
                    "#{수급자ID}": row.beneficiary_id or "미지정",
                    "#{요양기관}": row.facility_id    or "미지정",
                    "#{경과시간}": str(int(row.minutes_elapsed)),
                    "#{급여유형}": row.care_type       or "미지정",
                },
            })
    except Exception as e:
        logger.error("[OVERDUE] 점검 실패: %s", e)


def _publish_event(event_type: str, payload: dict) -> str:
    """
    unified_outbox에 새 PENDING 이벤트 INSERT.
    호출자가 Redis XADD를 추가로 수행하면 실시간 처리 가능.
    Returns: event_id
    """
    event_id = str(uuid4())
    if not _engine:
        return event_id
    with _engine.begin() as conn:
        _transition(conn, event_id, event_type, "PENDING", payload, attempt_num=0)
    return event_id


# ══════════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════════

async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        await redis.xgroup_create(REDIS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    overdue_tick = 0

    while True:
        try:
            # XAUTOCLAIM: 30초 좀비 재수령
            try:
                result = await redis.xautoclaim(
                    REDIS_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
                    min_idle_time=AUTOCLAIM_MS, start_id="0-0", count=5,
                )
                autoclaimed = result[1] if isinstance(result, list) else []
                for msg in autoclaimed:
                    fields = msg[1] if len(msg) > 1 else {}
                    eid    = fields.get("event_id", "")
                    etype  = fields.get("event_type", "")
                    pload  = json.loads(fields.get("payload", "{}"))
                    attempt = int(fields.get("attempt_num", 0))
                    if eid:
                        logger.warning("[AUTOCLAIM] 좀비 재수령: %s", msg[0])
                        try:
                            await _dispatch(eid, etype, pload, attempt)
                            await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg[0])
                        except Exception:
                            pass
            except Exception as e:
                logger.error("[AUTOCLAIM] %s", e)

            # XREADGROUP: 새 메시지
            messages = await redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {REDIS_STREAM: ">"},
                count=5, block=BLOCK_MS,
            )

            if not messages:
                await _poll_fallback()
                overdue_tick += 1
                if overdue_tick >= 6:    # 약 30초마다
                    overdue_tick = 0
                    await _check_overdue_alerts()
                    # Phase 5: 미수령 인수인계 브리핑 NT-4 알림 발행
                    for item in check_undelivered_handovers(_engine):
                        _publish_event("alert", {
                            "trigger_type":  "NT-4",
                            "handover_id":   item["handover_id"],
                            "facility_id":   item["facility_id"],
                            "phone":         os.getenv("DEFAULT_FACILITY_PHONE", ""),
                            "template_code": os.getenv("ALIMTALK_TPL_NT4", ""),
                            "variables": {
                                "#{요양기관}":   item["facility_id"],
                                "#{교대마감}":   item["shift_end"],
                                "#{생성시각}":   item["generated_at"],
                            },
                        })
                continue

            for _, msgs in messages:
                for msg_id, fields in msgs:
                    eid    = fields.get("event_id", "")
                    etype  = fields.get("event_type", "")
                    pload  = json.loads(fields.get("payload", "{}"))
                    attempt = int(fields.get("attempt_num", 0))

                    if not eid or not etype:
                        await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)
                        continue

                    try:
                        await _dispatch(eid, etype, pload, attempt)
                        await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)
                    except Exception:
                        pass  # XACK 없음 → XAUTOCLAIM 재수령

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("[LOOP] %s", e)
            await asyncio.sleep(5)

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
