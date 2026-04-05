"""
Voice Guard — Redis Streams 비동기 워커 v2.0
=============================================
핵심 원칙 (절대 규칙):
  1. Consumer Group + XREADGROUP: 워커 재시작 시 메시지 유실 없음
  2. XAUTOCLAIM: 30초 이상 멈춘 메시지 자동 재수령 (좀비 메시지 방지)
  3. Token Bucket: Redis INCR+TTL로 외부 API 속도 제한 방어
  4. DLQ Fallback: 5회 실패 시 dead_letter_queue 테이블로 이관
  5. XACK: 성공 후에만 처리 완료 승인 (처리 중 크래시 = 자동 재처리)

처리 파이프라인:
  [Redis Stream: voice:ingest]
      ↓ XREADGROUP (Consumer Group)
  [outbox 레코드 조회]
      ↓ B2 WORM 업로드 (Token Bucket 속도 제한)
      ↓ Whisper 음성 변환 (ThreadPoolExecutor)
      ↓ HMAC 해시 체인 생성
      ↓ evidence_ledger UPDATE (hash/transcript 채움)
      ↓ outbox 상태 → 'done'
      ↓ XACK
  [실패 시]
      → attempts++ → 30초 대기 재시도
      → attempts >= 5 → dead_letter_queue INSERT + outbox → 'dlq'
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3
import redis.asyncio as aioredis
import whisper
from botocore.client import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("redis_worker_v2")

# ── 설정 ─────────────────────────────────────────────────────
DATABASE_URL       = os.getenv("DATABASE_URL")
REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379")
SERVER_SECRET       = os.getenv("SECRET_KEY", "CHANGE_ME").encode("utf-8")
B2_KEY_ID          = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME     = os.getenv("B2_BUCKET_NAME", "voice-guard-korea")
B2_ENDPOINT_URL    = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")

# Redis Streams 설정
STREAM_KEY       = "voice:ingest"
CONSUMER_GROUP   = "voice-guard-workers"
CONSUMER_NAME    = f"worker-{os.getpid()}"
BLOCK_MS         = 5000         # XREADGROUP 블로킹 대기 (5초)
AUTOCLAIM_MS     = 30_000       # 30초 이상 미승인 메시지 재수령
MAX_ATTEMPTS     = 5            # DLQ 이관 임계값
WORM_YEARS       = 5

# Token Bucket 설정 (외부 API 속도 제한 방어)
TB_KEY           = "tb:voice_guard:worker"
TB_CAPACITY      = 10           # 최대 10 토큰
TB_REFILL_RATE   = 2            # 초당 2 토큰 충전
TB_COST          = 1            # 작업당 소비 토큰

# ── DB 엔진 ──────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

# ── Whisper 모델 + Thread Pool ───────────────────────────────
logger.info("[WORKER] Whisper 모델 로딩 중 (medium)...")
try:
    whisper_model = whisper.load_model("medium")
    logger.info("[WORKER] Whisper 준비 완료.")
except Exception as e:
    logger.error(f"[WORKER] Whisper 로딩 실패: {e}")
    whisper_model = None

_whisper_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")

# ── B2 클라이언트 ─────────────────────────────────────────────
def get_b2():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"),
    )

# ── 해시 체인 함수 ───────────────────────────────────────────
def build_chain_hash(ledger_id: str, facility_id: str, beneficiary_id: str,
                     shift_id: str, server_ts: str, audio_sha256: str,
                     transcript_sha256: str, b2_key: str) -> str:
    payload = json.dumps({
        "ledger_id": ledger_id, "facility_id": facility_id,
        "beneficiary_id": beneficiary_id, "shift_id": shift_id,
        "server_ts": server_ts, "audio_sha256": audio_sha256,
        "transcript_sha256": transcript_sha256, "b2_key": b2_key,
    }, sort_keys=True)
    raw_hash = hashlib.sha256(payload.encode()).hexdigest()
    return hmac.new(SERVER_SECRET, raw_hash.encode(), hashlib.sha256).hexdigest()

# ══════════════════════════════════════════════════════════════
# Token Bucket (Redis 기반)
# ══════════════════════════════════════════════════════════════
async def token_bucket_acquire(redis: aioredis.Redis) -> bool:
    """
    Redis INCR + TTL 기반 토큰 버킷.
    초당 TB_REFILL_RATE 토큰을 생성하여 외부 API 속도 초과 방지.
    토큰 부족 시 False 반환 → 호출자가 대기 후 재시도.
    """
    pipe = redis.pipeline()
    now_sec = int(time.time())
    window_key = f"{TB_KEY}:{now_sec}"
    pipe.incr(window_key)
    pipe.expire(window_key, 2)   # 2초 TTL (1초 창 + 여유)
    results = await pipe.execute()
    count = results[0]
    return count <= TB_REFILL_RATE   # 이 창에서 refill rate 이하이면 통과

async def wait_for_token(redis: aioredis.Redis, max_wait: float = 10.0):
    """토큰이 확보될 때까지 최대 max_wait 초 대기"""
    elapsed = 0.0
    while elapsed < max_wait:
        if await token_bucket_acquire(redis):
            return
        await asyncio.sleep(0.5)
        elapsed += 0.5
    logger.warning("[TOKEN-BUCKET] 토큰 확보 타임아웃 — 작업 강행")

# ══════════════════════════════════════════════════════════════
# DLQ 이관
# ══════════════════════════════════════════════════════════════
def send_to_dlq(ledger_id: str, outbox_id: str, reason: str, payload: str):
    """
    5회 실패 시 dead_letter_queue 테이블에 영구 기록.
    관리자가 수동으로 재처리하거나 조사할 수 있도록 보존.
    """
    if engine is None:
        logger.critical(f"[DLQ] DB 미연결로 DLQ 기록 불가! ledger={ledger_id}")
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.dead_letter_queue (
                    id, ledger_id, outbox_id, failure_reason, original_payload, detected_at
                ) VALUES (
                    gen_random_uuid(), :ledger_id, :outbox_id,
                    :reason, :payload, NOW()
                )
            """), {
                "ledger_id": ledger_id, "outbox_id": outbox_id,
                "reason": reason[:2000], "payload": payload,
            })
            conn.execute(text("""
                UPDATE public.notion_sync_outbox
                SET status = 'dlq', processed_at = NOW()
                WHERE id = :id
            """), {"id": outbox_id})
        logger.critical(f"[DLQ] 🚨 DLQ 이관 완료: ledger={ledger_id} | reason={reason}")
    except Exception as e:
        logger.critical(f"[DLQ] DLQ 기록 자체 실패: {e} | ledger={ledger_id}")

