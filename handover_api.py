"""
Voice Guard — Phase 5 Handover API (handover_api.py)
=====================================================
엔드포인트:
  POST  /api/v5/handover/trigger         — 인수인계 브리핑 생성 트리거
  PATCH /api/v5/handover/{id}/ack        — 수령 확인 (delivered_at 기록)
  GET   /api/v5/handover/latest          — 최신 브리핑 조회 (다음 근무자 앱용)

[결함 방어 매핑]
  결함 1 (대타/수동): POST trigger 에 trigger_mode='MANUAL' 허용
  결함 6 (미수령 루프): PATCH ack → delivered_at 기록 + 미수령 경과 시 alert 이벤트 발행 준비
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import APIRouter, Body, HTTPException, Path, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import create_engine, text

load_dotenv()

logger = logging.getLogger("handover_api")

DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM = "voice:events"

_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v5", tags=["인수인계 엔진"])


# ── 요청/응답 모델 ───────────────────────────────────────────

class HandoverTriggerRequest(BaseModel):
    facility_id:    str
    shift_start:    str          # ISO-8601 (예: "2026-04-09T06:00:00+09:00")
    shift_end:      str          # ISO-8601 (예: "2026-04-09T14:00:00+09:00")
    trigger_mode:   str  = "SCHEDULED"   # 결함 1 방어: MANUAL 허용
    caregiver_name: str  = "미지정"

    @field_validator("trigger_mode")
    @classmethod
    def validate_trigger_mode(cls, v: str) -> str:
        v = v.upper()
        if v not in ("SCHEDULED", "MANUAL"):
            raise ValueError("trigger_mode 은 SCHEDULED 또는 MANUAL 이어야 합니다.")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_iso8601(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError(f"ISO-8601 형식이어야 합니다: {v!r}")
        return v


class HandoverTriggerResponse(BaseModel):
    accepted:    bool
    event_id:    str
    facility_id: str
    trigger_mode:str
    message:     str


class AckRequest(BaseModel):
    device_id: str


class AckResponse(BaseModel):
    handover_id:  str
    delivered_to: str
    delivered_at: str
    message:      str


class HandoverBriefResponse(BaseModel):
    id:              str
    facility_id:     str
    shift_start:     str
    shift_end:       str
    trigger_mode:    str
    generation_mode: str
    brief_text:      str
    tts_object_key:  Optional[str]
    source_count:    int
    anomaly_count:   int
    delivered_at:    Optional[str]
    generated_at:    str


# ── 헬퍼: Redis XADD (handover 이벤트는 unified_outbox를 경유하지 않음) ──
# unified_outbox.event_type CHECK 제약을 건드리지 않기 위해
# handover 이벤트는 Redis Stream 직접 발행 방식을 사용한다.
# Redis 장애 시 폴백: POST /api/v5/handover/trigger 재호출로 복구 가능.

import json  # Redis payload 직렬화 전용

def _make_event_id() -> str:
    return str(uuid4())


async def _xadd_event(event_id: str, event_type: str, payload: dict) -> None:
    """Redis Stream 에 이벤트 추가 (워커 즉시 알림). 실패는 조용히 무시."""
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.xadd(REDIS_STREAM, {
            "event_id":   event_id,
            "event_type": event_type,
            "payload":    json.dumps(payload, ensure_ascii=False, default=str),
            "attempt_num":"0",
        })
        await r.aclose()
    except Exception as e:
        logger.warning("[HandoverAPI] Redis XADD 실패 (폴백으로 처리됨): %s", e)


# ══════════════════════════════════════════════════════════════
# POST /api/v5/handover/trigger
# ══════════════════════════════════════════════════════════════
#
# 프론트엔드 "지금 마감" 버튼 또는 pg_cron 에서 호출.
# trigger_mode=MANUAL 을 허용함으로써 결함 1 (대타 출근) 을 방어한다.

@router.post(
    "/handover/trigger",
    response_model=HandoverTriggerResponse,
    status_code=202,
    summary="인수인계 브리핑 생성 트리거 (SCHEDULED / MANUAL)",
)
async def trigger_handover(req: HandoverTriggerRequest = Body(...)):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    shift_start_dt = datetime.fromisoformat(req.shift_start)
    shift_end_dt   = datetime.fromisoformat(req.shift_end)

    if shift_end_dt <= shift_start_dt:
        raise HTTPException(
            status_code=422,
            detail="shift_end 는 shift_start 보다 이후여야 합니다.",
        )

    # 동일 facility + shift_end 에 대한 중복 트리거 차단
    # (아직 발행 중인 브리핑이 있으면 409 반환)
    with _engine.connect() as conn:
        existing = conn.execute(text("""
            SELECT id FROM public.shift_handover_ledger
            WHERE facility_id = :fid
              AND shift_end    = :send
              AND is_superseded = FALSE
            LIMIT 1
        """), {"fid": req.facility_id, "send": shift_end_dt}).fetchone()

    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"동일한 교대 마감({req.shift_end}) 에 대한 브리핑이 이미 존재합니다. "
                f"재생성이 필요하면 기존 브리핑을 무효화(superseded)한 후 재요청하십시오."
            ),
        )

    payload = {
        "facility_id":   req.facility_id,
        "shift_start":   req.shift_start,
        "shift_end":     req.shift_end,
        "trigger_mode":  req.trigger_mode,
        "caregiver_name":req.caregiver_name,
    }
    event_id = _make_event_id()
    await _xadd_event(event_id, "handover_trigger", payload)

    return HandoverTriggerResponse(
        accepted=True,
        event_id=event_id,
        facility_id=req.facility_id,
        trigger_mode=req.trigger_mode,
        message=(
            f"인수인계 브리핑 생성 요청 수락 (trigger_mode={req.trigger_mode}). "
            "처리 완료 후 /api/v5/handover/latest 에서 조회 가능합니다."
        ),
    )


# ══════════════════════════════════════════════════════════════
# PATCH /api/v5/handover/{id}/ack
# ══════════════════════════════════════════════════════════════
#
# 다음 근무자가 앱에서 브리핑을 열람하면 자동 호출.
# shift_handover_ledger 의 delivered_to / delivered_at 을 UPDATE.
# (트리거로 봉인된 다른 필드는 변경 불가 — fn_handover_ledger_update_guard)

@router.patch(
    "/handover/{handover_id}/ack",
    response_model=AckResponse,
    summary="브리핑 수령 확인 (delivered_to / delivered_at 기록)",
)
async def ack_handover(
    handover_id: str = Path(..., description="shift_handover_ledger.id (UUID)"),
    req:         AckRequest = Body(...),
):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    delivered_at = datetime.now(timezone.utc)

    try:
        with _engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE public.shift_handover_ledger
                SET
                    delivered_to = :device_id,
                    delivered_at = :delivered_at
                WHERE id            = :hid
                  AND is_superseded = FALSE
                  AND delivered_at  IS NULL
                RETURNING id, delivered_to, delivered_at
            """), {
                "device_id":    req.device_id,
                "delivered_at": delivered_at,
                "hid":          handover_id,
            })
            row = result.fetchone()
    except Exception as e:
        # DB 트리거가 봉인 필드 변경을 감지하면 여기서 잡힘
        logger.error("[HandoverAPI] ACK UPDATE 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"ACK 처리 실패: {e}")

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"handover_id={handover_id!r} 에 해당하는 미수령 브리핑이 없습니다. "
                "이미 수령 처리됐거나 무효화된 브리핑입니다."
            ),
        )

    # 미수령 알림 해제 이벤트 발행 (NT-4 취소 신호 — Redis 직접 발행)
    await _xadd_event(_make_event_id(), "handover_ack", {
        "handover_id":  handover_id,
        "device_id":    req.device_id,
        "delivered_at": delivered_at.isoformat(),
    })

    return AckResponse(
        handover_id=handover_id,
        delivered_to=row.delivered_to,
        delivered_at=row.delivered_at.isoformat(),
        message="브리핑 수령 확인 완료.",
    )


