"""
Voice Guard — Phase 3 Reconciliation Engine 적대적 검증
=======================================================
테스트 범위:
  T-01: 3각 매칭 로직 — 8가지 비트 조합 전수 검증
  T-02: Unplanned Care Classifier — 야간/긴급/주간 분류
  T-03: Substitution Handler — 대타/동일인/시간 초과 케이스
  T-04: Ratio-Based Tolerance — 정상/초과/미달/0계획 케이스
  T-05: 급여유형 겹침 (ILLEGAL_OVERLAP) 탐지
  T-06: REPEATABLE READ 트랜잭션 격리 — 쿼리 실행 가능 여부
  T-07: reconciliation_result Append-Only (UPDATE/DELETE 차단)
  T-08: run_reconciliation dry_run 모드 — DB INSERT 없이 결과 반환

[실행]
  pytest test_phase3_reconciliation.py -v
"""

from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# ── 엔진 함수 직접 임포트 ────────────────────────────────────
from reconciliation_engine import (
    _classify_triangulation,
    _classify_unplanned,
    _check_substitution,
    _check_tolerance,
    _check_overlap_in_batch,
    run_reconciliation,
)


# ══════════════════════════════════════════════════════════════
# T-01: 3각 매칭 로직 — 8비트 조합 전수
# ══════════════════════════════════════════════════════════════

class TestTriangulation:
    def test_full_match(self):
        status, code = _classify_triangulation(True, True, True)
        assert status == "MATCH"
        assert code is None

    def test_phantom_billing_no_plan_no_record(self):
        status, code = _classify_triangulation(False, False, True)
        assert status == "ANOMALY"
        assert code == "PHANTOM_BILLING"

    def test_phantom_billing_plan_no_record(self):
        """계획은 있으나 기록 없이 청구 — 최고 위험"""
        status, code = _classify_triangulation(True, False, True)
        assert status == "ANOMALY"
        assert code == "PHANTOM_BILLING"

    def test_unbilled_care(self):
        status, code = _classify_triangulation(True, True, False)
        assert status == "PARTIAL"
        assert code == "UNBILLED_CARE"

    def test_planned_not_executed(self):
        status, code = _classify_triangulation(True, False, False)
        assert status == "PARTIAL"
        assert code == "PLANNED_NOT_EXECUTED"

    def test_unplanned_care(self):
        status, code = _classify_triangulation(False, True, False)
        assert status == "UNPLANNED"
        assert code == "UNPLANNED_CARE"

    def test_unplanned_billing(self):
        status, code = _classify_triangulation(False, True, True)
        assert status == "ANOMALY"
        assert code == "UNPLANNED_BILLING"

    def test_empty_record(self):
        """(False, False, False) — 발생 시 ANOMALY"""
        status, code = _classify_triangulation(False, False, False)
        assert status == "ANOMALY"
        assert code == "EMPTY_RECORD"


# ══════════════════════════════════════════════════════════════
# T-02: Unplanned Care Classifier
# ══════════════════════════════════════════════════════════════

class TestUnplannedClassifier:
    def _kst_time(self, hour: int) -> datetime:
        """KST 기준 특정 시각 생성 (UTC로 저장)"""
        kst = datetime(2026, 4, 9, hour, 0, 0, tzinfo=timezone(timedelta(hours=9)))
        return kst.astimezone(timezone.utc)

    def test_night_care_22h(self):
        """22:00 KST → UNPLANNED_NIGHT"""
        status, code = _classify_unplanned(self._kst_time(22), "VISIT_CARE", None)
        assert status == "UNPLANNED"
        assert code == "UNPLANNED_NIGHT"

    def test_night_care_03h(self):
        """03:00 KST → UNPLANNED_NIGHT"""
        status, code = _classify_unplanned(self._kst_time(3), "VISIT_CARE", None)
        assert status == "UNPLANNED"
        assert code == "UNPLANNED_NIGHT"

    def test_is_night_care_flag_true(self):
        """canonical_time_fact is_night_care=True 플래그 우선"""
        status, code = _classify_unplanned(None, "VISIT_CARE", is_night_care=True)
        assert status == "UNPLANNED"
        assert code == "UNPLANNED_NIGHT"

    def test_emergency_keyword(self):
        """긴급 케어 유형 키워드 → UNPLANNED_EMERGENCY"""
        status, code = _classify_unplanned(
            self._kst_time(14), "긴급방문요양", None
        )
        assert status == "UNPLANNED"
        assert code == "UNPLANNED_EMERGENCY"

    def test_daytime_unplanned(self):
        """14:00 KST 무계획 돌봄 → ANOMALY (UNPLANNED_DAYTIME)"""
        status, code = _classify_unplanned(self._kst_time(14), "VISIT_CARE", None)
        assert status == "ANOMALY"
        assert code == "UNPLANNED_DAYTIME"

    def test_no_record_time(self):
        """record_time도 없고 is_night_care도 None → ANOMALY"""
        status, code = _classify_unplanned(None, "VISIT_CARE", None)
        assert status == "ANOMALY"
        assert code == "UNPLANNED_DAYTIME"


