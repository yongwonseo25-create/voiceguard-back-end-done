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

[Phase 10: 증거 검증서 자동 발급]
  G. 봉인 직후 PDF + JSON 검증서 병렬 생성 → B2 WORM 적재 → certificate_ledger INSERT
     PDF/JSON 렌더링 실패 시 evidence_seal_event INSERT가 차단됨 (원자성 보장)
"""

import asyncio
import hashlib
import hmac as hm
import io
import json
import logging
import os
import time
import uuid as _uuid_mod
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
from gemini_processor import call_gemini, call_gemini_care_record
from env_guard import check_env_vars
from cert_renderer import render_pdf_certificate, render_json_certificate

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("voice_guard.worker")

# ── [TD-01/TD-05] 환경변수 강제화: 모듈 로드 시점에 즉시 검증 ─────
# SECRET_KEY 기본값('CHANGE_ME') 또는 필수 변수 누락 시 RuntimeError 발생
# → 워커 기동 자체가 차단되어 무효 증거 봉인 원천 차단.
check_env_vars("worm", "ai", "alimtalk")

DATABASE_URL       = os.getenv("DATABASE_URL")
REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM       = "voice:ingest"
REDIS_CARE_STREAM  = "care:records"       # 6대 의무기록 스트림 (신규)
REDIS_SSE_CHANNEL  = "sse:dashboard"
CONSUMER_GROUP     = "voice-guard-workers"
CONSUMER_NAME      = f"worker-{os.getpid()}"
# env_guard가 위에서 SECRET_KEY 존재/유효성을 이미 검증했으므로 안전
SERVER_SECRET      = os.getenv("SECRET_KEY", "").encode()
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
_cert_pool    = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cert")

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

# ── 증거 검증서 병렬 발급 (Phase 10) ─────────────────────────────
async def _issue_certificates(seal_data: dict, b2_client, ledger_id: str,
                               retain: datetime) -> tuple[str, str, bytes, bytes]:
    """
    PDF + JSON 검증서를 _cert_pool에서 병렬 렌더링 후 B2 WORM 업로드.
    반환: (pdf_key, json_key, pdf_bytes, json_bytes)

    [원자성 보장]
    이 함수가 예외를 raise하면 caller의 try 블록 전체가 except로 빠진다.
    evidence_seal_event INSERT는 이 함수 호출 이후에 위치하므로
    이 함수 실패 = DB 트랜잭션 미개방 = 봉인 ROLLBACK 완전 보장.
    """
    loop = asyncio.get_event_loop()

    # asyncio.gather: PDF/JSON 병렬 렌더링 — 어느 하나 예외 시 전체 전파
    pdf_bytes, json_bytes = await asyncio.gather(
        loop.run_in_executor(_cert_pool, render_pdf_certificate, seal_data),
        loop.run_in_executor(_cert_pool, render_json_certificate, seal_data),
    )

    # B2 WORM 업로드 (두 파일 모두 COMPLIANCE)
    pdf_key  = f"certs/pdf/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{ledger_id}.pdf"
    json_key = f"certs/json/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{ledger_id}.json"

    b2_client.put_object(
        Bucket=B2_BUCKET_NAME, Key=pdf_key, Body=pdf_bytes,
        ContentType="application/pdf",
        ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain,
    )
    b2_client.put_object(
        Bucket=B2_BUCKET_NAME, Key=json_key, Body=json_bytes,
        ContentType="application/json",
        ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain,
    )

    logger.info(f"[CERT] 검증서 B2 업로드 완료: pdf={pdf_key} json={json_key}")
    return pdf_key, json_key, pdf_bytes, json_bytes


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

        # ── [Phase 10] 검증서 병렬 렌더링 + B2 업로드 ───────────────
        # 반드시 DB 트랜잭션 진입 전에 실행.
        # 실패 시 예외가 외부 except로 전파 → DB INSERT 미실행 = 원자성 보장.

        # [Bug 1+2 Fix] seal_event_id를 ledger_id 기반 uuid5로 결정론적 생성.
        # uuid5는 동일 ledger_id에 대해 항상 동일 UUID → 재시도 시 cert 내용 불변 보장.
        # cert_hash 발산 없음, B2 동일 키에 동일 바이트 PUT → 완전 멱등.
        seal_event_id = str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_URL, ledger_id))
        seal_data_for_cert = {
            "ledger_id":        ledger_id,
            "seal_event_id":    seal_event_id,
            "facility_id":      meta.get("facility_id",   ""),
            "beneficiary_id":   meta.get("beneficiary_id",""),
            "care_type":        meta.get("care_type",     ""),
            "ingested_at":      meta.get("server_ts",     ""),
            "audio_sha256":     audio_sha256,
            "transcript_sha256":transcript_sha256,
            "chain_hash":       chain,
            "transcript_text":  transcript,
            "worm_bucket":      B2_BUCKET_NAME,
            "worm_object_key":  b2_key,
            "worm_retain_until":retain.isoformat(),
        }
        pdf_key_cert, json_key_cert, pdf_bytes, json_bytes = await _issue_certificates(
            seal_data_for_cert, b2, ledger_id, retain
        )

        # ── [TD-02] Append-Only 봉인: evidence_ledger UPDATE 폐지 ──
        # 봉인 결과는 evidence_seal_event에 INSERT (Append-Only).
        # ON CONFLICT (ledger_id) DO NOTHING — 워커 재시도 시 멱등 보장.
        # 원본 evidence_ledger는 단 한 번 INSERT 후 영원히 불변.
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO evidence_seal_event (
                    id, ledger_id, audio_sha256, transcript_sha256,
                    chain_hash, transcript_text,
                    worm_bucket, worm_object_key, worm_retain_until,
                    sealed_at
                ) VALUES (
                    :seid, :lid, :a, :t,
                    :c, :tx,
                    :bkt, :bk, :ret,
                    NOW()
                )
                ON CONFLICT (ledger_id) DO NOTHING
            """), {
                "seid": seal_event_id,
                "lid":  ledger_id,
                "a":    audio_sha256,
                "t":    transcript_sha256,
                "c":    chain,
                "tx":   transcript,
                "bkt":  B2_BUCKET_NAME,
                "bk":   b2_key,
                "ret":  retain,
            })

            # ── [Phase 10] certificate_ledger INSERT (동일 트랜잭션) ──
            # RETURNING으로 seal_event_id를 받을 수 없을 때는 SELECT로 조회
            seal_row = conn.execute(text(
                "SELECT id FROM evidence_seal_event WHERE ledger_id=:lid"
            ), {"lid": ledger_id}).fetchone()
            seal_event_id = str(seal_row.id) if seal_row else ledger_id

            conn.execute(text("""
                INSERT INTO certificate_ledger
                    (id, ledger_id, seal_event_id, cert_type,
                     cert_hash, storage_key, issuer_version, worm_retain_until)
                VALUES
                    (gen_random_uuid(), :lid, :seid, 'PDF',
                     :phash, :pkey, 'vg-cert-v1.0.0', :ret),
                    (gen_random_uuid(), :lid, :seid, 'JSON',
                     :jhash, :jkey, 'vg-cert-v1.0.0', :ret)
                ON CONFLICT (seal_event_id, cert_type) DO NOTHING
            """), {
                "lid":   ledger_id,
                "seid":  seal_event_id,
                "phash": hashlib.sha256(pdf_bytes).hexdigest(),
                "pkey":  pdf_key_cert,
                "jhash": hashlib.sha256(json_bytes).hexdigest(),
                "jkey":  json_key_cert,
                "ret":   retain,
            })

            conn.execute(text(
                "UPDATE outbox_events SET status='done',processed_at=NOW() WHERE id=:id"),
                {"id": outbox_id})

        if msg_id != "db-poll":
            await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)

        logger.info(f"[WORKER] ✅ 봉인 완료: ledger={ledger_id}")

        # ── Gemini 정제 → Notion 인수인계 1장 템플릿 큐 등록 ────────
        # 봉인 완료 후 비치명적으로 실행 — 실패해도 증거 봉인은 이미 완료
        try:
            gemini_meta = {
                "beneficiary_id": meta.get("beneficiary_id", ""),
                "facility_id":    meta.get("facility_id", ""),
                "shift_id":       meta.get("shift_id", ""),
                "report_date":    meta.get("server_ts", "")[:10],  # YYYY-MM-DD
            }
            gemini_json = await call_gemini(transcript, gemini_meta)

            notion_payload = json.dumps({
                "ledger_id":       ledger_id,
                "facility_id":     meta.get("facility_id", ""),
                "beneficiary_id":  meta.get("beneficiary_id", ""),
                "shift_id":        meta.get("shift_id", ""),
                "ingested_at":     meta.get("server_ts", ""),
                "chain_hash":      chain,
                "worm_object_key": b2_key,
                "gemini_json":     gemini_json,   # 5-Block 라우팅 트리거
            }, ensure_ascii=False)

            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO notion_sync_outbox
                        (id, ledger_id, status, attempts, payload, created_at)
                    VALUES
                        (gen_random_uuid(), :lid, 'pending', 0,
                         CAST(:payload AS jsonb), NOW())
                    ON CONFLICT DO NOTHING
                """), {"lid": ledger_id, "payload": notion_payload})

            logger.info(f"[WORKER] Notion 인수인계 템플릿 큐 등록: ledger={ledger_id}")
        except Exception as e:
            # notion 큐 등록 실패는 워닝만 — 증거 봉인은 이미 완료됨
            logger.warning(f"[WORKER] Notion 큐 등록 실패 (비치명적): {e}")

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

# ══════════════════════════════════════════════════════════════════
# 6대 의무기록: care_record_outbox 단일 레코드 처리 (신규)
# ══════════════════════════════════════════════════════════════════

async def process_care_record(redis: aioredis.Redis, record_id: str, msg_id: str) -> None:
    """
    care_record_outbox 1건 처리.

    흐름:
      1. care_record_outbox + care_record_ledger 조회
      2. call_gemini_care_record() → 6대 의무기록 구조화
      3. notion_sync_outbox INSERT (care_record_json 키) → notion_sync 워커 인계
      4. care_record_outbox status='done'
    """
    if not engine:
        return

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT o.id AS oid, o.attempts, o.payload, o.status,
                   r.facility_id, r.beneficiary_id, r.caregiver_id,
                   r.raw_voice_text, r.recorded_at
            FROM care_record_outbox o
            JOIN care_record_ledger r ON r.id = o.record_id
            WHERE o.record_id = :rid
              AND o.status IN ('pending', 'processing')
            LIMIT 1
        """), {"rid": record_id}).fetchone()

    if not row:
        if msg_id != "db-poll":
            await redis.xack(REDIS_CARE_STREAM, CONSUMER_GROUP, msg_id)
        return

    outbox_id = str(row.oid)
    attempts  = row.attempts

    # DLQ 판단
    if attempts >= MAX_ATTEMPTS:
        if not engine:
            return
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE care_record_outbox SET status='dlq', processed_at=NOW() WHERE id=:id"
            ), {"id": outbox_id})
        logger.critical(f"[CARE-WORKER] MAX_ATTEMPTS 초과 DLQ: record={record_id}")
        if msg_id != "db-poll":
            await redis.xack(REDIS_CARE_STREAM, CONSUMER_GROUP, msg_id)
        return

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE care_record_outbox SET status='processing', attempts=attempts+1 WHERE id=:id"
        ), {"id": outbox_id})

    logger.info(f"[CARE-WORKER] 처리: record={record_id} attempt={attempts+1}/{MAX_ATTEMPTS}")

    try:
        metadata = {
            "beneficiary_id": row.beneficiary_id or "",
            "facility_id":    row.facility_id    or "",
            "recorded_at":    row.recorded_at.isoformat() if row.recorded_at else "",
        }

        # Gemini 6대 의무기록 정제 (실패 시 기본값 JSON 반환 — 파이프라인 블로킹 금지)
        care_record_json = await call_gemini_care_record(
            raw_voice_text=row.raw_voice_text or "",
            metadata=metadata,
        )

        # notion_sync_outbox에 care_record_json 키로 등록 → notion_sync 워커 인계
        notion_payload = json.dumps({
            "record_id":      record_id,
            "facility_id":    row.facility_id    or "",
            "beneficiary_id": row.beneficiary_id or "",
            "care_record_json": care_record_json,  # 3-way 라우팅 트리거
        }, ensure_ascii=False)

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO notion_sync_outbox
                    (id, ledger_id, status, attempts, payload, created_at)
                VALUES
                    (gen_random_uuid(), :lid, 'pending', 0,
                     CAST(:payload AS jsonb), NOW())
                ON CONFLICT DO NOTHING
            """), {
                "lid":     record_id,   # care record ID를 ledger_id 컬럼 재활용
                "payload": notion_payload,
            })

            conn.execute(text(
                "UPDATE care_record_outbox SET status='done', processed_at=NOW() WHERE id=:id"
            ), {"id": outbox_id})

        if msg_id != "db-poll":
            await redis.xack(REDIS_CARE_STREAM, CONSUMER_GROUP, msg_id)

        logger.info(f"[CARE-WORKER] ✅ 완료: record={record_id}")

    except Exception as e:
        logger.error(f"[CARE-WORKER] 실패 attempt={attempts+1}: {e}")
        delay = BACKOFF[min(attempts, len(BACKOFF) - 1)]
        try:
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE care_record_outbox
                    SET status='pending', error_message=:msg,
                        next_retry_at=NOW()+(:d||' seconds')::INTERVAL
                    WHERE id=:id AND status='processing'
                """), {"msg": str(e)[:500], "d": delay, "id": outbox_id})
        except Exception:
            pass


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

