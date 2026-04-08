"""
Voice Guard — Phase 3 Reconciliation API (reconciliation_api.py)
=================================================================
엔드포인트:
  POST /api/v3/reconcile          — 3각 검증 실행 (특정 날짜/기관)
  GET  /api/v3/reconcile/results  — 검증 결과 조회 (날짜/기관/상태 필터)
  GET  /api/v3/reconcile/summary  — 날짜별 요약 통계
  POST /api/v3/reconcile/refresh  — Materialized View 수동 갱신

[Gotcha 적용]
  - allow_methods에 POST, GET, OPTIONS, PATCH 포함 (ingest_api.py CORS 확인)
  - REPEATABLE READ는 reconciliation_engine.py 내부에서 처리
"""

import logging
import os
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from reconciliation_engine import run_reconciliation

logger = logging.getLogger("reconciliation_api")

DATABASE_URL = os.getenv("DATABASE_URL")
_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v3", tags=["3각 검증 엔진"])


# ── 응답 모델 ────────────────────────────────────────────────

class ReconcileRequest(BaseModel):
    facility_id:  Optional[str] = None
    target_date:  Optional[str] = None   # YYYY-MM-DD
    dry_run:      bool = False


class ReconcileResponse(BaseModel):
    total:     int
    match:     int
    partial:   int
    anomaly:   int
    unplanned: int
    errors:    int
    run_at:    str
    dry_run:   bool
    results:   list = []


# ── POST /api/v3/reconcile ──────────────────────────────────

@router.post(
    "/reconcile",
    response_model=ReconcileResponse,
    summary="3각 검증 실행",
    description=(
        "REPEATABLE READ 스냅샷 위에서 care_plan × evidence × billing 3각 검증 수행. "
        "dry_run=true 면 DB INSERT 없이 결과만 반환."
    ),
)
async def trigger_reconciliation(body: ReconcileRequest):
    try:
        target = date.fromisoformat(body.target_date) if body.target_date else None
    except ValueError:
        raise HTTPException(status_code=422, detail="target_date 형식 오류: YYYY-MM-DD")

    try:
        result = run_reconciliation(
            facility_id=body.facility_id,
            target_date=target,
            dry_run=body.dry_run,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"[RECONCILE] 엔진 실행 오류: {e}")
        raise HTTPException(status_code=500, detail=f"엔진 오류: {e}")

    return ReconcileResponse(dry_run=body.dry_run, **result)


# ── GET /api/v3/reconcile/results ───────────────────────────

@router.get(
    "/reconcile/results",
    summary="검증 결과 조회",
    description="날짜/기관/상태 필터로 reconciliation_result 조회.",
)
async def get_results(
    facility_id:   Optional[str] = Query(None),
    date_from:     Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to:       Optional[str] = Query(None, description="YYYY-MM-DD"),
    result_status: Optional[str] = Query(None, description="MATCH|PARTIAL|ANOMALY|UNPLANNED"),
    anomaly_code:  Optional[str] = Query(None),
    limit:         int           = Query(200, le=1000),
):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    where_parts  = []
    query_params: dict = {"limit": limit}

    if facility_id:
        where_parts.append("facility_id = :facility_id")
        query_params["facility_id"] = facility_id
    if date_from:
        where_parts.append("fact_date >= :date_from")
        query_params["date_from"] = date_from
    if date_to:
        where_parts.append("fact_date <= :date_to")
        query_params["date_to"] = date_to
    if result_status:
        if result_status not in ("MATCH", "PARTIAL", "ANOMALY", "UNPLANNED"):
            raise HTTPException(status_code=422, detail="유효하지 않은 result_status")
        where_parts.append("result_status = :result_status")
        query_params["result_status"] = result_status
    if anomaly_code:
        where_parts.append("anomaly_code = :anomaly_code")
        query_params["anomaly_code"] = anomaly_code

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    id, facility_id, beneficiary_id, fact_date, care_type,
                    result_status, anomaly_code, anomaly_detail,
                    has_plan, has_record, has_billing,
                    planned_min, recorded_count, billed_min,
                    engine_version, run_at, created_at
                FROM public.reconciliation_result
                {where_sql}
                ORDER BY fact_date DESC, created_at DESC
                LIMIT :limit
            """), query_params).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "count":   len(rows),
        "records": [dict(r._mapping) for r in rows],
    }


# ── GET /api/v3/reconcile/summary ───────────────────────────

@router.get(
    "/reconcile/summary",
    summary="날짜별 검증 요약 통계",
    description="최근 N일간 날짜별 MATCH/PARTIAL/ANOMALY/UNPLANNED 건수 집계.",
)
async def get_summary(
    facility_id: Optional[str] = Query(None),
    days:        int           = Query(30, le=90),
):
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    date_from = (date.today() - timedelta(days=days)).isoformat()
    where_parts  = ["fact_date >= :date_from"]
    query_params = {"date_from": date_from}

    if facility_id:
        where_parts.append("facility_id = :facility_id")
        query_params["facility_id"] = facility_id

    where_sql = "WHERE " + " AND ".join(where_parts)

    try:
        with _engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    fact_date,
                    COUNT(*) FILTER (WHERE result_status = 'MATCH')     AS match_count,
                    COUNT(*) FILTER (WHERE result_status = 'PARTIAL')   AS partial_count,
                    COUNT(*) FILTER (WHERE result_status = 'ANOMALY')   AS anomaly_count,
                    COUNT(*) FILTER (WHERE result_status = 'UNPLANNED') AS unplanned_count,
                    COUNT(*) AS total_count
                FROM public.reconciliation_result
                {where_sql}
                GROUP BY fact_date
                ORDER BY fact_date DESC
            """), query_params).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "days":    days,
        "summary": [dict(r._mapping) for r in rows],
    }


# ── POST /api/v3/reconcile/refresh ──────────────────────────

@router.post(
    "/reconcile/refresh",
    summary="Materialized View 수동 갱신",
    description=(
        "canonical_day_fact, canonical_time_fact MATERIALIZED VIEW를 수동 갱신. "
        "배치 스케줄러 미사용 시 검증 전 호출 필수."
    ),
)
async def refresh_views():
    if _engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    try:
        with _engine.begin() as conn:
            conn.execute(text(
                "REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_day_fact"
            ))
            conn.execute(text(
                "REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_time_fact"
            ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"갱신 실패: {e}")

    return {"refreshed": ["canonical_day_fact", "canonical_time_fact"]}
