"""
erp_integration/adapters/api_adapter.py
Tier 1: API 어댑터 — REST/SOAP/GraphQL 기반 ERP 연동

우선순위: API → File → UI (설계도 결단 1)
API가 있는 ERP는 반드시 이 어댑터를 먼저 시도한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from erp_integration.adapters.base import (
    AdapterResult, AdapterTier, BaseAdapter,
)
from erp_integration.cto import CanonicalTransferObject

logger = logging.getLogger("voice_guard.erp_integration.api_adapter")


class ApiAdapter(BaseAdapter):
    """REST API 기반 ERP 어댑터."""

    ADAPTER_TIER = AdapterTier.API
    ADAPTER_ID: str = "api_generic_v1"
    ERP_SYSTEM: str = "generic_api"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def execute(self, cto: CanonicalTransferObject) -> AdapterResult:
        payload = self._transform_cto_to_erp_format(cto)

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    f"{self._base_url}/records",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "X-Idempotency-Key": cto.idempotency_key,
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code in (200, 201):
                    data = response.json()
                    return AdapterResult(
                        success=True,
                        external_ref=str(data.get("id") or data.get("record_id")),
                        evidence={"http_status": response.status_code, "response": data},
                    )

                if response.status_code >= 500:
                    return AdapterResult(
                        success=False,
                        error_code="HTTP_5XX",
                        error_message=f"ERP 서버 오류: {response.status_code}",
                        is_retryable=True,
                    )

                return AdapterResult(
                    success=False,
                    error_code="PERMISSION_DENIED" if response.status_code == 403
                              else "MISSING_REQUIRED_FIELD",
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}",
                    is_retryable=False,
                )

        except httpx.TimeoutException:
            return AdapterResult(
                success=False,
                error_code="TIMEOUT",
                error_message="ERP API 응답 타임아웃",
                is_retryable=True,
            )
        except httpx.NetworkError as e:
            return AdapterResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=str(e),
                is_retryable=True,
            )

    def read_after_write_verify(
        self, cto: CanonicalTransferObject, external_ref: Optional[str]
    ) -> bool:
        if not external_ref:
            return False
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    f"{self._base_url}/records/{external_ref}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"[API] read-after-write 검증 실패: {e}")
            return False

    def _transform_cto_to_erp_format(self, cto: CanonicalTransferObject) -> dict:
        """CTO → 대상 ERP API 페이로드 변환. 서브클래스에서 오버라이드."""
        return {
            "tenant_id": cto.tenant_id,
            "facility_id": cto.facility_id,
            "record_id": cto.internal_record_id,
            "idempotency_key": cto.idempotency_key,
            "approved_at": cto.approved_at.isoformat(),
            "clinical_data": cto.clinical_payload.model_dump(exclude_none=True),
        }