# ══════════════════════════════════════════════════════════════
# T-03: Substitution Handler
# ══════════════════════════════════════════════════════════════

class TestSubstitution:
    def test_substitution_within_tolerance(self):
        """담당자 다름 + 오차 범위 내 → SUBSTITUTION"""
        code = _check_substitution(
            planned_caregiver="CG-001",
            record_device="CG-002",
            has_plan=True,
            has_record=True,
            planned_min=60,
            billed_min=65,
            tolerance=0.20,   # 20% → ±12분 허용
        )
        assert code == "SUBSTITUTION"

    def test_same_caregiver_no_substitution(self):
        """동일 담당자 → None"""
        code = _check_substitution(
            planned_caregiver="CG-001",
            record_device="CG-001",
            has_plan=True, has_record=True,
            planned_min=60, billed_min=65, tolerance=0.20,
        )
        assert code is None

    def test_substitution_exceeds_tolerance(self):
        """담당자 다름이지만 오차 초과 → None (후속 룰 처리)"""
        code = _check_substitution(
            planned_caregiver="CG-001",
            record_device="CG-002",
            has_plan=True, has_record=True,
            planned_min=60, billed_min=90,  # 50% 초과
            tolerance=0.20,
        )
        assert code is None

    def test_no_plan_no_substitution(self):
        """계획 없으면 대타 룰 적용 불가"""
        code = _check_substitution(
            planned_caregiver="CG-001",
            record_device="CG-002",
            has_plan=False, has_record=True,
            planned_min=60, billed_min=65, tolerance=0.20,
        )
        assert code is None


# ══════════════════════════════════════════════════════════════
# T-04: Ratio-Based Tolerance
# ══════════════════════════════════════════════════════════════

class TestToleranceCheck:
    def test_within_tolerance(self):
        """오차 범위 내 → MATCH"""
        status, code, detail = _check_tolerance(60, 68, 0.20)
        assert status == "MATCH"
        assert code is None

    def test_over_billing(self):
        """20% 초과 → OVER_BILLING ANOMALY"""
        status, code, detail = _check_tolerance(60, 85, 0.20)
        assert status == "ANOMALY"
        assert code == "OVER_BILLING"
        assert detail["excess_ratio"] > 0.20

    def test_under_billing(self):
        """20% 미달 → UNDER_BILLING PARTIAL"""
        status, code, detail = _check_tolerance(60, 40, 0.20)
        assert status == "PARTIAL"
        assert code == "UNDER_BILLING"
        assert detail["deficit_ratio"] < -0.20

    def test_zero_plan_with_billing(self):
        """계획 0분인데 청구 있음 → OVER_BILLING_ZERO_PLAN"""
        status, code, detail = _check_tolerance(0, 30, 0.20)
        assert status == "ANOMALY"
        assert code == "OVER_BILLING_ZERO_PLAN"

    def test_zero_plan_zero_billing(self):
        """둘 다 0 → MATCH"""
        status, code, detail = _check_tolerance(0, 0, 0.20)
        assert status == "MATCH"

    def test_exact_boundary(self):
        """정확히 20% 경계값 → MATCH (≤ 허용)"""
        status, code, _ = _check_tolerance(100, 120, 0.20)
        assert status == "MATCH"


# ══════════════════════════════════════════════════════════════
# T-05: 급여유형 겹침 (ILLEGAL_OVERLAP) 탐지
# ══════════════════════════════════════════════════════════════

