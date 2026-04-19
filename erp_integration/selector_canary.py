"""
erp_integration/selector_canary.py
Selector Canary — ERP UI 변경 사전 감지 (Rubric 4 항목 4)

설계도 결단 2 운영성 요건:
  "selector canary를 매일 돌려 ERP UI 변경을 사전 감지해야 한다."

동작:
  - 등록된 어댑터의 login_url에 접근하여 핵심 locator(role/label/text)가
    여전히 화면에 존재하는지 확인한다.
  - DOM 구조 변경으로 locator를 찾을 수 없으면 즉시 알람을 발생시킨다.
  - 매일 Cloud Scheduler → Cloud Run Jobs로 실행한다.

⚠️ 실행 환경 제한:
  이 모듈은 Playwright 설치 및 실제 ERP URL 접근이 가능한 환경에서만
  live 실행이 가능하다. 단위 테스트는 구조 존재 여부만 검증한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("voice_guard.erp_integration.selector_canary")


@dataclass
class CanaryCheckResult:
    erp_system: str
    adapter_id: str
    passed: bool
    failed_locators: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CanaryProfile:
    """각 ERP 어댑터의 카나리 검사 프로필."""
    erp_system: str
    adapter_id: str
    login_url: str
    critical_locators: list[dict]


CANARY_PROFILES: list[CanaryProfile] = [
    CanaryProfile(
        erp_system="angel",
        adapter_id="angel_ui_v1",
        login_url="",
        critical_locators=[
            {"type": "label", "name": "아이디"},
            {"type": "label", "name": "비밀번호"},
            {"type": "role", "role": "button", "name": "로그인"},
        ],
    ),
]


def run_canary(profile: CanaryProfile, headless: bool = True) -> CanaryCheckResult:
    """
    단일 ERP 어댑터의 카나리 검사를 실행한다.

    프로덕션: Cloud Scheduler 매일 00:00 UTC → Cloud Run Jobs 실행
    로컬 개발: 직접 호출 (Playwright 설치 필수)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CanaryCheckResult(
            erp_system=profile.erp_system,
            adapter_id=profile.adapter_id,
            passed=False,
            error="playwright 미설치 — conda.yaml 환경에서 실행 필요",
        )

    if not profile.login_url:
        return CanaryCheckResult(
            erp_system=profile.erp_system,
            adapter_id=profile.adapter_id,
            passed=False,
            error="login_url 미설정 — credential_refs에서 URL 주입 필요",
        )

    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            page.goto(profile.login_url, timeout=15000)

            for locator_spec in profile.critical_locators:
                locator_type = locator_spec["type"]
                try:
                    if locator_type == "label":
                        element = page.get_by_label(locator_spec["name"])
                    elif locator_type == "role":
                        element = page.get_by_role(
                            locator_spec["role"],
                            name=locator_spec.get("name"),
                        )
                    elif locator_type == "text":
                        element = page.get_by_text(locator_spec["text"])
                    else:
                        continue

                    element.wait_for(timeout=5000)

                except Exception as e:
                    spec_str = str(locator_spec)
                    failed.append(spec_str)
                    logger.warning(
                        f"[CANARY] locator 미발견: erp={profile.erp_system} "
                        f"locator={spec_str} error={e}"
                    )

        finally:
            page.close()
            browser.close()

    result = CanaryCheckResult(
        erp_system=profile.erp_system,
        adapter_id=profile.adapter_id,
        passed=len(failed) == 0,
        failed_locators=failed,
    )

    if result.passed:
        logger.info(f"[CANARY] PASS: erp={profile.erp_system}")
    else:
        logger.error(
            f"[CANARY] FAIL: erp={profile.erp_system} "
            f"broken_locators={failed}"
        )

    return result


def run_all_canaries(headless: bool = True) -> list[CanaryCheckResult]:
    """등록된 모든 ERP 어댑터에 대해 카나리 검사를 실행한다."""
    results = []
    for profile in CANARY_PROFILES:
        result = run_canary(profile, headless=headless)
        results.append(result)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    logger.info(f"[CANARY] 완료: {passed}/{total} 통과")
    return results
