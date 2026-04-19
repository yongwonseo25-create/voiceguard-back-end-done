"""
erp_integration/cto.py
Canonical Transfer Object (CTO) — 내부 표준 이관 모델

설계도 결단 1: 외부 ERP와 직접 매핑 금지.
모든 데이터는 반드시 이 CTO를 통과한 후 어댑터로 진입한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ClinicalPayload(BaseModel):
    """6대 의무기록 구조화 JSON."""
    meal: Optional[dict] = None
    medication: Optional[dict] = None
    excretion: Optional[dict] = None
    position_change: Optional[dict] = None
    hygiene: Optional[dict] = None
    special_note: Optional[dict] = None


class CanonicalTransferObject(BaseModel):
    """
    모든 ERP 어댑터의 진입 표준 모델.

    어댑터는 이 객체만 수신한다.
    외부 ERP 종류에 따른 분기는 orchestrator/adapter_registry가 담당.
    """
    idempotency_key: str = Field(
        description="sha256(6개 필드 조합) — erp_integration.idempotency로 생성"
    )
    tenant_id: str
    facility_id: str
    internal_record_id: str
    record_version: int = Field(ge=1)
    approved_at: datetime
    approved_by: str
    target_system: str = Field(
        description="angel|carefo|wiseman 등 소문자 식별자"
    )
    target_adapter_version: str = Field(
        description="어댑터 레지스트리의 adapter_id와 일치해야 함"
    )
    clinical_payload: ClinicalPayload
    legal_hash: str = Field(
        description="WORM 원장 row의 SHA-256 해시 (봉인 증거)"
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="WORM 원장 row ID 목록"
    )

    @model_validator(mode="after")
    def validate_idempotency_key_format(self) -> "CanonicalTransferObject":
        if len(self.idempotency_key) != 64:
            raise ValueError(
                f"idempotency_key는 SHA-256 (64자) 형식이어야 함. "
                f"현재 길이: {len(self.idempotency_key)}"
            )
        return self

    @model_validator(mode="after")
    def validate_clinical_payload_not_all_none(self) -> "CanonicalTransferObject":
        payload = self.clinical_payload
        all_none = all(
            getattr(payload, f) is None
            for f in ["meal", "medication", "excretion",
                      "position_change", "hygiene", "special_note"]
        )
        if all_none:
            raise ValueError("clinical_payload: 6대 의무기록 중 최소 1개 필드 필수")
        return self
