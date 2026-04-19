"""
erp_integration/adapters/ui_adapter.py
Tier 3: UI/RPA 어댑터 — Playwright 헤드리스 봇 기반

설계도 결단 2 완전 구현:
  - Playwright Auto-wait 강제 (time.sleep 금지)
  - Role-based locator 강제 (XPath 금지)
  - Robocorp 스캐폴딩 전제 (conda.yaml 의존성)
  - Trace Viewer 자동 활성화
  - UNKNOWN 발생 시 read_after_write_verify로 reconcile 선행

프로덕션 실행 환경: Cloud Run + Robocorp conda.yaml
로컬 개발: playwright install --with-deps chromium 선행 필요
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from erp_integration.adapters.base import AdapterResult, AdapterTier, BaseAdapter
from erp_integration.cto import CanonicalTransferObject

logger = logging.getLogger("voice_guard.erp_integration.ui_adapter")


class PlaywrightUiAdapter(BaseAdapter):
    """
    Playwright 헤드리스 브라우저 기반 UI 자동화 어댑터.

    안티패턴 강제 차단 (보안 스캔으로 자동 검출):
    - 동기 슬립 함수 사용 금지 (Auto-wait 대체 필수)
    - XPath 경로 기반 로케이터 사용 금지 (role/label/text 대체 필수)
    - 타임아웃 하드코딩 금지
    """

    ADAPTER_TIER = AdapterTier.UI
    ADAPTER_ID: str = "playwright_ui_v1"
    ERP_SYSTEM: str = "generic_ui"

    def __init__(
        self,
        login_url: Optional[str] = None,
        headless: bool = True,
        trace_dir: Optional[str] = None,
    ):
        # login_url이 None이면 CredentialManager가 런타임에 cred.login_url을 주입한다.
        self._login_url = login_url
        self._headless = headless
        self._trace_dir = trace_dir or os.environ.get(
            "PLAYWRIGHT_TRACE_DIR", "/tmp/playwright_traces"
        )

    def execute(self, cto: CanonicalTransferObject) -> AdapterResult:
        try:
            from playwright.sync_api import sync_playwright, Error as PlaywrightError
        except ImportError:
            return AdapterResult(
                success=False,
                error_code="MISSING_REQUIRED_FIELD",
                error_message="playwright 미설치. conda.yaml 환경에서 실행 필요.",
                is_retryable=False,
            )

        trace_path = None

        try:
            from erp_integration.credential_manager import CredentialManager
            cm = CredentialManager()

            with cm.get_credential(cto.tenant_id, self.ERP_SYSTEM) as cred:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=self._headless)
                    context = browser.new_context()

                    context.tracing.start(
                        screenshots=True,
                        snapshots=True,
                        sources=True,
                    )

                    try:
                        page = context.new_page()
                        result = self._run_transfer_flow(page, cto, cred)
                        return result

                    except PlaywrightError as e:
                        error_code = self._classify_playwright_error(str(e))
                        return AdapterResult(
                            success=False,
                            error_code=error_code,
                            error_message=str(e),
                            is_retryable=(error_code in {"TIMEOUT", "NETWORK_ERROR"}),
                            trace_artifact_path=trace_path,
                        )
                    finally:
                        trace_path = self._save_trace(context, cto)
                        context.close()
                        browser.close()

        except Exception as e:
            logger.error(f"[UI] 실행 예외: {e}")
            return AdapterResult(
                success=False,
                error_code="TRANSIENT_LOGIN_FAILURE",
                error_message=str(e),
                is_retryable=True,
                trace_artifact_path=trace_path,
            )

    def _run_transfer_flow(self, page, cto, cred) -> AdapterResult:
        """
        서브클래스에서 ERP별로 오버라이드.
        기본 구현은 로그인 + 폼 입력 + 저장 흐름을 추상화.

        필수 패턴:
          page.get_by_role(...)    ← role-based locator
          page.get_by_label(...)   ← label-based locator
          page.get_by_text(...)    ← text-based locator
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}._run_transfer_flow()를 구현해야 합니다."
        )

    def read_after_write_verify(
        self, cto: CanonicalTransferObject, external_ref: Optional[str]
    ) -> bool:
        """
        외부 ERP 조회 기반 존재 확인.
        서브클래스에서 ERP별 조회 로직 구현.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.read_after_write_verify()를 구현해야 합니다."
        )

    def _save_trace(self, context, cto: CanonicalTransferObject) -> Optional[str]:
        try:
            Path(self._trace_dir).mkdir(parents=True, exist_ok=True)
            trace_path = os.path.join(
                self._trace_dir,
                f"trace_{cto.idempotency_key[:12]}_{cto.tenant_id}.zip",
            )
            context.tracing.stop(path=trace_path)
            logger.info(f"[UI] Trace 저장: {trace_path}")
            return trace_path
        except Exception as e:
            logger.warning(f"[UI] Trace 저장 실패 (비치명적): {e}")
            return None

    @staticmethod
    def _classify_playwright_error(error_msg: str) -> str:
        msg_lower = error_msg.lower()
        if "timeout" in msg_lower:
            return "TIMEOUT"
        if "network" in msg_lower or "net::" in msg_lower:
            return "NETWORK_ERROR"
        if "selector" in msg_lower or "locator" in msg_lower:
            return "SELECTOR_BROKEN"
        return "TRANSIENT_LOGIN_FAILURE"