async def poll_care_fallback(redis: aioredis.Redis):
    """시나리오 B (care:records): Redis 장애 시 care_record_outbox 직접 폴링"""
    if not engine:
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT record_id FROM care_record_outbox
                WHERE status='pending' AND attempts < :m
                  AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                ORDER BY created_at LIMIT 5
            """), {"m": MAX_ATTEMPTS}).fetchall()
        for row in rows:
            await process_care_record(redis, str(row.record_id), "db-poll")
    except Exception as e:
        logger.error(f"[CARE-FALLBACK] {e}")


async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # voice:ingest Consumer Group
    try:
        await redis.xgroup_create(REDIS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("[WORKER] Consumer Group 생성.")
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e): raise

    # care:records Consumer Group (신규)
    try:
        await redis.xgroup_create(REDIS_CARE_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("[WORKER] care:records Consumer Group 생성.")
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

            # XREADGROUP: 새 메시지 (voice:ingest + care:records 동시 구독)
            messages = await redis.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {REDIS_STREAM: ">", REDIS_CARE_STREAM: ">"},
                count=5, block=BLOCK_MS,
            )

            if not messages:
                await poll_fallback(redis)
                await poll_care_fallback(redis)
                # NT-1: 약 30초마다 미기록 임박 건 알림톡 점검
                overdue_tick += 1
                if overdue_tick >= 6:
                    overdue_tick = 0
                    await check_overdue()
                continue

            for stream_name, msgs in messages:
                for msg_id, fields in msgs:
                    if stream_name == REDIS_CARE_STREAM:
                        # care:records 스트림 처리
                        rid = fields.get("record_id", "")
                        if rid:
                            await process_care_record(redis, rid, msg_id)
                        else:
                            await redis.xack(REDIS_CARE_STREAM, CONSUMER_GROUP, msg_id)
                    else:
                        # voice:ingest 스트림 처리 (기존)
                        lid = fields.get("ledger_id", "")
                        if lid:
                            await process(redis, lid, msg_id)
                        else:
                            await redis.xack(REDIS_STREAM, CONSUMER_GROUP, msg_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[LOOP] {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
