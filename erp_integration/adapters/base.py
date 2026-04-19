"""
erp_integration/adapters/base.py
어댑터 기본 인터페이스 — 설계도 결단 1·2 구현

모든 어댑터는 이 인터페이스를 구현해야 한다.
어댑터는 CTO 객체만 수신하며, 외부 ERP 포맷으로의 변환만 담당한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from erp_integration.cto import CanonicalTransferObject


class AdapterTier(str, Enum):
    API = "api"
    FILE = "file"
    UI = "ui"
    DESKTOP_VNC = "desktop_vnc"


@dataclass
class AdapterResult:
    """어댑터 실행 결과 — orchestrator가 이 객체로 상태 전이를 판정한다."""
    success: bool
    external_ref: Optional[str] = None
    evidence: Optional[dict] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    is_retryable: bool = False
    trace_artifact_path: Optional[str] = None

    @property
    def is_terminal_failure(self) -> bool:
        return not self.success and not self.is_retryable


RETRYABLE_ERROR_CODES = {
    "TIMEOUT", "NETWORK_ERROR", "HTTP_5XX",
    "TRANSIENT_LOGIN_FAILURE", "DOM_LOADING",
}

TERMINAL_ERROR_CODES = {
    "AUTH_FAILURE", "ACCOUNT_LOCKED",
    "SELECTOR_BROKEN", "MISSING_REQUIRED_FIELD",
    "DUPLICATE_DETECTED", "PERMISSION_DENIED",
}


class BaseAdapter(ABC):
    """
    범용 ERP 어댑터 기본 클래스.

    구현 규칙:
    - execute()는 CTO만 수신한다. 직접 DB/Notion 호출 금지.
    - 성공 판정은 read_after_write_verify()로만 한다.
    - 결과는 AdapterResult로만 반환한다.
    """

    ADAPTER_TIER: AdapterTier
    ADAPTER_ID: str
    ERP_SYSTEM: str

    @abstractmethod
    def execute(self, cto: CanonicalTransferObject) -> AdapterResult:
        """CTO를 받아 외부 ERP에 데이터를 전송하고 AdapterResult를 반환한다."""

    @abstractmethod
    def read_after_write_verify(
        self, cto: CanonicalTransferObject, external_ref: Optional[str]
    ) -> bool:
        """
        외부 ERP에 데이터가 실제로 존재하는지 조회 기반으로 검증.
        COMMITTED 상태는 이 메서드가 True를 반환한 후에만 찍힌다.
        """

    def classify_error(self, error_code: str) -> bool:
        """True = retryable, False = terminal"""
        if error_code in RETRYABLE_ERROR_CODES:
            return True
        if error_code in TERMINAL_ERROR_CODES:
            return False
        return False
