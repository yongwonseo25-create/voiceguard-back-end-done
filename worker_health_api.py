"""
Voice Guard — Phase 4 Worker Health API (worker_health_api.py)
==============================================================
GET /api/v2/worker/health

반환:
  {
    "lag":        int,   -- PENDING 이벤트 수 (처리 지연 건)
    "dlq_count":  int,   -- DLQ 미해결 건수
    "throughput": {      -- 핸들러별 최근 5분 처리량
        "IngestHandler": {"done": N, "failed": N},
        ...
    },
    "failed_recent": [...],  -- 최근 10건 실패 이벤트
    "stream_info": {...},    -- Redis Stream lag/consumer 정보
    "checked_at": "ISO"
  }
"""

import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text

load_dotenv()

logger = logging.getLogger("worker_health_api")

DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STREAM = "voice:events"
CONSUMER_GROUP = "vg-router"

_engine = create_engine(
    DATABASE_URL,
    pool_size=3, max_overflow=5,
    pool_pre_ping=True,
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v2", tags=["워커 헬스"])


@router.get(
    "/worker/health",
    summary="워커 헬스 및 처리량 조회",
    description=(
        "통합 워커(event_router_worker)의 실시간 상태를 반환. "
        "Lag(지연), DLQ 건수, 핸들러별 처리량, Redis Stream 정보 포함."
    ),
)
async def worker_health():
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    checked_at = datetime.now(timezone.utc).isoformat()
    result: dict = {
        "lag":           0,
        "dlq_count":     0,
        "throughput":    {},
        "failed_recent": [],
        "stream_info":   {},
        "checked_at":    checked_at,
    }

    # ── DB 쿼리 (단일 연결로 묶음) ───────────────────────────
    try:
        with _engine.connect() as conn:

            # 1. Lag: PENDING 이벤트 수
            lag_row = conn.execute(text("""
                SELECT COUNT(*) AS cnt
                FROM public.v_unified_outbox_current
                WHERE status IN ('PENDING', 'PROCESSING')
            """)).fetchone()
            result["lag"] = int(lag_row.cnt) if lag_row else 0

            # 2. DLQ: dead_letter_queue 미해결 건수 (기존 + 신규)
            dlq_row = conn.execute(text("""
                SELECT
                    (SELECT COUNT(*) FROM public.dead_letter_queue WHERE is_resolved = FALSE)
                    +
                    (SELECT COUNT(*) FROM public.v_unified_outbox_current WHERE status = 'FAILED')
                AS total_dlq
            """)).fetchone()
            result["dlq_count"] = int(dlq_row.total_dlq) if dlq_row else 0

            # 3. Throughput: 핸들러별 최근 5분 처리량
            tput_rows = conn.execute(text("""
                SELECT
                    handler_name,
                    COUNT(*) FILTER (WHERE result = 'DONE')   AS done_count,
                    COUNT(*) FILTER (WHERE result = 'FAILED') AS failed_count,
                    AVG(duration_ms) FILTER (WHERE result = 'DONE') AS avg_duration_ms
                FROM public.worker_throughput_log
                WHERE logged_at >= NOW() - INTERVAL '5 minutes'
                GROUP BY handler_name
                ORDER BY handler_name
            """)).fetchall()
            for row in tput_rows:
                result["throughput"][row.handler_name] = {
                    "done":            int(row.done_count),
                    "failed":          int(row.failed_count),
                    "avg_duration_ms": round(float(row.avg_duration_ms or 0), 1),
                }

            # 4. 최근 실패 이벤트 10건
            failed_rows = conn.execute(text("""
                SELECT
                    uo.event_id,
                    uo.event_type,
                    uo.error_message,
                    uo.attempt_num,
                    uo.created_at
                FROM public.unified_outbox uo
                WHERE uo.status = 'FAILED'
                ORDER BY uo.created_at DESC
                LIMIT 10
            """)).fetchall()
            result["failed_recent"] = [
                {
                    "event_id":     str(r.event_id),
                    "event_type":   r.event_type,
                    "error_message": r.error_message,
                    "attempt_num":  r.attempt_num,
                    "created_at":   r.created_at.isoformat() if r.created_at else None,
                }
                for r in failed_rows
            ]

    except Exception as e:
        logger.error("[HEALTH] DB 조회 실패: %s", e)
        raise HTTPException(status_code=500, detail=f"DB 조회 실패: {e}")

    # ── Redis Stream 정보 ────────────────────────────────────
    try:
        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            info = await redis.xinfo_groups(REDIS_STREAM)
            group_info = next(
                (g for g in info if g.get("name") == CONSUMER_GROUP), {}
            )
            stream_len = await redis.xlen(REDIS_STREAM)
            result["stream_info"] = {
                "stream":         REDIS_STREAM,
                "stream_length":  stream_len,
                "group":          CONSUMER_GROUP,
                "pending_count":  group_info.get("pending", 0),
                "last_delivered": group_info.get("last-delivered-id", ""),
            }
        finally:
            await redis.aclose()
    except Exception as e:
        logger.warning("[HEALTH] Redis 조회 실패: %s", e)
        result["stream_info"] = {"error": str(e)}

    return result
