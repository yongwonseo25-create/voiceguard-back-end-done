"""
Voice Guard — backend/app_check_middleware.py
Firebase App Check 인증 게이트 (UPGRADE-01)

[원칙]
  - Fail-Closed: 토큰 누락 / 검증 실패 / 환경변수 미설정 → 즉시 401. 예외 없음.
  - Dev-Bypass: APP_CHECK_DEV_MODE=true 명시적 설정 시에만 허용. 키 부재로 우회 불가.
  - SDK 전용: firebase_admin.app_check.verify_token() — JWKS 로컬 검증, 외부 REST 호출 없음.
  - 이중 노동 제로: 토큰 검증 로직은 이 파일에만 존재.

[적용 범위]
  - POST /api/v2/ingest (클라이언트 음성 투척)
  - POST /api/v8/care-record (6대 의무기록)
  - /internal/* 경로는 Cloud Run OIDC로 별도 보호 (이 미들웨어 제외)
"""

import json
import logging
import os
from typing import Callable

import firebase_admin
from firebase_admin import app_check, credentials
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("voice_guard.app_check")

# ── Firebase Admin SDK 초기화 (모듈 로드 시 1회) ─────────────────────
_firebase_app: firebase_admin.App | None = None


def _init_firebase() -> firebase_admin.App | None:
    """
    Firebase Admin SDK를 초기화한다.
    FIREBASE_SERVICE_ACCOUNT_JSON 환경변수(JSON 문자열)로 인증.
    실패 시 None 반환 — 호출자가 fail-closed 처리.
    """
    try:
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip()
        if not sa_json or not project_id:
            logger.error(
                "[APP-CHECK] FIREBASE_PROJECT_ID 또는 FIREBASE_SERVICE_ACCOUNT_JSON 미설정 — SDK 초기화 불가"
            )
            return None

        sa_dict = json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)

        # 이미 초기화된 앱이 있으면 재사용
        try:
            return firebase_admin.get_app("voice_guard_app_check")
        except ValueError:
            return firebase_admin.initialize_app(
                cred,
                {"projectId": project_id},
                name="voice_guard_app_check",
            )
    except Exception as exc:
        logger.error(f"[APP-CHECK] Firebase SDK 초기화 실패: {exc}")
        return None


_firebase_app = _init_firebase()


# ── 토큰 검증 (핵심 로직) ─────────────────────────────────────────────
def _verify_token(token: str) -> bool:
    """
    Firebase App Check 토큰을 로컬 JWKS로 검증한다.
    단 하나라도 실패 조건이 충족되면 False 반환 (fail-closed).

    Returns:
        True  — 토큰 유효
        False — 토큰 무효, 누락, SDK 미초기화, 예외 발생 모두 포함
    """
    if not token:
        return False
    if _firebase_app is None:
        logger.warning("[APP-CHECK] SDK 미초기화 상태에서 토큰 검증 요청 — 거부")
        return False
    try:
        app_check.verify_token(token, app=_firebase_app)
        return True
    except Exception as exc:
        logger.warning(f"[APP-CHECK] 토큰 검증 실패: {type(exc).__name__}: {exc}")
        return False


# ── FastAPI Middleware ─────────────────────────────────────────────────
# 보호 대상 경로 prefix (클라이언트가 직접 호출하는 엔드포인트만)
_PROTECTED_PREFIXES = (
    "/api/v2/ingest",
    "/api/v8/care-record",
)


async def app_check_middleware(request: Request, call_next: Callable):
    """
    Starlette/FastAPI 미들웨어 — App Check 게이트.

    [흐름]
    1. 요청 경로가 보호 대상인지 확인
    2. DEV_MODE=true이면 통과 (단, 명시적 env 설정 필수)
    3. X-Firebase-AppCheck 헤더에서 토큰 추출
    4. _verify_token() → False면 즉시 401 반환, call_next 미호출
    """
    path = request.url.path

    # 보호 대상 경로가 아니면 바이패스
    if not any(path.startswith(p) for p in _PROTECTED_PREFIXES):
        return await call_next(request)

    # DEV_MODE: 명시적 env 설정 시에만 허용 (env 부재로 우회 불가)
    dev_mode = os.getenv("APP_CHECK_DEV_MODE", "").strip().lower() == "true"
    if dev_mode:
        logger.warning(
            f"[APP-CHECK] ⚠ DEV_MODE 활성 — 토큰 검증 생략 ({path})"
        )
        return await call_next(request)

    # 토큰 추출 및 검증
    token = request.headers.get("X-Firebase-AppCheck", "").strip()
    if not _verify_token(token):
        logger.warning(
            f"[APP-CHECK] ❌ 접근 거부 — 토큰 무효 또는 누락 "
            f"[{request.client.host if request.client else 'unknown'}] {path}"
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": "App Check 인증 실패",
                "detail": "유효한 Firebase App Check 토큰이 필요합니다.",
            },
        )

    logger.info(f"[APP-CHECK] ✅ 토큰 검증 통과 — {path}")
    return await call_next(request)
