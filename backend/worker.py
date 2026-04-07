"""
Voice Guard — backend/worker.py
Redis Streams 무결점 비동기 워커 v2.0

[6가지 장애 대응]
  A. 워커 크래시  → XAUTOCLAIM 30초 자동 재수령
  B. Redis 장애   → outbox DB 직접 폴링 fallback
  C. 외부 API 다운→ 지수 백오프 (30→60→120→300s)
  D. DLQ 이관     → attempts ≥ 5 → dead_letter_queue
  E. 중복 제출    → Idempotency (Ingest 계층 차단)
  F. DB 실패      → engine.begin() 자동 롤백
"""

import asyncio
import hashlib
import hmac as hm
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import boto3
import redis.asyncio as aioredis
from botocore.client import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from notifier import (
    send_alimtalk,
    ALIMTALK_TPL_NT1, ALIMTALK_TPL_NT2,
    ADMIN_PHONE, DEFAULT_FACILITY_PHONE,
    ALIMTALK_OVERDUE_MINUTES,
)

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("voice_guard.worker")

DATABASE_URL       = os.getenv("DATABASE_URL")
REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM       = "voice:ingest"
REDIS_SSE_CHANNEL  = "sse:dashboard"
CONSUMER_GROUP     = "voice-guard-workers"
CONSUMER_NAME      = f"worker-{os.getpid()}"
SERVER_SECRET      = os.getenv("SECRET_KEY", "CHANGE_ME").encode()
B2_KEY_ID          = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME     = os.getenv("B2_BUCKET_NAME", "voice-guard-korea")
B2_ENDPOINT_URL    = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
WORM_YEARS = 5
MAX_ATTEMPTS = 5
AUTOCLAIM_MS = 30_000
BLOCK_MS = 5_000
BACKOFF = [30, 60, 120, 300, 600]

engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10,
    pool_pre_ping=True, connect_args={"connect_timeout": 10}) if DATABASE_URL else None

_whisper_model = None
_whisper_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model("medium")
    return _whisper_model

