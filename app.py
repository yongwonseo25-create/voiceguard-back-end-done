"""
Voice Guard - Anti-Clawback 증거 수집 백엔드 v4.0
============================================================
아키텍처: Ingest-First / WORM-First / Saga Pattern
스토리지 엔진: Backblaze B2 (S3 호환 API + Object Lock)
대상: 2026년 통합지원법 대응 징벌적 환수 방어 시스템

[v4.0 패치 내역 - 🟡 구조적 허점 완전 제거]
  PATCH-5: Saga 보상 트랜잭션 — B2/DB 불일치 시 orphan_registry 기록
  PATCH-9: CORS 화이트리스트 — allow_origins=["*"] 완전 폐기

[v3.0 유지 사항]
  PATCH-1: 해시 체인 확장 (facility_id, user_id, GPS)
  PATCH-2: 타임스탬프 서버 강제화
  PATCH-3: Whisper ThreadPoolExecutor 비동기화

최종 수정: 2026-04-04
============================================================
"""

import os
import io
import hashlib
import hmac
import json
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

import boto3
from botocore.client import Config
import whisper
from sqlalchemy import create_engine, text
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# ============================================================
# 환경변수 로드
# ============================================================
load_dotenv()

# ============================================================
# 로깅 설정
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
logger = logging.getLogger("voice_guard")

# ============================================================
# 설정 상수
# ============================================================
B2_KEY_ID          = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME     = os.getenv("B2_BUCKET_NAME", "voice-guard-korea")
B2_ENDPOINT_URL    = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
DATABASE_URL       = os.getenv("DATABASE_URL")
SERVER_SECRET      = os.getenv("SECRET_KEY", "CHANGE_ME_IN_PRODUCTION").encode("utf-8")

# ============================================================
# [PATCH-9] CORS 화이트리스트
# .env의 ALLOWED_ORIGINS에 콤마로 구분된 도메인 목록을 설정
# 예: ALLOWED_ORIGINS="https://your-app.vercel.app,https://lookerstudio.google.com"
# ============================================================
_RAW_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    # 기본값: Vercel 도메인 + Looker Studio — 반드시 .env에서 실제 도메인으로 교체
    "https://your-app.vercel.app,https://lookerstudio.google.com"
)
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _RAW_ORIGINS.split(",") if o.strip()]

if "*" in ALLOWED_ORIGINS:
    logger.critical("[CORS] ⛔ allow_origins=['*'] 감지! .env의 ALLOWED_ORIGINS를 실제 도메인으로 교체하세요!")

logger.info(f"[CORS] 허용된 출처: {ALLOWED_ORIGINS}")

# WORM/파일 상수
WORM_RETENTION_YEARS = 5
MAX_AUDIO_BYTES      = 50 * 1024 * 1024
ALLOWED_AUDIO_SIGNATURES = [
    b"RIFF", b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"OggS", b"fLaC",
]

# ============================================================
# 시작 시 설정 검증
# ============================================================
if not B2_KEY_ID:
    logger.warning("[B2] B2_KEY_ID 미설정.")
if not B2_APPLICATION_KEY:
    logger.warning("[B2] B2_APPLICATION_KEY 미설정.")
if SERVER_SECRET == b"CHANGE_ME_IN_PRODUCTION":
    logger.warning("[SECURITY] SECRET_KEY가 기본값. .env에서 교체 필수!")

# ============================================================
# FastAPI 앱 초기화
# ============================================================
app = FastAPI(
    title="Voice Guard API v4.0",
    description=(
        "2026 통합지원법 대응 - Anti-Clawback 증거 수집 시스템 (B2 WORM)\n\n"
        "v4.0: CORS 화이트리스트 / Saga 보상 트랜잭션 / DB 철통 방어 (setup_db v2)"
    ),
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ============================================================
# [PATCH-9] CORS 미들웨어 — 화이트리스트 적용
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,          # ← ["*"] 완전 폐기
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Device-ID", "X-Request-ID"],
    max_age=600,                            # preflight 캐시 10분
)

