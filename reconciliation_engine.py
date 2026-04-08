"""
Voice Guard — Phase 3 Reconciliation Engine (reconciliation_engine.py)
=======================================================================
3각 검증 엔진: care_plan_ledger × evidence_ledger × billing_ledger

[핵심 설계 원칙]
  1. REPEATABLE READ 스냅샷 위에서 전체 쿼리 실행 → Phantom Read 원천 차단
  2. 4대 특수 룰을 함수 체인으로 분리 → 단일 책임 원칙
  3. DB 룰 테이블(overlap_rule, tolerance_ratio)만 참조 → 하드코딩 0
  4. reconciliation_result Append-Only INSERT → 재검증은 새 row

[4대 특수 룰]
  Rule 1: 3각 매칭 로직 (PHANTOM_BILLING 등)
  Rule 2: Unplanned Care Classifier (야간/긴급 정상 분류)
  Rule 3: Substitution Handler (대타 = SUBSTITUTION 정상)
  Rule 4: Ratio-Based Tolerance & Overlap (DB 테이블 기준)

[Gotcha 적용]
  - CAST(:p AS jsonb) — SQLAlchemy text() JSONB 파라미터 충돌 방지
  - CHAR(64) 컬럼 미사용 (reconciliation_result는 UUID PK)
  - CORS PATCH 허용: reconciliation_api.py에서 선언
"""

import json
import logging
import os
from datetime import date, datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

logger = logging.getLogger("reconciliation_engine")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

DATABASE_URL    = os.getenv("DATABASE_URL")
ENGINE_VERSION  = "1.0"

_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None


# ══════════════════════════════════════════════════════════════
# DB 룰 테이블 로더 (트랜잭션 내에서 호출)
# ══════════════════════════════════════════════════════════════

def _load_overlap_rules(conn) -> dict:
    """
    overlap_rule 테이블 → {(type_a, type_b): bool} 딕셔너리.
    대칭 쌍 자동 생성 (A,B) = (B,A).
    """
    rows = conn.execute(text(
        "SELECT care_type_a, care_type_b, is_overlap_allowed FROM public.overlap_rule"
    )).fetchall()
    rules: dict[tuple, bool] = {}
    for r in rows:
        rules[(r.care_type_a, r.care_type_b)] = r.is_overlap_allowed
        rules[(r.care_type_b, r.care_type_a)] = r.is_overlap_allowed
    return rules


def _load_tolerance_ratios(conn) -> dict:
    """
    tolerance_ratio 테이블 → {care_type: float} 딕셔너리.
    미정의 급여유형은 20% 기본값 적용.
    """
    rows = conn.execute(text(
        "SELECT care_type, tolerance_ratio FROM public.tolerance_ratio"
    )).fetchall()
    return {r.care_type: float(r.tolerance_ratio) for r in rows}


# ══════════════════════════════════════════════════════════════
# Rule 1: 3각 매칭 로직
# ══════════════════════════════════════════════════════════════

def _classify_triangulation(
    has_plan: bool, has_record: bool, has_billing: bool
) -> tuple[str, Optional[str]]:
    """
    (has_plan, has_record, has_billing) 비트 조합으로 1차 분류.

    Returns:
        (result_status, anomaly_code)
        result_status: 'MATCH' | 'PARTIAL' | 'ANOMALY' | 'UNPLANNED'
        anomaly_code:  None 또는 상세 코드 문자열
    """
    combo = (has_plan, has_record, has_billing)

    if combo == (True,  True,  True ):
        return "MATCH",     None                    # 완전 매칭 → 심층 검증 진행
    elif combo == (False, False, True ):
        return "ANOMALY",   "PHANTOM_BILLING"        # 계획·기록 없이 청구 — 최고 위험
    elif combo == (True,  False, True ):
        return "ANOMALY",   "PHANTOM_BILLING"        # 계획은 있으나 기록 없이 청구
    elif combo == (True,  True,  False):
        return "PARTIAL",   "UNBILLED_CARE"          # 케어 실행했으나 미청구
    elif combo == (True,  False, False):
        return "PARTIAL",   "PLANNED_NOT_EXECUTED"   # 계획만 있고 미실행·미청구
    elif combo == (False, True,  False):
        return "UNPLANNED", "UNPLANNED_CARE"         # 무계획 실행 → Rule 2로 세분화
    elif combo == (False, True,  True ):
        return "ANOMALY",   "UNPLANNED_BILLING"      # 무계획 실행+청구
    else:                                            # (False, False, False) 발생 불가
        return "ANOMALY",   "EMPTY_RECORD"


