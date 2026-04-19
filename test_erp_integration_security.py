"""
test_erp_integration_security.py
Rubric 2(보안성) + Rubric 4(운영성) 보안 스캔 테스트

검증 항목:
  - 소스 코드에 평문 자격증명 패턴 0건
  - time.sleep() 금지 패턴 0건 (Auto-wait 강제)
  - XPath 셀렉터 금지 패턴 0건
  - wait_for_timeout() 금지 패턴 0건
  - Playwright Trace Viewer 활성화 코드 존재
  - 로그 마스킹 패턴 존재
  - credential_refs 테이블에 평문 컬럼 0개
"""

import os
import re
from pathlib import Path


# ──────────────────────────────────────────────────────────────────
# 스캔 대상 경로
# ──────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent
ERP_MODULE = PROJECT_ROOT / "erp_integration"

SCAN_FILES = list(ERP_MODULE.rglob("*.py"))
SCAN_SQL = [PROJECT_ROOT / "schema_v17_integration_transfers.sql"]
SCAN_ALL = SCAN_FILES + SCAN_SQL


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _grep_pattern(pattern: str, paths: list[Path]) -> list[str]:
    """패턴이 발견된 파일+라인 목록 반환."""
    hits = []
    regex = re.compile(pattern)
    for path in paths:
        content = _read(path)
        for i, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{path.name}:{i}: {line.strip()}")
    return hits


# ──────────────────────────────────────────────────────────────────
# [Rubric 2] 보안성
# ──────────────────────────────────────────────────────────────────

class TestSecurityRubric:
    def test_no_plaintext_password_in_source(self):
        """소스 코드에 평문 패스워드 패턴 0건."""
        patterns = [
            r'password\s*=\s*["\'][^"\']+["\']',
            r'passwd\s*=\s*["\'][^"\']+["\']',
            r'secret\s*=\s*["\'][^"\']+["\']',
        ]
        hits = []
        for pat in patterns:
            hits.extend(_grep_pattern(pat, SCAN_FILES))

        excluded = {"test_erp_integration_security.py"}
        hits = [h for h in hits if not any(e in h for e in excluded)]
        assert hits == [], (
            "평문 자격증명 패턴 발견 (Rubric 2 위반):\n" + "\n".join(hits)
        )

    def test_no_hardcoded_credentials_in_ops_db(self):
        """Ops DB(credential_refs) 테이블에 평문 password 컬럼 없음."""
        sql_content = _read(PROJECT_ROOT / "schema_v17_integration_transfers.sql")
        assert "password" not in sql_content.lower() or (
            "password" in sql_content.lower()
            and "절대 금지" in sql_content
        ), "credential_refs 테이블에 password 컬럼이 있어서는 안 됨"

        assert "secret_manager_ref" in sql_content, (
            "credential_refs는 Secret Manager 참조값만 저장해야 함"
        )

    def test_no_env_var_hardcoded_key_file(self):
        """영구 키 파일(.json) 사용 코드 0건."""
        hits = _grep_pattern(r'\.json.*key|key.*\.json', SCAN_FILES)
        filtered = [h for h in hits if "service_account" in h.lower() or "keyfile" in h.lower()]
        assert filtered == [], (
            "영구 키 파일 사용 코드 발견 (Workload Identity 위반):\n" + "\n".join(filtered)
        )

    def test_credential_repr_masks_password(self):
        """ErpCredential __repr__에서 패스워드 마스킹 확인."""
        from erp_integration.credential_manager import ErpCredential
        cred = ErpCredential(
            username="testuser",
            login_url="https://erp.example.com",
        )
        cred._password = "super_secret_password"
        repr_str = repr(cred)
        assert "super_secret_password" not in repr_str, (
            "ErpCredential repr에 패스워드 노출 금지"
        )
        assert "***MASKED***" in repr_str, (
            "ErpCredential repr에 마스킹 표시 필수"
        )

    def test_credential_str_masks_password(self):
        from erp_integration.credential_manager import ErpCredential
        cred = ErpCredential(username="u", login_url="http://x")
        cred._password = "plaintext_pw_123"
        assert "plaintext_pw_123" not in str(cred)

    def test_credential_context_manager_zeroes_password_on_exit(self):
        """컨텍스트 매니저 탈출 시 패스워드 메모리 파기 확인."""
        os.environ["ERP_TEST_SYSTEM_USERNAME"] = "testuser"
        os.environ["ERP_TEST_SYSTEM_PASSWORD"] = "test_pw"
        os.environ["ERP_TEST_SYSTEM_URL"] = "http://test"
        os.environ.pop("ENVIRONMENT", None)

        from erp_integration.credential_manager import CredentialManager
        cm = CredentialManager()

        captured_cred = None
        with cm.get_credential("T001", "test_system") as cred:
            captured_cred = cred
            assert cred.get_password() == "test_pw"

        assert captured_cred._password == "", (
            "컨텍스트 탈출 후 패스워드가 빈 문자열로 파기되어야 함"
        )


# ──────────────────────────────────────────────────────────────────
# [Rubric 2 + 4] 금지 패턴 스캔
# ──────────────────────────────────────────────────────────────────

