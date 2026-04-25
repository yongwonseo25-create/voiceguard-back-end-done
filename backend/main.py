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
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional
from uuid import uuid4

# ── load_dotenv 최우선 실행: 로컬 모듈 임포트 전에 환경변수 주입 ──
# Cloud Run에서는 Secret Manager가 환경변수를 직접 주입하므로 no-op.
# 로컬에서는 backend/.env를 읽어 os.environ에 적재.
from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from app_check_middleware import app_check_middleware
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import httpx
import openai
import re

from notifier import (
    send_alimtalk,
    ALIMTALK_TPL_NT3,
    DEFAULT_FACILITY_PHONE,
    # ── Phase 7-v7 ────────────────────────────────────────────────
    ALIMTALK_TPL_EMERGENCY,
    ALIMTALK_TPL_SHIFT_GROUP,
    EMERGENCY_RECIPIENTS,
    resolve_shift_code_auto,
    resolve_shift_recipients,
    fanout_alimtalk,
    _KST,  # KST ZoneInfo — v8 handover 교대조 계산용
)
from angel_bridge import router as angel_router, init_angel_bridge
from angel_export import router as angel_export_router, init_angel_export
from angel_rpa import router as angel_rpa_router, init_angel_rpa
from env_guard import check_env_vars
from notion_pipeline import run_pipeline as notion_run_pipeline
from notion_pipeline import create_handover_row as notion_create_handover_row

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("voice_guard.main")

# ── [TD-01/TD-05] 환경변수 강제화: 모듈 로드 시점 즉시 검증 ────────
# SECRET_KEY='CHANGE_ME' 또는 핵심 API 키 누락 시 RuntimeError로 즉시 종료.
# uvicorn 기동 자체가 차단되어 무효 증거 인프라 가동 원천 차단.
check_env_vars("worm", "ai", "notion", "alimtalk")

# ── 설정 ──────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_SSE_CHANNEL  = "sse:dashboard"          # Pub/Sub 채널 (워커 → SSE)
REDIS_STREAM       = "voice:ingest"
REDIS_CARE_STREAM  = "care:records"           # 6대 의무기록 스트림 (신규)
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000"
).split(",") if o.strip()]

# ── AI API 설정 (사령관 지시) ──────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

GEMINI_REST_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

async def _call_gemini(prompt: str) -> str:
    """Gemini REST API — x-goog-api-key 헤더 인증 (키 로그 노출 차단) + 지수 백오프 재시도"""
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    last_exc: Exception = RuntimeError("Gemini unreachable")
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(GEMINI_REST_URL, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    raise last_exc

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
    # 엔젤 브리지 엔진 주입
    init_angel_bridge(engine, redis_pub)
    init_angel_export(engine, redis_pub)
    init_angel_rpa(engine, redis_pub)
    logger.info("[STARTUP] 엔젤 브리지 + Export + RPA 초기화 완료.")

    # care_record_outbox 불사조 폴링 워커 기동 (60초 주기 재시도)
    outbox_poll_task = asyncio.create_task(_care_record_outbox_poll_worker())
    logger.info("[STARTUP] care_record_outbox 불사조 폴링 워커 기동.")

    yield

    outbox_poll_task.cancel()
    try:
        await outbox_poll_task
    except asyncio.CancelledError:
        logger.info("[SHUTDOWN] care_record_outbox 폴링 워커 정상 종료.")

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
    allow_headers=["Content-Type", "Authorization", "Cache-Control", "Idempotency-Key", "X-Firebase-AppCheck"],
)
app.middleware("http")(app_check_middleware)

# ── 엔젤 브리지 라우터 마운트 (기생형 Bounded Context) ──────────
app.include_router(angel_router)
app.include_router(angel_export_router)
app.include_router(angel_rpa_router)

# ══════════════════════════════════════════════════════════════════
# [AI 연동 긴급 실전 테스트] POST /api/v8/test-ai-pipeline
# ══════════════════════════════════════════════════════════════════

_MOCK_STT_TRANSCRIPT = (
    "박영옥 수급자, 오전 식사 80% 완료. 투약(혈압약) 모두 복용. "
    "배설 1회(양호). 체위 변경 2회 실시. 특이사항 없음. 야간 인수인계 합니다."
)


