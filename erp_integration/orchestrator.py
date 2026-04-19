"""
erp_integration/orchestrator.py
Integration Orchestrator — 상태 머신 + Saga 패턴 구현
설계도 결단 1·3 완전 구현

상태 전이:
  정상: APPROVED → QUEUED → DISPATCHED → AUTHENTICATED
        → WRITING → SUBMITTED → VERIFYING → COMMITTED
  실패: RETRYABLE_FAILED / UNKNOWN_OUTCOME / TERMINAL_FAILED
        / MANUAL_REVIEW_REQUIRED

핵심 원칙:
  - COMMITTED는 read_after_write_verify() True 이후에만
  - UNKNOWN 발생 시 reconcile 선행, 즉시 재전송 금지
  - TERMINAL 발생 시 보상 트랜잭션 즉시 발동
  - orchestrator만 최종 상태를 찍는다 (worker 직접 난사 금지)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from erp_integration.adapter_registry import get_adapters
from erp_integration.adapters.base import AdapterResult
from erp_integration.cto import CanonicalTransferObject

logger = logging.getLogger("voice_guard.erp_integration.orchestrator")


def _is_cloud_tasks_managed() -> bool:
    """Cloud Tasks 환경에서 실행 중이면 True — in-process sleep 불필요."""
    return bool(os.environ.get("CLOUD_TASKS_QUEUE_NAME"))


class TransferState(str, Enum):
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    AUTHENTICATED = "AUTHENTICATED"
    WRITING = "WRITING"
    SUBMITTED = "SUBMITTED"
    VERIFYING = "VERIFYING"
    COMMITTED = "COMMITTED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    TERMINAL_FAILED = "TERMINAL_FAILED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


VALID_FORWARD_TRANSITIONS = {
    TransferState.APPROVED:         [TransferState.QUEUED],
    TransferState.QUEUED:           [TransferState.DISPATCHED],
    TransferState.DISPATCHED:       [TransferState.AUTHENTICATED,
                                     TransferState.SUBMITTED,
                                     TransferState.RETRYABLE_FAILED,
                                     TransferState.UNKNOWN_OUTCOME,
                                     TransferState.TERMINAL_FAILED],
    TransferState.AUTHENTICATED:    [TransferState.WRITING,
                                     TransferState.SUBMITTED,
                                     TransferState.RETRYABLE_FAILED,
                                     TransferState.TERMINAL_FAILED],
    TransferState.WRITING:          [TransferState.SUBMITTED,
                                     TransferState.RETRYABLE_FAILED,
                                     TransferState.UNKNOWN_OUTCOME,
                                     TransferState.TERMINAL_FAILED],
    TransferState.SUBMITTED:        [TransferState.VERIFYING,
                                     TransferState.UNKNOWN_OUTCOME],
    TransferState.VERIFYING:        [TransferState.COMMITTED,
                                     TransferState.RETRYABLE_FAILED,
                                     TransferState.UNKNOWN_OUTCOME],
    TransferState.RETRYABLE_FAILED: [TransferState.QUEUED],
    TransferState.UNKNOWN_OUTCOME:  [TransferState.VERIFYING,
                                     TransferState.COMMITTED,
                                     TransferState.MANUAL_REVIEW_REQUIRED],
    TransferState.COMMITTED:        [],
    TransferState.TERMINAL_FAILED:  [TransferState.MANUAL_REVIEW_REQUIRED],
    TransferState.MANUAL_REVIEW_REQUIRED: [],
}


@dataclass
class TransferContext:
    """단일 이관 트랜잭션의 런타임 컨텍스트."""
    cto: CanonicalTransferObject
    state: TransferState = TransferState.APPROVED
    attempt_count: int = 0
    last_error: Optional[str] = None
    external_ref: Optional[str] = None
    committed_at: Optional[datetime] = None


class IntegrationOrchestrator:
    """
    Saga 오케스트레이터.

    외부 의존성(WORM, Notion, Ops DB)은 인터페이스로 주입.
    테스트 시 mock 주입 가능.
    """

    MAX_RETRY_ATTEMPTS = 5
    BACKOFF_BASE_SECONDS = 2.0

    def __init__(
        self,
        ops_db=None,
        worm_appender=None,
        notion_patcher=None,
    ):
        self._ops_db = ops_db
        self._worm_appender = worm_appender
        self._notion_patcher = notion_patcher

    def run(self, cto: CanonicalTransferObject) -> TransferContext:
        """
        CTO를 받아 전체 Saga를 실행하고 최종 TransferContext를 반환.

        이 메서드가 COMMITTED 또는 MANUAL_REVIEW_REQUIRED 상태에서 종료되어야
        스프린트 완료 조건이 충족된다.
        """
        ctx = TransferContext(cto=cto)
        self._transition(ctx, TransferState.QUEUED, "이관 큐 진입")

        adapters = get_adapters(cto.target_system)
        if not adapters:
            self._trigger_compensation(ctx, "어댑터 미등록")
            return ctx

        meta, adapter_cls = adapters[0]
        # login_url은 CredentialManager에서 런타임에 주입됨. 생성자에 전달 불필요.
        adapter = adapter_cls()

        while ctx.attempt_count < self.MAX_RETRY_ATTEMPTS:
            ctx.attempt_count += 1
            self._transition(ctx, TransferState.DISPATCHED, f"시도 #{ctx.attempt_count}")

            result: AdapterResult = adapter.execute(cto)

            if result.success:
                ctx.external_ref = result.external_ref
                self._transition(ctx, TransferState.SUBMITTED, "어댑터 성공 신호")

                self._transition(ctx, TransferState.VERIFYING, "Read-after-write 검증 중")
                verified = adapter.read_after_write_verify(cto, result.external_ref)

                if verified:
                    self._commit(ctx)
                    return ctx
                else:
                    self._transition(
                        ctx, TransferState.UNKNOWN_OUTCOME,
                        "외부 ERP 검증 실패 — reconcile 수행"
                    )
                    reconciled = self._reconcile(ctx, adapter)
                    if reconciled:
                        self._commit(ctx)
                        return ctx
                    self._trigger_compensation(ctx, "Reconcile 후에도 미확인")
                    return ctx

            if result.is_terminal_failure:
                ctx.last_error = result.error_message
                self._transition(
                    ctx, TransferState.TERMINAL_FAILED,
                    f"Terminal 오류: {result.error_code}"
                )
                self._trigger_compensation(ctx, result.error_message or "")
                return ctx

            ctx.last_error = result.error_message
            self._transition(
                ctx, TransferState.RETRYABLE_FAILED,
                f"재시도 가능 오류: {result.error_code} (#{ctx.attempt_count})"
            )

            backoff = self.BACKOFF_BASE_SECONDS * (2 ** (ctx.attempt_count - 1))
            backoff = min(backoff, 60.0)
            logger.info(f"[ORCH] Backoff {backoff:.1f}s 후 재시도 (프로덕션: Cloud Tasks가 처리)")
            # 프로덕션에서는 Cloud Tasks의 min_backoff/max_backoff이 재시도 타이밍을 관리한다.
            # 로컬 테스트 시에만 in-process 대기를 허용한다.
            if not _is_cloud_tasks_managed():
                import time as _time
                _time.sleep(backoff)

            self._transition(ctx, TransferState.QUEUED, "재시도 큐 복귀")

        self._trigger_compensation(ctx, f"최대 재시도 횟수 초과: {self.MAX_RETRY_ATTEMPTS}회")
        return ctx

    def _commit(self, ctx: TransferContext) -> None:
        """Read-after-write 확인 후에만 호출되는 최종 커밋."""
        ctx.committed_at = datetime.now(timezone.utc)
        self._transition(ctx, TransferState.COMMITTED, "외부 ERP 존재 확인 완료")

        if self._worm_appender:
            self._worm_appender.append(
                event_type="MIGRATION_CONFIRMED",
                idempotency_key=ctx.cto.idempotency_key,
                external_ref=ctx.external_ref,
                committed_at=ctx.committed_at,
            )

        if self._notion_patcher:
            self._notion_patcher.patch(
                internal_record_id=ctx.cto.internal_record_id,
                status=f"이관 완료: {ctx.cto.target_system} / {ctx.committed_at.isoformat()}",
            )

        logger.info(
            f"[ORCH] COMMITTED: idempotency_key={ctx.cto.idempotency_key[:12]} "
            f"external_ref={ctx.external_ref}"
        )

    def _trigger_compensation(self, ctx: TransferContext, reason: str) -> None:
        """
        보상 트랜잭션 (Backward Recovery).
        TERMINAL_FAILED 또는 복구 불가 UNKNOWN 발생 시 즉시 발동.
        """
        self._transition(ctx, TransferState.MANUAL_REVIEW_REQUIRED, reason)

        if self._worm_appender:
            self._worm_appender.append(
                event_type="MIGRATION_FAILED",
                idempotency_key=ctx.cto.idempotency_key,
                reason=reason,
            )

        if self._notion_patcher:
            self._notion_patcher.patch(
                internal_record_id=ctx.cto.internal_record_id,
                status="이관 실패 (담당자 점검 필요)",
                error=reason,
            )

        logger.error(
            f"[ORCH] 보상 트랜잭션 발동: "
            f"idempotency_key={ctx.cto.idempotency_key[:12]} reason={reason}"
        )

    def _reconcile(self, ctx: TransferContext, adapter) -> bool:
        """
        UNKNOWN_OUTCOME 처리 — reconcile 선행 (즉시 재전송 금지).
        외부 ERP에 자연키 기반 조회를 수행하여 존재 여부 판정.
        """
        logger.info(
            f"[ORCH] RECONCILE 시작: idempotency_key={ctx.cto.idempotency_key[:12]}"
        )
        try:
            return adapter.read_after_write_verify(ctx.cto, ctx.external_ref)
        except Exception as e:
            logger.error(f"[ORCH] RECONCILE 실패: {e}")
            return False

    def _transition(
        self, ctx: TransferContext, new_state: TransferState, reason: str
    ) -> None:
        allowed = VALID_FORWARD_TRANSITIONS.get(ctx.state, [])
        if new_state not in allowed:
            raise ValueError(
                f"상태 전이 불가: {ctx.state} → {new_state}. 허용: {allowed}"
            )

        old_state = ctx.state
        ctx.state = new_state

        logger.info(
            f"[ORCH] 상태 전이: {old_state} → {new_state} | {reason} | "
            f"key={ctx.cto.idempotency_key[:12]}"
        )

        if self._ops_db:
            self._ops_db.record_state_transition(
                idempotency_key=ctx.cto.idempotency_key,
                from_state=old_state,
                to_state=new_state,
                reason=reason,
                attempt_count=ctx.attempt_count,
            )