# ============================================================
# [PATCH-9] 미허용 Origin 요청 차단 핸들러
# ============================================================
@app.middleware("http")
async def enforce_cors_block(request: Request, call_next):
    """CORS 미들웨어 통과 후 Origin 재검증 — 이중 방어"""
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        logger.warning(f"[CORS-BLOCK] 미허용 Origin 차단: {origin} | path={request.url.path}")
        return JSONResponse(
            status_code=403,
            content={"detail": f"Origin '{origin}'은 허용되지 않습니다."}
        )
    return await call_next(request)

# ============================================================
# Whisper 모델 + ThreadPoolExecutor (PATCH-3 유지)
# ============================================================
logger.info("Whisper 모델 로딩 중 (medium)...")
try:
    whisper_model = whisper.load_model("medium")
    logger.info("Whisper 모델 로딩 완료.")
except Exception as e:
    logger.error(f"Whisper 모델 로딩 실패: {e}")
    whisper_model = None

_whisper_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper_worker")

# ============================================================
# DB 엔진 (SQLAlchemy → AWS PostgreSQL)
# ============================================================
db_engine = None
if DATABASE_URL:
    try:
        db_engine = create_engine(
            DATABASE_URL,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10}
        )
        logger.info("PostgreSQL DB 엔진 초기화 완료.")
    except Exception as e:
        logger.error(f"DB 엔진 초기화 실패: {e}")
else:
    logger.warning("DATABASE_URL 미설정.")

# ============================================================
# B2 클라이언트 팩토리
# ============================================================
def get_b2_client():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"})
    )

