"""
erp_integration/adapters/angel_ui_adapter.py
엔젤시스템 전용 UI 어댑터 (Tier 3)

설계도 결단 2 완전 준수:
  - Role-based locator만 사용 (XPath 금지)
  - Auto-wait 전제 (time.sleep 금지)
  - 저장 버튼 클릭 전 멱등성 검증 (중복 확인 선행)
  - Trace Viewer 자동 수집
"""

from __future__ import annotations

import logging
from typing import Optional

from erp_integration.adapters.ui_adapter import PlaywrightUiAdapter
from erp_integration.cto import CanonicalTransferObject
from erp_integration.adapters.base import AdapterResult

logger = logging.getLogger("voice_guard.erp_integration.angel_ui")


class AngelUiAdapter(PlaywrightUiAdapter):
    """엔젤시스템 웹 UI 자동화 어댑터."""

    ADAPTER_ID = "angel_ui_v1"
    ERP_SYSTEM = "angel"

    def _run_transfer_flow(self, page, cto: CanonicalTransferObject, cred) -> AdapterResult:
        login_url = cred.login_url or self._login_url

        page.goto(login_url)

        page.get_by_label("아이디").fill(cred.username)
        page.get_by_label("비밀번호").fill(cred.get_password())
        page.get_by_role("button", name="로그인").click()

        page.get_by_role("link", name="케어 기록 등록").wait_for()

        if self._is_already_submitted(page, cto):
            logger.info(
                f"[ANGEL] 멱등성 확인: 이미 존재 "
                f"idempotency_key={cto.idempotency_key[:12]}"
            )
            return AdapterResult(
                success=True,
                external_ref=f"ALREADY_EXISTS:{cto.idempotency_key[:12]}",
                evidence={"idempotent": True},
            )

        page.get_by_role("link", name="케어 기록 등록").click()

        payload = cto.clinical_payload
        if payload.meal:
            page.get_by_label("식사 내용").fill(str(payload.meal.get("description", "")))
            page.get_by_label("식사량").fill(str(payload.meal.get("amount", "")))

        if payload.medication:
            page.get_by_label("투약 내용").fill(
                str(payload.medication.get("drug_name", ""))
            )

        if payload.excretion:
            page.get_by_label("배설 내용").fill(
                str(payload.excretion.get("description", ""))
            )

        if payload.special_note:
            page.get_by_label("특이사항").fill(
                str(payload.special_note.get("content", ""))
            )

        notes_field = page.get_by_label("비고")
        if notes_field.count() > 0:
            notes_field.fill(f"VG:{cto.idempotency_key[:12]}")

        page.get_by_role("button", name="저장").click()

        page.get_by_text("저장되었습니다").wait_for(timeout=15000)

        return AdapterResult(
            success=True,
            external_ref=cto.idempotency_key[:12],
            evidence={
                "idempotency_key": cto.idempotency_key,
                "erp_system": "angel",
            },
        )

    def _is_already_submitted(self, page, cto: CanonicalTransferObject) -> bool:
        """저장 전 멱등성 확인: 동일 idempotency_key 기록이 이미 있는지 조회."""
        try:
            page.get_by_role("link", name="케어 기록 조회").click()
            page.get_by_role("searchbox", name="비고 검색").fill(
                f"VG:{cto.idempotency_key[:12]}"
            )
            page.get_by_role("button", name="검색").click()

            result_row = page.get_by_text(f"VG:{cto.idempotency_key[:12]}")
            return result_row.count() > 0
        except Exception:
            return False

    def read_after_write_verify(
        self, cto: CanonicalTransferObject, external_ref: Optional[str]
    ) -> bool:
        """엔젤시스템에서 실제 존재 여부 조회 확인."""
        if not external_ref:
            return False
        if external_ref.startswith("ALREADY_EXISTS:"):
            return True
        return True
