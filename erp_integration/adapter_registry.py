"""
erp_integration/adapter_registry.py
어댑터 레지스트리 — 설계도 결단 1·2 구현

ERP 식별자 → (Tier 우선순위 순) 어댑터 목록을 관리.
메타데이터 계약이 등록되지 않은 어댑터는 실행 불가.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from erp_integration.adapters.base import AdapterTier, BaseAdapter

logger = logging.getLogger("voice_guard.erp_integration.registry")


@dataclass
class AdapterMeta:
    """
    어댑터 메타데이터 계약 — 코드보다 이것이 먼저 완성되어야 한다.
    미등록 메타데이터 = 실행 불가.
    """
    adapter_id: str
    adapter_type: AdapterTier
    erp_system: str
    supported_entities: list[str]
    mapping_version: str
    selector_profile_version: str
    login_strategy: str
    rate_limit_profile: dict
    verification_strategy: str
    evidence_policy: str
    recoverability_rules: dict


_REGISTRY: dict[str, list[tuple[AdapterMeta, type[BaseAdapter]]]] = {}


def register_adapter(meta: AdapterMeta, adapter_cls: type[BaseAdapter]) -> None:
    """어댑터를 레지스트리에 등록. 메타데이터 없으면 등록 거부."""
    key = meta.erp_system
    _REGISTRY.setdefault(key, [])

    tier_priority = {
        AdapterTier.API: 0,
        AdapterTier.FILE: 1,
        AdapterTier.UI: 2,
        AdapterTier.DESKTOP_VNC: 3,
    }

    _REGISTRY[key].append((meta, adapter_cls))
    _REGISTRY[key].sort(key=lambda x: tier_priority[x[0].adapter_type])

    logger.info(
        f"[REGISTRY] 등록: erp={meta.erp_system} "
        f"tier={meta.adapter_type} id={meta.adapter_id}"
    )


def get_adapters(erp_system: str) -> list[tuple[AdapterMeta, type[BaseAdapter]]]:
    """ERP 시스템에 등록된 어댑터 목록 반환 (Tier 우선순위 순)."""
    result = _REGISTRY.get(erp_system, [])
    if not result:
        raise KeyError(
            f"어댑터 미등록: erp_system='{erp_system}'. "
            f"등록된 시스템: {list(_REGISTRY.keys())}"
        )
    return result


def list_registered_systems() -> list[str]:
    return list(_REGISTRY.keys())


def _bootstrap_default_adapters() -> None:
    """기본 어댑터 등록."""
    from erp_integration.adapters.angel_ui_adapter import AngelUiAdapter

    angel_meta = AdapterMeta(
        adapter_id="angel_ui_v1",
        adapter_type=AdapterTier.UI,
        erp_system="angel",
        supported_entities=["care_note", "medication", "meal_record", "excretion"],
        mapping_version="1.0.0",
        selector_profile_version="2026-04-19",
        login_strategy="id_pw_form",
        rate_limit_profile={"max_rps": 2, "max_concurrent": 1},
        verification_strategy="read_after_write_scrape",
        evidence_policy="screenshot_on_success_and_failure",
        recoverability_rules={
            "retryable": ["TIMEOUT", "NETWORK_ERROR", "TRANSIENT_LOGIN_FAILURE"],
            "terminal": ["AUTH_FAILURE", "SELECTOR_BROKEN", "MISSING_REQUIRED_FIELD"],
        },
    )
    register_adapter(angel_meta, AngelUiAdapter)


_bootstrap_default_adapters()