class TestOverlapCheck:
    _rules = {
        ("BATH_CARE", "VISIT_CARE"):    True,   # 허용
        ("BATH_CARE", "BATH_CARE"):     False,  # 불허
        ("DAYCARE",   "VISIT_CARE"):    False,  # 불허
        ("NURSING",   "WELFARE_EQUIP"): True,   # 허용
    }

    def _make_rows(self, types: list[str]) -> list[dict]:
        return [{"care_type": t, "has_billing": True} for t in types]

    def test_no_overlap_single_type(self):
        """단일 유형 → 겹침 없음"""
        result = _check_overlap_in_batch(self._make_rows(["VISIT_CARE"]), self._rules)
        assert result == []

    def test_allowed_overlap(self):
        """방문요양+방문목욕 허용 → 겹침 없음"""
        result = _check_overlap_in_batch(
            self._make_rows(["VISIT_CARE", "BATH_CARE"]), self._rules
        )
        assert result == []

    def test_illegal_overlap_same_type(self):
        """방문목욕 2회 → ILLEGAL_OVERLAP"""
        result = _check_overlap_in_batch(
            self._make_rows(["BATH_CARE", "BATH_CARE"]), self._rules
        )
        assert len(result) == 1
        assert result[0]["type_a"] == "BATH_CARE"

    def test_illegal_overlap_daycare_visit(self):
        """주야간보호+방문요양 → ILLEGAL_OVERLAP"""
        result = _check_overlap_in_batch(
            self._make_rows(["DAYCARE", "VISIT_CARE"]), self._rules
        )
        assert len(result) == 1

    def test_unknown_pair_defaults_allowed(self):
        """룰에 없는 조합 → 기본 허용 (보수적)"""
        result = _check_overlap_in_batch(
            self._make_rows(["UNKNOWN_A", "UNKNOWN_B"]), self._rules
        )
        assert result == []


# ══════════════════════════════════════════════════════════════
# T-06 ~ T-08: DB 연동 테스트 (mock 사용)
# ══════════════════════════════════════════════════════════════

class TestEngineDB:
    """
    실제 DB 연결 없이 SQLAlchemy 엔진을 mock으로 대체.
    REPEATABLE READ 설정, dry_run INSERT 억제 검증.
    """

    def _make_mock_row(self, has_plan=True, has_record=True, has_billing=True):
        r = MagicMock()
        r._mapping = {
            "facility_id":         "FAC-001",
            "beneficiary_id":      "BEN-001",
            "fact_date":           date(2026, 4, 8),
            "care_type":           "VISIT_CARE",
            "planned_caregiver_id": "CG-001",
            "record_shift_id":     "CG-001",
            "has_plan":            has_plan,
            "has_record":          has_record,
            "has_billing":         has_billing,
            "total_planned_min":   60,
            "record_count":        1,
            "total_billed_min":    65,
            "total_billed_amount": 50000,
            "first_record_at":     None,
        }
        return r

    @patch("reconciliation_engine._engine")
    def test_repeatable_read_called(self, mock_engine):
        """REPEATABLE READ SET 명령이 반드시 실행됨을 검증"""
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        # overlap_rule 조회
        mock_conn.execute.return_value.fetchall.return_value = []

        run_reconciliation(target_date=date(2026, 4, 8), dry_run=True)

        # REPEATABLE READ 설정 호출 확인
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("REPEATABLE READ" in c for c in calls), (
            "REPEATABLE READ가 설정되지 않았습니다."
        )

    @patch("reconciliation_engine._engine")
    def test_dry_run_no_insert(self, mock_engine):
        """dry_run=True → INSERT 호출 0건"""
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [self._make_mock_row()]

        result = run_reconciliation(target_date=date(2026, 4, 8), dry_run=True)

        # INSERT 문이 실행되면 안 됨
        insert_calls = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO public.reconciliation_result" in str(c)
        ]
        assert len(insert_calls) == 0
        assert result["total"] >= 0   # dry_run 실행 자체는 성공

    @patch("reconciliation_engine._engine")
    def test_append_only_no_update_delete(self, mock_engine):
        """
        run_reconciliation 내부에서 UPDATE/DELETE가 실행되지 않음.
        (DB 트리거와 이중 방어)
        """
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        run_reconciliation(target_date=date(2026, 4, 8), dry_run=False)

        bad_calls = [
            c for c in mock_conn.execute.call_args_list
            if any(kw in str(c).upper() for kw in ["UPDATE ", "DELETE ", "TRUNCATE "])
        ]
        assert bad_calls == [], f"금지된 DML 실행됨: {bad_calls}"