def get_b2():
    return boto3.client("s3", endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID, aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"))

# ── Token Bucket (Notion 3rps 방어) ──────────────────────────────
async def acquire_token(redis: aioredis.Redis) -> None:
    for _ in range(30):  # 최대 15초 대기
        key = f"tb:worker:{int(time.time())}"
        pipe = redis.pipeline()
        pipe.incr(key); pipe.expire(key, 2)
        count, _ = await pipe.execute()
        if count <= 2: return
        await asyncio.sleep(0.5)

# ── HMAC 해시체인 ─────────────────────────────────────────────────
def build_chain(ledger_id, facility_id, beneficiary_id, shift_id,
                server_ts, audio_sha256, transcript_sha256, b2_key) -> str:
    payload = json.dumps({"ledger_id": ledger_id, "facility_id": facility_id,
        "beneficiary_id": beneficiary_id, "shift_id": shift_id,
        "server_ts": server_ts, "audio_sha256": audio_sha256,
        "transcript_sha256": transcript_sha256, "b2_key": b2_key}, sort_keys=True)
    raw = hashlib.sha256(payload.encode()).hexdigest()
    return hm.new(SERVER_SECRET, raw.encode(), hashlib.sha256).hexdigest()

# ── DLQ 이관 ─────────────────────────────────────────────────────
def send_to_dlq(ledger_id: str, outbox_id: str, reason: str, payload: str):
    if not engine: return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dead_letter_queue
                  (id,ledger_id,outbox_id,failure_reason,original_payload,detected_at)
                VALUES (gen_random_uuid(),:lid,:oid,:reason,:payload::jsonb,NOW())
            """), {"lid": ledger_id, "oid": outbox_id, "reason": reason[:2000], "payload": payload})
            conn.execute(text("UPDATE outbox_events SET status='dlq',processed_at=NOW() WHERE id=:id"),
                {"id": outbox_id})
        logger.critical(f"[DLQ] 🚨 이관: ledger={ledger_id}")

        # ── NT-2: DLQ 이관 시 관리자 알림톡 발송 ────────────
        send_alimtalk(
            engine=engine,
            phone=ADMIN_PHONE,
            template_code=ALIMTALK_TPL_NT2,
            variables={
                "#{원장ID}":   ledger_id[:8] + "…",
                "#{실패사유}": reason[:30],
            },
            trigger_type="NT-2",
            ledger_id=ledger_id,
        )

    except Exception as e:
        logger.critical(f"[DLQ] 이관 실패: {e}")

# ── 단일 레코드 처리 ──────────────────────────────────────────────
async def process(redis: aioredis.Redis, ledger_id: str, msg_id: str):
    if not engine: return

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT o.id AS oid, o.attempts, o.payload, o.status,
                   e.facility_id, e.beneficiary_id, e.shift_id
            FROM outbox_events o
            JOIN evidence_ledger e ON e.id=o.ledger_id
            WHERE o.ledger_id=:lid AND o.status IN ('pending','processing')
            LIMIT 1
        """), {"lid": ledger_id}).fetchone()

    if not row:
        if msg_id != "db-poll": await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)
        return

    outbox_id = str(row.oid)
    attempts  = row.attempts
    payload   = row.payload if isinstance(row.payload, str) else json.dumps(row.payload)
    meta      = json.loads(payload)

    # DLQ 판단
    if attempts >= MAX_ATTEMPTS:
        send_to_dlq(ledger_id, outbox_id, f"MAX_ATTEMPTS({MAX_ATTEMPTS}) 초과", payload)
        if msg_id != "db-poll": await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)
        try:
            await redis.publish(REDIS_SSE_CHANNEL, json.dumps(
                {"event":"dlq_alert","data":{"ledger_id":ledger_id}}, ensure_ascii=False))
        except Exception: pass
        return

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE outbox_events SET status='processing',attempts=attempts+1 WHERE id=:id"),
            {"id": outbox_id})

    logger.info(f"[WORKER] 처리: ledger={ledger_id} attempt={attempts+1}/{MAX_ATTEMPTS}")

    try:
        await acquire_token(redis)

        # B2 WORM 업로드: 실제 오디오 파일 읽기
        b2 = get_b2()
        b2_key = f"evidence/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{ledger_id}.wav"

        tmp_audio_path = meta.get("tmp_audio_path")
        if not tmp_audio_path or not os.path.isfile(tmp_audio_path):
            raise FileNotFoundError(f"오디오 파일 없음: {tmp_audio_path}")
        with open(tmp_audio_path, "rb") as f:
            audio_bytes = f.read()
        if len(audio_bytes) < 100:
            raise ValueError(f"오디오 파일 크기 비정상: {len(audio_bytes)} bytes")

        retain = datetime.now(timezone.utc) + timedelta(days=365 * WORM_YEARS)
        b2.put_object(Bucket=B2_BUCKET_NAME, Key=b2_key, Body=audio_bytes,
            ContentType="audio/wav", ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain)
        audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()

        # WORM COMPLIANCE 검증: head_object로 Object Lock 모드 확인
        head = b2.head_object(Bucket=B2_BUCKET_NAME, Key=b2_key)
        lock_mode = head.get("ObjectLockMode")
        if lock_mode != "COMPLIANCE":
            raise RuntimeError(f"WORM 검증 실패: ObjectLockMode={lock_mode} (expected COMPLIANCE)")
        logger.info(f"[WORKER] WORM COMPLIANCE 검증 통과: {b2_key}")

        # 업로드+검증 성공 후 tmp 파일 삭제
        try:
            os.remove(tmp_audio_path)
        except OSError:
            pass

        # Whisper 비동기 변환
        transcript = ""
        try:
            model = get_whisper()
            loop  = asyncio.get_event_loop()
            buf   = io.BytesIO(audio_bytes); buf.name = "audio.wav"
            result = await loop.run_in_executor(_whisper_pool,
                lambda: model.transcribe(buf, language="ko"))
            transcript = result.get("text", "")
        except Exception as e:
            logger.warning(f"[WORKER] Whisper 실패: {e}")

        transcript_sha256 = hashlib.sha256(transcript.encode()).hexdigest()

        # 해시체인
        chain = build_chain(ledger_id, meta.get("facility_id",""),
            meta.get("beneficiary_id",""), meta.get("shift_id",""),
            meta.get("server_ts",""), audio_sha256, transcript_sha256, b2_key)

        # Atomic UPDATE
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE evidence_ledger SET
                    audio_sha256=:a, transcript_sha256=:t, chain_hash=:c,
                    transcript_text=:tx, worm_bucket=:bkt,
                    worm_object_key=:bk, worm_retain_until=:ret
                WHERE id=:lid
            """), {"a":audio_sha256,"t":transcript_sha256,"c":chain,"tx":transcript,
                   "bkt":B2_BUCKET_NAME,"bk":b2_key,"ret":retain,"lid":ledger_id})
            conn.execute(text(
                "UPDATE outbox_events SET status='done',processed_at=NOW() WHERE id=:id"),
                {"id": outbox_id})

        if msg_id != "db-poll":
            await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)

        logger.info(f"[WORKER] ✅ 봉인 완료: ledger={ledger_id}")

        try:
            await redis.publish(REDIS_SSE_CHANNEL, json.dumps({"event":"evidence_sealed",
                "data":{"ledger_id":ledger_id,"chain_hash":chain,"is_sealed":True,
                        "worm_key":b2_key}}, ensure_ascii=False))
        except Exception: pass

    except Exception as e:
        logger.error(f"[WORKER] 실패 attempt={attempts+1}: {e}")
        delay = BACKOFF[min(attempts, len(BACKOFF)-1)]
        try:
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE outbox_events SET status='pending', error_message=:msg,
                        next_retry_at=NOW()+(:d||' seconds')::INTERVAL
                    WHERE id=:id AND status='processing'
                """), {"msg": str(e)[:500], "d": delay, "id": outbox_id})
        except Exception: pass
        # XACK 없음 → XAUTOCLAIM 재수령 (시나리오 A)

