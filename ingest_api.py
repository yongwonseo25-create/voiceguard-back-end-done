"""
Voice Guard — Ingest API v2.0  (Phase 2)
=========================================
핵심 원칙 (절대 규칙):
  1. Ingest-First: 수신 즉시 DB 커밋 → AI/외부 처리 일절 대기 없음
  2. Idempotency Key: facility_id + beneficiary_id + shift_id 조합 SHA-256
  3. Atomic Split: evidence_ledger + notion_sync_outbox 동일 트랜잭션
  4. 커밋 완료 후 Redis Stream XADD → 워커 즉시 알림

데이터 흐름:
  POST /api/v2/ingest
      → Idempotency 검증 (DB UNIQUE → 409 중복 차단)
      → [단일 트랜잭션]
            INSERT evidence_ledger
            INSERT notion_sync_outbox (pending)
      → COMMIT
      → XADD voice:ingest (Redis 워커 알림)
      → 202 Accepted 반환
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("ingest_api_v2")

# ── 설정 ────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS",
    "https://your-app.vercel.app,https://lookerstudio.google.com"
).split(",") if o.strip()]

REDIS_STREAM_KEY = "voice:ingest"   # 워커가 구독하는 스트림 키

# ── DB 엔진 ─────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

# ── Redis 클라이언트 (비동기) ───────────────────────────────
redis_client: Optional[aioredis.Redis] = None

# ── FastAPI ─────────────────────────────────────────────────
app = FastAPI(
    title="Voice Guard Ingest API v2",
    description="Anti-Clawback 증거 수집 — Ingest-First / Transactional Outbox",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── 응답 모델 ────────────────────────────────────────────────
class IngestResponse(BaseModel):
    accepted:        bool
    ledger_id:       str
    idempotency_key: str
    server_ts:       str
    message:         str

# ── 앱 시작/종료 훅 ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global redis_client
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("[STARTUP] Redis 연결 성공.")
    except Exception as e:
        logger.warning(f"[STARTUP] Redis 연결 실패 (워커 알림 불가): {e}")

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()

# ── Idempotency Key 생성 ────────────────────────────────────
def make_idempotency_key(facility_id: str, beneficiary_id: str, shift_id: str) -> str:
    """
    facility_id + beneficiary_id + shift_id 조합의 SHA-256.
    동일한 근무 교대(shift) 내 중복 제출을 원천 차단.
    DB UNIQUE 제약과 연동하여 이중으로 보호.
    """
    raw = f"{facility_id}::{beneficiary_id}::{shift_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ── 헬스체크 ────────────────────────────────────────────────
@app.get("/health", tags=["시스템"])
async def health():
    r_ok = False
    if redis_client:
        try:
            await redis_client.ping()
            r_ok = True
        except Exception:
            pass
    return {
        "status":   "운영 중",
        "version":  "2.0.0",
        "db":       "연결됨" if engine else "미연결",
        "redis":    "연결됨" if r_ok else "미연결",
        "pipeline": "Ingest-First → Atomic Outbox → Redis Stream",
    }

# ── 핵심 엔드포인트: 음성 + 메타데이터 수신 ────────────────
@app.post(
    "/api/v2/ingest",
    response_model=IngestResponse,
    status_code=202,
    tags=["증거 수집"],
    summary="음성 증거 수집 v2 — Ingest-First + Transactional Outbox",
)
async def ingest_v2(
    audio_file:     UploadFile      = File(...,  description="현장 녹음 파일"),
    facility_id:    str             = Form(...,  description="요양기관 코드"),
    beneficiary_id: str             = Form(...,  description="수급자 ID"),
    shift_id:       str             = Form(...,  description="근무 교대 ID (당일 업무 단위)"),
    user_id:        str             = Form(...,  description="요양보호사 ID"),
    gps_lat:        Optional[float] = Form(None, description="GPS 위도"),
    gps_lon:        Optional[float] = Form(None, description="GPS 경도"),
    device_id:      Optional[str]   = Form(None, description="기기 ID"),
    care_type:      Optional[str]   = Form(None, description="급여 유형 (방문요양/목욕/간호 등)"),
):
    # ── Step 0: 서버 타임스탬프 확정 (클라이언트 주입 불가) ──
    server_ts  = datetime.now(timezone.utc)
    ledger_id  = str(uuid4())

    # ── Step 1: Idempotency Key 생성 ────────────────────────
    idem_key = make_idempotency_key(facility_id, beneficiary_id, shift_id)
    logger.info(f"[INGEST-v2] facility={facility_id} | beneficiary={beneficiary_id} | shift={shift_id} | key={idem_key[:12]}...")

    # ── Step 2: 오디오 바이트 읽기 (최대 50MB) ──────────────
    audio_bytes = await audio_file.read()
    if len(audio_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일 크기 초과 (최대 50MB)")
    audio_size_kb = len(audio_bytes) // 1024

    # ── Step 3: 단일 DB 트랜잭션 (Atomic Split) ─────────────
    #
    #   evidence_ledger   → 불변 원장 (원본 메타데이터 봉인)
    #   notion_sync_outbox → 비동기 처리 큐 (Transactional Outbox Pattern)
    #
    #   두 테이블이 반드시 동일 트랜잭션으로 커밋됨.
    #   이중 쓰기(Dual-write) 문제 원천 차단:
    #     → DB 커밋 성공 = outbox 존재 보장
    #     → DB 커밋 실패 = 둘 다 롤백 (불일치 없음)
    #
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

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
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:   # ← begin() = 자동 커밋/롤백
            # [3-A] evidence_ledger INSERT
            conn.execute(text("""
                INSERT INTO public.evidence_ledger (
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
                    :audio_sha256, :transcript_sha256, :chain_hash,
                    :transcript_text, :language_code,
                    :case_type, :is_flagged,
                    :beneficiary_id, :shift_id, :idempotency_key,
                    :care_type, :gps_lat, :gps_lon,
                    :audio_size_kb, :worm_bucket, :worm_object_key, :worm_retain_until
                )
            """), {
                "id":               ledger_id,
                "session_id":       str(uuid4()),
                "recorded_at":      server_ts,
                "ingested_at":      server_ts,
                "device_id":        device_id or "unknown",
                "facility_id":      facility_id,
                "audio_sha256":     "pending",        # Whisper 워커가 채움
                "transcript_sha256":"pending",
                "chain_hash":       "pending",
                "transcript_text":  "",               # Whisper 워커가 채움
                "language_code":    "ko",
                "case_type":        care_type,
                "is_flagged":       False,
                "beneficiary_id":   beneficiary_id,
                "shift_id":         shift_id,
                "idempotency_key":  idem_key,
                "care_type":        care_type,
                "gps_lat":          gps_lat,
                "gps_lon":          gps_lon,
                "audio_size_kb":    audio_size_kb,
                "worm_bucket":      "pending",        # B2 워커가 채움
                "worm_object_key":  "pending",
                "worm_retain_until": server_ts,       # 임시값, 워커가 업데이트
            })

            # [3-B] notion_sync_outbox INSERT (동일 트랜잭션)
            conn.execute(text("""
                INSERT INTO public.notion_sync_outbox (
                    id, ledger_id, status, attempts,
                    payload, created_at
                ) VALUES (
                    :id, :ledger_id, 'pending', 0,
                    :payload, :created_at
                )
            """), {
                "id":         str(uuid4()),
                "ledger_id":  ledger_id,
                "payload":    outbox_payload,
                "created_at": server_ts,
            })

        # ← engine.begin() 블록 종료 = 자동 COMMIT
        logger.info(f"[INGEST-v2] ✅ DB COMMIT 완료. ledger_id={ledger_id}")

    except IntegrityError as e:
        # Idempotency Key UNIQUE 위반 → 중복 요청
        if "idempotency_key" in str(e).lower():
            logger.warning(f"[INGEST-v2] 중복 요청 차단: key={idem_key[:12]}...")
            raise HTTPException(
                status_code=409,
                detail=f"중복 요청: shift_id='{shift_id}'는 이미 처리되었습니다."
            )
        raise HTTPException(status_code=500, detail=f"DB 오류: {e}")
    except Exception as e:
        logger.error(f"[INGEST-v2] DB 트랜잭션 실패: {e}")
        raise HTTPException(status_code=500, detail=f"저장 실패: {e}")

    # ── Step 4: Redis 스트림에 워커 알림 (XADD) ─────────────
    #   DB 커밋 후 실행 → Redis 실패해도 DB는 이미 안전하게 보존됨
    #   워커는 outbox 폴링으로도 복구 가능 (이중 안전망)
    if redis_client:
        try:
            await redis_client.xadd(
                REDIS_STREAM_KEY,
                {
                    "ledger_id":  ledger_id,
                    "facility_id": facility_id,
                    "server_ts":  server_ts.isoformat(),
                },
                maxlen=10000,   # 스트림 최대 길이 (메모리 보호)
            )
            logger.info(f"[INGEST-v2] Redis XADD 완료: stream={REDIS_STREAM_KEY}")
        except Exception as e:
            # Redis 실패는 경고만 — outbox 폴링이 백업으로 처리
            logger.warning(f"[INGEST-v2] Redis XADD 실패 (outbox 폴링으로 보정): {e}")

    # ── Step 5: 202 Accepted 즉시 반환 ──────────────────────
    #   AI(Whisper) 처리, B2 업로드 등 무거운 작업은 워커가 비동기로 처리
    return IngestResponse(
        accepted        = True,
        ledger_id       = ledger_id,
        idempotency_key = idem_key,
        server_ts       = server_ts.isoformat(),
        message         = (
            "증거 수신 완료 (Ingest-First). "
            "음성 변환 및 WORM 봉인은 비동기 워커가 처리 중입니다."
        ),
    )

# ── 미기록 조회 (Alert View 용) ──────────────────────────────
@app.get("/api/v2/alerts", tags=["대시보드"])
async def get_alerts(minutes: int = 5):
    """N분 이내 임박 미기록 건 조회 — Alert View 실시간 폴링"""
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    query = text("""
        SELECT id, facility_id, beneficiary_id, shift_id,
               care_type, ingested_at, chain_hash, is_flagged,
               gps_lat, gps_lon,
               EXTRACT(EPOCH FROM (NOW() - ingested_at)) / 60 AS minutes_elapsed
        FROM public.evidence_ledger
        WHERE transcript_text = ''          -- 미처리(Whisper 미완료)
          AND ingested_at >= NOW() - INTERVAL ':minutes minutes'
        ORDER BY ingested_at ASC
        LIMIT 100
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query.bindparams(minutes=minutes)).fetchall()
        return {"alerts": [dict(r._mapping) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Audit-Ready 조회 (AG Grid 용) ───────────────────────────
@app.get("/api/v2/audit", tags=["대시보드"])
async def get_audit_records(facility_id: Optional[str] = None, limit: int = 200):
    """수급자별 해시/WORM/타임스탬프 — AG Grid 원본 데이터"""
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    where = "WHERE facility_id = :fid" if facility_id else ""
    query = text(f"""
        SELECT id, facility_id, beneficiary_id, shift_id,
               care_type, recorded_at, ingested_at,
               audio_sha256, chain_hash,
               worm_bucket, worm_object_key, worm_retain_until,
               transcript_text != '' AS has_audio,
               is_flagged
        FROM public.evidence_ledger
        {where}
        ORDER BY recorded_at DESC
        LIMIT :limit
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query.bindparams(
                limit=limit, **({"fid": facility_id} if facility_id else {})
            )).fetchall()
        return {"records": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ingest_api:app", host="0.0.0.0", port=8001, reload=True)
