"""
erp_integration/adapters/file_adapter.py
Tier 2: File 어댑터 — CSV/XLSX/SFTP 배치 업로드 기반 ERP 연동

API가 없어도 파일 업로드 인터페이스가 있는 ERP에 사용.
UI 자동화보다 훨씬 안정적이므로 UI(Tier 3) 전에 반드시 시도한다.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from erp_integration.adapters.base import AdapterResult, AdapterTier, BaseAdapter
from erp_integration.cto import CanonicalTransferObject

logger = logging.getLogger("voice_guard.erp_integration.file_adapter")


class CsvFileAdapter(BaseAdapter):
    """CSV 파일 생성 + SFTP/HTTP 업로드 기반 어댑터."""

    ADAPTER_TIER = AdapterTier.FILE
    ADAPTER_ID: str = "csv_file_v1"
    ERP_SYSTEM: str = "generic_csv"

    CSV_COLUMNS = [
        "idempotency_key", "facility_id", "beneficiary_id",
        "record_date", "care_type", "detail",
        "recorded_by", "approved_at",
    ]

    def __init__(self, upload_endpoint: str, auth_header: str):
        self._endpoint = upload_endpoint
        self._auth_header = auth_header

    def execute(self, cto: CanonicalTransferObject) -> AdapterResult:
        csv_bytes = self._build_csv(cto)

        try:
            import httpx
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    self._endpoint,
                    content=csv_bytes,
                    headers={
                        "Authorization": self._auth_header,
                        "Content-Type": "text/csv; charset=utf-8",
                        "X-Idempotency-Key": cto.idempotency_key,
                    },
                )

                if response.status_code in (200, 201, 202):
                    return AdapterResult(
                        success=True,
                        external_ref=response.headers.get("X-Upload-ID"),
                        evidence={"http_status": response.status_code},
                    )

                if response.status_code >= 500:
                    return AdapterResult(
                        success=False,
                        error_code="HTTP_5XX",
                        error_message=f"파일 업로드 서버 오류: {response.status_code}",
                        is_retryable=True,
                    )

                return AdapterResult(
                    success=False,
                    error_code="MISSING_REQUIRED_FIELD",
                    error_message=f"업로드 거부: HTTP {response.status_code}",
                    is_retryable=False,
                )

        except Exception as e:
            return AdapterResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=str(e),
                is_retryable=True,
            )

    def read_after_write_verify(
        self, cto: CanonicalTransferObject, external_ref: Optional[str]
    ) -> bool:
        return bool(external_ref)

    def _build_csv(self, cto: CanonicalTransferObject) -> bytes:
        """CTO → CSV 바이트 변환."""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self.CSV_COLUMNS)
        writer.writeheader()

        payload = cto.clinical_payload
        care_fields = payload.model_dump(exclude_none=True)

        for care_type, detail in care_fields.items():
            writer.writerow({
                "idempotency_key": cto.idempotency_key,
                "facility_id": cto.facility_id,
                "beneficiary_id": cto.internal_record_id,
                "record_date": cto.approved_at.strftime("%Y-%m-%d"),
                "care_type": care_type,
                "detail": str(detail),
                "recorded_by": cto.approved_by,
                "approved_at": cto.approved_at.isoformat(),
            })

        return output.getvalue().encode("utf-8-sig")