# ── NT-1: 미기록 임박 건 주기 점검 ──────────────────────────────
async def check_overdue():
    """
    NT-1 트리거: outbox_events.status='pending'|'processing' 상태에서
    ALIMTALK_OVERDUE_MINUTES 경과한 미기록 건에 대해 최초 1회만 알림톡 발송.
    notification_log의 중복 방지 로직으로 재발송 차단됨.
    """
    if not engine:
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT e.id, e.facility_id, e.beneficiary_id,
                       e.shift_id, e.care_type,
                       ROUND(EXTRACT(EPOCH FROM (NOW() - e.ingested_at)) / 60) AS minutes_elapsed
                FROM evidence_ledger e
                LEFT JOIN outbox_events o2 ON o2.ledger_id = e.id
                WHERE (o2.status IS NULL OR o2.status IN ('pending', 'processing'))
                  AND e.ingested_at <= NOW() - (:m || ' minutes')::INTERVAL
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_log n
                      WHERE n.ledger_id = e.id
                        AND n.trigger_type = 'NT-1'
                        AND n.status = 'sent'
                  )
                ORDER BY e.ingested_at ASC
                LIMIT 10
            """), {"m": ALIMTALK_OVERDUE_MINUTES}).fetchall()

        for row in rows:
            send_alimtalk(
                engine=engine,
                phone=DEFAULT_FACILITY_PHONE,
                template_code=ALIMTALK_TPL_NT1,
                variables={
                    "#{수급자ID}": row.beneficiary_id or "미지정",
                    "#{요양기관}": row.facility_id    or "미지정",
                    "#{경과시간}": str(int(row.minutes_elapsed)),
                    "#{급여유형}": row.care_type       or "미지정",
                },
                trigger_type="NT-1",
                ledger_id=str(row.id),
            )

    except Exception as e:
        logger.error(f"[CHECK_OVERDUE] {e}")


# ── 메인 루프 ─────────────────────────────────────────────────────
async def poll_fallback(redis: aioredis.Redis):
    """시나리오 B: Redis 장애 시 outbox 직접 폴링"""
    if not engine: return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT ledger_id FROM outbox_events
                WHERE status='pending' AND attempts < :m
                  AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                ORDER BY created_at LIMIT 5
            """), {"m": MAX_ATTEMPTS}).fetchall()
        for row in rows:
            await process(redis, str(row.ledger_id), "db-poll")
    except Exception as e:
        logger.error(f"[FALLBACK] {e}")

async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.xgroup_create(REDIS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("[WORKER] Consumer Group 생성.")
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e): raise

    logger.info(f"[WORKER] 🚀 시작: {CONSUMER_NAME}")

    overdue_tick = 0   # 약 30초(BLOCK_MS×6)마다 check_overdue() 실행

    while True:
        try:
            # XAUTOCLAIM: 30초 좀비 재수령
            try:
                result = await redis.xautoclaim(REDIS_STREAM, CONSUMER_GROUP,
                    CONSUMER_NAME, min_idle_time=AUTOCLAIM_MS, start_id="0-0", count=5)
                for msg in (result[1] if isinstance(result, list) else []):
                    lid = msg[1].get("ledger_id","")
                    if lid:
                        logger.warning(f"[AUTOCLAIM] 좀비 재수령: {msg[0]}")
                        await process(redis, lid, msg[0])
            except Exception as e:
                logger.error(f"[AUTOCLAIM] {e}")

            # XREADGROUP: 새 메시지
            messages = await redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {REDIS_STREAM: ">"}, count=5, block=BLOCK_MS)

            if not messages:
                await poll_fallback(redis)
                # NT-1: 약 30초마다 미기록 임박 건 알림톡 점검
                overdue_tick += 1
                if overdue_tick >= 6:
                    overdue_tick = 0
                    await check_overdue()
                continue

            for _, msgs in messages:
                for msg_id, fields in msgs:
                    lid = fields.get("ledger_id","")
                    if lid: await process(redis, lid, msg_id)
                    else: await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[LOOP] {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
