"""
test_erp_integration_unit.py
Rubric 1(기능성) + Rubric 3(무결성) 단위 테스트

검증 항목:
  - CTO 필수 필드 완전성 (12개)
  - Idempotency Key 생성 규칙 (SHA-256 64자)
  - 동일 입력 2회 → 동일 키 (결정론적)
  - 상태 머신 전이 유효성
  - UNKNOWN 발생 시 즉시 재전송 금지 (reconcile 선행 강제)
  - COMMITTED는 read-after-write 이후에만
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from erp_integration.cto import CanonicalTransferObject, ClinicalPayload
from erp_integration.idempotency import generate_idempotency_key, verify_idempotency_key
from erp_integration.orchestrator import (
    IntegrationOrchestrator, TransferState, VALID_FORWARD_TRANSITIONS
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

def _make_idem_key(
    tenant="T001", facility="F001", record_id="R001",
    version=1, system="angel", adapter="angel_ui_v1"
):
    return generate_idempotency_key(tenant, facility, record_id, version, system, adapter)


def _make_cto(**overrides) -> CanonicalTransferObject:
    defaults = dict(
        idempotency_key=_make_idem_key(),
        tenant_id="T001",
        facility_id="F001",
        internal_record_id="R001",
        record_version=1,
        approved_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        approved_by="admin@test.com",
        target_system="angel",
        target_adapter_version="angel_ui_v1",
        clinical_payload=ClinicalPayload(meal={"description": "쌀밥", "amount": "200g"}),
        legal_hash="a" * 64,
        evidence_refs=["worm-row-001"],
    )
    defaults.update(overrides)
    return CanonicalTransferObject(**defaults)


# ──────────────────────────────────────────────────────────────────
# [Rubric 1] 기능성 테스트
# ──────────────────────────────────────────────────────────────────

class TestIdempotencyKey:
    def test_sha256_output_is_64_chars(self):
        key = _make_idem_key()
        assert len(key) == 64, f"SHA-256 키는 64자여야 함. 실제: {len(key)}"

    def test_deterministic_same_input(self):
        key1 = _make_idem_key()
        key2 = _make_idem_key()
        assert key1 == key2, "동일 입력 → 동일 키 (결정론적)"

    def test_different_version_produces_different_key(self):
        key1 = _make_idem_key(version=1)
        key2 = _make_idem_key(version=2)
        assert key1 != key2, "record_version이 다르면 키가 달라야 함"

    def test_different_system_produces_different_key(self):
        key_angel = _make_idem_key(system="angel")
        key_carefo = _make_idem_key(system="carefo")
        assert key_angel != key_carefo

    def test_verify_returns_true_for_matching_key(self):
        key = _make_idem_key()
        assert verify_idempotency_key(
            key,
            tenant_id="T001", facility_id="F001",
            internal_record_id="R001", record_version=1,
            target_system="angel", target_adapter_version="angel_ui_v1"
        )

    def test_verify_returns_false_for_tampered_key(self):
        key = _make_idem_key()
        tampered = key[:-1] + ("0" if key[-1] != "0" else "1")
        assert not verify_idempotency_key(
            tampered,
            tenant_id="T001", facility_id="F001",
            internal_record_id="R001", record_version=1,
            target_system="angel", target_adapter_version="angel_ui_v1"
        )


class TestCanonicalTransferObject:
    def test_valid_cto_creation(self):
        cto = _make_cto()
        assert cto.tenant_id == "T001"
        assert len(cto.idempotency_key) == 64

    def test_idempotency_key_wrong_length_raises(self):
        with pytest.raises(Exception):
            _make_cto(idempotency_key="short_key")

    def test_all_null_clinical_payload_raises(self):
        with pytest.raises(Exception):
            _make_cto(clinical_payload=ClinicalPayload())

    def test_record_version_zero_raises(self):
        with pytest.raises(Exception):
            _make_cto(record_version=0)

    def test_cto_has_all_12_required_fields(self):
        cto = _make_cto()
        required = [
            "idempotency_key", "tenant_id", "facility_id",
            "internal_record_id", "record_version", "approved_at",
            "approved_by", "target_system", "target_adapter_version",
            "clinical_payload", "legal_hash", "evidence_refs",
        ]
        for field in required:
            assert hasattr(cto, field), f"필수 필드 누락: {field}"


# ──────────────────────────────────────────────────────────────────
# [Rubric 3] 무결성 테스트 — 상태 머신
# ──────────────────────────────────────────────────────────────────

class TestStateMachineTransitions:
    def test_approved_to_queued_allowed(self):
        assert TransferState.QUEUED in VALID_FORWARD_TRANSITIONS[TransferState.APPROVED]

    def test_committed_has_no_forward_transitions(self):
        assert VALID_FORWARD_TRANSITIONS[TransferState.COMMITTED] == []

    def test_manual_review_required_has_no_forward_transitions(self):
        assert VALID_FORWARD_TRANSITIONS[TransferState.MANUAL_REVIEW_REQUIRED] == []

    def test_invalid_transition_raises(self):
        mock_ops = MagicMock()
        mock_worm = MagicMock()
        mock_notion = MagicMock()
        orch = IntegrationOrchestrator(
            ops_db=mock_ops,
            worm_appender=mock_worm,
            notion_patcher=mock_notion,
        )
        from erp_integration.orchestrator import TransferContext
        ctx = TransferContext(cto=_make_cto())
        ctx.state = TransferState.COMMITTED

        with pytest.raises(ValueError):
            orch._transition(ctx, TransferState.QUEUED, "역행 금지 테스트")

    def test_committed_state_only_after_verify(self):
        """
        COMMITTED는 read_after_write_verify() True 이후에만.
        orchestrator가 verify 없이 commit하면 이 테스트가 실패한다.
        """
        mock_ops = MagicMock()
        mock_worm = MagicMock()
        mock_notion = MagicMock()

        orch = IntegrationOrchestrator(
            ops_db=mock_ops,
            worm_appender=mock_worm,
            notion_patcher=mock_notion,
        )

        cto = _make_cto()

        mock_adapter = MagicMock()
        from erp_integration.adapters.base import AdapterResult
        mock_adapter.execute.return_value = AdapterResult(
            success=True, external_ref="EXT-001"
        )
        mock_adapter.read_after_write_verify.return_value = True

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_meta = MagicMock()
            mock_meta.adapter_type.value = "ui"

            mock_adapter_cls = MagicMock(return_value=mock_adapter)
            mock_reg.return_value = [(mock_meta, mock_adapter_cls)]

            ctx = orch.run(cto)

        assert ctx.state == TransferState.COMMITTED, (
            f"read_after_write_verify=True 이후 COMMITTED여야 함. 실제: {ctx.state}"
        )
        mock_adapter.read_after_write_verify.assert_called_once()

    def test_unknown_outcome_triggers_reconcile_not_immediate_resend(self):
        """
        UNKNOWN 발생 시 즉시 재전송 금지 — reconcile(_reconcile) 선행 강제.
        이 테스트가 통과해야만 설계도 결단 3 준수 확인.
        """
        mock_ops = MagicMock()
        mock_worm = MagicMock()
        mock_notion = MagicMock()

        orch = IntegrationOrchestrator(
            ops_db=mock_ops,
            worm_appender=mock_worm,
            notion_patcher=mock_notion,
        )
        cto = _make_cto()

        mock_adapter = MagicMock()
        from erp_integration.adapters.base import AdapterResult
        mock_adapter.execute.return_value = AdapterResult(
            success=True, external_ref="EXT-002"
        )
        mock_adapter.read_after_write_verify.return_value = False

        reconcile_called = {"count": 0}

        def spy_reconcile(ctx, adapter):
            reconcile_called["count"] += 1
            return False

        orch._reconcile = spy_reconcile

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_meta = MagicMock()
            mock_adapter_cls = MagicMock(return_value=mock_adapter)
            mock_reg.return_value = [(mock_meta, mock_adapter_cls)]

            ctx = orch.run(cto)

        assert reconcile_called["count"] >= 1, "UNKNOWN 시 reconcile이 반드시 호출되어야 함"
        assert ctx.state != TransferState.COMMITTED, (
            "reconcile 실패 후 COMMITTED 찍으면 안 됨"
        )

    def test_terminal_failure_triggers_compensation(self):
        """TERMINAL_FAILED → 보상 트랜잭션 발동 (WORM + Notion 업데이트)."""
        mock_ops = MagicMock()
        mock_worm = MagicMock()
        mock_notion = MagicMock()

        orch = IntegrationOrchestrator(
            ops_db=mock_ops,
            worm_appender=mock_worm,
            notion_patcher=mock_notion,
        )
        cto = _make_cto()

        mock_adapter = MagicMock()
        from erp_integration.adapters.base import AdapterResult
        mock_adapter.execute.return_value = AdapterResult(
            success=False,
            error_code="AUTH_FAILURE",
            error_message="자격증명 불일치",
            is_retryable=False,
        )

        with patch("erp_integration.orchestrator.get_adapters") as mock_reg:
            mock_meta = MagicMock()
            mock_adapter_cls = MagicMock(return_value=mock_adapter)
            mock_reg.return_value = [(mock_meta, mock_adapter_cls)]

            ctx = orch.run(cto)

        assert ctx.state == TransferState.MANUAL_REVIEW_REQUIRED
        mock_worm.append.assert_called()
        mock_notion.patch.assert_called()

        worm_call_args = mock_worm.append.call_args
        assert worm_call_args.kwargs.get("event_type") == "MIGRATION_FAILED", (
            "TERMINAL 시 WORM에 MIGRATION_FAILED 이벤트 append 필수"
        )
