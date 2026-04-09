"""
Voice Guard — sharelink_api.py
Phase 7: 자료 발송 & 2-Stage ACK API

[설계 원칙]
  ① POST /api/v7/dispatch       — 발송 원장 생성 + Outbox 큐잉
  ② GET  /api/v7/ack/{token}    — 1차 ACK: link_clicked (토큰 검증)
  ③ POST /api/v7/ack/{token}    — 2차 ACK: read_confirmed (dwell_seconds 필수)
  ④ 포크 방지: pg_advisory_xact_lock(ack_chain_lock_id) 트랜잭션 락 필수
  ⑤ 토큰 위조 방지: HMAC-SHA256 72시간 만료 서명
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

logger = logging.getLogger("voice_guard.sharelink")

router = APIRouter(prefix="/api/v7", tags=["sharelink"])

# ── 환경변수 ──────────────────────────────────────────────────────
SHARELINK_SECRET = os.getenv("SHARELINK_SECRET", "")   # HMAC 서명 키
ACK_TOKEN_TTL_HOURS = 72
DATABASE_URL = os.getenv("DATABASE_URL", "")

_engine = (
    create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
    if DATABASE_URL
    else None
)


# ══════════════════════════════════════════════════════════════════
# 토큰 유틸 (HMAC-SHA256 Signed Token)
# ══════════════════════════════════════════════════════════════════

def _build_ack_token(dispatch_id: str, expires_at: datetime) -> str:
    """
    HMAC-SHA256 서명 토큰 생성.
    payload = f"{dispatch_id}:{expires_at_iso}"
    token   = f"{payload}.{signature_hex}"
    """
    expires_iso = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = f"{dispatch_id}:{expires_iso}"
    sig = hmac.new(
        SHARELINK_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _verify_ack_token(token: str) -> tuple[str, datetime]:
    """
    토큰 검증 후 (dispatch_id, expires_at) 반환.
    위조 또는 만료 시 HTTPException(403) 발생.
    """
    if not SHARELINK_SECRET:
        raise HTTPException(status_code=500, detail="SHARELINK_SECRET 미설정")

    try:
        payload, sig = token.rsplit(".", 1)
        dispatch_id, expires_iso = payload.split(":", 1)
    except ValueError:
        raise HTTPException(status_code=403, detail="유효하지 않은 ACK 토큰 형식")

    expected_sig = hmac.new(
        SHARELINK_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, sig):
        raise HTTPException(status_code=403, detail="ACK 토큰 서명 불일치 (위조 감지)")

    expires_at = datetime.strptime(expires_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now_utc = datetime.now(timezone.utc)
    if now_utc > expires_at:
        raise HTTPException(
            status_code=403,
            detail=f"ACK 토큰 만료: expires_at={expires_iso}",
        )

    return dispatch_id, expires_at


# ══════════════════════════════════════════════════════════════════
# 해시 유틸
# ══════════════════════════════════════════════════════════════════

def _sha256_payload(payload_json: dict) -> str:
    """SHA-256(canonical JSON) — sort_keys=True, ensure_ascii=False"""
    canonical = json.dumps(payload_json, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_chain(
    dispatch_id: str,
    ack_type: str,
    acked_at: datetime,
    dwell_seconds: Optional[int],
    prev_hash: Optional[str],
) -> str:
    """
    chain_hash = SHA-256(dispatch_id || ack_type || acked_at_iso || dwell_seconds || prev_hash)
    None 필드는 빈 문자열로 대체.
    """
    acked_at_iso = acked_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    parts = [
        dispatch_id,
        ack_type,
        acked_at_iso,
        str(dwell_seconds) if dwell_seconds is not None else "",
        prev_hash or "",
    ]
    data = "|".join(parts)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════
# pg_advisory_xact_lock 상수
# ══════════════════════════════════════════════════════════════════

# 해시체인 포크 방지용 락 ID (전역 고정값 — 동시성 충돌 원천 차단)
ACK_CHAIN_LOCK_ID = 7_000_000_001


# ══════════════════════════════════════════════════════════════════
# Pydantic 스키마
# ══════════════════════════════════════════════════════════════════

class DispatchRequest(BaseModel):
    facility_id:     str
    worker_id:       str
    recipient_phone: str
    recipient_name:  Optional[str] = None
    material_type:   str
    material_ref_id: Optional[str] = None
    payload_json:    dict
    channel:         str = Field(default="kakao", pattern="^(kakao|lms|sms)$")
    dispatched_by:   str


class AckConfirmRequest(BaseModel):
    dwell_seconds: int = Field(ge=0, description="체류 시간(초) — 0 이상 필수")


# ══════════════════════════════════════════════════════════════════
# PART 1: 발송 원장 생성 + Outbox 큐잉
# ══════════════════════════════════════════════════════════════════

def _require_engine():
    if _engine is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL 미설정 — DB 연결 불가")
    return _engine


@router.post("/dispatch", status_code=201)
def create_dispatch(req: DispatchRequest):
    """
    발송 원장(material_dispatch) 생성 + 대기열(material_dispatch_outbox) 큐잉.

    [보장]
    - payload_hash UNIQUE → 동일 payload 중복 발송 원천 차단
    - ack_token = HMAC-SHA256(dispatch_id + expires_at) 72시간 만료
    - outbox 행은 워커가 SELECT FOR UPDATE SKIP LOCKED 로 소비
    """
    engine = _require_engine()
    payload_hash = _sha256_payload(req.payload_json)

    dispatch_at = datetime.now(timezone.utc)
    ack_expires_at = dispatch_at + timedelta(hours=ACK_TOKEN_TTL_HOURS)

    with engine.begin() as conn:
        # 중복 발송 사전 검사
        existing = conn.execute(
            text("SELECT id FROM material_dispatch WHERE payload_hash = :h"),
            {"h": payload_hash},
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"동일 payload 이미 발송됨: dispatch_id={existing.id}",
            )

        # dispatch_id 는 DB gen_random_uuid() 생성 후 RETURNING으로 회수
        # ack_token은 임시값으로 INSERT 후 dispatch_id를 이용해 갱신
        result = conn.execute(
            text("""
                INSERT INTO material_dispatch
                    (facility_id, worker_id, recipient_phone, recipient_name,
                     material_type, material_ref_id, payload_json, payload_hash,
                     channel, ack_token, ack_expires_at, dispatched_by, dispatch_at)
                VALUES
                    (:facility_id, :worker_id, :recipient_phone, :recipient_name,
                     :material_type, :material_ref_id::uuid, :payload_json::jsonb,
                     :payload_hash, :channel, :ack_token_placeholder,
                     :ack_expires_at, :dispatched_by, :dispatch_at)
                RETURNING id
            """),
            {
                "facility_id":           req.facility_id,
                "worker_id":             req.worker_id,
                "recipient_phone":       req.recipient_phone,
                "recipient_name":        req.recipient_name,
                "material_type":         req.material_type,
                "material_ref_id":       req.material_ref_id,
                "payload_json":          json.dumps(req.payload_json, ensure_ascii=False),
                "payload_hash":          payload_hash,
                "channel":               req.channel,
                "ack_token_placeholder": "__PENDING__",
                "ack_expires_at":        ack_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dispatched_by":         req.dispatched_by,
                "dispatch_at":           dispatch_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        dispatch_id = str(result.fetchone().id)

        # dispatch_id 기반 실제 토큰 생성 후 갱신 (동일 트랜잭션 — 커밋 전)
        ack_token = _build_ack_token(dispatch_id, ack_expires_at)
        conn.execute(
            text("UPDATE material_dispatch SET ack_token = :t WHERE id = :id::uuid"),
            {"t": ack_token, "id": dispatch_id},
        )

        # Outbox 큐잉
        conn.execute(
            text("""
                INSERT INTO material_dispatch_outbox
                    (dispatch_id, channel, status, attempt_count, next_attempt_at)
                VALUES
                    (:dispatch_id::uuid, :channel, 'PENDING', 0, NOW())
            """),
            {"dispatch_id": dispatch_id, "channel": req.channel},
        )

    logger.info(
        f"[DISPATCH] 발송 원장 생성: dispatch_id={dispatch_id} "
        f"channel={req.channel} material_type={req.material_type}"
    )
    return {
        "dispatch_id":    dispatch_id,
        "ack_token":      ack_token,
        "ack_expires_at": ack_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":         "QUEUED",
    }


# ══════════════════════════════════════════════════════════════════
# PART 2: 1차 ACK — GET /ack/{token} (link_clicked)
# ══════════════════════════════════════════════════════════════════

@router.get("/ack/{token}", status_code=200)
def ack_link_clicked(token: str, request: Request):
    """
    1차 ACK: 링크 클릭 확인 (link_clicked).

    [처리 순서]
    1. HMAC-SHA256 토큰 검증 + 만료 확인
    2. pg_advisory_xact_lock(ACK_CHAIN_LOCK_ID) 획득 (포크 방지)
    3. 직전 chain_hash 조회 (prev_hash)
    4. chain_hash 계산 후 material_ack_ledger INSERT
    """
    engine = _require_engine()
    dispatch_id, _ = _verify_ack_token(token)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    acked_at = datetime.now(timezone.utc)

    with engine.begin() as conn:
        # 해시체인 포크 방지 트랜잭션 락
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": ACK_CHAIN_LOCK_ID},
        )

        # 직전 chain_hash 조회
        prev_row = conn.execute(
            text("""
                SELECT chain_hash FROM material_ack_ledger
                WHERE dispatch_id = :did::uuid
                ORDER BY acked_at DESC
                LIMIT 1
            """),
            {"did": dispatch_id},
        ).fetchone()
        prev_hash = prev_row.chain_hash if prev_row else None

        chain_hash = _sha256_chain(
            dispatch_id, "link_clicked", acked_at, None, prev_hash
        )

        conn.execute(
            text("""
                INSERT INTO material_ack_ledger
                    (dispatch_id, ack_type, acked_at, ip_address,
                     user_agent, dwell_seconds, prev_hash, chain_hash)
                VALUES
                    (:did::uuid, 'link_clicked', :acked_at,
                     :ip, :ua, NULL, :prev_hash, :chain_hash)
            """),
            {
                "did":        dispatch_id,
                "acked_at":   acked_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "ip":         ip_address,
                "ua":         user_agent,
                "prev_hash":  prev_hash,
                "chain_hash": chain_hash,
            },
        )

    logger.info(
        f"[ACK] link_clicked: dispatch_id={dispatch_id} "
        f"chain_hash={chain_hash[:16]}... ip={ip_address}"
    )
    return {
        "dispatch_id": dispatch_id,
        "ack_type":    "link_clicked",
        "acked_at":    acked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain_hash":  chain_hash,
    }


# ══════════════════════════════════════════════════════════════════
# PART 3: 2차 ACK — POST /ack/{token} (read_confirmed)
# ══════════════════════════════════════════════════════════════════

@router.post("/ack/{token}", status_code=200)
def ack_read_confirmed(
    token: str,
    body: AckConfirmRequest,
    request: Request,
):
    """
    2차 ACK: 열람 완료 확인 (read_confirmed).
    dwell_seconds (체류 시간) 필수. 0 이상 정수.

    [처리 순서]
    1. HMAC-SHA256 토큰 검증 + 만료 확인
    2. pg_advisory_xact_lock(ACK_CHAIN_LOCK_ID) 획득 (포크 방지)
    3. 직전 chain_hash 조회 (prev_hash)
    4. chain_hash 계산 후 material_ack_ledger INSERT
    """
    engine = _require_engine()
    dispatch_id, _ = _verify_ack_token(token)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    acked_at = datetime.now(timezone.utc)
    dwell_seconds = body.dwell_seconds

    with engine.begin() as conn:
        # 해시체인 포크 방지 트랜잭션 락
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": ACK_CHAIN_LOCK_ID},
        )

        # 직전 chain_hash 조회
        prev_row = conn.execute(
            text("""
                SELECT chain_hash FROM material_ack_ledger
                WHERE dispatch_id = :did::uuid
                ORDER BY acked_at DESC
                LIMIT 1
            """),
            {"did": dispatch_id},
        ).fetchone()
        prev_hash = prev_row.chain_hash if prev_row else None

        chain_hash = _sha256_chain(
            dispatch_id, "read_confirmed", acked_at, dwell_seconds, prev_hash
        )

        conn.execute(
            text("""
                INSERT INTO material_ack_ledger
                    (dispatch_id, ack_type, acked_at, ip_address,
                     user_agent, dwell_seconds, prev_hash, chain_hash)
                VALUES
                    (:did::uuid, 'read_confirmed', :acked_at,
                     :ip, :ua, :dwell, :prev_hash, :chain_hash)
            """),
            {
                "did":        dispatch_id,
                "acked_at":   acked_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "ip":         ip_address,
                "ua":         user_agent,
                "dwell":      dwell_seconds,
                "prev_hash":  prev_hash,
                "chain_hash": chain_hash,
            },
        )

    logger.info(
        f"[ACK] read_confirmed: dispatch_id={dispatch_id} "
        f"dwell={dwell_seconds}s chain_hash={chain_hash[:16]}... ip={ip_address}"
    )
    return {
        "dispatch_id":   dispatch_id,
        "ack_type":      "read_confirmed",
        "acked_at":      acked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dwell_seconds": dwell_seconds,
        "chain_hash":    chain_hash,
    }
