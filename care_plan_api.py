"""
Voice Guard — Care Plan API v2.0  (Phase 1 - Blind Spot 4)
============================================================
케어 계획(Plan) 원장 수집 엔드포인트

  POST /api/v2/care-plan/upload   — Excel/CSV 다건 적재 (부분 적재 허용)
  POST /api/v2/care-plan/entry    — 단건 수동 입력
  GET  /api/v2/care-plan          — 조회

핵심 원칙:
  1. plan_hash(SHA-256 UNIQUE) 기반 Idempotency — 중복 적재 원천 차단
  2. 파싱 실패 Row는 무시, 성공 Row는 즉시 커밋 (Partial Insert)
  3. Append-Only 원장 — INSERT만 허용, UPDATE는 is_superseded 전이만 가능

최종 수정: 2026-04-08
============================================================
"""

import hashlib
import io
import logging
import os
from datetime import date, datetime
from typing import Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("care_plan_api")

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v2/care-plan", tags=["케어 계획"])


# ── 해시 생성 ─────────────────────────────────────────────────
def make_plan_hash(
    facility_id: str, beneficiary_id: str, caregiver_id: str,
    plan_date: str, care_type: str, planned_start: str,
    planned_end: str, planned_duration_min: int,
) -> str:
    """계획 데이터의 SHA-256 해시. 동일 내용 중복 적재 방지."""
    raw = (
        f"{facility_id}::{beneficiary_id}::{caregiver_id}::"
        f"{plan_date}::{care_type}::{planned_start}::"
        f"{planned_end}::{planned_duration_min}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── 요청/응답 모델 ────────────────────────────────────────────
class CarePlanEntry(BaseModel):
    facility_id: str
    beneficiary_id: str
    caregiver_id: str
    plan_date: date
    care_type: str
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    planned_duration_min: int
    plan_source: str = "MANUAL"


class CarePlanEntryResponse(BaseModel):
    accepted: bool
    plan_id: str
    plan_hash: str
    message: str


class UploadResponse(BaseModel):
    total_rows: int
    inserted: int
    duplicates: int
    errors: int
    error_details: list[dict] = Field(default_factory=list)


# ── INSERT 공통 함수 ──────────────────────────────────────────
INSERT_SQL = text("""
    INSERT INTO public.care_plan_ledger (
        id, facility_id, beneficiary_id, caregiver_id,
        plan_date, care_type, planned_start, planned_end,
        planned_duration_min, plan_source, plan_hash
    ) VALUES (
        :id, :facility_id, :beneficiary_id, :caregiver_id,
        :plan_date, :care_type, :planned_start, :planned_end,
        :planned_duration_min, :plan_source, :plan_hash
    )
    RETURNING id
""")


def _insert_single_plan(conn, entry: CarePlanEntry) -> tuple[str, str]:
    """단건 INSERT. (plan_id, plan_hash) 반환. IntegrityError는 호출자가 처리."""
    plan_hash = make_plan_hash(
        entry.facility_id, entry.beneficiary_id, entry.caregiver_id,
        str(entry.plan_date), entry.care_type,
        entry.planned_start.isoformat() if entry.planned_start else "",
        entry.planned_end.isoformat() if entry.planned_end else "",
        entry.planned_duration_min,
    )
    plan_id = str(uuid4())
    conn.execute(INSERT_SQL, {
        "id": plan_id,
        "facility_id": entry.facility_id,
        "beneficiary_id": entry.beneficiary_id,
        "caregiver_id": entry.caregiver_id,
        "plan_date": entry.plan_date,
        "care_type": entry.care_type,
        "planned_start": entry.planned_start,
        "planned_end": entry.planned_end,
        "planned_duration_min": entry.planned_duration_min,
        "plan_source": entry.plan_source,
        "plan_hash": plan_hash,
    })
    return plan_id, plan_hash


# ── POST /entry — 단건 수동 입력 ──────────────────────────────
@router.post(
    "/entry",
    response_model=CarePlanEntryResponse,
    status_code=201,
    summary="케어 계획 단건 수동 입력",
)
async def create_care_plan_entry(entry: CarePlanEntry):
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")
    try:
        with engine.begin() as conn:
            plan_id, plan_hash = _insert_single_plan(conn, entry)
        logger.info(f"[CARE-PLAN] INSERT 완료. id={plan_id}")
        return CarePlanEntryResponse(
            accepted=True, plan_id=plan_id, plan_hash=plan_hash,
            message="케어 계획 등록 완료.",
        )
    except IntegrityError as e:
        if "plan_hash" in str(e).lower():
            raise HTTPException(status_code=409, detail="동일한 케어 계획이 이미 존재합니다.")
        raise HTTPException(status_code=500, detail=f"DB 오류: {e}")


# ── POST /upload — Excel/CSV 다건 적재 ────────────────────────
@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=200,
    summary="케어 계획 Excel/CSV 다건 업로드 (부분 적재 허용)",
)
async def upload_care_plans(file: UploadFile = File(..., description="Excel(.xlsx) 또는 CSV 파일")):
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    content = await file.read()
    filename = file.filename or ""

    # 파싱
    try:
        import pandas as pd
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content))
        else:
            raise HTTPException(status_code=400, detail="지원 형식: .csv, .xlsx, .xls")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"파일 파싱 실패: {e}")

    required_cols = {
        "facility_id", "beneficiary_id", "caregiver_id",
        "plan_date", "care_type", "planned_duration_min",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise HTTPException(status_code=400, detail=f"필수 컬럼 누락: {missing}")

    inserted = 0
    duplicates = 0
    errors = 0
    error_details = []

    for idx, row in df.iterrows():
        try:
            entry = CarePlanEntry(
                facility_id=str(row["facility_id"]),
                beneficiary_id=str(row["beneficiary_id"]),
                caregiver_id=str(row["caregiver_id"]),
                plan_date=pd.Timestamp(row["plan_date"]).date(),
                care_type=str(row["care_type"]),
                planned_start=pd.Timestamp(row["planned_start"]) if pd.notna(row.get("planned_start")) else None,
                planned_end=pd.Timestamp(row["planned_end"]) if pd.notna(row.get("planned_end")) else None,
                planned_duration_min=int(row["planned_duration_min"]),
                plan_source=str(row.get("plan_source", "EXCEL_UPLOAD")),
            )
            with engine.begin() as conn:
                _insert_single_plan(conn, entry)
            inserted += 1
        except IntegrityError:
            duplicates += 1
        except Exception as e:
            errors += 1
            error_details.append({"row": int(idx) + 2, "error": str(e)[:200]})

    logger.info(f"[CARE-PLAN-UPLOAD] total={len(df)} inserted={inserted} dup={duplicates} err={errors}")
    return UploadResponse(
        total_rows=len(df), inserted=inserted,
        duplicates=duplicates, errors=errors,
        error_details=error_details[:50],
    )


# ── GET / — 조회 ──────────────────────────────────────────────
@router.get("/", summary="케어 계획 조회")
async def list_care_plans(
    facility_id: Optional[str] = Query(None),
    beneficiary_id: Optional[str] = Query(None),
    plan_date: Optional[date] = Query(None),
    include_superseded: bool = Query(False),
    limit: int = Query(200, le=1000),
):
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    conditions = []
    params: dict = {"limit": limit}

    if facility_id:
        conditions.append("facility_id = :facility_id")
        params["facility_id"] = facility_id
    if beneficiary_id:
        conditions.append("beneficiary_id = :beneficiary_id")
        params["beneficiary_id"] = beneficiary_id
    if plan_date:
        conditions.append("plan_date = :plan_date")
        params["plan_date"] = plan_date
    if not include_superseded:
        conditions.append("is_superseded = FALSE")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = text(f"""
        SELECT id, facility_id, beneficiary_id, caregiver_id,
               plan_date, care_type, planned_start, planned_end,
               planned_duration_min, plan_source, plan_hash,
               is_superseded, superseded_by, created_at
        FROM public.care_plan_ledger
        {where}
        ORDER BY plan_date DESC, created_at DESC
        LIMIT :limit
    """)

    try:
        with engine.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        records = []
        for r in rows:
            d = dict(r._mapping)
            for k, v in d.items():
                if isinstance(v, (datetime, date)):
                    d[k] = v.isoformat()
            records.append(d)
        return {"records": records, "count": len(records)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"조회 실패: {e}")
