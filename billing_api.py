"""
Voice Guard — Billing API v2.0  (Phase 1 - Blind Spot 4)
============================================================
청구(Billing) 원장 수집 엔드포인트

  POST /api/v2/billing/upload  — 건보공단 CSV/Excel 적재 (부분 적재 허용)
  GET  /api/v2/billing         — 조회

핵심 원칙:
  1. billing_hash(SHA-256 UNIQUE) 기반 Idempotency — 중복 적재 원천 차단
  2. 파싱 실패 Row는 무시, 성공 Row는 즉시 커밋 (Partial Insert)
  3. 완전 Append-Only 원장 — UPDATE/DELETE/TRUNCATE 전면 차단

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
logger = logging.getLogger("billing_api")

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None

router = APIRouter(prefix="/api/v2/billing", tags=["청구"])


# ── 해시 생성 ─────────────────────────────────────────────────
def make_billing_hash(
    facility_id: str, beneficiary_id: str, billing_month: str,
    billing_date: str, care_type: str, billed_duration_min: int,
    billing_code: str, billed_amount_krw: int,
) -> str:
    """청구 데이터의 SHA-256 해시. 동일 내용 중복 적재 방지."""
    raw = (
        f"{facility_id}::{beneficiary_id}::{billing_month}::"
        f"{billing_date}::{care_type}::{billed_duration_min}::"
        f"{billing_code}::{billed_amount_krw}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── 응답 모델 ─────────────────────────────────────────────────
class UploadResponse(BaseModel):
    total_rows: int
    inserted: int
    duplicates: int
    errors: int
    error_details: list[dict] = Field(default_factory=list)


# ── INSERT SQL ────────────────────────────────────────────────
INSERT_SQL = text("""
    INSERT INTO public.billing_ledger (
        id, facility_id, beneficiary_id, billing_month,
        billing_date, care_type, billed_duration_min,
        billing_code, billed_amount_krw, claim_status,
        upload_source, billing_hash
    ) VALUES (
        :id, :facility_id, :beneficiary_id, :billing_month,
        :billing_date, :care_type, :billed_duration_min,
        :billing_code, :billed_amount_krw, :claim_status,
        :upload_source, :billing_hash
    )
    RETURNING id
""")


# ── POST /upload — 건보공단 CSV/Excel 적재 ────────────────────
@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=200,
    summary="건보공단 청구 데이터 CSV/Excel 업로드 (부분 적재 허용)",
)
async def upload_billing(file: UploadFile = File(..., description="건보공단 CSV/Excel 파일")):
    if engine is None:
        raise HTTPException(status_code=503, detail="DB 미연결.")

    content = await file.read()
    filename = file.filename or ""

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
        "facility_id", "beneficiary_id", "billing_month",
        "billing_date", "care_type", "billed_duration_min",
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
            facility_id = str(row["facility_id"])
            beneficiary_id = str(row["beneficiary_id"])
            billing_month = str(row["billing_month"]).strip()[:7]  # YYYY-MM
            billing_date_val = pd.Timestamp(row["billing_date"]).date()
            care_type = str(row["care_type"])
            billed_duration_min = int(row["billed_duration_min"])
            billing_code = str(row.get("billing_code", "")) if pd.notna(row.get("billing_code")) else ""
            billed_amount_krw = int(row.get("billed_amount_krw", 0)) if pd.notna(row.get("billed_amount_krw")) else 0
            claim_status = str(row.get("claim_status", "PENDING")) if pd.notna(row.get("claim_status")) else "PENDING"
            upload_source = str(row.get("upload_source", "NHIS_UPLOAD")) if pd.notna(row.get("upload_source")) else "NHIS_UPLOAD"

            billing_hash = make_billing_hash(
                facility_id, beneficiary_id, billing_month,
                str(billing_date_val), care_type, billed_duration_min,
                billing_code, billed_amount_krw,
            )

            with engine.begin() as conn:
                conn.execute(INSERT_SQL, {
                    "id": str(uuid4()),
                    "facility_id": facility_id,
                    "beneficiary_id": beneficiary_id,
                    "billing_month": billing_month,
                    "billing_date": billing_date_val,
                    "care_type": care_type,
                    "billed_duration_min": billed_duration_min,
                    "billing_code": billing_code,
                    "billed_amount_krw": billed_amount_krw,
                    "claim_status": claim_status,
                    "upload_source": upload_source,
                    "billing_hash": billing_hash,
                })
            inserted += 1
        except IntegrityError:
            duplicates += 1
        except Exception as e:
            errors += 1
            error_details.append({"row": int(idx) + 2, "error": str(e)[:200]})

    logger.info(f"[BILLING-UPLOAD] total={len(df)} inserted={inserted} dup={duplicates} err={errors}")
    return UploadResponse(
        total_rows=len(df), inserted=inserted,
        duplicates=duplicates, errors=errors,
        error_details=error_details[:50],
    )


# ── GET / — 조회 ──────────────────────────────────────────────
@router.get("/", summary="청구 데이터 조회")
async def list_billing(
    facility_id: Optional[str] = Query(None),
    beneficiary_id: Optional[str] = Query(None),
    billing_month: Optional[str] = Query(None, description="YYYY-MM 형식"),
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
    if billing_month:
        conditions.append("billing_month = :billing_month")
        params["billing_month"] = billing_month

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = text(f"""
        SELECT id, facility_id, beneficiary_id, billing_month,
               billing_date, care_type, billed_duration_min,
               billing_code, billed_amount_krw, claim_status,
               upload_source, billing_hash, created_at
        FROM public.billing_ledger
        {where}
        ORDER BY billing_month DESC, created_at DESC
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