@app.post("/api/v8/test-ai-pipeline", tags=["AI 테스트"])
async def test_ai_pipeline(
    audio_file: UploadFile = File(...),
    x_mock_stt: Optional[str] = Header(None),
):
    """
    Whisper STT → Gemini 2.5 Flash 기본 정제 파이프라인.
    X-Mock-Stt: true 헤더 시 Whisper를 우회하고 캐드 트랜스크립트 사용 (시연 모드).
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY 누락")

    mock_stt = (x_mock_stt or "").strip().lower() == "true"

    # ── 1. Whisper STT ────────────────────────────────────────────
    if mock_stt:
        text_result = _MOCK_STT_TRANSCRIPT
        logger.info(
            f"[AI-PIPELINE] ✅ 위스퍼 STT 변환 성공 (Mock 모드) "
            f"— transcript: {text_result[:80]}..."
        )
    else:
        if not OPENAI_API_KEY:
            raise HTTPException(500, "OPENAI_API_KEY 누락")
        tmp_path = Path(tempfile.gettempdir()) / f"test_{uuid4()}.wav"
        try:
            content = await audio_file.read()
            tmp_path.write_bytes(content)
            logger.info("[AI-PIPELINE] OpenAI Whisper 변환 시작...")
            client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
            with open(tmp_path, "rb") as f:
                transcript = await client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="ko"
                )
            text_result = transcript.text
            logger.info(
                f"[AI-PIPELINE] ✅ 위스퍼 STT 변환 성공 "
                f"— transcript: {text_result[:80]}..."
            )
        except Exception as e:
            logger.error(f"[AI-PIPELINE] Whisper 오류: {e}")
            raise HTTPException(500, f"Whisper STT 실패: {e}")
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    # ── 2. Gemini 구조화 JSON 정제 ───────────────────────────────────────
    gemini_structured: dict = {}
    try:
        logger.info("[AI-PIPELINE] Gemini 2.5 Flash 구조화 정제 시작...")
        prompt = (
            "당신은 요양보호사의 음성 인수인계를 분석하는 요양 전문 AI입니다.\n"
            "아래 발화 내용을 분석하여 반드시 다음 JSON 형식 그대로만 응답하십시오.\n"
            "다른 설명, 마크다운 코드블록, 추가 텍스트 없이 JSON만 출력하십시오.\n\n"
            "{\n"
            '  "full_refined": "발화 전체를 문어체로 완전히 정제한 인수인계 보고문 (150자 이내)",\n'
            '  "summary": "핵심 케어 행위 요약 — 식사/투약/배설/체위변경/특이사항 중심 (80자 이내)",\n'
            '  "urgent_note": "즉시 조치 필요 사항 또는 이상 징후. 없으면 빈 문자열"\n'
            "}\n\n"
            f"[발화 내용]\n{text_result}"
        )
        raw = await _call_gemini(prompt)
        # 마크다운 코드블록 제거 방어 (```json ... ``` 또는 ``` ... ```)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        gemini_structured = json.loads(raw)
        logger.info(
            f"[AI-PIPELINE] ✅ 제미나이 정제 성공 "
            f"— summary: {gemini_structured.get('summary','')[:60]}..."
        )
    except Exception as e:
        logger.error(f"[AI-PIPELINE] Gemini 오류 또는 JSON 파싱 실패: {e}")
        gemini_structured = {
            "full_refined": text_result,
            "summary":      text_result[:80],
            "urgent_note":  "",
        }

    return {
        "success":            True,
        "whisper_transcript": text_result,
        "gemini_analysis":    gemini_structured.get("full_refined", text_result),
        "gemini_summary":     gemini_structured.get("summary", ""),
        "gemini_urgent_note": gemini_structured.get("urgent_note", ""),
        "gemini_structured":  gemini_structured,
    }


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

    # ── C. 파일 수신 (최대 50MB) + 임시 저장 ────────────────
    audio_bytes = await audio_file.read()
    if len(audio_bytes) > 50 * 1024 * 1024:
        raise HTTPException(413, "파일 초과 (최대 50MB)")
    audio_size_kb = len(audio_bytes) // 1024

    # 워커가 실제 오디오를 읽을 수 있도록 tmp에 저장
    tmp_dir = Path(tempfile.gettempdir()) / "voice_guard_audio"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_audio_path = tmp_dir / f"{ledger_id}.wav"
    tmp_audio_path.write_bytes(audio_bytes)

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
        "tmp_audio_path": str(tmp_audio_path),
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [D-1] evidence_ledger: 불변 원장 INSERT
            # CHAR(64) CHECK 제약 대응: 'pending' 7자 불가 → SHA-256 64자 플레이스홀더 사용
            pending_audio = hashlib.sha256(f"audio_pending_{ledger_id}".encode()).hexdigest()
            pending_tx    = hashlib.sha256(f"tx_pending_{ledger_id}".encode()).hexdigest()
            pending_chain = hashlib.sha256(f"chain_pending_{ledger_id}".encode()).hexdigest()

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
                    :audio_sha256, :transcript_sha256, :chain_hash,
                    '', 'ko', :case_type, false,
                    :beneficiary_id, :shift_id, :idempotency_key,
                    :care_type, :gps_lat, :gps_lon,
                    :audio_size_kb, 'voice-guard-korea', :worm_object_key, :recorded_at
                )
            """), {
                "id": ledger_id, "session_id": str(uuid4()),
                "recorded_at": server_ts, "ingested_at": server_ts,
                "device_id": device_id or "unknown",
                "facility_id": facility_id,
                "audio_sha256": pending_audio,
                "transcript_sha256": pending_tx,
                "chain_hash": pending_chain,
                "case_type": care_type,
                "beneficiary_id": beneficiary_id,
                "shift_id": shift_id,
                "idempotency_key": idem_key,
                "care_type": care_type,
                "gps_lat": gps_lat, "gps_lon": gps_lon,
                "audio_size_kb": audio_size_kb,
                "worm_object_key": f"ingest/{ledger_id[:8]}.wav",
            })

            # [D-2] outbox_events: 비동기 처리 큐 INSERT (동일 트랜잭션)
            conn.execute(text("""
                INSERT INTO outbox_events (
                    id, ledger_id, status, attempts,
                    payload, created_at
                ) VALUES (
                    :id, :ledger_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
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
        # [E-1] Stream: 워커 비동기 처리 알림 (XADD 실패해도 PUBLISH는 독립 실행)
        try:
            await redis_pub.xadd(
                REDIS_STREAM,
                {"ledger_id": ledger_id, "server_ts": server_ts.isoformat()},
                maxlen=10000,
            )
            logger.info("[INGEST] Redis XADD 완료.")
        except Exception as e:
            logger.warning(f"[INGEST] Redis XADD 실패 (outbox 폴링 백업): {e}")

        # [E-2] Pub/Sub: SSE 대시보드 즉시 갱신 (XADD 실패와 무관하게 항상 실행)
        try:
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({"event": "new_evidence", "data": sse_event_data}, ensure_ascii=False),
            )
            logger.info("[INGEST] Redis PUBLISH → SSE 완료.")
        except Exception as e:
            logger.warning(f"[INGEST] Redis PUBLISH 실패: {e}")

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
            idle_ticks = 0                           # keep-alive 간격 카운터
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
                    idle_ticks = 0                   # 메시지 수신 시 카운터 리셋
                    try:
                        payload = json.loads(message["data"])
                        event   = payload.get("event", "update")
                        data    = payload.get("data", {})
                        yield sse_format(event, data)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"[SSE] 메시지 파싱 실패: {e}")
                else:
                    idle_ticks += 1
                    if idle_ticks >= 30:             # 30 × 1초 = 30초 간격
                        yield ": keep-alive\n\n"
                        idle_ticks = 0
                    # asyncio.sleep 불필요 — get_message(timeout=1.0)이 이미 대기 수행

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
    """Alert View: N분 이내 미처리 건 조회 — v_evidence_sealed 뷰 사용"""
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
                FROM v_evidence_sealed e
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
    """
    Audit-Ready View: 수급자별 해시/WORM/타임스탬프 원장.
    v_evidence_sealed: 봉인된 값 우선 노출 + is_sealed/is_flagged 자동 파생.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    try:
        # ── [TD-06 패턴] WHERE 1=1 안전 동적 빌더 ──
        filters = ["1=1"]
        params: dict = {"lim": limit}
        if facility_id:
            filters.append("e.facility_id = :fid")
            params["fid"] = facility_id
        where_sql = "WHERE " + " AND ".join(filters)

        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT e.id, e.facility_id, e.beneficiary_id,
                       e.shift_id, e.care_type,
                       e.recorded_at, e.ingested_at,
                       e.audio_sha256, e.chain_hash,
                       e.worm_bucket, e.worm_object_key, e.worm_retain_until,
                       (e.transcript_text != '') AS has_audio,
                       e.is_sealed,
                       e.is_flagged,
                       o.status AS outbox_status
                FROM v_evidence_sealed e
                LEFT JOIN outbox_events o ON o.ledger_id = e.id
                {where_sql}
                ORDER BY e.recorded_at DESC
                LIMIT :lim
            """), params).fetchall()
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


# ══════════════════════════════════════════════════════════════════
# [UPGRADE-03] /internal/dlq-recovery — Cloud Scheduler 전용 DLQ 재처리
# ══════════════════════════════════════════════════════════════════

_INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


@app.post("/internal/dlq-recovery", tags=["내부"], include_in_schema=False)
async def trigger_dlq_recovery(request: Request):
    """
    Cloud Scheduler 전용 DLQ 재처리 트리거.
    Swagger UI 미노출 (include_in_schema=False).
    Authorization: Bearer {INTERNAL_API_SECRET} 헤더 검증.
    """
    # INTERNAL_API_SECRET 설정된 경우 Bearer 토큰 검증 (설정 안 된 경우 로컬 개발로 허용)
    if _INTERNAL_API_SECRET:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or \
                auth_header[len("Bearer "):].strip() != _INTERNAL_API_SECRET:
            raise HTTPException(status_code=401, detail="내부 API 인증 실패")

    from dlq_recovery_worker import run_recovery
    import asyncio as _asyncio
    _asyncio.create_task(run_recovery())
    return {"accepted": True, "message": "DLQ 재처리 시작"}


class ResolutionBody(BaseModel):
    cause: str
    memo:  str = ""


@app.patch("/api/v2/evidence/{ledger_id}", tags=["대시보드"])
async def patch_evidence_resolution(ledger_id: str, body: ResolutionBody):
    """
    미기록 건 처리 사유 기록.
    AlertDrawer → 현장 확인 요청 전송 시 호출.

    [TD-03] evidence_ledger UPDATE 폐지 — Append-Only 원칙 복구.
    flagging 이력은 evidence_flag_event 테이블에 INSERT.
    is_flagged 상태는 v_evidence_sealed 뷰에서 EXISTS로 파생.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    if not body.cause.strip():
        raise HTTPException(422, "cause 는 필수값입니다.")
    try:
        with engine.begin() as conn:
            # ledger 존재 검증 (FK 참조 무결성)
            exists = conn.execute(text(
                "SELECT 1 FROM evidence_ledger WHERE id = :lid"
            ), {"lid": ledger_id}).fetchone()
            if not exists:
                raise HTTPException(
                    404, f"ledger_id='{ledger_id}' 를 찾을 수 없습니다."
                )

            # Append-Only INSERT — 이력은 누적됨, 절대 덮어쓰지 않음
            conn.execute(text("""
                INSERT INTO evidence_flag_event (
                    id, ledger_id, resolution_cause, resolution_memo,
                    flagged_by, flagged_at
                ) VALUES (
                    gen_random_uuid(), :lid, :cause, :memo,
                    :by, NOW()
                )
            """), {
                "lid":   ledger_id,
                "cause": body.cause.strip(),
                "memo":  body.memo.strip() if body.memo else None,
                "by":    "admin",
            })
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
    where = "WHERE dc.beneficiary_id = :bid" if beneficiary_id else ""  # nosec B608 — hardcoded SQL fragment, user values bound via :bid param
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

    # ── [TD-06 패턴] WHERE 1=1 안전 동적 빌더 ──
    filters = ["1=1"]
    params: dict = {}
    if facility_id:
        filters.append("e.facility_id = :fid")
        params["fid"] = facility_id
    where_sql = "WHERE " + " AND ".join(filters)

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT e.beneficiary_id,
                       e.care_type,
                       e.is_sealed
                FROM v_evidence_sealed e
                {where_sql}
                ORDER BY e.beneficiary_id, e.ingested_at DESC
            """), params).fetchall()
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


# ══════════════════════════════════════════════════════════════════
# [6] 6대 의무기록 수집 — POST /api/v8/care-record
#
# 데이터 플로우:
#   수신(JSON) → server_ts 봉인
#   → [Atomic Tx: care_record_ledger INSERT + care_record_outbox INSERT]
#   → Redis XADD (care:records 스트림 → 워커 비동기 처리)
#   → 202 반환
#
# 워커가 care_record_outbox를 소비 → call_gemini_care_record()
# → notion_sync_outbox에 care_record_json 키로 등록
# → notion_sync 워커가 '일일 케어 기록 DB'에 적재
#
# 환경변수:
#   NOTION_CARE_RECORD_DB_ID — 노션 일일 케어 기록 DB ID (신규 필수)
# ══════════════════════════════════════════════════════════════════

class CareRecordBody(BaseModel):
    facility_id:    str
    beneficiary_id: str
    caregiver_id:   str
    raw_voice_text: str              # 현장 발화 원문 텍스트
    recorded_at:    Optional[str] = None  # ISO-8601, 없으면 server_ts 사용


@app.post("/api/v8/care-record", status_code=202, tags=["6대 의무기록"])
async def ingest_care_record(body: CareRecordBody):
    """
    현장 발화 기반 6대 의무기록 수집.

    [불변 원칙]
      - care_record_ledger + care_record_outbox 단일 트랜잭션 원자 적재
      - Gemini 처리 및 Notion 적재는 트랜잭션 외부의 워커가 담당
      - recorded_at 미제공 시 server_ts(UTC) 강제 적용
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    if not body.raw_voice_text.strip():
        raise HTTPException(422, "raw_voice_text 는 필수값입니다.")

    server_ts = datetime.now(timezone.utc)
    record_id = str(uuid4())

    # 클라이언트 제공 recorded_at 사용, 없으면 server_ts
    recorded_at = body.recorded_at or server_ts.isoformat()

    outbox_payload = json.dumps({
        "record_id":      record_id,
        "facility_id":    body.facility_id,
        "beneficiary_id": body.beneficiary_id,
        "caregiver_id":   body.caregiver_id,
        "raw_voice_text": body.raw_voice_text,
        "recorded_at":    recorded_at,
        "server_ts":      server_ts.isoformat(),
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [A] care_record_ledger: 불변 원장 INSERT (append-only)
            conn.execute(text("""
                INSERT INTO care_record_ledger (
                    id, facility_id, beneficiary_id, caregiver_id,
                    raw_voice_text, server_ts, recorded_at
                ) VALUES (
                    :id, :facility_id, :beneficiary_id, :caregiver_id,
                    :raw_voice_text, :server_ts, :recorded_at
                )
            """), {
                "id":             record_id,
                "facility_id":    body.facility_id,
                "beneficiary_id": body.beneficiary_id,
                "caregiver_id":   body.caregiver_id,
                "raw_voice_text": body.raw_voice_text,
                "server_ts":      server_ts,
                "recorded_at":    recorded_at,
            })

            # [B] care_record_outbox: 비동기 처리 큐 INSERT (동일 트랜잭션)
            conn.execute(text("""
                INSERT INTO care_record_outbox (
                    id, record_id, status, attempts, payload, created_at
                ) VALUES (
                    :id, :record_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":        str(uuid4()),
                "record_id": record_id,
                "payload":   outbox_payload,
                "created_at": server_ts,
            })

        # ← COMMIT 완료. 케어 기록 봉인 완료.
        logger.info(
            f"[CARE-RECORD] ✅ COMMIT: record={record_id} "
            f"facility={body.facility_id} beneficiary={body.beneficiary_id}"
        )

    except Exception as e:
        logger.error(f"[CARE-RECORD] DB 트랜잭션 실패: {e}")
        raise HTTPException(500, f"저장 실패: {e}")

    # ── Redis XADD: care:records 스트림 → 워커 비동기 처리 알림 ──
    if redis_pub:
        try:
            await redis_pub.xadd(
                REDIS_CARE_STREAM,
                {"record_id": record_id, "server_ts": server_ts.isoformat()},
                maxlen=10000,
            )
            logger.info("[CARE-RECORD] Redis XADD → care:records 완료.")
        except Exception as e:
            logger.warning(f"[CARE-RECORD] Redis XADD 실패 (outbox 폴링 백업): {e}")

    return {
        "accepted":   True,
        "record_id":  record_id,
        "server_ts":  server_ts.isoformat(),
        "message":    "케어 기록 봉인 완료. Gemini 정제·Notion 적재는 비동기 처리 중.",
    }


# ══════════════════════════════════════════════════════════════════
# [Phase 7-v7] 카카오톡 서브박스 라우팅 — 사령관 특별 지시 룰 v1
#   원칙 #1: emergency = 원장 + 관리자 fan-out
#   원칙 #2: shiftCode 100% AUTO (서버 단일 진실원)
#   원칙 #3: TTL 골든타임은 FE 측에서 데드라인 강제 (20s / 60s)
#   원칙 #4: 템플릿 코드는 환경변수 더미 — 인프라팀 추후 처리
# ══════════════════════════════════════════════════════════════════

class V7EmergencyBody(BaseModel):
    transcript:  str
    severity:    Optional[str] = "critical"
    occurredAt:  Optional[str] = None
    deviceId:    Optional[str] = None
    ledgerRefId: Optional[str] = None


class V7ShiftGroupBody(BaseModel):
    transcript:    str
    shiftCode:     str = "AUTO"
    occurredAt:    Optional[str] = None
    deviceId:      Optional[str] = None
    handoverState: Optional[str] = "share"


async def _v7_dedupe_or_409(idempotency_key: str) -> None:
    """
    Redis SETNX 기반 24h 멱등 가드.
    동일 Idempotency-Key 재호출 시 즉시 409 → 카카오 중복 발송 차단.
    """
    if not idempotency_key:
        raise HTTPException(400, "MISSING_IDEMPOTENCY_KEY")
    if redis_pub is None:
        # Redis 미가동 환경(테스트 등) — 가드 우회하되 경고
        logger.warning("[V7-DEDUPE] Redis 미가동 — 멱등 가드 SKIP")
        return
    ok = await redis_pub.set(
        f"v7:idem:{idempotency_key}",
        "1",
        ex=86400,
        nx=True,
    )
    if not ok:
        raise HTTPException(409, "DUPLICATE_IDEMPOTENCY")


@app.post("/api/v7/notify/emergency", status_code=202, tags=["v7 카카오 라우팅"])
async def v7_notify_emergency(
    body: V7EmergencyBody,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """
    🚨 긴급 직통 알림톡 — 원장 + 관리자 fan-out (사령관 원칙 #1).
    care_record_ledger 등 원장 테이블에 UPDATE/DELETE 발생 0건 보장.
    """
    if not body.transcript.strip():
        raise HTTPException(400, "INVALID_TRANSCRIPT")

    await _v7_dedupe_or_409(idempotency_key)

    targets = [p for p in (EMERGENCY_RECIPIENTS or [DEFAULT_FACILITY_PHONE]) if p]
    if not targets:
        logger.error("[V7-EMERGENCY] 수신자 명단 비어있음 — 환경변수 EMERGENCY_RECIPIENTS 미설정")
        raise HTTPException(503, "NOTIFY_NO_TARGETS")

    variables = {
        "#{발화내용}": body.transcript[:800],
        "#{발생시각}": body.occurredAt or datetime.now(timezone.utc).isoformat(),
        "#{심각도}":   body.severity or "critical",
    }

    sent, failed = fanout_alimtalk(
        engine=engine,
        phones=targets,
        template_code=ALIMTALK_TPL_EMERGENCY,
        variables=variables,
        trigger_type="V7-EMERGENCY",
        idempotency_key=idempotency_key,     # ← 추가: Header에서 수신한 값
    )

    if sent == 0:
        # 전원 실패 → 카카오 게이트웨이 장애로 간주, 503 → FE는 데드라인 내 재시도
        raise HTTPException(503, "NOTIFY_PROVIDER_DOWN")

    return {
        "idempotencyKey": idempotency_key,
        "deliveryId":     str(uuid4()),
        "channel":        "alimtalk",
        "templateCode":   ALIMTALK_TPL_EMERGENCY,
        "targetCount":    sent,
        "failedCount":    failed,
        "queuedAt":       datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/v7/notify/shift-group", status_code=202, tags=["v7 카카오 라우팅"])
async def v7_notify_shift_group(
    body: V7ShiftGroupBody,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """
    👥 교대조 단체 알림톡 — shiftCode 100% AUTO (사령관 원칙 #2).
    FE가 'AUTO' 외 값을 보내도 서버는 무시하고 시계 기반 산정.
    """
    if not body.transcript.strip():
        raise HTTPException(400, "INVALID_TRANSCRIPT")
    if body.shiftCode != "AUTO":
        # 우회 시도 차단 — 거부하되 합법 경로(AUTO 산정)로 강제 진행
        logger.warning(
            f"[V7-SHIFT] FE shiftCode 우회 시도 거부: '{body.shiftCode}' → AUTO 강제"
        )

    await _v7_dedupe_or_409(idempotency_key)

    resolved = resolve_shift_code_auto()
    targets  = resolve_shift_recipients(resolved)
    if not targets:
        logger.error(f"[V7-SHIFT] 교대조 '{resolved}' 수신자 명단 비어있음")
        raise HTTPException(400, "EMPTY_SHIFT_GROUP")

    variables = {
        "#{발화내용}": body.transcript[:1200],
        "#{인계시각}": body.occurredAt or datetime.now(timezone.utc).isoformat(),
        "#{교대조}":   resolved,
    }

    sent, failed = fanout_alimtalk(
        engine=engine,
        phones=targets,
        template_code=ALIMTALK_TPL_SHIFT_GROUP,
        variables=variables,
        trigger_type="V7-SHIFT",
        idempotency_key=idempotency_key,     # ← 추가
    )

    if sent == 0:
        raise HTTPException(503, "NOTIFY_PROVIDER_DOWN")

    return {
        "idempotencyKey":       idempotency_key,
        "deliveryId":           str(uuid4()),
        "channel":              "alimtalk",
        "templateCode":         ALIMTALK_TPL_SHIFT_GROUP,
        "resolvedShiftCode":    resolved,
        "targetCount":          sent,
        "failedCount":          failed,
        "handoverTransitioned": body.handoverState == "complete",
        "queuedAt":             datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════
# [v8 Director/Admin Dashboard] Directer_Dashboard 용접 레이어
#
# 불변 원칙 준수:
#   - evidence_ledger / care_record_ledger / care_plan_ledger 스키마 수정 0
#   - Instruct → evidence_flag_event INSERT (Append-Only)
#   - Approve/Resolve/Ack → outbox 큐 상태 전환만 (ledger 불변)
#   - 카카오·노션 외부 API: BypassStub (로그만 기록, 실 호출 없음)
# ══════════════════════════════════════════════════════════════════

def _fmt_elapsed(minutes: float) -> str:
    """경과 분수 → 한국어 시간 문자열"""
    if minutes < 1:
        return "방금 전"
    if minutes < 60:
        return f"{int(minutes)}분 전"
    if minutes < 1440:
        h = int(minutes // 60)
        m = int(minutes % 60)
        return f"{h}시간 {m}분 전" if m else f"{h}시간 전"
    d = int(minutes // 1440)
    return f"{d}일 전"


# ══════════════════════════════════════════════════════════════════
# [DASHBOARD GATEWAY] GET /api/v8/dashboard/worm-records
#
# 사령관 절대 지시 — WORM 관제 대시보드 전용 이력 조회 API
#
# [기능]
#   - evidence_ledger 전체 이력 페이지네이션 조회
#   - case_type, is_flagged, 날짜 범위 필터링
#   - 최신순(DESC) 정렬 기본값
#   - 총 레코드 수 + 페이지 메타 반환 (무한 스크롤/페이지 대응)
#
# [보안]
#   - INSERT-ONLY WORM 원장에 대한 SELECT ONLY (수정 불가)
#   - 클라이언트에 chain_hash / audio_sha256 노출하여 무결성 검증 가능
# ══════════════════════════════════════════════════════════════════

@app.get("/api/v8/dashboard/worm-records", tags=["v8 대시보드"])
def dashboard_worm_records(
    page:       int   = 1,
    page_size:  int   = 20,
    case_type:  str   = None,    # 'work_record' | 'handover' | None(전체)
    is_flagged: bool  = None,    # True | False | None(전체)
    date_from:  str   = None,    # ISO8601 날짜 문자열 (예: 2025-01-01)
    date_to:    str   = None,    # ISO8601 날짜 문자열
):
    """
    WORM 관제 대시보드 전용 — evidence_ledger 이력 페이지네이션 조회 API.

    대시보드 페이지 최초 진입 시 과거 봉인 데이터 전체를 렌더링하기 위해 호출합니다.
    실시간 신규 데이터는 GET /api/sse/stream SSE 스트림으로 수신합니다.

    Args:
        page        : 페이지 번호 (1-indexed, 기본 1)
        page_size   : 페이지당 레코드 수 (기본 20, 최대 100)
        case_type   : 'work_record' 또는 'handover' 필터 (생략 시 전체)
        is_flagged  : 위험 플래그 여부 필터 (생략 시 전체)
        date_from   : 조회 시작 날짜 (recorded_at >= date_from)
        date_to     : 조회 종료 날짜 (recorded_at <= date_to 23:59:59)

    Returns:
        {
          "total"     : 전체 레코드 수,
          "page"      : 현재 페이지,
          "page_size" : 페이지 크기,
          "pages"     : 전체 페이지 수,
          "records"   : [ ...각 WORM 봉인 레코드 ... ]
        }
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    if not (1 <= page_size <= 100):
        raise HTTPException(422, "page_size는 1~100 사이여야 합니다.")
    if page < 1:
        raise HTTPException(422, "page는 1 이상이어야 합니다.")

    offset = (page - 1) * page_size

    # ── 동적 WHERE 절 조립 ─────────────────────────────────────
    conditions = ["1=1"]
    params: dict = {"limit": page_size, "offset": offset}

    if case_type:
        conditions.append("case_type = :case_type")
        params["case_type"] = case_type
    if is_flagged is not None:
        conditions.append("is_flagged = :is_flagged")
        params["is_flagged"] = is_flagged
    if date_from:
        conditions.append("recorded_at >= :date_from ::timestamptz")
        params["date_from"] = date_from
    if date_to:
        conditions.append("recorded_at < (:date_to ::date + INTERVAL '1 day')")
        params["date_to"] = date_to

    where = " AND ".join(conditions)

    try:
        with engine.connect() as conn:
            # ── 전체 건수 (페이지 메타 계산용) ────────────────
            total = conn.execute(text(
                f"SELECT COUNT(*) FROM evidence_ledger WHERE {where}"
            ), params).scalar() or 0

            # ── 페이지 데이터 조회 (최신순 DESC) ──────────────
            rows = conn.execute(text(f"""
                SELECT
                    id,
                    recorded_at,
                    ingested_at,
                    device_id,
                    facility_id,
                    case_type,
                    care_type,
                    is_flagged,
                    beneficiary_id,
                    shift_id,
                    transcript_text,
                    transcript_sha256,
                    chain_hash,
                    language_code,
                    worm_object_key,
                    worm_retain_until
                FROM evidence_ledger
                WHERE {where}
                ORDER BY recorded_at DESC, ingested_at DESC
                LIMIT :limit OFFSET :offset
            """), params).mappings().all()

    except Exception as e:
        logger.error(f"[DASHBOARD-WORM] evidence_ledger 조회 실패: {e}")
        raise HTTPException(500, f"WORM 이력 조회 실패: {e}")

    import math
    records = []
    for row in rows:
        records.append({
            "id":                str(row["id"]),
            "recorded_at":       row["recorded_at"].isoformat() if row["recorded_at"] else None,
            "ingested_at":       row["ingested_at"].isoformat() if row["ingested_at"] else None,
            "device_id":         row["device_id"],
            "facility_id":       row["facility_id"],
            "case_type":         row["case_type"],
            "care_type":         row["care_type"],
            "is_flagged":        row["is_flagged"],
            "beneficiary_id":    row["beneficiary_id"],
            "shift_id":          row["shift_id"],
            "transcript_text":   row["transcript_text"],
            "transcript_sha256": row["transcript_sha256"],
            "chain_hash":        row["chain_hash"],
            "language_code":     row["language_code"],
            "worm_object_key":   row["worm_object_key"],
            "worm_retain_until": row["worm_retain_until"].isoformat() if row["worm_retain_until"] else None,
        })

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     math.ceil(total / page_size) if total > 0 else 1,
        "records":   records,
    }


# ── 1. GET /api/v8/director/kpi ────────────────────────────────────

@app.get("/api/v8/director/kpi", tags=["v8 대시보드"])
def v8_director_kpi():
    """원장장 대시보드 4대 핵심 KPI — DB 실시간 집계"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.connect() as conn:
        # 1) 미조치 고위험 건수: 증거 플래그 이력이 있는 원장 건수
        red_flags = conn.execute(text(
            "SELECT COUNT(*) FROM v_evidence_sealed WHERE is_flagged = TRUE"
        )).scalar() or 0

        # 2) 오늘 케어기록 완결률
        today_row = conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'done') AS done_count,
                COUNT(*)                                 AS total_count
            FROM care_record_outbox
            WHERE created_at >= CURRENT_DATE
        """)).mappings().one()
        total = today_row["total_count"] or 0
        done  = today_row["done_count"]  or 0
        completion_rate = round(done * 100.0 / total) if total > 0 else 100

        # 3) SLA 24h 초과 미처리: outbox_events + care_record_outbox 합산
        sla_exceeded = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT id FROM care_record_outbox
                 WHERE status IN ('pending', 'processing')
                   AND created_at < NOW() - INTERVAL '24 hours'
                UNION ALL
                SELECT o.id FROM outbox_events o
                 WHERE o.status NOT IN ('done', 'sent')
                   AND o.created_at < NOW() - INTERVAL '24 hours'
            ) t
        """)).scalar() or 0

        # 4) 인수인계 누락: DLQ 처리 실패 건수
        missing_ack = conn.execute(text(
            "SELECT COUNT(*) FROM care_record_outbox WHERE status = 'dlq'"
        )).scalar() or 0

    return {
        "redFlags":       int(red_flags),
        "completionRate": int(completion_rate),
        "slaExceeded":    int(sla_exceeded),
        "missingAck":     int(missing_ack),
    }


# ── 2. GET /api/v8/director/decision-queue ────────────────────────

@app.get("/api/v8/director/decision-queue", tags=["v8 대시보드"])
def v8_director_decision_queue():
    """우선 조치 리스트: 플래그된 증거 원장 + DLQ 지연 통합"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                e.id::text                                                  AS id,
                e.facility_id                                               AS facility_name,
                COALESCE(NULLIF(e.shift_id, ''), '담당자 미지정')          AS admin_name,
                CASE WHEN e.is_flagged THEN '케어 누락 위험'
                     ELSE '증거 봉인 지연'
                END                                                         AS problem_type,
                GREATEST(
                    EXTRACT(EPOCH FROM (NOW() - e.ingested_at)) / 60.0, 0
                )                                                           AS minutes_elapsed,
                CASE WHEN e.is_sealed                THEN 'ready'
                     WHEN COALESCE(e.audio_sha256,'') != '' THEN 'partial'
                     ELSE 'missing'
                END                                                         AS evidence_status,
                e.is_flagged                                                AS flagged,
                LEFT(e.chain_hash, 12)                                      AS worm_hash_short
            FROM v_evidence_sealed e
            WHERE e.is_flagged = TRUE
               OR EXISTS (
                   SELECT 1 FROM outbox_events o
                    WHERE o.ledger_id = e.id AND o.status = 'dlq'
               )
            ORDER BY e.ingested_at ASC
            LIMIT 100
        """)).mappings().all()

        # 시설별 영향 수급자 수 일괄 조회
        facility_ids = list({r["facility_name"] for r in rows})
        affected_map: dict = {}
        if facility_ids:
            placeholders = ", ".join(f":f{i}" for i in range(len(facility_ids)))
            params = {f"f{i}": fid for i, fid in enumerate(facility_ids)}
            aff_rows = conn.execute(text(f"""
                SELECT facility_id, COUNT(DISTINCT beneficiary_id) AS cnt
                FROM evidence_ledger
                WHERE facility_id IN ({placeholders})
                GROUP BY facility_id
            """), params).mappings().all()
            affected_map = {r["facility_id"]: int(r["cnt"]) for r in aff_rows}

    result = []
    for r in rows:
        mins     = float(r["minutes_elapsed"] or 0)
        flagged  = bool(r["flagged"])
        affected = max(1, affected_map.get(r["facility_name"], 1))
        days     = max(1, int(mins // 1440))

        # 위험 등급: 플래그 + 24h 초과 → severe, 플래그 → high, 그 외 → medium
        if flagged and mins > 1440:
            risk = "severe"
        elif flagged:
            risk = "high"
        else:
            risk = "medium"

        # 예상 환수액 추정: 수급자 × 15만원 × 경과일 (실제 청구 단가 대용)
        clawback = affected * 150_000 * days

        result.append({
            "id":                 r["id"],
            "facilityName":       r["facility_name"],
            "adminName":          r["admin_name"],
            "problemType":        r["problem_type"],
            "elapsedTime":        _fmt_elapsed(mins),
            "expectedClawback":   clawback,
            "affectedRecipients": affected,
            "evidenceStatus":     r["evidence_status"],
            "riskLevel":          risk,
            "wormHashShort":      r["worm_hash_short"] or "N/A",
        })
    return result


# ── 3. POST /api/v8/director/decision/{id}/instruct ───────────────

class V8InstructBody(BaseModel):
    actionType: str


@app.post(
    "/api/v8/director/decision/{ledger_id}/instruct",
    status_code=202,
    tags=["v8 대시보드"],
)
def v8_director_instruct(ledger_id: str, body: V8InstructBody):
    """
    원장장 긴급 지시 — evidence_flag_event INSERT (Append-Only 원칙 준수).
    카카오 알림톡 BypassStub: 실 API 호출 없이 로그만 기록.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO evidence_flag_event (
                id, ledger_id, resolution_cause, resolution_memo,
                flagged_by, flagged_at
            ) VALUES (
                gen_random_uuid(), :lid::uuid,
                :cause, :memo, 'director', NOW()
            )
        """), {
            "lid":   ledger_id,
            "cause": body.actionType,
            "memo":  f"원장장 긴급 지시: {body.actionType}",
        })
    # ── 카카오 알림톡 BypassStub (작전 지침: 외부 API 보류) ──────
    logger.info(
        f"[BYPASS-KAKAO] director instruct ledger={ledger_id} action={body.actionType}"
    )
    return {"accepted": True, "ledger_id": ledger_id, "action": body.actionType}


# ── 4. GET /api/v8/admin/pending-reviews ─────────────────────────

@app.get("/api/v8/admin/pending-reviews", tags=["v8 대시보드"])
def v8_admin_pending_reviews():
    """기존 시스템 전송 전 1차 검수 목록 — care_record_outbox pending"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                o.id::text                                          AS id,
                c.facility_id || ' — ' || c.beneficiary_id         AS title,
                LEFT(c.raw_voice_text, 300)                         AS details
            FROM care_record_outbox o
            JOIN care_record_ledger c ON c.id = o.record_id
            WHERE o.status = 'pending'
            ORDER BY o.created_at ASC
            LIMIT 50
        """)).mappings().all()
    return [{"id": r["id"], "title": r["title"], "details": r["details"]} for r in rows]


# ── 5. POST /api/v8/admin/pending-reviews/approve-all ─────────────

@app.post(
    "/api/v8/admin/pending-reviews/approve-all",
    status_code=202,
    tags=["v8 대시보드"],
)
def v8_admin_approve_all():
    """
    전체 검수 승인 — care_record_outbox pending→done 상태 전환.
    원장(ledger) 불변 원칙 준수: outbox 큐 상태만 전환.
    노션 연동 BypassStub: 실 API 호출 없이 로그만 기록.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE care_record_outbox
               SET status = 'done', processed_at = NOW()
             WHERE status = 'pending'
        """))
        updated = result.rowcount
    # ── 노션 연동 BypassStub ──────────────────────────────────────
    logger.info(f"[BYPASS-NOTION] approve-all updated={updated} rows")
    return {"accepted": True, "approvedCount": updated}


# ── 6. GET /api/v8/admin/action-queue ────────────────────────────

@app.get("/api/v8/admin/action-queue", tags=["v8 대시보드"])
def v8_admin_action_queue():
    """통합 긴급 지시 대기열 — DLQ 항목 (evidence + care_record 통합)"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.connect() as conn:
        evid_rows = conn.execute(text("""
            SELECT
                o.id::text AS id,
                e.facility_id || ' [증거파일 처리 실패] '
                    || COALESCE(e.case_type, '')                  AS issue,
                CASE WHEN o.attempts >= 5 THEN 'high'
                     WHEN o.attempts >= 3 THEN 'medium'
                     ELSE 'low'
                END                                              AS urgency
            FROM outbox_events o
            JOIN v_evidence_sealed e ON e.id = o.ledger_id
            WHERE o.status = 'dlq'
            ORDER BY o.created_at ASC
            LIMIT 25
        """)).mappings().all()

        care_rows = conn.execute(text("""
            SELECT
                o.id::text AS id,
                c.facility_id || ' [케어기록 처리 실패] '
                    || LEFT(c.raw_voice_text, 40)                 AS issue,
                CASE WHEN o.attempts >= 5 THEN 'high'
                     WHEN o.attempts >= 3 THEN 'medium'
                     ELSE 'low'
                END                                              AS urgency
            FROM care_record_outbox o
            JOIN care_record_ledger c ON c.id = o.record_id
            WHERE o.status = 'dlq'
            ORDER BY o.created_at ASC
            LIMIT 25
        """)).mappings().all()

    return [
        {"id": r["id"], "issue": r["issue"], "urgency": r["urgency"]}
        for r in list(evid_rows) + list(care_rows)
    ]


# ── 7. POST /api/v8/admin/action-queue/{id}/resolve ───────────────

@app.post(
    "/api/v8/admin/action-queue/{item_id}/resolve",
    status_code=202,
    tags=["v8 대시보드"],
)
def v8_admin_resolve_action(item_id: str):
    """
    긴급 지시 해결 완료 — DLQ→done 상태 전환.
    care_record_outbox / outbox_events 양쪽 시도 (어느 쪽 ID인지 무관).
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.begin() as conn:
        r1 = conn.execute(text("""
            UPDATE care_record_outbox
               SET status = 'done', processed_at = NOW()
             WHERE id = :id::uuid AND status = 'dlq'
        """), {"id": item_id})
        r2 = conn.execute(text("""
            UPDATE outbox_events
               SET status = 'done', processed_at = NOW()
             WHERE id = :id::uuid AND status = 'dlq'
        """), {"id": item_id})
        if (r1.rowcount + r2.rowcount) == 0:
            raise HTTPException(404, "해당 ID의 DLQ 항목이 없습니다.")
    return {"accepted": True, "id": item_id}


# ── 8. GET /api/v8/admin/handovers ───────────────────────────────

@app.get("/api/v8/admin/handovers", tags=["v8 대시보드"])
def v8_admin_handovers():
    """이전 교대조 브리핑 확인 목록 — pending 케어기록을 교대조별로 제공"""
    if engine is None:
        raise HTTPException(503, "DB 미연결")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                o.id::text              AS id,
                c.facility_id           AS facility_id,
                c.recorded_at           AS recorded_at,
                LEFT(c.raw_voice_text, 400) AS briefing
            FROM care_record_outbox o
            JOIN care_record_ledger c ON c.id = o.record_id
            WHERE o.status = 'pending'
            ORDER BY o.created_at DESC
            LIMIT 30
        """)).mappings().all()

    result = []
    for r in rows:
        # KST 교대조 계산 (notifier._KST 재활용)
        recorded_at = r["recorded_at"]
        if recorded_at:
            try:
                kst_dt = recorded_at.astimezone(_KST)
                h = kst_dt.hour
                if 6 <= h < 14:
                    shift = "DAY 교대조"
                elif 14 <= h < 22:
                    shift = "EVENING 교대조"
                else:
                    shift = "NIGHT 교대조"
            except Exception:
                shift = "교대조 미확인"
        else:
            shift = "교대조 미확인"

        result.append({
            "id":       r["id"],
            "shift":    shift,
            "briefing": r["briefing"] or "",
        })
    return result


# ── 9. POST /api/v8/admin/handovers/{id}/ack ──────────────────────

@app.post(
    "/api/v8/admin/handovers/{handover_id}/ack",
    status_code=202,
    tags=["v8 대시보드"],
)
async def v8_admin_ack_handover(handover_id: str):
    """
    인수인계 확인(ACK) — care_record_outbox pending→done 상태 전환.
    [Closed-Loop] DB 커밋 후 Notion 파이프라인 fire-and-forget 실행.
    Notion 실패는 로그만 기록, DB 커밋은 이미 완료 상태 유지.
    """
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    # ── DB 업데이트 + 케어 기록 조회 (단일 트랜잭션) ──────────────
    care_row = None
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE care_record_outbox
               SET status = 'done', processed_at = NOW()
             WHERE id = :id::uuid AND status = 'pending'
        """), {"id": handover_id})
        if result.rowcount == 0:
            raise HTTPException(404, "해당 인수인계 항목이 없습니다.")

        # Notion 파이프라인에 넘길 케어 기록 조회
        care_row = conn.execute(text("""
            SELECT c.id::text        AS record_id,
                   c.facility_id,
                   c.beneficiary_id,
                   c.caregiver_id,
                   c.raw_voice_text,
                   c.recorded_at::text AS recorded_at
            FROM care_record_outbox o
            JOIN care_record_ledger  c ON c.id = o.record_id
            WHERE o.id = :id::uuid
        """), {"id": handover_id}).fetchone()

    # ── [Closed-Loop] Notion 파이프라인 fire-and-forget ───────────
    if care_row:
        asyncio.create_task(_fire_notion_pipeline(
            facility_id=care_row.facility_id    or "demo",
            beneficiary_id=care_row.beneficiary_id or "demo",
            caregiver_id=care_row.caregiver_id  or "demo",
            care_record_id=care_row.record_id,
            raw_voice_text=care_row.raw_voice_text or "",
            recorded_at=care_row.recorded_at,
            label="ACK-NOTION",
        ))
        logger.info(f"[ACK] ✅ Notion 파이프라인 예약: handover={handover_id}")
    else:
        logger.warning(f"[ACK] care_record 조회 실패 — Notion 파이프라인 건너뜀: id={handover_id}")

    return {"accepted": True, "id": handover_id}


# ══════════════════════════════════════════════════════════════════
# [헬퍼] ACK / 로그 저장 후 Notion 파이프라인 비동기 실행
# fire-and-forget — DB 커밋 이후 독립 실행, 실패해도 응답 차단 없음
# ══════════════════════════════════════════════════════════════════

async def _fire_notion_pipeline(
    facility_id:    str,
    beneficiary_id: str,
    caregiver_id:   str,
    care_record_id: str,
    raw_voice_text: str,
    recorded_at:    Optional[str],
    label:          str = "NOTION",
) -> None:
    gemini_care_json: dict = {
        "meal":          {"done": False},
        "medication":    {"done": False},
        "excretion":     {"done": False},
        "repositioning": {"done": False},
        "hygiene":       {"done": False},
        "special_notes": {"done": True, "detail": raw_voice_text[:500]},
    }
    try:
        result = await notion_run_pipeline(
            gemini_care_json=gemini_care_json,
            facility_id=facility_id,
            beneficiary_id=beneficiary_id,
            caregiver_id=caregiver_id,
            care_record_id=care_record_id,
            raw_voice_text=raw_voice_text,
            recorded_at=recorded_at,
            redis_url=REDIS_URL,
        )
        logger.info(
            f"[{label}] ✅ Notion 파이프라인 완료: "
            f"hot={result['hot_path']['success']} "
            f"cold_ok={result['cold_path']['success']}/{result['cold_path']['total']} "
            f"care={care_record_id[:8]}…"
        )
    except Exception as exc:
        logger.error(f"[{label}] ❌ Notion 파이프라인 실패 (DB 커밋은 유지): {exc}")


async def _fire_handover_pipeline(
    care_record_id: str,
    full_refined:   str,
    summary:        str,
    urgent_note:    str,
    beneficiary_id: str,
    caregiver_id:   str,
    recorded_at:    Optional[str],
) -> None:
    """
    인수인계 DB 전용 Notion 행 생성 (fire-and-forget).
    create_handover_row() → NOTION_HANDOVER_DB_ID (34cdbdd0...) 직접 적재.
    """
    import httpx as _httpx
    from notion_pipeline import lookup_page_id, RESIDENT_DB_ID, CAREGIVER_DB_ID
    from notion_pipeline import RESIDENT_LOOKUP_PROP, CAREGIVER_LOOKUP_PROP

    try:
        async with _httpx.AsyncClient() as client:
            resident_page_id, caregiver_page_id = await asyncio.gather(
                lookup_page_id(client, RESIDENT_DB_ID,  RESIDENT_LOOKUP_PROP,  beneficiary_id, None),
                lookup_page_id(client, CAREGIVER_DB_ID, CAREGIVER_LOOKUP_PROP, caregiver_id,   None),
            )
            ok, page_id, err = await notion_create_handover_row(
                care_record_id=care_record_id,
                full_refined=full_refined,
                summary=summary,
                urgent_note=urgent_note,
                recorded_at=recorded_at,
                client=client,
                resident_page_id=resident_page_id,
                caregiver_page_id=caregiver_page_id,
            )
        if ok:
            logger.info(
                f"[HANDOVER-NOTION] ✅ 인수인계 DB 적재 완료: "
                f"care={care_record_id[:8]}… page={page_id[:8] if page_id else 'None'}…"
            )
        else:
            logger.error(f"[HANDOVER-NOTION] ❌ 인수인계 DB 적재 실패: {err}")
    except Exception as exc:
        logger.error(f"[HANDOVER-NOTION] ❌ 파이프라인 예외: {exc}")


# ══════════════════════════════════════════════════════════════════
# care_record_outbox 불사조 폴링 워커
#
# 역할: Redis XADD가 실패하거나 워커가 누락된 pending 행을 60초마다
#       순찰하여 Notion 파이프라인에 재투입. 5회 초과 시 DLQ 격리.
# ══════════════════════════════════════════════════════════════════

_MAX_OUTBOX_ATTEMPTS = 5
_POLL_INTERVAL_SECS  = 60


async def _outbox_process_one(outbox_id: str, payload: dict) -> None:
    """
    care_record_outbox 단건 처리.
    notion_run_pipeline() 직접 호출 → success 플래그로 done/retry 판단.
    _fire_notion_pipeline() 우회: 그 함수는 예외를 삼키므로 워커에서 사용 불가.
    """
    gemini_care_json: dict = {
        "meal":          {"done": False},
        "medication":    {"done": False},
        "excretion":     {"done": False},
        "repositioning": {"done": False},
        "hygiene":       {"done": False},
        "special_notes": {"done": True, "detail": payload.get("raw_voice_text", "")[:500]},
    }
    result = await notion_run_pipeline(
        gemini_care_json=gemini_care_json,
        facility_id=payload.get("facility_id",    "unknown"),
        beneficiary_id=payload.get("beneficiary_id", "unknown"),
        caregiver_id=payload.get("caregiver_id",  "unknown"),
        care_record_id=payload.get("record_id",   outbox_id),
        raw_voice_text=payload.get("raw_voice_text", ""),
        recorded_at=payload.get("recorded_at"),
        redis_url=REDIS_URL,
    )
    hot_ok  = result.get("hot_path",  {}).get("success", False)
    cold_ok = result.get("cold_path", {}).get("success", False)
    if not (hot_ok or cold_ok):
        raise RuntimeError(
            f"Notion 파이프라인 실패 — hot={hot_ok} cold={cold_ok} "
            f"err={result.get('hot_path',{}).get('error') or result.get('cold_path',{}).get('error','unknown')}"
        )


async def _care_record_outbox_poll_worker() -> None:
    """
    care_record_outbox 불사조 폴링 워커.
    기동 즉시 1회 순찰 후 60초 주기로 반복.
    attempts >= 5 → DLQ 격리 (운영자가 /api/v8/admin/dlq 에서 확인).
    """
    logger.info("[OUTBOX-WORKER] 불사조 폴링 워커 기동 완료.")

    async def _poll_once() -> None:
        if engine is None:
            return

        # ── 1. pending 행 최대 10건 조회 ──────────────────────────
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT o.id::text AS outbox_id,
                           o.attempts,
                           o.payload
                    FROM care_record_outbox o
                    WHERE o.status = 'pending'
                      AND o.attempts < :max_att
                    ORDER BY o.created_at ASC
                    LIMIT 10
                """), {"max_att": _MAX_OUTBOX_ATTEMPTS}).mappings().fetchall()
        except Exception as exc:
            logger.error(f"[OUTBOX-WORKER] DB 조회 실패: {exc}")
            return

        if not rows:
            logger.debug("[OUTBOX-WORKER] pending 항목 없음 — 다음 순찰 대기.")
            return

        logger.info(f"[OUTBOX-WORKER] pending {len(rows)}건 Notion 재시도 시작.")

        # ── 2. 각 행 재처리 ───────────────────────────────────────
        for row in rows:
            outbox_id = row["outbox_id"]
            attempts  = row["attempts"]
            payload   = (
                row["payload"]
                if isinstance(row["payload"], dict)
                else json.loads(row["payload"])
            )

            try:
                await _outbox_process_one(outbox_id, payload)
                with engine.begin() as conn:
                    # CAST 사용 — ':id::uuid' 구문은 SQLAlchemy 파라미터 파서 충돌
                    conn.execute(text("""
                        UPDATE care_record_outbox
                           SET status = 'done', processed_at = NOW()
                         WHERE id = CAST(:id AS uuid)
                    """), {"id": outbox_id})
                logger.info(
                    f"[OUTBOX-WORKER] ✅ 처리 완료: outbox={outbox_id[:8]}…"
                )

            except Exception as exc:
                new_attempts = attempts + 1
                new_status   = "dlq" if new_attempts >= _MAX_OUTBOX_ATTEMPTS else "pending"
                try:
                    with engine.begin() as conn:
                        # CAST 사용 — ':id::uuid' 구문은 SQLAlchemy 파라미터 파서 충돌
                        conn.execute(text("""
                            UPDATE care_record_outbox
                               SET attempts = :att, status = :st
                             WHERE id = CAST(:id AS uuid)
                        """), {"att": new_attempts, "st": new_status, "id": outbox_id})
                except Exception as db_exc:
                    logger.error(
                        f"[OUTBOX-WORKER] attempts 업데이트 실패: {db_exc}"
                    )
                logger.warning(
                    f"[OUTBOX-WORKER] ⚠️ 실패(시도 {new_attempts}/{_MAX_OUTBOX_ATTEMPTS}): "
                    f"outbox={outbox_id[:8]}… err={exc}"
                )

    # 기동 즉시 1회 순찰
    await _poll_once()

    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECS)
        await _poll_once()