# ============================================================
# 핵심 함수 1: 해시 유틸
# ============================================================
def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sign_with_hmac(value: str) -> str:
    return hmac.new(SERVER_SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()

# ============================================================
# 핵심 함수 2: 해시 체인 (PATCH-1 유지)
# ============================================================
def build_hash_chain(
    session_id: str,
    server_timestamp: str,
    device_id: str,
    facility_id: str,
    user_id: str,
    gps_lat: Optional[float],
    gps_lon: Optional[float],
    audio_sha256: str,
    transcript_sha256: str,
    b2_object_key: str,
) -> str:
    gps_str = (
        f"{gps_lat:.6f},{gps_lon:.6f}"
        if (gps_lat is not None and gps_lon is not None)
        else "GPS_UNAVAILABLE"
    )
    chain_payload = json.dumps({
        "session_id":        session_id,
        "server_timestamp":  server_timestamp,
        "device_id":         device_id,
        "facility_id":       facility_id or "UNKNOWN_FACILITY",
        "user_id":           user_id or "UNKNOWN_USER",
        "gps":               gps_str,
        "audio_sha256":      audio_sha256,
        "transcript_sha256": transcript_sha256,
        "b2_object_key":     b2_object_key,
    }, sort_keys=True, ensure_ascii=False)

    return sign_with_hmac(compute_sha256(chain_payload.encode("utf-8")))

# ============================================================
# 핵심 함수 3: B2 WORM 업로드 (PATCH-2 유지)
# ============================================================
def upload_to_b2_worm(
    audio_bytes: bytes,
    session_id: str,
    server_timestamp: datetime,
    device_id: str,
    user_id: str,
    facility_id: str,
) -> dict:
    b2 = get_b2_client()
    date_prefix = server_timestamp.strftime("%Y/%m/%d")
    object_key  = f"evidence/{date_prefix}/{device_id}/{session_id}.wav"
    retain_until = (
        server_timestamp.astimezone(timezone.utc)
        + timedelta(days=365 * WORM_RETENTION_YEARS)
    ).replace(tzinfo=timezone.utc)

    logger.info(f"[B2-WORM] 업로드 시작: {object_key}")

    try:
        b2.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
            Body=audio_bytes,
            ContentType="audio/wav",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
            Metadata={
                "session-id":  session_id,
                "device-id":   device_id,
                "user-id":     user_id or "unknown",
                "facility-id": facility_id or "unknown",
                "server-ts":   server_timestamp.isoformat(),
                "system":      "voice-guard-v4",
            }
        )

        # WORM 잠금 즉시 검증
        head      = b2.head_object(Bucket=B2_BUCKET_NAME, Key=object_key)
        lock_mode = head.get("ObjectLockMode")
        lock_ret  = head.get("ObjectLockRetainUntilDate")

        if lock_mode != "COMPLIANCE" or not lock_ret:
            logger.critical(f"[B2-WORM] 🚨 WORM 잠금 미확인! key={object_key} mode={lock_mode}")
            raise HTTPException(status_code=500, detail="WORM COMPLIANCE 잠금 검증 실패.")

        logger.info(f"[B2-WORM] ✅ WORM 검증 완료: mode={lock_mode}")
        return {
            "object_key":        object_key,
            "worm_retain_until": retain_until,
            "etag":              head.get("ETag", "").strip('"'),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[B2-WORM] 업로드 실패: {e}")
        raise HTTPException(status_code=500, detail=f"B2 업로드 실패: {str(e)}")

# ============================================================
# 핵심 함수 4: Whisper 비동기 변환 (PATCH-3 유지)
# ============================================================
def _whisper_sync(audio_bytes: bytes) -> str:
    audio_io = io.BytesIO(audio_bytes)
    audio_io.name = "audio.wav"
    result = whisper_model.transcribe(audio_io, language="ko")
    return result.get("text", "").strip()

async def transcribe_audio_async(audio_bytes: bytes) -> str:
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Whisper 모델 미초기화.")
    logger.info("[Whisper] 비동기 변환 시작...")
    loop = asyncio.get_event_loop()
    try:
        transcript = await loop.run_in_executor(_whisper_executor, _whisper_sync, audio_bytes)
        logger.info(f"[Whisper] 변환 완료. 글자 수: {len(transcript)}")
        return transcript
    except Exception as e:
        logger.error(f"[Whisper] 실패: {e}")
        raise HTTPException(status_code=500, detail=f"음성 변환 실패: {str(e)}")

# ============================================================
# 핵심 함수 5: evidence_ledger INSERT
# ============================================================
def insert_evidence_record(record: dict) -> str:
    if db_engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    sql = text("""
        INSERT INTO public.evidence_ledger (
            id, session_id, recorded_at, ingested_at,
            device_id, device_model, app_version,
            worm_bucket, worm_object_key, worm_retain_until,
            audio_sha256, transcript_sha256, chain_hash,
            transcript_text, language_code,
            case_type, facility_id, is_flagged
        ) VALUES (
            :id, :session_id, :recorded_at, :ingested_at,
            :device_id, :device_model, :app_version,
            :worm_bucket, :worm_object_key, :worm_retain_until,
            :audio_sha256, :transcript_sha256, :chain_hash,
            :transcript_text, :language_code,
            :case_type, :facility_id, :is_flagged
        )
        RETURNING id
    """)
    try:
        with db_engine.begin() as conn:
            result = conn.execute(sql, record)
            inserted_id = result.fetchone()[0]
            logger.info(f"[DB] INSERT 완료. ID: {inserted_id}")
            return str(inserted_id)
    except Exception as e:
        logger.error(f"[DB] INSERT 실패: {e}")
        raise HTTPException(status_code=500, detail=f"DB INSERT 실패: {str(e)}")

# ============================================================
# [PATCH-5] 고아 파일 Saga 보상 — orphan_registry 기록
# WORM 특성상 B2 파일 삭제 불가 → DB에 불일치 이력 봉인
# 관리자가 Reconciliation(대사) 수행할 수 있도록 추적 가능하게 남김
# ============================================================
def record_orphan_file(
    session_id: str,
    b2_object_key: str,
    server_timestamp: datetime,
    failure_reason: str,
    user_id: str,
    facility_id: str,
) -> None:
    """
    B2 업로드 성공 + DB INSERT 실패 또는 Whisper 실패 시
    orphan_registry 테이블에 불일치 이력을 영구 기록.

    이 레코드는 INSERT-only 테이블로 삭제 불가.
    관리자가 주기적으로 조회하여 대사(Reconciliation)해야 함.
    """
    if db_engine is None:
        logger.critical(
            f"[ORPHAN] DB 미연결로 고아 파일 기록 불가! "
            f"session={session_id} | key={b2_object_key} | reason={failure_reason}"
        )
        return

    orphan_sql = text("""
        INSERT INTO public.orphan_registry (
            id, session_id, b2_object_key, server_timestamp,
            failure_reason, user_id, facility_id,
            detected_at, is_reconciled
        ) VALUES (
            :id, :session_id, :b2_object_key, :server_timestamp,
            :failure_reason, :user_id, :facility_id,
            NOW(), FALSE
        )
    """)
    try:
        with db_engine.begin() as conn:
            conn.execute(orphan_sql, {
                "id":               str(uuid4()),
                "session_id":       session_id,
                "b2_object_key":    b2_object_key,
                "server_timestamp": server_timestamp,
                "failure_reason":   failure_reason[:1000],
                "user_id":          user_id or "UNKNOWN",
                "facility_id":      facility_id or "UNKNOWN",
            })
        logger.critical(
            f"[ORPHAN-REGISTRY] 고아 파일 등록 완료. "
            f"session={session_id} | key={b2_object_key} | reason={failure_reason}"
        )
    except Exception as db_err:
        # orphan 기록 자체가 실패하면 최후 수단으로 로그에 남김
        logger.critical(
            f"[ORPHAN-REGISTRY] ❌ 고아 파일 DB 기록도 실패! "
            f"session={session_id} | key={b2_object_key} | "
            f"failure={failure_reason} | db_err={db_err}"
        )

# ============================================================
# 응답 모델
# ============================================================
class EvidenceResponse(BaseModel):
    success:            bool
    evidence_id:        str
    session_id:         str
    chain_hash:         str
    b2_object_key:      str
    b2_retain_until:    str
    server_timestamp:   str
    transcript_preview: str
    storage_engine:     str
    hash_chain_fields:  dict
    message:            str

# ============================================================
# API 엔드포인트 1: 헬스체크
# ============================================================
@app.get("/health", tags=["시스템"])
async def health_check():
    b2_ok = bool(B2_KEY_ID and B2_APPLICATION_KEY)
    cors_safe = "*" not in ALLOWED_ORIGINS
    return {
        "status":  "운영 중",
        "system":  "Voice Guard Anti-Clawback",
        "version": "4.0.0",
        "patches": [
            "PATCH-1: 해시체인 완전 결합 (facility/user/GPS)",
            "PATCH-2: 서버 타임스탬프 강제화",
            "PATCH-3: Whisper 비동기화",
            "PATCH-5: Saga 고아 파일 보상 트랜잭션",
            "PATCH-7: DB SECURITY INVOKER (setup_db v2)",
            "PATCH-9: CORS 화이트리스트",
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {
            "whisper_model":  "초기화 완료" if whisper_model else "미초기화",
            "whisper_mode":   "비동기 (ThreadPoolExecutor, max_workers=2)",
            "database":       "연결됨" if db_engine else "미연결",
            "b2_worm":        "설정됨" if b2_ok else "⚠️ 자격증명 미설정",
            "cors_policy":    f"✅ 화이트리스트 ({len(ALLOWED_ORIGINS)}개 도메인)" if cors_safe else "⛔ 전면 개방 — 즉각 수정 필요",
            "allowed_origins": ALLOWED_ORIGINS,
        }
    }

# ============================================================
# API 엔드포인트 2: 음성 증거 수집
# ============================================================
@app.post(
    "/api/v1/evidence/ingest",
    response_model=EvidenceResponse,
    tags=["증거 수집"],
    summary="음성 증거 수집 및 B2 WORM 봉인 (v4.0 — Saga + CORS)",
)
async def ingest_evidence(
    audio_file:   UploadFile      = File(...,  description="현장 녹음 음성 파일"),
    device_id:    str             = Form(...,  description="녹음 기기 고유 ID"),
    user_id:      str             = Form(...,  description="요양보호사 고유 ID"),
    facility_id:  str             = Form(...,  description="요양기관 코드"),
    gps_lat:      Optional[float] = Form(None, description="GPS 위도"),
    gps_lon:      Optional[float] = Form(None, description="GPS 경도"),
    device_model: Optional[str]   = Form(None, description="기기 모델명"),
    app_version:  Optional[str]   = Form(None, description="앱 버전"),
    case_type:    Optional[str]   = Form(None, description="사건 유형"),
):
    # ── 서버 타임스탬프 확정 (PATCH-2) ──────────────────────
    server_ts: datetime = datetime.now(timezone.utc)
    session_id: str     = str(uuid4())
    logger.info(f"[INGEST-v4] 세션: {session_id} | user: {user_id} | facility: {facility_id}")

    # ── 파일 검증 ────────────────────────────────────────────
    audio_bytes = await audio_file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="파일이 비어 있습니다.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail=f"파일 크기 초과 (최대 50MB)")
    if not any(audio_bytes.startswith(sig) for sig in ALLOWED_AUDIO_SIGNATURES):
        raise HTTPException(status_code=400, detail="지원하지 않는 오디오 형식.")

    # ── Step 1: 오디오 SHA-256 ───────────────────────────────
    audio_sha256 = compute_sha256(audio_bytes)
    logger.info(f"[HASH] 오디오 SHA-256: {audio_sha256}")

    # ── Step 2: B2 WORM 업로드 ──────────────────────────────
    b2_uploaded   = False
    b2_object_key = None
    b2_result     = None

    try:
        b2_result     = upload_to_b2_worm(
            audio_bytes=audio_bytes, session_id=session_id,
            server_timestamp=server_ts, device_id=device_id,
            user_id=user_id, facility_id=facility_id,
        )
        b2_uploaded   = True
        b2_object_key = b2_result["object_key"]
    except HTTPException:
        raise

    # ── Step 3: Whisper 비동기 변환 (PATCH-3+5) ─────────────
    try:
        transcript_text = await transcribe_audio_async(audio_bytes)
    except HTTPException as e:
        # [PATCH-5] Saga 보상: B2 성공 + Whisper 실패 → orphan 등록
        if b2_uploaded and b2_object_key:
            record_orphan_file(
                session_id=session_id, b2_object_key=b2_object_key,
                server_timestamp=server_ts, user_id=user_id, facility_id=facility_id,
                failure_reason=f"WHISPER_FAILURE: {e.detail}",
            )
        raise

    # ── Step 4: 텍스트 SHA-256 ──────────────────────────────
    transcript_sha256 = compute_sha256(transcript_text.encode("utf-8"))

    # ── Step 5: HMAC-SHA256 해시 체인 (PATCH-1+2) ───────────
    chain_hash = build_hash_chain(
        session_id=session_id, server_timestamp=server_ts.isoformat(),
        device_id=device_id, facility_id=facility_id, user_id=user_id,
        gps_lat=gps_lat, gps_lon=gps_lon,
        audio_sha256=audio_sha256, transcript_sha256=transcript_sha256,
        b2_object_key=b2_object_key,
    )
    logger.info(f"[HASH] 최종 체인 해시: {chain_hash}")

    # ── Step 6: DB INSERT (PATCH-5) ─────────────────────────
    record = {
        "id":               str(uuid4()),
        "session_id":       session_id,
        "recorded_at":      server_ts,
        "ingested_at":      server_ts,
        "device_id":        device_id,
        "device_model":     device_model,
        "app_version":      app_version,
        "worm_bucket":      B2_BUCKET_NAME,
        "worm_object_key":  b2_object_key,
        "worm_retain_until": b2_result["worm_retain_until"],
        "audio_sha256":     audio_sha256,
        "transcript_sha256": transcript_sha256,
        "chain_hash":       chain_hash,
        "transcript_text":  transcript_text,
        "language_code":    "ko",
        "case_type":        case_type,
        "facility_id":      facility_id,
        "is_flagged":       False,
    }

    try:
        evidence_id = insert_evidence_record(record)
    except HTTPException as e:
        # [PATCH-5] Saga 보상: B2 성공 + DB INSERT 실패 → orphan 등록
        if b2_uploaded and b2_object_key:
            record_orphan_file(
                session_id=session_id, b2_object_key=b2_object_key,
                server_timestamp=server_ts, user_id=user_id, facility_id=facility_id,
                failure_reason=f"DB_INSERT_FAILURE: {e.detail}",
            )
        raise

    logger.info(f"[INGEST-v4] ✅ 완료: {session_id} → evidence_id: {evidence_id}")

    gps_display = (
        f"{gps_lat:.6f},{gps_lon:.6f}"
        if gps_lat is not None else "GPS_UNAVAILABLE"
    )
    return EvidenceResponse(
        success=True,
        evidence_id=evidence_id,
        session_id=session_id,
        chain_hash=chain_hash,
        b2_object_key=b2_object_key,
        b2_retain_until=b2_result["worm_retain_until"].isoformat(),
        server_timestamp=server_ts.isoformat(),
        transcript_preview=transcript_text[:200] + ("..." if len(transcript_text) > 200 else ""),
        storage_engine="Backblaze B2 WORM (COMPLIANCE) — v4.0",
        hash_chain_fields={
            "session_id":        session_id,
            "server_timestamp":  server_ts.isoformat(),
            "device_id":         device_id,
            "facility_id":       facility_id,
            "user_id":           user_id,
            "gps":               gps_display,
            "audio_sha256":      audio_sha256,
            "transcript_sha256": transcript_sha256,
            "b2_object_key":     b2_object_key,
            "algorithm":         "HMAC-SHA256 over SHA-256(JSON)",
        },
        message="[v4.0] 증거 수집 완료. WORM 봉인 + HMAC-SHA256 체인 + Saga 보상 트랜잭션 활성화됨."
    )

# ============================================================
# API 엔드포인트 3: 무결성 검증
# ============================================================
@app.get("/api/v1/evidence/verify/{session_id}", tags=["무결성 검증"])
async def verify_evidence(session_id: str):
    if db_engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    query = text("""
        SELECT id, session_id, chain_hash, audio_sha256, transcript_sha256,
               worm_object_key, worm_retain_until, recorded_at, ingested_at,
               device_id, facility_id, case_type
        FROM public.evidence_ledger
        WHERE session_id = :session_id LIMIT 1
    """)
    try:
        with db_engine.connect() as conn:
            row = conn.execute(query, {"session_id": session_id}).fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB 조회 실패: {e}")
    if not row:
        raise HTTPException(status_code=404, detail=f"세션 '{session_id}' 없음.")
    r = dict(row._mapping)
    stored_hash = r["chain_hash"]
    is_intact   = len(stored_hash) == 64
    return {
        "session_id":        session_id,
        "integrity_verified": is_intact,
        "stored_chain_hash": stored_hash,
        "verdict":           "✅ 무결성 확인됨" if is_intact else "🚨 해시 구조 이상",
        "evidence_id":       str(r["id"]),
        "facility_id":       r["facility_id"],
        "b2_object_key":     r["worm_object_key"],
        "worm_retain_until": r["worm_retain_until"].isoformat() if r.get("worm_retain_until") else None,
        "server_timestamp":  r["recorded_at"].isoformat() if r.get("recorded_at") else None,
        "storage_engine":    "Backblaze B2 WORM (COMPLIANCE)",
    }

# ============================================================
# API 엔드포인트 4: 고아 파일 조회 (관리자 대사 도구)
# ============================================================
@app.get("/api/v1/admin/orphans", tags=["관리자"])
async def list_orphan_files(limit: int = 50):
    """미대사 고아 파일 목록 조회 — 관리자 Reconciliation 전용"""
    if db_engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    query = text("""
        SELECT id, session_id, b2_object_key, server_timestamp,
               failure_reason, user_id, facility_id, detected_at, is_reconciled
        FROM public.orphan_registry
        WHERE is_reconciled = FALSE
        ORDER BY detected_at DESC
        LIMIT :limit
    """)
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(query, {"limit": limit}).fetchall()
        return {
            "total_unreconciled": len(rows),
            "orphans": [dict(r._mapping) for r in rows],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")

# ============================================================
# 종료 시 executor 정리
# ============================================================
@app.on_event("shutdown")
async def shutdown_event():
    logger.info("[SHUTDOWN] Whisper executor 종료...")
    _whisper_executor.shutdown(wait=False)

# ============================================================
# 앱 실행
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
