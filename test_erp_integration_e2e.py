"""
test_erp_integration_e2e.py
E2E 통합 테스트 (Mock 기반) — Rubric 1·3 검증

검증 항목:
  CTO 생성 → Orchestrator 실행 → WORM append → Notion PATCH 전체 흐름
  성공 경로: APPROVED → ... → COMMITTED + WORM MIGRATION_CONFIRMED + Notion 완료
  실패 경로: TERMINAL → MANUAL_REVIEW + WORM MIGRATION_FAILED + Notion 실패
  UNKNOWN 경로: reconcile 선행 후 COMMITTED 또는 MANUAL_REVIEW
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from erp_integration.cto import CanonicalTransferObject, ClinicalPayload
from erp_integration.idempotency import generate_idempotency_key
from erp_integration.orchestrator import IntegrationOrchestrator, TransferState
from erp_integration.adapters.base import AdapterResult


def _make_cto(system="angel", version=1) -> CanonicalTransferObject:
    key = generate_idempotency_key(
        "T001", "F001", "R-E2E-001", version, system, f"{system}_ui_v1"
    )
    return CanonicalTransferObject(
        idempotency_key=key,
        tenant_id="T001",
        facility_id="F001",
        internal_record_id="R-E2E-001",
        record_version=version,
        approved_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        approved_by="supervisor@care.com",
        target_system=system,
        target_adapter_version=f"{system}_ui_v1",
        clinical_payload=ClinicalPayload(
            meal={"description": "죽", "amount": "150g"},
            medication={"drug_name": "혈압약", "dosage": "1정"},
        ),
        legal_hash="b" * 64,
        evidence_refs=["worm-e2e-001"],
    )


def _make_orch():
    mock_ops = MagicMock()
    mock_worm = MagicMock()
    mock_notion = MagicMock()
    return (
        IntegrationOrchestrator(
            ops_db=mock_ops,
            worm_appender=mock_worm,
            notion_patcher=mock_notion,
        ),
        mock_ops, mock_worm, mock_notion
    )


def _mock_adapter(success=True, retryable=False, error_code=None, verify=True):
    adapter = MagicMock()
    adapter.execute.return_value = AdapterResult(
        success=success,
        external_ref="EXT-E2E-001" if success else None,
        is_retryable=retryable,
        error_code=error_code,
        error_message="테스트 오류" if not success else None,
    )
    adapter.read_after_write_verify.return_value = verify
    return adapter


# ──────────────────────────────────────────────────────────────────
# [E2E 1] 성공 경로
# ──────────────────────────────────────────────────────────────────

class TestE2ESuccessPath:
    def test_full_success_ends_in_committed(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(success=True, verify=True)

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            ctx = orch.run(cto)

        assert ctx.state == TransferState.COMMITTED, (
            f"성공 경로는 COMMITTED로 종료해야 함. 실제: {ctx.state}"
        )
        assert ctx.committed_at is not None

    def test_worm_migration_confirmed_appended_on_success(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(success=True, verify=True)

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            orch.run(cto)

        mock_worm.append.assert_called_once()
        kwargs = mock_worm.append.call_args.kwargs
        assert kwargs["event_type"] == "MIGRATION_CONFIRMED", (
            "WORM에 MIGRATION_CONFIRMED append 필수"
        )
        assert kwargs["idempotency_key"] == cto.idempotency_key

    def test_notion_patched_with_completion_on_success(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(success=True, verify=True)

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            orch.run(cto)

        mock_notion.patch.assert_called_once()
        kwargs = mock_notion.patch.call_args.kwargs
        assert "이관 완료" in kwargs.get("status", ""), (
            "Notion PATCH에 '이관 완료' 포함 필수"
        )

    def test_ops_db_state_transitions_recorded(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(success=True, verify=True)

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            orch.run(cto)

        assert mock_ops.record_state_transition.call_count >= 4, (
            "Ops DB에 최소 4번 이상 상태 전이 기록 필요"
        )


# ──────────────────────────────────────────────────────────────────
# [E2E 2] TERMINAL 실패 → 보상 트랜잭션
# ──────────────────────────────────────────────────────────────────

class TestE2ETerminalFailurePath:
    def test_terminal_failure_ends_in_manual_review(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(
            success=False, retryable=False,
            error_code="AUTH_FAILURE"
        )

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            ctx = orch.run(cto)

        assert ctx.state == TransferState.MANUAL_REVIEW_REQUIRED

    def test_terminal_failure_appends_migration_failed_to_worm(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(
            success=False, retryable=False,
            error_code="SELECTOR_BROKEN"
        )

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            orch.run(cto)

        kwargs = mock_worm.append.call_args.kwargs
        assert kwargs["event_type"] == "MIGRATION_FAILED"

    def test_terminal_failure_patches_notion_with_failure_status(self):
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()
        mock_adapter = _mock_adapter(
            success=False, retryable=False,
            error_code="AUTH_FAILURE"
        )

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            orch.run(cto)

        kwargs = mock_notion.patch.call_args.kwargs
        assert "이관 실패" in kwargs.get("status", ""), (
            "보상 트랜잭션: Notion에 '이관 실패' 패치 필수"
        )


# ──────────────────────────────────────────────────────────────────
# [E2E 3] UNKNOWN → reconcile → COMMITTED
# ──────────────────────────────────────────────────────────────────

class TestE2EUnknownOutcomePath:
    def test_unknown_with_reconcile_success_ends_in_committed(self):
        """verify 실패 → reconcile 성공 → COMMITTED"""
        orch, mock_ops, mock_worm, mock_notion = _make_orch()
        cto = _make_cto()

        call_count = {"n": 0}

        def verify_side_effect(cto, external_ref):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False
            return True

        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = AdapterResult(
            success=True, external_ref="EXT-003"
        )
        mock_adapter.read_after_write_verify.side_effect = verify_side_effect

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_reg.return_value = [(MagicMock(), MagicMock(return_value=mock_adapter))]
            ctx = orch.run(cto)

        assert mock_adapter.read_after_write_verify.call_count >= 2, (
            "UNKNOWN 시 reconcile(read_after_write_verify) 반드시 재호출"
        )
        assert ctx.state == TransferState.COMMITTED
