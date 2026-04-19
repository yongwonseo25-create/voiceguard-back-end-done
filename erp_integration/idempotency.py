"""
erp_integration/idempotency.py
멱등성 키 생성 — 설계도 결단 3 구현

규칙: sha256(tenant_id + facility_id + internal_record_id +
            record_version + target_system + target_adapter_version)

이 키는:
  1. WORM 원장에 봉인
  2. Ops DB integration_transfers에 UNIQUE CONSTRAINT 적용
  3. API ERP면 헤더/request body에 포함
  4. UI ERP면 가능하면 메모/비고 필드에 기록
"""

import hashlib


def generate_idempotency_key(
    tenant_id: str,
    facility_id: str,
    internal_record_id: str,
    record_version: int,
    target_system: str,
    target_adapter_version: str,
) -> str:
    """6개 필드 조합의 SHA-256 64자 hex string 반환."""
    raw = "|".join([
        tenant_id,
        facility_id,
        internal_record_id,
        str(record_version),
        target_system,
        target_adapter_version,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_idempotency_key(key: str, **kwargs) -> bool:
    """제공된 키가 필드들로부터 재생성한 키와 일치하는지 검증."""
    expected = generate_idempotency_key(**kwargs)
    return key == expected