# ══════════════════════════════════════════════════════════════
# 단일 outbox 레코드 처리
# ══════════════════════════════════════════════════════════════
async def process_outbox(redis: aioredis.Redis, ledger_id: str, stream_msg_id: str):
    """
    outbox 레코드 1건 처리:
      1. outbox 조회 → attempts 확인 → DLQ 판단
      2. Token Bucket 대기
      3. B2 WORM 업로드 (임시 바이트 생성 — 실제는 원본 파일 경로에서 읽어야 함)
      4. Whisper 변환 (비동기)
      5. 해시 체인 생성
      6. evidence_ledger UPDATE + outbox 'done'
      7. XACK
    """
    if engine is None:
        logger.error("[WORKER] DB 미연결.")
        return

    # ── outbox 레코드 조회 ────────────────────────────────────
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT o.id AS outbox_id, o.attempts, o.payload, o.status,
                   e.ingested_at, e.facility_id, e.beneficiary_id, e.shift_id
            FROM public.notion_sync_outbox o
            JOIN public.evidence_ledger e ON e.id = o.ledger_id
            WHERE o.ledger_id = :lid AND o.status IN ('pending', 'processing')
            LIMIT 1
        """), {"lid": ledger_id}).fetchone()

        if not row:
            logger.warning(f"[WORKER] outbox 레코드 없음 또는 이미 처리됨: {ledger_id}")
            await redis.xack(STREAM_KEY, CONSUMER_GROUP, stream_msg_id)
            return

        outbox_id = str(row.outbox_id)
        attempts  = row.attempts
        payload   = row.payload if isinstance(row.payload, str) else json.dumps(row.payload)

        # DLQ 임계값 초과 확인
        if attempts >= MAX_ATTEMPTS:
            logger.error(f"[WORKER] DLQ 이관: ledger={ledger_id} attempts={attempts}")
            send_to_dlq(ledger_id, outbox_id, f"최대 재시도({MAX_ATTEMPTS}) 초과", payload)
            await redis.xack(STREAM_KEY, CONSUMER_GROUP, stream_msg_id)
            return

        # 처리 중 상태로 전환 + attempts 증가
        conn.execute(text("""
            UPDATE public.notion_sync_outbox
            SET status = 'processing', attempts = attempts + 1
            WHERE id = :id
        """), {"id": outbox_id})

    logger.info(f"[WORKER] 처리 시작: ledger={ledger_id} | attempt={attempts + 1}/{MAX_ATTEMPTS}")
    meta = json.loads(payload) if isinstance(payload, str) else payload
    server_ts = meta.get("server_ts", datetime.now(timezone.utc).isoformat())

    try:
        # ── Step 1: Token Bucket 대기 ──────────────────────
        await wait_for_token(redis)

        # ── Step 2: B2 WORM 업로드 ─────────────────────────
        #   실제 운영: 원본 파일을 임시 경로에서 읽거나 스트리밍으로 전달
        b2 = get_b2()
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        b2_key      = f"evidence/{date_prefix}/{ledger_id}.wav"
        dummy_audio = b"RIFF" + b"\x00" * 100   # 실제 운영 시 원본 바이트로 교체
        retain_until = (
            datetime.now(timezone.utc) + timedelta(days=365 * WORM_YEARS)
        ).replace(tzinfo=timezone.utc)

        b2.put_object(
            Bucket=B2_BUCKET_NAME, Key=b2_key, Body=dummy_audio,
            ContentType="audio/wav",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )
        audio_sha256 = hashlib.sha256(dummy_audio).hexdigest()
        logger.info(f"[WORKER] B2 업로드 완료: {b2_key}")

        # ── Step 3: Whisper 비동기 변환 ───────────────────
        transcript = ""
        if whisper_model:
            loop = asyncio.get_event_loop()
            audio_io = io.BytesIO(dummy_audio)
            audio_io.name = "audio.wav"
            transcript = await loop.run_in_executor(
                _whisper_pool,
                lambda: whisper_model.transcribe(audio_io, language="ko").get("text", "")
            )

        transcript_sha256 = hashlib.sha256(transcript.encode()).hexdigest()

        # ── Step 4: 해시 체인 생성 ────────────────────────
        chain_hash = build_chain_hash(
            ledger_id=ledger_id, facility_id=meta.get("facility_id", ""),
            beneficiary_id=meta.get("beneficiary_id", ""),
            shift_id=meta.get("shift_id", ""), server_ts=server_ts,
            audio_sha256=audio_sha256, transcript_sha256=transcript_sha256,
            b2_key=b2_key,
        )

        # ── Step 5: evidence_ledger + outbox 동시 업데이트 ─
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.evidence_ledger SET
                    audio_sha256      = :a_hash,
                    transcript_sha256 = :t_hash,
                    chain_hash        = :c_hash,
                    transcript_text   = :transcript,
                    worm_bucket       = :bucket,
                    worm_object_key   = :b2_key,
                    worm_retain_until = :retain
                WHERE id = :lid
            """), {
                "a_hash": audio_sha256, "t_hash": transcript_sha256,
                "c_hash": chain_hash,   "transcript": transcript,
                "bucket": B2_BUCKET_NAME, "b2_key": b2_key,
                "retain": retain_until, "lid": ledger_id,
            })
            conn.execute(text("""
                UPDATE public.notion_sync_outbox
                SET status = 'done', processed_at = NOW()
                WHERE id = :id
            """), {"id": outbox_id})

        # ── Step 6: XACK → 처리 완료 승인 ────────────────
        #   반드시 DB 커밋 성공 후에만 승인.
        #   워커 크래시 시 = XACK 없음 = 다음 XREADGROUP/XAUTOCLAIM에서 재처리
        await redis.xack(STREAM_KEY, CONSUMER_GROUP, stream_msg_id)
        logger.info(f"[WORKER] ✅ XACK 완료: ledger={ledger_id} | chain={chain_hash[:12]}...")

    except Exception as e:
        logger.error(f"[WORKER] 처리 실패: ledger={ledger_id} | {e}")
        # XACK 하지 않음 → XAUTOCLAIM이 30초 후 재수령
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.notion_sync_outbox
                SET status = 'pending'
                WHERE id = :id AND status = 'processing'
            """), {"id": outbox_id})

# ══════════════════════════════════════════════════════════════
# 메인 워커 루프
# ══════════════════════════════════════════════════════════════
async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Consumer Group 생성 (이미 존재해도 무관)
    try:
        await redis.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info(f"[WORKER] Consumer Group '{CONSUMER_GROUP}' 생성 완료.")
    except aioredis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.info(f"[WORKER] Consumer Group 이미 존재. 재사용.")
        else:
            raise

    logger.info(f"[WORKER] 🚀 워커 시작: {CONSUMER_NAME} | stream={STREAM_KEY}")

    while True:
        try:
            # ── XAUTOCLAIM: 30초 이상 처리 중인 좀비 메시지 재수령 ──
            autoclaim_result = await redis.xautoclaim(
                STREAM_KEY, CONSUMER_GROUP, CONSUMER_NAME,
                min_idle_time=AUTOCLAIM_MS, start_id="0-0", count=5,
            )
            # xautoclaim 반환: [next_start_id, [[msg_id, fields], ...], deleted_ids]
            autoclaimed_msgs = autoclaim_result[1] if isinstance(autoclaim_result, list) else []
            for msg in autoclaimed_msgs:
                msg_id = msg[0]
                fields = msg[1]
                ledger_id = fields.get("ledger_id", "")
                if ledger_id:
                    logger.warning(f"[WORKER/AUTOCLAIM] 좀비 메시지 재수령: {msg_id} | ledger={ledger_id}")
                    await process_outbox(redis, ledger_id, msg_id)

            # ── XREADGROUP: 새 메시지 읽기 ──────────────────
            messages = await redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {STREAM_KEY: ">"},  # '>' = 아직 미전달 메시지만
                count=5,
                block=BLOCK_MS,
            )

            if not messages:
                # Outbox 폴링 백업: Redis 알림 없이도 pending 건 처리
                await poll_outbox_fallback(redis)
                continue

            for stream, msgs in messages:
                for msg_id, fields in msgs:
                    ledger_id = fields.get("ledger_id", "")
                    if not ledger_id:
                        await redis.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)
                        continue
                    await process_outbox(redis, ledger_id, msg_id)

        except Exception as e:
            logger.error(f"[WORKER] 루프 오류: {e}")
            await asyncio.sleep(5)

async def poll_outbox_fallback(redis: aioredis.Redis):
    """
    Redis 알림이 없을 때 outbox를 직접 폴링.
    Redis가 다운되거나 XADD가 실패한 경우의 안전망.
    """
    if engine is None:
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT ledger_id FROM public.notion_sync_outbox
                WHERE status = 'pending'
                  AND attempts < :max_a
                ORDER BY created_at ASC
                LIMIT 10
            """), {"max_a": MAX_ATTEMPTS}).fetchall()
        for row in rows:
            # 임시 가짜 stream_id로 처리 (XACK 없이 DB만 업데이트)
            await process_outbox(redis, str(row.ledger_id), "db-poll-fallback")
    except Exception as e:
        logger.error(f"[WORKER/FALLBACK] 폴링 실패: {e}")

if __name__ == "__main__":
    asyncio.run(main())