# ══════════════════════════════════════════════════════════════
# Rule 2: Unplanned Care Classifier
# ══════════════════════════════════════════════════════════════

_EMERGENCY_KEYWORDS = frozenset({"응급", "긴급", "EMERGENCY", "URGENT", "EMERG"})


def _classify_unplanned(
    record_time: Optional[datetime],
    care_type: str,
    is_night_care: Optional[bool],
) -> tuple[str, str]:
    """
    계획 없는 케어를 야간/긴급/주간으로 세분류.

    - 야간(22:00~06:00 KST): UNPLANNED_NIGHT  → 정상 (환수 방어)
    - 긴급 care_type 키워드: UNPLANNED_EMERGENCY → 정상
    - 그 외 주간: UNPLANNED_DAYTIME → ANOMALY

    Returns:
        (result_status, anomaly_code)
    """
    # DB canonical_time_fact의 is_night_care 우선 사용 (이미 KST 변환됨)
    if is_night_care is True:
        return "UNPLANNED", "UNPLANNED_NIGHT"

    # record_time 직접 계산 (fallback)
    if record_time is not None:
        hour = (record_time.astimezone(timezone.utc) + timedelta(hours=9)).hour
        if hour >= 22 or hour < 6:
            return "UNPLANNED", "UNPLANNED_NIGHT"

    # 긴급 케어 유형 키워드 탐지
    upper_type = (care_type or "").upper()
    if any(kw in upper_type for kw in _EMERGENCY_KEYWORDS):
        return "UNPLANNED", "UNPLANNED_EMERGENCY"

    return "ANOMALY", "UNPLANNED_DAYTIME"


# ══════════════════════════════════════════════════════════════
# Rule 3: Substitution Handler (대타 담당자)
# ══════════════════════════════════════════════════════════════

def _check_substitution(
    planned_caregiver: Optional[str],
    record_device: Optional[str],
    has_plan: bool,
    has_record: bool,
    planned_min: int,
    billed_min: int,
    tolerance: float,
) -> Optional[str]:
    """
    담당자가 달라도 (대타 출근) 시간 오차 허용 범위 내면 SUBSTITUTION 정상 처리.

    조건:
      1. 계획 + 기록 모두 존재
      2. 담당자 ID가 서로 다름 (caregiver_id ≠ device_id)
      3. 시간 오차가 tolerance 범위 내

    Returns:
        'SUBSTITUTION' 또는 None (해당 없음)
    """
    if not (has_plan and has_record):
        return None

    # 담당자 동일 → 대타 아님
    if not planned_caregiver or not record_device:
        return None
    if planned_caregiver == record_device:
        return None

    # 시간 오차 검사
    if planned_min > 0:
        ratio = abs(billed_min - planned_min) / planned_min
        if ratio <= tolerance:
            return "SUBSTITUTION"

    return None


# ══════════════════════════════════════════════════════════════
# Rule 4: Ratio-Based Tolerance Check
# ══════════════════════════════════════════════════════════════

def _check_tolerance(
    planned_min: int,
    billed_min: int,
    tolerance_ratio: float,
) -> tuple[str, Optional[str], dict]:
    """
    계획 시간 대비 청구 시간의 비율 기반 오차 검증.

    Returns:
        (result_status, anomaly_code, detail_dict)
    """
    if planned_min == 0:
        if billed_min > 0:
            return "ANOMALY", "OVER_BILLING_ZERO_PLAN", {
                "planned_min": 0, "billed_min": billed_min,
            }
        return "MATCH", None, {}

    ratio = (billed_min - planned_min) / planned_min

    if abs(ratio) <= tolerance_ratio:
        return "MATCH", None, {"duration_ratio": round(ratio, 4)}

    if ratio > tolerance_ratio:
        return "ANOMALY", "OVER_BILLING", {
            "planned_min":   planned_min,
            "billed_min":    billed_min,
            "excess_ratio":  round(ratio, 4),
            "allowed_ratio": tolerance_ratio,
        }

    # ratio < -tolerance_ratio → under-billing
    return "PARTIAL", "UNDER_BILLING", {
        "planned_min":    planned_min,
        "billed_min":     billed_min,
        "deficit_ratio":  round(ratio, 4),
        "allowed_ratio":  tolerance_ratio,
    }


