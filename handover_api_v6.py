"""
Voice Guard — Phase 6 Handover API v6 (handover_api_v6.py)
============================================================
엔드포인트:
  POST  /api/v6/handover/trigger        — 멱등성 보장 보고서 생성 트리거
  PATCH /api/v6/handover/{id}/ack       — 법적 수신 확인 (Notion 위변조 감지 포함)
  POST  /api/v6/handover/utterance      — 수시 발화 기록 수신 (멱등성 키 서버 생성)
  GET   /api/v6/handover/report/{id}    — 보고서 상태 조회

[Phase 6 4대 방어]
  ① POST trigger: 클라이언트 키 무시 → sha256(worker_id+shift_date) 서버 결정론적 생성
  ② PATCH ack:    ACK 시 Notion 현재 데이터 재조회 → notion_snapshot 해시 비교
                  불일치 → tamper_detected:true 플래그 + handover_ack_ledger INSERT
  ③ POST utterance: sha256(worker_id+shift_date+device_id+recorded_at) 멱등성 보장
  ④ TTS 코드: 이 모듈에 TTS 관련 코드 없음 (pyttsx3/gTTS/OpenAI TTS 완전 제거)
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import create_engine, text

from handover_compile_handler import (
    make_report_idempotency_key,
    make_utterance_idempotency_key,
)

load_dotenv()

logger = logging.getLogger("handover_api_v6")

DATABASE_URL  = os.getenv("DATABASE_URL")
REDIS_URL     = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM  = "voice:events"

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_VERSION = "2022-06-28"
NOTION_API_URL = "https://api.notion.com/v1"

_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v6", tags=["인수인계 엔진 v6"])


# ── Redis XADD 헬퍼 ──────────────────────────────────────────

async def _xadd(event_type: str, payload: dict) -> str:
    event_id = str(uuid4())
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.xadd(REDIS_STREAM, {
            "event_id":   event_id,
            "event_type": event_type,
            "payload":    json.dumps(payload, ensure_ascii=False, default=str),
            "attempt_num": "0",
        })
        await r.aclose()
    except Exception as e:
        logger.warning("[HandoverAPIv6] Redis XADD 실패: %s", e)
    return event_id


# ══════════════════════════════════════════════════════════════
# 요청/응답 모델
# ══════════════════════════════════════════════════════════════

class TriggerRequest(BaseModel):
    facility_id: str
    worker_id:   str
    shift_date:  str      # 'YYYY-MM-DD'

    @field_validator("shift_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"shift_date 는 YYYY-MM-DD 형식이어야 합니다: {v!r}")
        return v


class TriggerResponse(BaseModel):
    accepted:        bool
    report_id:       str
    idempotency_key: str
    status:          str
    message:         str


class UtteranceRequest(BaseModel):
    facility_id:     str
    worker_id:       str
    shift_date:      str          # 'YYYY-MM-DD'
    device_id:       str
    recorded_at:     str          # ISO-8601
    transcript_text: Optional[str] = None
    audio_sha256:    Optional[str] = None
    beneficiary_id:  Optional[str] = None

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError(f"recorded_at ISO-8601 형식 오류: {v!r}")
        return v


class UtteranceResponse(BaseModel):
    accepted:        bool
    utterance_id:    str
    idempotency_key: str
    duplicate:       bool
    message:         str


class AckRequest(BaseModel):
    device_id:  str
    ip_address: Optional[str] = None


class AckResponse(BaseModel):
    ack_id:          str
    report_id:       str
    device_id:       str
    ack_at:          str
    tamper_detected: bool
    message:         str


class ReportResponse(BaseModel):
    id:                    str
    facility_id:           str
    worker_id:             str
    shift_date:            str
    idempotency_key:       str
    status:                str
    trigger_at:            str
    expires_at:            str
    gemini_failed:         bool
    tamper_detected:       bool
    notion_page_id:        Optional[str]
    has_gemini_json:       bool
    has_raw_fallback:      bool


# ══════════════════════════════════════════════════════════════
# POST /api/v6/handover/utterance — 수시 발화 기록 수신
# ══════════════════════════════════════════════════════════════

@router.post(
    "/handover/utterance",
    response_model=UtteranceResponse,
    status_code=201,
    summary="수시 발화 기록 수신 (멱등성 키 서버 강제 생성)",
)
async def receive_utterance(req: UtteranceRequest = Body(...)):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    recorded_dt = datetime.fromisoformat(req.recorded_at)
    recorded_utc = recorded_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 클라이언트 키 무시 — 서버 결정론적 생성
    idem_key = make_utterance_idempotency_key(
        req.worker_id, req.shift_date, req.device_id, recorded_utc
    )

    try:
        with _engine.begin() as conn:
            result = conn.execute(text("""
                INSERT INTO public.handover_utterance_ledger
                    (idempotency_key, facility_id, worker_id, shift_date,
                     device_id, recorded_at, transcript_text, audio_sha256, beneficiary_id)
                VALUES
                    (:ikey, :fid, :wid, :sdate,
                     :did, :rat, :txt, :asha, :bid)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
            """), {
                "ikey":  idem_key,
                "fid":   req.facility_id,
                "wid":   req.worker_id,
                "sdate": req.shift_date,
                "did":   req.device_id,
                "rat":   recorded_dt,
                "txt":   req.transcript_text,
                "asha":  req.audio_sha256,
                "bid":   req.beneficiary_id,
            })
            row = result.fetchone()

        if row is None:
            # ON CONFLICT DO NOTHING → 중복
            with _engine.connect() as conn:
                dup_row = conn.execute(text("""
                    SELECT id FROM public.handover_utterance_ledger
                    WHERE idempotency_key = :ikey
                """), {"ikey": idem_key}).fetchone()
            return UtteranceResponse(
                accepted=True,
                utterance_id=str(dup_row.id) if dup_row else "",
                idempotency_key=idem_key,
                duplicate=True,
                message="중복 발화 기록 — 멱등성 키 일치, 재수신 무시.",
            )

        return UtteranceResponse(
            accepted=True,
            utterance_id=str(row.id),
            idempotency_key=idem_key,
            duplicate=False,
            message="발화 기록 저장 완료.",
        )

    except Exception as e:
        logger.error("[HandoverAPIv6] utterance INSERT 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"발화 기록 저장 실패: {e}")


# ══════════════════════════════════════════════════════════════
# POST /api/v6/handover/trigger — 멱등성 보장 보고서 생성 트리거
# ══════════════════════════════════════════════════════════════

@router.post(
    "/handover/trigger",
    response_model=TriggerResponse,
    status_code=202,
    summary="인수인계 보고서 생성 트리거 (서버 결정론적 멱등성 키)",
)
async def trigger_handover(req: TriggerRequest = Body(...)):
    """
    [핵심 방어]
    클라이언트가 보내는 키를 무시하고 서버에서 sha256(worker_id+shift_date)로
    결정론적 idempotency_key 를 생성한다.
    동일 worker_id + shift_date 요청이 중복으로 들어오면 409를 반환하여
    중복 보고서 생성을 원천 차단한다.
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    # 서버 결정론적 멱등성 키 생성 (클라이언트 키 무시)
    idem_key = make_report_idempotency_key(req.worker_id, req.shift_date)

    # 중복 확인
    with _engine.connect() as conn:
        existing = conn.execute(text("""
            SELECT id, status FROM public.handover_report_ledger
            WHERE idempotency_key = :ikey
        """), {"ikey": idem_key}).fetchone()

    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"동일한 worker_id={req.worker_id!r} + shift_date={req.shift_date!r} "
                f"에 대한 보고서가 이미 존재합니다 (report_id={existing.id}, "
                f"status={existing.status}). "
                "재생성이 필요하면 기존 보고서가 EXPIRED 또는 FAILED 상태일 때 재요청하십시오."
            ),
        )

    # INSERT into handover_report_ledger
    report_id = str(uuid4())
    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.handover_report_ledger
                    (id, facility_id, worker_id, shift_date, idempotency_key, status)
                VALUES
                    (:rid, :fid, :wid, :sdate, :ikey, 'PENDING')
            """), {
                "rid":   report_id,
                "fid":   req.facility_id,
                "wid":   req.worker_id,
                "sdate": req.shift_date,
                "ikey":  idem_key,
            })
    except Exception as e:
        logger.error("[HandoverAPIv6] report INSERT 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"보고서 생성 실패: {e}")

    # Redis에 handover_compile 이벤트 발행
    event_id = await _xadd("handover_compile", {
        "report_id":   report_id,
        "facility_id": req.facility_id,
        "worker_id":   req.worker_id,
        "shift_date":  req.shift_date,
    })

    return TriggerResponse(
        accepted=True,
        report_id=report_id,
        idempotency_key=idem_key,
        status="PENDING",
        message=(
            f"인수인계 보고서 생성 요청 수락. "
            f"idempotency_key={idem_key[:8]}... (서버 결정론적 생성). "
            f"처리 완료 후 GET /api/v6/handover/report/{report_id} 에서 조회 가능."
        ),
    )


# ══════════════════════════════════════════════════════════════
# PATCH /api/v6/handover/{report_id}/ack — 법적 수신 확인
# ══════════════════════════════════════════════════════════════

@router.patch(
    "/handover/{report_id}/ack",
    response_model=AckResponse,
    summary="법적 수신 확인 — Notion 위변조 감지 포함",
)
async def ack_handover(
    report_id: str       = Path(..., description="handover_report_ledger.id (UUID)"),
    req:       AckRequest = Body(...),
    request:   Request   = None,
):
    """
    [핵심 방어]
    1. DB에서 notion_snapshot_sha256(전송 당시 해시) 조회
    2. Notion 현재 페이지 데이터 재조회 → sha256 계산
    3. 해시 불일치 → tamper_detected=True 플래그
    4. handover_ack_ledger INSERT (Append-Only — 법적 증거)
    5. handover_report_ledger.tamper_detected UPDATE (허용 필드)
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    # 보고서 조회
    with _engine.connect() as conn:
        report = conn.execute(text("""
            SELECT
                id, status, notion_page_id,
                notion_snapshot_sha256, gemini_failed
            FROM public.handover_report_ledger
            WHERE id = :rid
        """), {"rid": report_id}).fetchone()

    if report is None:
        raise HTTPException(status_code=404, detail=f"report_id={report_id!r} 없음")

    if report.status not in ("DONE",):
        raise HTTPException(
            status_code=422,
            detail=f"보고서 상태가 DONE 이 아닙니다 (현재: {report.status}). ACK 불가.",
        )

    # Notion 재조회 → 해시 비교 (tamper detection)
    current_sha: str  = ""
    tamper:      bool = False
    stored_sha         = report.notion_snapshot_sha256 or ""

    if report.notion_page_id and NOTION_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{NOTION_API_URL}/pages/{report.notion_page_id}",
                    headers={
                        "Authorization":  f"Bearer {NOTION_API_KEY}",
                        "Notion-Version": NOTION_VERSION,
                    },
                )
                resp.raise_for_status()
                current_data = resp.json()

            current_str = json.dumps(current_data, ensure_ascii=False, sort_keys=True)
            current_sha = hashlib.sha256(current_str.encode("utf-8")).hexdigest()

            if stored_sha and current_sha != stored_sha:
                tamper = True
                logger.warning(
                    "[HandoverAPIv6] 위변조 감지! report_id=%s "
                    "stored=%s current=%s",
                    report_id, stored_sha[:16], current_sha[:16],
                )
        except Exception as e:
            logger.warning("[HandoverAPIv6] Notion 재조회 실패 (tamper 검사 생략): %s", e)
    elif not NOTION_API_KEY:
        logger.warning("[HandoverAPIv6] NOTION_API_KEY 미설정 — tamper 검사 생략")

    ack_at       = datetime.now(timezone.utc)
    ip_address   = req.ip_address or (request.client.host if request and request.client else None)
    ack_id       = str(uuid4())

    # handover_ack_ledger INSERT (Append-Only)
    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.handover_ack_ledger
                    (id, report_id, device_id, ack_at, ip_address,
                     tamper_detected, snapshot_sha256_at_ack)
                VALUES
                    (:aid, :rid, :did, :aat, :ip, :tamper, :sha)
            """), {
                "aid":    ack_id,
                "rid":    report_id,
                "did":    req.device_id,
                "aat":    ack_at,
                "ip":     ip_address,
                "tamper": tamper,
                "sha":    current_sha or None,
            })

            # tamper_detected 플래그 UPDATE (허용 필드)
            if tamper:
                conn.execute(text("""
                    UPDATE public.handover_report_ledger
                    SET tamper_detected = TRUE
                    WHERE id = :rid
                """), {"rid": report_id})

    except Exception as e:
        logger.error("[HandoverAPIv6] ACK INSERT 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"ACK 처리 실패: {e}")

    msg = "수신 확인 완료."
    if tamper:
        msg = (
            "⚠️ 수신 확인 완료 — 단, Notion 위변조 감지됨. "
            "tamper_detected=True 플래그가 기록되었습니다. 법적 검토 필요."
        )

    return AckResponse(
        ack_id=ack_id,
        report_id=report_id,
        device_id=req.device_id,
        ack_at=ack_at.isoformat(),
        tamper_detected=tamper,
        message=msg,
    )


# ══════════════════════════════════════════════════════════════
# GET /api/v6/handover/report/{report_id} — 보고서 상태 조회
# ══════════════════════════════════════════════════════════════

@router.get(
    "/handover/report/{report_id}",
    response_model=ReportResponse,
    summary="인수인계 보고서 상태 조회",
)
async def get_report(report_id: str = Path(...)):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    with _engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
                id, facility_id, worker_id, shift_date,
                idempotency_key, status, trigger_at, expires_at,
                gemini_failed, tamper_detected, notion_page_id,
                gemini_json IS NOT NULL     AS has_gemini_json,
                raw_fallback IS NOT NULL    AS has_raw_fallback
            FROM public.handover_report_ledger
            WHERE id = :rid
        """), {"rid": report_id}).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"report_id={report_id!r} 없음")

    return ReportResponse(
        id=str(row.id),
        facility_id=row.facility_id,
        worker_id=row.worker_id,
        shift_date=str(row.shift_date),
        idempotency_key=row.idempotency_key,
        status=row.status,
        trigger_at=row.trigger_at.isoformat(),
        expires_at=row.expires_at.isoformat(),
        gemini_failed=row.gemini_failed,
        tamper_detected=row.tamper_detected,
        notion_page_id=row.notion_page_id,
        has_gemini_json=bool(row.has_gemini_json),
        has_raw_fallback=bool(row.has_raw_fallback),
    )