# ══════════════════════════════════════════════════════════════════
# [FRONT-BRIDGE v2] POST /api/v8/work-record
#
# 사령관 절대 지시 — '업무 기록' 뱃지 전용 듀얼 페이로드 분기 라우터
#
# [데이터 플로우]
#   FE 단 1회 POST (/api/v8/work-record)
#       ↓
#   ┌─────────────────────────────────────────────────────────────┐
#   │  Fork A: WORM 관제 대시보드 (evidence_ledger + outbox)       │
#   │    - SHA-256 체인 해시 봉인 (수정/삭제 원천 차단)            │
#   │    - 서버 타임스탬프 강제 적용 (클라이언트 조작 불가)         │
#   │    - Append-Only INSERT ONLY 불변 원장                        │
#   │    - SSE Pub/Sub → 대시보드 실시간 브로드캐스트               │
#   └─────────────────────────────────────────────────────────────┘
#       ↓ (COMMIT 완료 후 비동기 병렬 실행)
#   ┌─────────────────────────────────────────────────────────────┐
#   │  Fork B: Notion 워크스페이스 (수정 가능 실무용 텍스트 블록)  │
#   │    - 일반 마크다운/텍스트 블록 형태 (Editable)               │
#   │    - care_record_ledger → 노션 업무기록 DB 자동 적재         │
#   │    - fire-and-forget (응답 차단 없음)                        │
#   └─────────────────────────────────────────────────────────────┘
#       ↓
#   202 즉시 반환 (WORM COMMIT 기준)
#
# ⚠ WORM과 Notion의 데이터 형식 분리는 철저히 Backend 서비스 레이어에서 처리
# ══════════════════════════════════════════════════════════════════