# ══════════════════════════════════════════════════════════════
# Rule 4b: 동일 수급자 당일 급여유형 겹침 검사
# ══════════════════════════════════════════════════════════════

def _check_overlap_in_batch(
    day_rows: list[dict],
    overlap_rules: dict,
) -> list[dict]:
    """
    같은 (facility_id, beneficiary_id, fact_date) 그룹 내
    여러 care_type이 있을 때 불허 겹침 조합 탐지.

    overlap_rules에서 is_overlap_allowed=False인 쌍 → ILLEGAL_OVERLAP ANOMALY.

    Returns:
        illegal_pairs: [{"type_a": ..., "type_b": ..., "note": ...}]
    """
    types = [r["care_type"] for r in day_rows if r.get("has_billing")]
    illegal: list[dict] = []

    for i, ta in enumerate(types):
        for tb in types[i + 1:]:
            key = (min(ta, tb), max(ta, tb))
            allowed = overlap_rules.get(key, True)   # 룰 없으면 기본 허용
            if not allowed:
                illegal.append({"type_a": ta, "type_b": tb})

    return illegal


# ══════════════════════════════════════════════════════════════
# 메인 엔진
# ══════════════════════════════════════════════════════════════

def run_reconciliation(
    facility_id: Optional[str] = None,
    target_date: Optional[date] = None,
    dry_run: bool = False,
) -> dict:
    """
    REPEATABLE READ 스냅샷 위에서 3각 검증 전체 실행.

    Args:
        facility_id: 특정 기관만 대상 (None = 전체)
        target_date: 검증 대상 날짜 (None = 어제)
        dry_run:     True면 DB INSERT 없이 결과 리스트만 반환

    Returns:
        {
            "total":     int,
            "match":     int,
            "partial":   int,
            "anomaly":   int,
            "unplanned": int,
            "errors":    int,
            "run_at":    str (ISO),
            "results":   list  (dry_run=True 시에만 채워짐)
        }
    """
    if _engine is None:
        raise RuntimeError("DATABASE_URL 미설정 — DB 연결 불가")

    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    run_at = datetime.now(timezone.utc)
    stats  = {"total": 0, "match": 0, "partial": 0, "anomaly": 0, "unplanned": 0, "errors": 0}
    results: list[dict] = []

    with _engine.begin() as conn:
        # ── REPEATABLE READ 스냅샷 설정 ─────────────────────────
        # Phantom Read 방지: 이 트랜잭션 내 모든 SELECT는 동일 스냅샷 사용
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

        # ── DB 룰 테이블 로드 (스냅샷 내에서) ───────────────────
        overlap_rules    = _load_overlap_rules(conn)
        tolerance_ratios = _load_tolerance_ratios(conn)

        # ── canonical_day_fact 조회 (스냅샷) ────────────────────
        where_parts  = ["fact_date = :target_date"]
        query_params = {"target_date": target_date}

        if facility_id:
            where_parts.append("facility_id = :facility_id")
            query_params["facility_id"] = facility_id

        where_sql = " AND ".join(where_parts)

        day_rows = conn.execute(text(f"""
            SELECT
                facility_id,
                beneficiary_id,
                fact_date,
                care_type,
                planned_caregiver_id,
                record_shift_id,
                has_plan,
                has_record,
                has_billing,
                total_planned_min,
                record_count,
                total_billed_min,
                total_billed_amount,
                first_record_at
            FROM public.canonical_day_fact
            WHERE {where_sql}
            ORDER BY facility_id, beneficiary_id, care_type
        """), query_params).fetchall()

        # ── 겹침 검사용 그룹화 ───────────────────────────────────
        # {(facility_id, beneficiary_id, fact_date): [row_dict, ...]}
        group_map: dict[tuple, list] = {}
        for row in day_rows:
            r = dict(row._mapping)
            key = (r["facility_id"], r["beneficiary_id"], str(r["fact_date"]))
            group_map.setdefault(key, []).append(r)

        # ── 행별 검증 루프 ───────────────────────────────────────
        for row in day_rows:
            stats["total"] += 1
            r = dict(row._mapping)

            try:
                has_plan    = bool(r["has_plan"])
                has_record  = bool(r["has_record"])
                has_billing = bool(r["has_billing"])
                planned_min = int(r["total_planned_min"] or 0)
                billed_min  = int(r["total_billed_min"]  or 0)
                care_type   = r["care_type"] or ""
                tolerance   = tolerance_ratios.get(care_type, 0.20)

                anomaly_detail: dict = {
                    "has_plan":       has_plan,
                    "has_record":     has_record,
                    "has_billing":    has_billing,
                    "planned_min":    planned_min,
                    "billed_min":     billed_min,
                    "record_count":   r["record_count"] or 0,
                    "tolerance_ratio": tolerance,
                }

                # ── Rule 1: 3각 매칭 ──────────────────────────────
                status, anomaly_code = _classify_triangulation(has_plan, has_record, has_billing)

                # ── Rule 2: Unplanned Care 세분화 ─────────────────
                if anomaly_code == "UNPLANNED_CARE":
                    time_row = conn.execute(text("""
                        SELECT record_time, is_night_care
                        FROM public.canonical_time_fact
                        WHERE facility_id    = :fid
                          AND beneficiary_id = :bid
                          AND fact_date      = :fdate
                          AND care_type      = :ctype
                        LIMIT 1
                    """), {
                        "fid":   r["facility_id"],
                        "bid":   r["beneficiary_id"],
                        "fdate": r["fact_date"],
                        "ctype": care_type,
                    }).fetchone()

                    record_time   = time_row.record_time   if time_row else None
                    is_night_care = time_row.is_night_care if time_row else None

                    status, anomaly_code = _classify_unplanned(
                        record_time, care_type, is_night_care
                    )
                    anomaly_detail["record_time"]   = str(record_time)   if record_time else None
                    anomaly_detail["is_night_care"] = is_night_care

                # ── Rule 3: Substitution Handler ──────────────────
                if status == "MATCH":
                    sub_code = _check_substitution(
                        r["planned_caregiver_id"],
                        r["record_shift_id"],
                        has_plan, has_record,
                        planned_min, billed_min, tolerance,
                    )
                    if sub_code == "SUBSTITUTION":
                        anomaly_code = "SUBSTITUTION"
                        anomaly_detail["substitution"] = True

                # ── Rule 4a: Ratio-Based Tolerance ────────────────
                if status == "MATCH" and has_billing and planned_min >= 0:
                    tol_status, tol_code, tol_detail = _check_tolerance(
                        planned_min, billed_min, tolerance
                    )
                    if tol_status != "MATCH":
                        status       = tol_status
                        anomaly_code = tol_code
                        anomaly_detail.update(tol_detail)

                # ── Rule 4b: 급여유형 겹침 검사 ───────────────────
                grp_key      = (r["facility_id"], r["beneficiary_id"], str(r["fact_date"]))
                grp_rows     = group_map.get(grp_key, [])
                illegal_pairs = _check_overlap_in_batch(grp_rows, overlap_rules)
                if illegal_pairs:
                    # 겹침이 있으면 최우선 ANOMALY로 승격
                    status       = "ANOMALY"
                    anomaly_code = "ILLEGAL_OVERLAP"
                    anomaly_detail["illegal_pairs"] = illegal_pairs

                # ── 카운팅 ────────────────────────────────────────
                bucket = status.lower()
                stats[bucket] = stats.get(bucket, 0) + 1

                result_row = {
                    "id":             str(uuid4()),
                    "facility_id":    r["facility_id"],
                    "beneficiary_id": r["beneficiary_id"],
                    "fact_date":      r["fact_date"],
                    "care_type":      care_type,
                    "result_status":  status,
                    "anomaly_code":   anomaly_code,
                    "anomaly_detail": anomaly_detail,
                    "has_plan":       has_plan,
                    "has_record":     has_record,
                    "has_billing":    has_billing,
                    "planned_min":    planned_min,
                    "recorded_count": int(r["record_count"] or 0),
                    "billed_min":     billed_min,
                    "engine_version": ENGINE_VERSION,
                    "run_at":         run_at,
                }

                if dry_run:
                    results.append(result_row)
                else:
                    # ── Append-Only INSERT ──────────────────────
                    # CAST(:anomaly_detail AS jsonb) — SQLAlchemy ::jsonb 충돌 방지
                    conn.execute(text("""
                        INSERT INTO public.reconciliation_result (
                            id, facility_id, beneficiary_id, fact_date, care_type,
                            result_status, anomaly_code, anomaly_detail,
                            has_plan, has_record, has_billing,
                            planned_min, recorded_count, billed_min,
                            engine_version, run_at
                        ) VALUES (
                            :id, :facility_id, :beneficiary_id, :fact_date, :care_type,
                            :result_status, :anomaly_code, CAST(:anomaly_detail AS jsonb),
                            :has_plan, :has_record, :has_billing,
                            :planned_min, :recorded_count, :billed_min,
                            :engine_version, :run_at
                        )
                    """), {
                        **result_row,
                        "anomaly_detail": json.dumps(
                            result_row["anomaly_detail"],
                            ensure_ascii=False,
                            default=str,
                        ),
                    })

                    logger.info(
                        f"[ENGINE] {r['facility_id']}/{r['beneficiary_id']}"
                        f"/{r['fact_date']}/{care_type} → {status}"
                        + (f" [{anomaly_code}]" if anomaly_code else "")
                    )

            except SQLAlchemyError as e:
                logger.error(
                    f"[ENGINE] DB 오류 {r.get('facility_id')}/{r.get('beneficiary_id')}"
                    f"/{r.get('fact_date')}: {e}"
                )
                stats["errors"] += 1

            except Exception as e:
                logger.error(
                    f"[ENGINE] 처리 오류 {r.get('facility_id')}/{r.get('beneficiary_id')}"
                    f"/{r.get('fact_date')}: {e}"
                )
                stats["errors"] += 1

        # dry_run=False 이면 여기서 트랜잭션 COMMIT (engine.begin() 컨텍스트 종료)

    logger.info(f"[ENGINE] 검증 완료 — {stats}")

    return {
        **stats,
        "run_at":  run_at.isoformat(),
        "results": results,     # dry_run=True 시에만 채워짐
    }