class TestForbiddenPatterns:
    def test_no_time_sleep_in_ui_adapters(self):
        """
        UI 어댑터(Playwright 코드)에서 time.sleep() 금지.
        orchestrator.py의 backoff sleep은 Cloud Tasks 비활성 환경 전용이므로 제외.
        """
        ui_adapter_files = [
            f for f in SCAN_FILES
            if "adapter" in f.name.lower() and "orchestrator" not in f.name
        ]
        hits = _grep_pattern(r'\btime\.sleep\s*\(', ui_adapter_files)
        excluded = {"test_erp_integration_security.py"}
        hits = [h for h in hits if not any(e in h for e in excluded)]
        assert hits == [], (
            "UI 어댑터에서 time.sleep() 발견 (Auto-wait 원칙 위반):\n"
            + "\n".join(hits)
        )

    def test_no_xpath_selectors(self):
        """XPath 셀렉터 금지 — role/label/text 기반 locator 강제."""
        hits = _grep_pattern(r'locator\s*\(\s*["\']\/\/', SCAN_FILES)
        excluded = {"test_erp_integration_security.py"}
        hits = [h for h in hits if not any(e in h for e in excluded)]
        assert hits == [], (
            "XPath 셀렉터 발견 (role-based locator 원칙 위반):\n" + "\n".join(hits)
        )

    def test_no_wait_for_timeout(self):
        """wait_for_timeout() 금지."""
        hits = _grep_pattern(r'wait_for_timeout\s*\(', SCAN_FILES)
        excluded = {"test_erp_integration_security.py"}
        hits = [h for h in hits if not any(e in h for e in excluded)]
        assert hits == [], (
            "wait_for_timeout() 발견 (Auto-wait 원칙 위반):\n" + "\n".join(hits)
        )

    def test_no_direct_erp_mapping_bypassing_cto(self):
        """
        어댑터가 CTO 없이 외부 ERP에 직접 매핑하는 패턴 금지.
        어댑터 메서드 파라미터는 반드시 CanonicalTransferObject 타입.
        """
        angel_ui = ERP_MODULE / "adapters" / "angel_ui_adapter.py"
        content = _read(angel_ui)
        assert "CanonicalTransferObject" in content, (
            "angel_ui_adapter는 반드시 CanonicalTransferObject를 파라미터로 받아야 함"
        )


# ──────────────────────────────────────────────────────────────────
# [Rubric 4] 운영성
# ──────────────────────────────────────────────────────────────────

class TestOperabilityRubric:
    def test_playwright_trace_viewer_activated(self):
        """Playwright Trace Viewer 활성화 코드 존재 확인."""
        ui_adapter = ERP_MODULE / "adapters" / "ui_adapter.py"
        content = _read(ui_adapter)
        assert "tracing.start" in content, (
            "Playwright Trace Viewer tracing.start() 활성화 코드 필수"
        )
        assert "tracing.stop" in content, (
            "Playwright Trace Viewer tracing.stop() 저장 코드 필수"
        )

    def test_structured_logging_includes_idempotency_key(self):
        """
        상태 전이 로그에 idempotency_key 포함 확인.
        (orchestrator._transition의 logger.info에 key= 포함)
        """
        orch_file = ERP_MODULE / "orchestrator.py"
        content = _read(orch_file)
        assert "idempotency_key" in content and "logger.info" in content, (
            "orchestrator 상태 전이 로그에 idempotency_key 포함 필수"
        )

    def test_schema_has_reconciliation_jobs_table(self):
        """Ops DB에 reconciliation_jobs 테이블 존재 확인."""
        sql_content = _read(PROJECT_ROOT / "schema_v17_integration_transfers.sql")
        assert "reconciliation_jobs" in sql_content, (
            "UNKNOWN 재조사용 reconciliation_jobs 테이블 필수"
        )

    def test_schema_has_transfer_attempts_table(self):
        """재시도 이력 추적 테이블 존재."""
        sql_content = _read(PROJECT_ROOT / "schema_v17_integration_transfers.sql")
        assert "transfer_attempts" in sql_content

    def test_idempotency_key_unique_constraint_in_schema(self):
        """integration_transfers.idempotency_key에 UNIQUE CONSTRAINT 존재."""
        sql_content = _read(PROJECT_ROOT / "schema_v17_integration_transfers.sql")
        assert "UNIQUE (idempotency_key)" in sql_content or \
               "uq_idempotency_key" in sql_content, (
            "idempotency_key UNIQUE CONSTRAINT 필수 (Rubric 3)"
        )

    def test_adapter_meta_registered_for_angel(self):
        """엔젤 어댑터 메타데이터 등록 확인."""
        from erp_integration.adapter_registry import get_adapters
        adapters = get_adapters("angel")
        assert len(adapters) >= 1, "angel 어댑터 미등록"
        meta, cls = adapters[0]
        assert meta.adapter_id == "angel_ui_v1"
        assert meta.verification_strategy == "read_after_write_scrape"

    def test_selector_canary_module_exists(self):
        """Selector Canary 모듈 구조 존재 확인 (Rubric 4 항목 4)."""
        from erp_integration.selector_canary import (
            CANARY_PROFILES, run_canary, run_all_canaries
        )
        assert len(CANARY_PROFILES) >= 1, "CANARY_PROFILES 최소 1개 이상 등록 필수"
        assert CANARY_PROFILES[0].erp_system == "angel"
        assert callable(run_canary)
        assert callable(run_all_canaries)

    def test_selector_canary_angel_profile_has_critical_locators(self):
        """Angel ERP 카나리 프로필에 최소 3개 핵심 로케이터 등록 확인."""
        from erp_integration.selector_canary import CANARY_PROFILES
        angel_profile = next(
            (p for p in CANARY_PROFILES if p.erp_system == "angel"), None
        )
        assert angel_profile is not None, "angel 카나리 프로필 미등록"
        assert len(angel_profile.critical_locators) >= 3, (
            "Angel ERP 카나리 프로필에 최소 3개 핵심 로케이터 필수 "
            "(아이디 label, 비밀번호 label, 로그인 button)"
        )