class WorkRecordBody(BaseModel):
    text:      str
    timestamp: Optional[str] = None
    source:    str = "work_record_badge"  # 호출 출처 식별자 (업무 기록 뱃지 고정)


@app.post("/api/v8/work-record", status_code=202, tags=["업무 기록 듀얼 라우팅"])
async def post_work_record(body: WorkRecordBody):
    """
    [사령관 절대 명령] '업무 기록' 뱃지 전용 듀얼 페이로드 분기 엔드포인트.

    FE는 단 1번 호출 — 내부에서 Fork A(WORM) + Fork B(Notion)로 자동 분기.

    Fork A [WORM 관제 대시보드]:
      - evidence_ledger INSERT ONLY (불변 원장)
      - SHA-256 체인 해시 + 서버 타임스탬프 봉인
      - outbox_events 동일 원자 트랜잭션 (Atomic Split)
      - SSE 실시간 대시보드 브로드캐스트

    Fork B [Notion 워크스페이스]:
      - care_record_ledger + care_record_outbox INSERT
      - 노션 파이프라인 fire-and-forget (수정 가능 텍스트 블록)
    """
    if not body.text.strip():
        raise HTTPException(422, "text 는 필수값입니다.")
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    server_ts   = datetime.now(timezone.utc)
    ledger_id   = str(uuid4())
    record_id   = str(uuid4())
    recorded_at = body.timestamp or server_ts.isoformat()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # FORK A — WORM 관제 대시보드 (evidence_ledger + outbox_events)
    # 수정·삭제가 원천 차단된 Append-Only 불변 원장 봉인
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SHA-256 보안 해시 생성 (텍스트 무결성 봉인)
    text_bytes   = body.text.encode("utf-8")
    text_sha256  = hashlib.sha256(text_bytes).hexdigest()
    # 체인 해시: 텍스트 + 타임스탬프 + ledger_id 혼합 (위변조 감지)
    chain_source = f"{text_sha256}::{server_ts.isoformat()}::{ledger_id}"
    chain_hash   = hashlib.sha256(chain_source.encode()).hexdigest()
    # CHAR(64) CHECK 제약 대응 — 오디오 플레이스홀더
    pending_audio = hashlib.sha256(f"audio_pending_{ledger_id}".encode()).hexdigest()

    worm_payload = json.dumps({
        "ledger_id":       ledger_id,
        "facility_id":     "work_record",
        "beneficiary_id":  "badge_user",
        "shift_id":        f"badge_{ledger_id[:8]}",
        "source":          body.source,
        "text_sha256":     text_sha256,
        "chain_hash":      chain_hash,
        "server_ts":       server_ts.isoformat(),
        "raw_transcript":  body.text[:500],
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [Fork A-1] evidence_ledger: 불변 WORM 원장 INSERT
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
                    :audio_sha256, :transcript_sha256, :chain_hash,
                    :transcript_text, 'ko', :case_type, false,
                    :beneficiary_id, :shift_id, :idempotency_key,
                    :care_type, NULL, NULL,
                    0, 'voice-guard-korea', :worm_object_key, :recorded_at
                )
            """), {
                "id":               ledger_id,
                "session_id":       str(uuid4()),
                "recorded_at":      server_ts,
                "ingested_at":      server_ts,
                "device_id":        "badge_frontend",
                "facility_id":      "work_record",
                "audio_sha256":     pending_audio,
                "transcript_sha256": text_sha256,
                "chain_hash":       chain_hash,
                "transcript_text":  body.text[:4000],
                "case_type":        "work_record",
                "beneficiary_id":   "badge_user",
                "shift_id":         f"badge_{ledger_id[:8]}",
                "idempotency_key":  chain_hash,   # 체인해시로 중복 방어
                "care_type":        "work_record",
                "worm_object_key":  f"work_record/{ledger_id[:8]}.txt",
            })

            # [Fork A-2] outbox_events: 비동기 처리 큐 (동일 원자 트랜잭션)
            conn.execute(text("""
                INSERT INTO outbox_events (
                    id, ledger_id, status, attempts,
                    payload, created_at
                ) VALUES (
                    :id, :ledger_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":         str(uuid4()),
                "ledger_id":  ledger_id,
                "payload":    worm_payload,
                "created_at": server_ts,
            })

        # ← COMMIT 완료. WORM 봉인 확정. 이후 어떤 수단으로도 수정 불가.
        logger.info(
            f"[WORK-RECORD] ✅ FORK-A WORM COMMIT: ledger={ledger_id} "
            f"sha256={text_sha256[:12]}… chain={chain_hash[:12]}…"
        )

    except IntegrityError as e:
        if "idempotency_key" in str(e).lower():
            # 동일 체인 해시 → 중복 제출로 간주, 409 반환
            logger.warning(f"[WORK-RECORD] 중복 WORM 제출 감지: chain={chain_hash[:16]}…")
            raise HTTPException(409, "이미 동일 내용이 봉인되었습니다. (WORM 중복 방어)")
        raise HTTPException(500, f"WORM DB 오류: {e}")
    except Exception as e:
        logger.error(f"[WORK-RECORD] Fork A WORM 트랜잭션 실패: {e}")
        raise HTTPException(500, f"WORM 봉인 실패: {e}")

    # ── SSE: 관제 대시보드 실시간 브로드캐스트 (COMMIT 후 독립 실행) ──
    if redis_pub:
        try:
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({
                    "event": "work_record_sealed",
                    "data": {
                        "ledger_id":   ledger_id,
                        "source":      body.source,
                        "text_sha256": text_sha256,
                        "chain_hash":  chain_hash,
                        "ingested_at": server_ts.isoformat(),
                        "is_flagged":  False,
                        "sync_status": "pending",
                    },
                }, ensure_ascii=False),
            )
            logger.info("[WORK-RECORD] SSE PUBLISH → 대시보드 브로드캐스트 완료")
        except Exception as e:
            logger.warning(f"[WORK-RECORD] SSE PUBLISH 실패 (WORM 봉인은 유지): {e}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # FORK B — Notion 워크스페이스 (care_record_ledger → 업무기록 DB)
    # 수정 가능한 일반 텍스트 블록 형태로 노션에 실무용 적재
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    notion_payload = json.dumps({
        "record_id":      record_id,
        "facility_id":    "work_record",
        "beneficiary_id": "badge_user",
        "caregiver_id":   "badge_user",
        "raw_voice_text": body.text,
        "recorded_at":    recorded_at,
        "server_ts":      server_ts.isoformat(),
        "worm_ledger_ref": ledger_id,   # WORM 원장과의 참조 연결
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO care_record_ledger (
                    id, facility_id, beneficiary_id, caregiver_id,
                    raw_voice_text, server_ts, recorded_at
                ) VALUES (
                    :id, :facility_id, :beneficiary_id, :caregiver_id,
                    :raw_voice_text, :server_ts, :recorded_at
                )
            """), {
                "id":             record_id,
                "facility_id":    "work_record",
                "beneficiary_id": "badge_user",
                "caregiver_id":   "badge_user",
                "raw_voice_text": body.text,
                "server_ts":      server_ts,
                "recorded_at":    recorded_at,
            })
            conn.execute(text("""
                INSERT INTO care_record_outbox (
                    id, record_id, status, attempts, payload, created_at
                ) VALUES (
                    :id, :record_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":         str(uuid4()),
                "record_id":  record_id,
                "payload":    notion_payload,
                "created_at": server_ts,
            })
        logger.info(
            f"[WORK-RECORD] ✅ FORK-B Notion DB COMMIT: record={record_id}"
        )
    except Exception as e:
        # Fork B 실패는 경고만 — Fork A(WORM)는 이미 봉인 완료
        logger.error(f"[WORK-RECORD] Fork B Notion DB 적재 실패 (WORM 봉인은 유지): {e}")

    # Fork B — Notion 파이프라인 fire-and-forget (care_record_ledger 커밋 후)
    asyncio.create_task(_fire_notion_pipeline(
        facility_id="work_record",
        beneficiary_id="badge_user",
        caregiver_id="badge_user",
        care_record_id=record_id,
        raw_voice_text=body.text,
        recorded_at=recorded_at,
        label="WORK-RECORD-NOTION",
    ))
    logger.info(
        "[WORK-RECORD] 🚀 FORK-B Notion 파이프라인 예약 완료 (fire-and-forget)"
    )

    # ── 202 즉시 반환 (WORM COMMIT 기준) ────────────────────────
    return {
        "accepted":       True,
        "ledger_id":      ledger_id,
        "record_id":      record_id,
        "worm_sealed":    True,
        "chain_hash":     chain_hash,
        "text_sha256":    text_sha256,
        "notion_queued":  True,
        "server_ts":      server_ts.isoformat(),
        "message":        "WORM 봉인 완료 + Notion 업무기록 DB 적재 진행 중.",
    }



# ══════════════════════════════════════════════════════════════════
# [FRONT-BRIDGE v2] POST /api/v8/handover
#
# 사령관 절대 지시 — '인수인계' 버튼 전용 듀얼 페이로드 분기 라우터
#
# [데이터 플로우]
#   FE 단 1회 POST (/api/v8/handover)
#       ↓
#   Fork A: WORM 관제 대시보드 (evidence_ledger + outbox_events)
#     - SHA-256 체인 해시 봉인 (수정/삭제 원천 차단)
#     - 서버 타임스탬프 강제 적용 (클라이언트 조작 불가)
#     - Append-Only INSERT ONLY 불변 원장
#     - SSE Pub/Sub → 대시보드 실시간 브로드캐스트
#   Fork B: Notion 워크스페이스 (수정 가능 실무용 텍스트 블록)
#     - care_record_ledger → 노션 인수인계 DB 자동 적재
#     - fire-and-forget (응답 차단 없음)
#       ↓
#   202 즉시 반환 (WORM COMMIT 기준)
# ══════════════════════════════════════════════════════════════════

class HandoverBody(BaseModel):
    text:           str
    timestamp:      Optional[str] = None
    source:         str = "handover_badge"
    # G-03: 실제 식별자 — 미제공 시 Notion Relation 생략 (WORM 봉인은 영향 없음)
    facility_id:    Optional[str] = None
    beneficiary_id: Optional[str] = None
    caregiver_id:   Optional[str] = None


@app.post("/api/v8/handover", status_code=202, tags=["인수인계 듀얼 라우팅"])
async def post_handover(body: HandoverBody):
    """
    [사령관 절대 명령] '인수인계' 버튼 전용 듀얼 페이로드 분기 엔드포인트.

    FE는 단 1번 호출 — 내부에서 Fork A(WORM) + Fork B(Notion)로 자동 분기.

    Fork A [WORM 관제 대시보드]:
      - evidence_ledger INSERT ONLY (불변 원장)
      - SHA-256 체인 해시 + 서버 타임스탬프 봉인
      - outbox_events 동일 원자 트랜잭션 (Atomic Split)
      - SSE 실시간 대시보드 브로드캐스트

    Fork B [Notion 워크스페이스]:
      - care_record_ledger + care_record_outbox INSERT
      - 노션 파이프라인 fire-and-forget (수정 가능 텍스트 블록)
    """
    if not body.text.strip():
        raise HTTPException(422, "text 는 필수값입니다.")
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    server_ts   = datetime.now(timezone.utc)
    ledger_id   = str(uuid4())
    record_id   = str(uuid4())
    recorded_at = body.timestamp or server_ts.isoformat()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # FORK A — WORM 관제 대시보드 (evidence_ledger + outbox_events)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    text_bytes   = body.text.encode("utf-8")
    text_sha256  = hashlib.sha256(text_bytes).hexdigest()
    chain_source = f"{text_sha256}::{server_ts.isoformat()}::{ledger_id}"
    chain_hash   = hashlib.sha256(chain_source.encode()).hexdigest()
    pending_audio = hashlib.sha256(f"audio_pending_{ledger_id}".encode()).hexdigest()

    worm_payload = json.dumps({
        "ledger_id":       ledger_id,
        "facility_id":     "handover",
        "beneficiary_id":  "resident_user",
        "shift_id":        f"handover_{ledger_id[:8]}",
        "source":          body.source,
        "text_sha256":     text_sha256,
        "chain_hash":      chain_hash,
        "server_ts":       server_ts.isoformat(),
        "raw_transcript":  body.text[:500],
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            # [Fork A-1] evidence_ledger: 불변 WORM 원장 INSERT
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
                    :audio_sha256, :transcript_sha256, :chain_hash,
                    :transcript_text, 'ko', :case_type, false,
                    :beneficiary_id, :shift_id, :idempotency_key,
                    :care_type, NULL, NULL,
                    0, 'voice-guard-korea', :worm_object_key, :recorded_at
                )
            """), {
                "id":               ledger_id,
                "session_id":       str(uuid4()),
                "recorded_at":      server_ts,
                "ingested_at":      server_ts,
                "device_id":        "handover_frontend",
                "facility_id":      "handover",
                "audio_sha256":     pending_audio,
                "transcript_sha256": text_sha256,
                "chain_hash":       chain_hash,
                "transcript_text":  body.text[:4000],
                "case_type":        "handover",
                "beneficiary_id":   "resident_user",
                "shift_id":         f"handover_{ledger_id[:8]}",
                "idempotency_key":  chain_hash,
                "care_type":        "handover",
                "worm_object_key":  f"handover/{ledger_id[:8]}.txt",
            })

            # [Fork A-2] outbox_events: 동일 원자 트랜잭션
            conn.execute(text("""
                INSERT INTO outbox_events (
                    id, ledger_id, status, attempts,
                    payload, created_at
                ) VALUES (
                    :id, :ledger_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":         str(uuid4()),
                "ledger_id":  ledger_id,
                "payload":    worm_payload,
                "created_at": server_ts,
            })

        # ← COMMIT 완료. WORM 봉인 확정.
        logger.info(
            f"[HANDOVER] ✅ FORK-A WORM COMMIT: ledger={ledger_id} "
            f"sha256={text_sha256[:12]}… chain={chain_hash[:12]}…"
        )

    except IntegrityError as e:
        if "idempotency_key" in str(e).lower():
            logger.warning(f"[HANDOVER] 중복 WORM 제출 감지: chain={chain_hash[:16]}…")
            raise HTTPException(409, "이미 동일 내용이 봉인되었습니다. (WORM 중복 방어)")
        raise HTTPException(500, f"WORM DB 오류: {e}")
    except Exception as e:
        logger.error(f"[HANDOVER] Fork A WORM 트랜잭션 실패: {e}")
        raise HTTPException(500, f"WORM 봉인 실패: {e}")

    # ── SSE: 관제 대시보드 실시간 브로드캐스트 ──
    if redis_pub:
        try:
            await redis_pub.publish(
                REDIS_SSE_CHANNEL,
                json.dumps({
                    "event": "handover_sealed",
                    "data": {
                        "ledger_id":   ledger_id,
                        "source":      body.source,
                        "text_sha256": text_sha256,
                        "chain_hash":  chain_hash,
                        "ingested_at": server_ts.isoformat(),
                        "is_flagged":  False,
                        "sync_status": "pending",
                    },
                }, ensure_ascii=False),
            )
            logger.info("[HANDOVER] SSE PUBLISH → 대시보드 브로드캐스트 완료")
        except Exception as e:
            logger.warning(f"[HANDOVER] SSE PUBLISH 실패 (WORM 봉인은 유지): {e}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # FORK B — Gemini 구조화 JSON 생성 → Notion 인수인계 DB 적재
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    fork_b_facility    = (body.facility_id    or "handover").strip() or "handover"
    fork_b_beneficiary = (body.beneficiary_id or "resident_user").strip() or "resident_user"
    fork_b_caregiver   = (body.caregiver_id   or "handover_staff").strip() or "handover_staff"

    # Gemini 구조화 정제: summary / urgent_note / full_refined 분리
    gemini_fields: dict = {"full_refined": body.text, "summary": body.text[:80], "urgent_note": ""}
    try:
        logger.info("[HANDOVER] Gemini 인수인계 구조화 정제 시작...")
        _prompt = (
            "당신은 요양보호사의 인수인계 텍스트를 정리하는 요양 전문 AI입니다.\n"
            "아래 텍스트를 분석하여 반드시 다음 JSON 형식 그대로만 응답하십시오.\n"
            "다른 설명, 마크다운 코드블록, 추가 텍스트 없이 JSON만 출력하십시오.\n\n"
            "{\n"
            '  "full_refined": "전체 인수인계 내용을 문어체로 완전히 정제한 보고문 (200자 이내)",\n'
            '  "summary": "핵심 케어 행위 요약 — 식사/투약/배설/체위변경 중심 (80자 이내)",\n'
            '  "urgent_note": "즉시 조치 필요 이상 징후. 없으면 빈 문자열"\n'
            "}\n\n"
            f"[인수인계 텍스트]\n{body.text}"
        )
        _raw = await _call_gemini(_prompt)
        # 마크다운 코드블록 제거 방어 (```json ... ``` 또는 ``` ... ```)
        _raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", _raw.strip())
        gemini_fields = json.loads(_raw)
        logger.info(
            f"[HANDOVER] ✅ Gemini 구조화 정제 완료 — "
            f"summary={gemini_fields.get('summary','')[:50]}… "
            f"urgent={gemini_fields.get('urgent_note','')[:30] or '없음'}"
        )
    except Exception as _e:
        logger.warning(f"[HANDOVER] Gemini 구조화 실패 — 원문 폴백: {_e}")

    notion_payload = json.dumps({
        "record_id":       record_id,
        "facility_id":     fork_b_facility,
        "beneficiary_id":  fork_b_beneficiary,
        "caregiver_id":    fork_b_caregiver,
        "raw_voice_text":  body.text,
        "full_refined":    gemini_fields.get("full_refined", body.text),
        "summary":         gemini_fields.get("summary", ""),
        "urgent_note":     gemini_fields.get("urgent_note", ""),
        "recorded_at":     recorded_at,
        "server_ts":       server_ts.isoformat(),
        "worm_ledger_ref": ledger_id,
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO care_record_ledger (
                    id, facility_id, beneficiary_id, caregiver_id,
                    raw_voice_text, server_ts, recorded_at
                ) VALUES (
                    :id, :facility_id, :beneficiary_id, :caregiver_id,
                    :raw_voice_text, :server_ts, :recorded_at
                )
            """), {
                "id":             record_id,
                "facility_id":    fork_b_facility,
                "beneficiary_id": fork_b_beneficiary,
                "caregiver_id":   fork_b_caregiver,
                "raw_voice_text": body.text,
                "server_ts":      server_ts,
                "recorded_at":    recorded_at,
            })
            conn.execute(text("""
                INSERT INTO care_record_outbox (
                    id, record_id, status, attempts, payload, created_at
                ) VALUES (
                    :id, :record_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":         str(uuid4()),
                "record_id":  record_id,
                "payload":    notion_payload,
                "created_at": server_ts,
            })
        logger.info(
            f"[HANDOVER] ✅ FORK-B Notion DB COMMIT: record={record_id}"
        )
    except Exception as e:
        logger.error(f"[HANDOVER] Fork B Notion DB 적재 실패 (WORM 봉인은 유지): {e}")

    # Fork B — 인수인계 DB 전용 Notion 행 생성 (fire-and-forget)
    asyncio.create_task(_fire_handover_pipeline(
        care_record_id=record_id,
        full_refined=gemini_fields.get("full_refined", body.text),
        summary=gemini_fields.get("summary", ""),
        urgent_note=gemini_fields.get("urgent_note", ""),
        beneficiary_id=fork_b_beneficiary,
        caregiver_id=fork_b_caregiver,
        recorded_at=recorded_at,
    ))
    logger.info("[HANDOVER] 🚀 FORK-B 인수인계 DB 파이프라인 예약 완료 (fire-and-forget)")

    # ── 202 즉시 반환 (WORM COMMIT 기준) ────────────────────────
    return {
        "accepted":          True,
        "ledger_id":         ledger_id,
        "record_id":         record_id,
        "worm_sealed":       True,
        "chain_hash":        chain_hash,
        "text_sha256":       text_sha256,
        "notion_queued":     True,
        "gemini_summary":    gemini_fields.get("summary", ""),
        "gemini_urgent":     gemini_fields.get("urgent_note", ""),
        "server_ts":         server_ts.isoformat(),
        "message":           "WORM 봉인 완료 + Notion 인수인계 DB 적재 진행 중.",
    }


# ══════════════════════════════════════════════════════════════════
# [FRONT-BRIDGE] POST /api/logs
#
# FRONT END api.ts saveLog(text) 수신 → care_record_ledger 적재
# + Notion 파이프라인 fire-and-forget
# ══════════════════════════════════════════════════════════════════

class LogBody(BaseModel):
    text:      str
    timestamp: Optional[str] = None


@app.post("/api/logs", status_code=202, tags=["프론트 연동"])
async def post_log(body: LogBody):
    """
    FRONT END api.ts saveLog() 수신 엔드포인트.
    care_record_ledger/outbox 단일 트랜잭션 적재 후
    Notion 파이프라인 비동기 실행.
    """
    if not body.text.strip():
        raise HTTPException(422, "text 는 필수값입니다.")
    if engine is None:
        raise HTTPException(503, "DB 미연결")

    server_ts   = datetime.now(timezone.utc)
    record_id   = str(uuid4())
    recorded_at = body.timestamp or server_ts.isoformat()

    outbox_payload = json.dumps({
        "record_id":      record_id,
        "facility_id":    "demo",
        "beneficiary_id": "demo",
        "caregiver_id":   "demo",
        "raw_voice_text": body.text,
        "recorded_at":    recorded_at,
        "server_ts":      server_ts.isoformat(),
    }, ensure_ascii=False)

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO care_record_ledger (
                    id, facility_id, beneficiary_id, caregiver_id,
                    raw_voice_text, server_ts, recorded_at
                ) VALUES (
                    :id, :facility_id, :beneficiary_id, :caregiver_id,
                    :raw_voice_text, :server_ts, :recorded_at
                )
            """), {
                "id":             record_id,
                "facility_id":    "demo",
                "beneficiary_id": "demo",
                "caregiver_id":   "demo",
                "raw_voice_text": body.text,
                "server_ts":      server_ts,
                "recorded_at":    recorded_at,
            })
            conn.execute(text("""
                INSERT INTO care_record_outbox (
                    id, record_id, status, attempts, payload, created_at
                ) VALUES (
                    :id, :record_id, 'pending', 0,
                    CAST(:payload AS jsonb), :created_at
                )
            """), {
                "id":         str(uuid4()),
                "record_id":  record_id,
                "payload":    outbox_payload,
                "created_at": server_ts,
            })
    except Exception as e:
        logger.error(f"[API-LOGS] DB 오류: {e}")
        raise HTTPException(500, str(e))

    # Notion 파이프라인 fire-and-forget
    asyncio.create_task(_fire_notion_pipeline(
        facility_id="demo",
        beneficiary_id="demo",
        caregiver_id="demo",
        care_record_id=record_id,
        raw_voice_text=body.text,
        recorded_at=recorded_at,
        label="API-LOGS-NOTION",
    ))

    logger.info(f"[API-LOGS] ✅ 저장 완료: record={record_id}")
    return {"success": True, "record_id": record_id, "accepted": True}


# ══════════════════════════════════════════════════════════════════
# [FRONT-BRIDGE] POST /api/kakao/send
#
# FRONT END api.ts sendKakao(text) 수신 → 긴급 알림톡 fan-out
# ══════════════════════════════════════════════════════════════════

class KakaoSendBody(BaseModel):
    text: str


@app.post("/api/kakao/send", status_code=202, tags=["프론트 연동"])
async def post_kakao_send(body: KakaoSendBody):
    """
    FRONT END api.ts sendKakao() 수신 엔드포인트.
    긴급 상황 원장 + 관리자 알림톡 fan-out 실행.
    Idempotency-Key 자동 생성 — FE 헤더 불필요.
    """
    if not body.text.strip():
        raise HTTPException(422, "text 는 필수값입니다.")

    idem_key = str(uuid4())  # FE가 헤더를 안 보내도 되도록 서버 자동 생성
    targets  = [p for p in (EMERGENCY_RECIPIENTS or [DEFAULT_FACILITY_PHONE]) if p]

    if not targets:
        logger.warning("[API-KAKAO] 수신자 명단 비어있음 — 환경변수 미설정, 로그만 기록")
        return {"success": True, "sent": 0, "message": "수신자 미설정"}

    variables = {
        "#{발화내용}": body.text[:800],
        "#{발생시각}": datetime.now(timezone.utc).isoformat(),
        "#{심각도}":   "normal",
    }

    sent, failed = fanout_alimtalk(
        engine=engine,
        phones=targets,
        template_code=ALIMTALK_TPL_EMERGENCY,
        variables=variables,
        trigger_type="KAKAO-SEND",
        idempotency_key=idem_key,
    )

    logger.info(f"[API-KAKAO] 알림톡 발송 — sent={sent} failed={failed}")
    return {"success": True, "sent": sent, "failed": failed}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