# ══════════════════════════════════════════════════════════════
# CLI 진입점
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Voice Guard — Phase 3 Reconciliation Engine"
    )
    parser.add_argument("--facility", help="기관 ID (미입력 시 전체)")
    parser.add_argument(
        "--date",
        help="대상 날짜 YYYY-MM-DD (미입력 시 어제)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 저장 없이 결과만 출력",
    )
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    result = run_reconciliation(
        facility_id=args.facility,
        target_date=target,
        dry_run=args.dry_run,
    )

    print("\n[결과 요약]")
    print(f"  total    : {result['total']}")
    print(f"  match    : {result['match']}")
    print(f"  partial  : {result['partial']}")
    print(f"  anomaly  : {result['anomaly']}")
    print(f"  unplanned: {result['unplanned']}")
    print(f"  errors   : {result['errors']}")
    print(f"  run_at   : {result['run_at']}")

    if args.dry_run and result["results"]:
        print(f"\n[Dry-run 결과 ({len(result['results'])}건)]")
        for r in result["results"]:
            print(
                f"  {r['facility_id']}/{r['beneficiary_id']}"
                f"/{r['fact_date']}/{r['care_type']}"
                f" → {r['result_status']}"
                + (f" [{r['anomaly_code']}]" if r['anomaly_code'] else "")
            )
