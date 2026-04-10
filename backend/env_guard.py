"""
Voice Guard — backend/env_guard.py
필수 환경변수 / 시크릿 강제화 가드 (TD-01, TD-05)

[원칙]
  - 누락된 환경변수 = 경고가 아닌 RuntimeError로 즉시 종료
  - SECRET_KEY 기본값("CHANGE_ME") = 법적 증거 무효 → 치명적 종료
  - main.py / worker.py 기동 시 반드시 호출 (모듈 로드 시점)

[설계]
  - 이중 노동 제로 — 동일 검증 로직을 여러 파일에 복사 금지
  - 카테고리(role) 단위로 검사: core / worm / ai / notion / alimtalk
  - core는 항상 강제 (DATABASE_URL + SECRET_KEY)
"""

import logging
import os

logger = logging.getLogger("voice_guard.env_guard")


# ── 카테고리별 필수 환경변수 ──────────────────────────────────────
_REQUIRED: dict = {
    "core": [
        "DATABASE_URL",
        "SECRET_KEY",
    ],
    "worm": [
        "B2_KEY_ID",
        "B2_APPLICATION_KEY",
        "B2_BUCKET_NAME",
    ],
    "ai": [
        "GEMINI_API_KEY",
    ],
    "notion": [
        "NOTION_API_KEY",
        "NOTION_DATABASE_ID",
        "NOTION_HANDOVER_DB_ID",
        "NOTION_CARE_RECORD_DB_ID",
    ],
    "alimtalk": [
        "SOLAPI_API_KEY",
        "SOLAPI_API_SECRET",
        "KAKAO_SENDER_KEY",
        "ALIMTALK_TPL_NT1",
        "ALIMTALK_TPL_NT2",
        "ALIMTALK_TPL_NT3",
        "ADMIN_PHONE",
        "DEFAULT_FACILITY_PHONE",
    ],
}

# ── 금지된 기본값 (운영값으로 교체되지 않은 placeholder) ──────────
_FORBIDDEN_DEFAULTS: dict = {
    "SECRET_KEY": {
        "CHANGE_ME", "changeme", "change_me",
        "secret", "default", "todo", "xxx",
    },
}


def check_env_vars(*roles: str) -> None:
    """
    필수 환경변수 검증. 누락 또는 금지 기본값 발견 시 RuntimeError로 즉시 종료.

    Args:
        *roles: 검사할 카테고리 — 'worm' | 'ai' | 'notion' | 'alimtalk'
                'core'는 항상 자동 포함 (생략 가능).

    Raises:
        RuntimeError: 환경변수 검증 실패 시 — 호출자의 기동을 차단

    Example:
        check_env_vars("worm", "ai", "notion", "alimtalk")
    """
    targets: set = set(_REQUIRED["core"])
    for role in roles:
        targets.update(_REQUIRED.get(role, []))

    missing: list = []
    forbidden: list = []

    for var in sorted(targets):
        val = os.getenv(var, "").strip()
        if not val:
            missing.append(var)
            continue
        bad = _FORBIDDEN_DEFAULTS.get(var)
        if bad and val in bad:
            forbidden.append(f"{var}={val!r}")

    if missing or forbidden:
        lines = [
            "",
            "═══════════════════════════════════════════════════════════",
            "🔥 [ENV-GUARD] 치명적: 환경변수 검증 실패 — 기동 중단",
            "═══════════════════════════════════════════════════════════",
        ]
        if missing:
            lines.append(f"  ❌ 누락된 필수 변수 ({len(missing)}건):")
            for v in missing:
                lines.append(f"     - {v}")
        if forbidden:
            lines.append(f"  ❌ 금지된 기본값 사용 ({len(forbidden)}건):")
            for v in forbidden:
                lines.append(f"     - {v}")
        lines.append(
            "  → 법적 증거 인프라는 모든 시크릿이 운영값으로 채워진"
        )
        lines.append(
            "    상태에서만 기동됩니다. .env 파일을 검토하십시오."
        )
        lines.append(
            "═══════════════════════════════════════════════════════════"
        )
        msg = "\n".join(lines)
        logger.critical(msg)
        raise RuntimeError(msg)

    logger.info(
        f"[ENV-GUARD] ✅ 환경변수 검증 통과 — roles={['core'] + list(roles)} "
        f"({len(targets)}개 변수)"
    )
