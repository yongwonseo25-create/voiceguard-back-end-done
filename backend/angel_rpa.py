"""
Voice Guard — backend/angel_rpa.py
엔젤시스템 Attended RPA 연계 API

[기능]
  1. POST /rpa/start     — RPA 실행 시작 (배치 상태 → RPA_IN_PROGRESS)
  2. POST /rpa/callback  — RPA 봇 실행 완료 콜백 (실행 증거 기록)
  3. GET  /rpa/log        — RPA 실행 이력 조회 (감사용)

[불변 원칙]
  - evidence_ledger / outbox_events 수정 0
  - bridge_rpa_execution_log Append-Only (INSERT만)
  - bridge_export_batch.status 전이만 UPDATE
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger("voice_guard.angel_rpa")

router = APIRouter(
    prefix="/api/v2/angel/rpa",
    tags=["엔젤 RPA"],
)

_engine = None
_redis_pub = None


def init_angel_rpa(engine, redis_pub=None):
    global _engine, _redis_pub
    _engine = engine
    _redis_pub = redis_pub


# ══════════════════════════════════════════════════════════════════
# [1] RPA 시작 — POST /api/v2/angel/rpa/start
#
# Export 완료된 배치를 RPA 봇에 넘기기 전 상태 전이
# CREATED → RPA_IN_PROGRESS
# ══════════════════════════════════════════════════════════════════

class RpaStartRequest(BaseModel):
    batch_id: str
    bot_id: str = "playwright-bot-1"


@router.post("/start", status_code=200)
async def rpa_start(body: RpaStartRequest):
    """RPA 실행 시작: 배치 상태를 RPA_IN_PROGRESS로 전이."""
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    try:
        with _engine.begin() as conn:
            # 현재 상태 확인
            batch = conn.execute(text("""
                SELECT id, status, facility_id, item_count
                FROM bridge_export_batch
                WHERE id = :bid
            """), {"bid": body.batch_id}).fetchone()

            if not batch:
                raise HTTPException(
                    404, f"batch_id '{body.batch_id}' 없음",
                )

            if batch.status not in ("CREATED", "DOWNLOADED"):
                raise HTTPException(
                    409,
                    f"RPA 시작 불가: 현재 상태 {batch.status}. "
                    f"CREATED 또는 DOWNLOADED만 가능.",
                )

            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = 'RPA_IN_PROGRESS'
                WHERE id = :bid
            """), {"bid": body.batch_id})

        logger.info(
            f"[RPA] 시작: batch={body.batch_id[:8]} "
            f"bot={body.bot_id}"
        )

        # SSE 알림
        if _redis_pub:
            try:
                await _redis_pub.publish(
                    "sse:dashboard",
                    json.dumps({
                        "event": "rpa_started",
                        "data": {
                            "batch_id": body.batch_id,
                            "bot_id": body.bot_id,
                        },
                    }, ensure_ascii=False),
                )
            except Exception:
                pass

        return {
            "started": True,
            "batch_id": body.batch_id,
            "status": "RPA_IN_PROGRESS",
            "facility_id": batch.facility_id,
            "item_count": batch.item_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════
# [2] RPA 콜백 — POST /api/v2/angel/rpa/callback
#
# RPA 봇이 엔젤시스템 입력 완료 후 호출
# → bridge_rpa_execution_log INSERT (실행 증거)
# → bridge_export_batch 최종 상태 전이
# ══════════════════════════════════════════════════════════════════

class RpaCallbackRequest(BaseModel):
    batch_id: str
    status: str                    # SUCCESS | FAILED | PARTIAL
    screenshot_hash: Optional[str] = None
    angel_receipt: Optional[dict] = None
    error_msg: Optional[str] = None
    items_applied: int = 0
    items_failed: int = 0
    executed_by: str = "playwright-bot-1"


@router.post("/callback", status_code=201)
async def rpa_callback(body: RpaCallbackRequest):
    """
    RPA 실행 완료 콜백.

    1) bridge_rpa_execution_log INSERT (Append-Only 증거)
    2) bridge_export_batch 최종 상태 전이:
       SUCCESS → APPLIED_CONFIRMED
       FAILED/PARTIAL → APPLY_FAILED
    """
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    if body.status not in ("SUCCESS", "FAILED", "PARTIAL"):
        raise HTTPException(
            422, "status는 SUCCESS/FAILED/PARTIAL만 허용",
        )

    if body.screenshot_hash and len(body.screenshot_hash) != 64:
        raise HTTPException(
            422, "screenshot_hash는 SHA-256 (64자) 형식",
        )

    now = datetime.now(timezone.utc)
    log_id = str(uuid4())

    # 최종 배치 상태 결정
    final_status = (
        "APPLIED_CONFIRMED"
        if body.status == "SUCCESS"
        else "APPLY_FAILED"
    )

    try:
        with _engine.begin() as conn:
            # 배치 존재 + 상태 확인
            batch = conn.execute(text("""
                SELECT id, status, facility_id
                FROM bridge_export_batch
                WHERE id = :bid
            """), {"bid": body.batch_id}).fetchone()

            if not batch:
                raise HTTPException(
                    404, f"batch_id '{body.batch_id}' 없음",
                )

            if batch.status not in (
                "RPA_IN_PROGRESS", "CREATED", "DOWNLOADED",
            ):
                raise HTTPException(
                    409,
                    f"콜백 불가: 현재 상태 {batch.status}. "
                    f"RPA_IN_PROGRESS만 가능.",
                )

            # [A] bridge_rpa_execution_log INSERT
            conn.execute(text("""
                INSERT INTO bridge_rpa_execution_log (
                    id, batch_id, status,
                    screenshot_hash, angel_receipt,
                    error_msg,
                    items_applied, items_failed,
                    executed_by, executed_at
                ) VALUES (
                    :id, :bid, :status,
                    :ss_hash,
                    CAST(:receipt AS jsonb),
                    :err,
                    :applied, :failed,
                    :bot, :ts
                )
            """), {
                "id": log_id,
                "bid": body.batch_id,
                "status": body.status,
                "ss_hash": body.screenshot_hash,
                "receipt": (
                    json.dumps(body.angel_receipt, ensure_ascii=False)
                    if body.angel_receipt else None
                ),
                "err": body.error_msg,
                "applied": body.items_applied,
                "failed": body.items_failed,
                "bot": body.executed_by,
                "ts": now,
            })

            # [B] bridge_export_batch 최종 상태 전이
            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = :final
                WHERE id = :bid
            """), {
                "final": final_status,
                "bid": body.batch_id,
            })

        logger.info(
            f"[RPA] 콜백: batch={body.batch_id[:8]} "
            f"rpa={body.status} → batch={final_status}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RPA] 콜백 실패: {e}")
        raise HTTPException(500, str(e))

    # SSE 알림
    if _redis_pub:
        try:
            await _redis_pub.publish(
                "sse:dashboard",
                json.dumps({
                    "event": "rpa_completed",
                    "data": {
                        "batch_id": body.batch_id,
                        "rpa_status": body.status,
                        "batch_status": final_status,
                        "items_applied": body.items_applied,
                        "items_failed": body.items_failed,
                    },
                }, ensure_ascii=False),
            )
        except Exception:
            pass

    return {
        "log_id": log_id,
        "batch_id": body.batch_id,
        "rpa_status": body.status,
        "batch_final_status": final_status,
        "executed_at": now.isoformat(),
    }


# ══════════════════════════════════════════════════════════════════
# [3] RPA 실행 이력 — GET /api/v2/angel/rpa/log
# ══════════════════════════════════════════════════════════════════

@router.get("/log")
async def list_rpa_logs(
    batch_id: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    """RPA 실행 이력 조회 (감사용)."""
    if _engine is None:
        raise HTTPException(503, "DB 미연결")

    where = "WHERE r.batch_id = :bid" if batch_id else ""  # nosec B608 — hardcoded SQL fragment
    params = {"bid": batch_id, "lim": limit}

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    r.id, r.batch_id, r.status,
                    r.screenshot_hash, r.angel_receipt,
                    r.error_msg,
                    r.items_applied, r.items_failed,
                    r.executed_by, r.executed_at,
                    b.facility_id, b.item_count,
                    b.status AS batch_status
                FROM bridge_rpa_execution_log r
                JOIN bridge_export_batch b ON b.id = r.batch_id
                {where}
                ORDER BY r.executed_at DESC
                LIMIT :lim
            """), params).fetchall()

        return {
            "logs": [dict(r._mapping) for r in rows],
        }
    except Exception as e:
        raise HTTPException(500, str(e))