# ══════════════════════════════════════════════════════════════
# GET /api/v5/handover/latest
# ══════════════════════════════════════════════════════════════
#
# 다음 근무자 앱이 화면 진입 시 호출. 가장 최신 유효 브리핑 반환.

@router.get(
    "/handover/latest",
    response_model=HandoverBriefResponse,
    summary="최신 인수인계 브리핑 조회 (다음 근무자 앱용)",
)
async def get_latest_handover(
    facility_id: str           = Query(..., description="요양기관 코드"),
    shift_end:   Optional[str] = Query(None,  description="교대 마감 시각 (ISO-8601). 생략 시 최신"),
):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    params: dict = {"fid": facility_id}
    shift_filter = ""
    if shift_end:
        try:
            shift_end_dt = datetime.fromisoformat(shift_end)
        except ValueError:
            raise HTTPException(status_code=422, detail="shift_end ISO-8601 형식 오류")
        shift_filter = "AND shift_end = :send"
        params["send"] = shift_end_dt

    with _engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT
                id, facility_id, shift_start, shift_end,
                trigger_mode, generation_mode,
                brief_text, tts_object_key,
                source_count, anomaly_count,
                delivered_at, generated_at
            FROM public.shift_handover_ledger
            WHERE facility_id   = :fid
              AND is_superseded = FALSE
              {shift_filter}
            ORDER BY shift_end DESC
            LIMIT 1
        """), params).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"facility_id={facility_id!r} 에 대한 유효한 브리핑이 없습니다.",
        )

    return HandoverBriefResponse(
        id=str(row.id),
        facility_id=row.facility_id,
        shift_start=row.shift_start.isoformat(),
        shift_end=row.shift_end.isoformat(),
        trigger_mode=row.trigger_mode,
        generation_mode=row.generation_mode,
        brief_text=row.brief_text,
        tts_object_key=row.tts_object_key,
        source_count=row.source_count,
        anomaly_count=row.anomaly_count,
        delivered_at=row.delivered_at.isoformat() if row.delivered_at else None,
        generated_at=row.generated_at.isoformat(),
    )
