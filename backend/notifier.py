"""
Voice Guard — backend/notifier.py
카카오 알림톡 자동 발송 모듈 (Solapi REST API v4)

[설계 원칙]
  ① 알림 실패 ≠ Ingest 차단: 모든 예외는 logging.error만 처리, 호출자에 미전파
  ② 중복 방지: notification_log 조회 → 동일 ledger_id+trigger_type 재발송 차단
  ③ 발송 이력 WORM: notification_log는 DELETE 차단 트리거로 보존
"""

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy import text

logger = logging.getLogger("voice_guard.notifier")

# ── 환경변수 ──────────────────────────────────────────────────────
SOLAPI_API_KEY         = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET      = os.getenv("SOLAPI_API_SECRET", "")
KAKAO_SENDER_KEY       = os.getenv("KAKAO_SENDER_KEY", "")
SOLAPI_BASE_URL        = "https://api.solapi.com"

ALIMTALK_TPL_NT1       = os.getenv("ALIMTALK_TPL_NT1", "")   # 미기록 임박 알림
ALIMTALK_TPL_NT2       = os.getenv("ALIMTALK_TPL_NT2", "")   # DLQ 긴급 알림
ALIMTALK_TPL_NT3       = os.getenv("ALIMTALK_TPL_NT3", "")   # 현장 확인 요청

ADMIN_PHONE            = os.getenv("ADMIN_PHONE", "")
DEFAULT_FACILITY_PHONE = os.getenv("DEFAULT_FACILITY_PHONE", "")
ALIMTALK_OVERDUE_MINUTES = int(os.getenv("ALIMTALK_OVERDUE_MINUTES", "3"))


# ══════════════════════════════════════════════════════════════════
# 내부 유틸
# ══════════════════════════════════════════════════════════════════

def _build_auth_header() -> str:
    """
    Solapi HMAC-SHA256 인증 헤더 생성.

    [강화 사항 — Phase 7]
    - date: datetime.now(timezone.utc).isoformat() 동등 → UTC Z suffix 강제
      시계 비동기화로 인한 인증 에러 방지
    - salt: os.urandom(16).hex() → crypto.randomBytes(16) 동등, 정확히 32자 hex
      uuid4 기반 salt 대비 진정한 CSPRNG 바이트 보장
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # UTC Z suffix
    salt = os.urandom(16).hex()  # crypto.randomBytes(16) 동등: 32자 hex
    data = date + salt
    signature = hmac.new(
        SOLAPI_API_SECRET.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, "
        f"date={date}, salt={salt}, signature={signature}"
    )


def _call_solapi_raw(phone: str, payload_json: dict, channel: str) -> None:
    """
    Solapi 직접 발송 (채널 분기 포함).
    sharelink_worker 전용 — notification_log 기록 없음.

    channel: 'kakao' → 알림톡, 'lms' → LMS, 'sms' → SMS
    HTTP 오류 또는 네트워크 오류 시 예외 발생 (워커가 처리).
    """
    kakao_options = payload_json.get("kakaoOptions", {})
    message: dict = {"to": phone.replace("-", "")}

    if channel == "kakao" and kakao_options:
        message["kakaoOptions"] = {
            "senderKey":    kakao_options.get("senderKey", KAKAO_SENDER_KEY),
            "templateCode": kakao_options.get("templateCode", ""),
            "variables":    kakao_options.get("variables", {}),
        }
    elif channel == "lms":
        message["type"]    = "LMS"
        message["from"]    = os.getenv("SOLAPI_SENDER_PHONE", "")
        message["subject"] = payload_json.get("subject", "")
        message["text"]    = payload_json.get("text", "")
    else:  # sms
        message["type"] = "SMS"
        message["from"] = os.getenv("SOLAPI_SENDER_PHONE", "")
        message["text"] = payload_json.get("text", "")[:90]  # SMS 90자 제한

    resp = requests.post(
        f"{SOLAPI_BASE_URL}/messages/v4/send",
        headers={
            "Authorization": _build_auth_header(),
            "Content-Type":  "application/json",
        },
        json={"message": message},
        timeout=10,
    )
    resp.raise_for_status()


def _call_solapi(phone: str, template_code: str, variables: dict) -> None:
    """
    Solapi REST API v4 알림톡 발송.
    HTTP 오류 또는 네트워크 오류 시 예외 발생 (호출자가 처리).
    """
    payload = {
        "message": {
            "to": phone.replace("-", ""),
            "kakaoOptions": {
                "senderKey":    KAKAO_SENDER_KEY,
                "templateCode": template_code,
                "variables":    variables,
            },
        }
    }
    resp = requests.post(
        f"{SOLAPI_BASE_URL}/messages/v4/send",
        headers={
            "Authorization": _build_auth_header(),
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()


def _is_already_sent(conn, ledger_id: Optional[str], trigger_type: str) -> bool:
    """중복 발송 방지: 동일 ledger_id + trigger_type 조합이 이미 'sent' 이력 있으면 True"""
    if not ledger_id:
        return False
    row = conn.execute(text("""
        SELECT 1 FROM notification_log
        WHERE ledger_id = :lid::uuid
          AND trigger_type = :tt
          AND status = 'sent'
        LIMIT 1
    """), {"lid": ledger_id, "tt": trigger_type}).fetchone()
    return row is not None


def _log_notification(conn, ledger_id: Optional[str], trigger_type: str,
                       recipient_phone: str, template_code: str,
                       status: str, error_msg: Optional[str] = None) -> None:
    """발송 결과를 notification_log에 INSERT"""
    conn.execute(text("""
        INSERT INTO notification_log
            (id, ledger_id, trigger_type, recipient_phone,
             template_code, status, error_msg, sent_at)
        VALUES
            (gen_random_uuid(),
             :lid::uuid, :tt, :phone, :tpl, :status, :err,
             NOW())
    """), {
        "lid":    ledger_id,
        "tt":     trigger_type,
        "phone":  recipient_phone,
        "tpl":    template_code,
        "status": status,
        "err":    error_msg,
    })


# ══════════════════════════════════════════════════════════════════
# 공개 인터페이스
# ══════════════════════════════════════════════════════════════════

def send_alimtalk(
    engine,
    phone: str,
    template_code: str,
    variables: dict,
    trigger_type: str,
    ledger_id: Optional[str] = None,
) -> bool:
    """
    카카오 알림톡 발송 + notification_log 기록.

    [핵심 보장]
    - 모든 예외를 내부에서 흡수하고 False 반환.
    - 호출자(worker, main)의 주요 플로우를 절대 차단하지 않음.
    - 중복 발송(동일 ledger+trigger) 자동 차단.

    Args:
        engine:        SQLAlchemy 엔진 (notification_log 기록용)
        phone:         수신자 전화번호 (010-xxxx-xxxx 형식 허용)
        template_code: 카카오 알림톡 템플릿 코드
        variables:     템플릿 변수 dict  e.g. {"#{수급자ID}": "A001"}
        trigger_type:  'NT-1' | 'NT-2' | 'NT-3'
        ledger_id:     evidence_ledger UUID (중복 방지 키, None 허용)

    Returns:
        True  — 발송 성공 (또는 정상 중복 스킵)
        False — 발송 실패 (로그 기록됨)
    """
    # ── 사전 조건 검사 ──────────────────────────────────────────
    if not SOLAPI_API_KEY or not KAKAO_SENDER_KEY:
        logger.warning("[NOTIFIER] Solapi/카카오 키 미설정 — 알림톡 스킵")
        return False

    if not phone:
        logger.warning(
            f"[NOTIFIER] 수신자 번호 없음 — trigger={trigger_type} ledger={ledger_id}"
        )
        return False

    if not template_code:
        logger.warning(
            f"[NOTIFIER] 템플릿 코드 미설정 — trigger={trigger_type}"
        )
        return False

    # ── 발송 + 로그 (단일 트랜잭션) ────────────────────────────
    try:
        with engine.begin() as conn:
            if _is_already_sent(conn, ledger_id, trigger_type):
                logger.info(
                    f"[NOTIFIER] 중복 스킵: ledger={ledger_id} trigger={trigger_type}"
                )
                return True

            _call_solapi(phone, template_code, variables)
            _log_notification(
                conn, ledger_id, trigger_type, phone, template_code, "sent"
            )

        logger.info(
            f"[NOTIFIER] ✅ 발송 완료: trigger={trigger_type} "
            f"phone=****{phone[-4:]} ledger={ledger_id}"
        )
        return True

    except Exception as e:
        logger.error(
            f"[NOTIFIER] ❌ 발송 실패: trigger={trigger_type} "
            f"ledger={ledger_id} — {e}"
        )
        # 실패 이력 기록 (별도 커넥션 — 위 트랜잭션이 롤백됐으므로)
        try:
            with engine.begin() as conn:
                _log_notification(
                    conn, ledger_id, trigger_type, phone, template_code,
                    "failed", str(e)[:500]
                )
        except Exception as log_err:
            logger.error(f"[NOTIFIER] 실패 이력 기록 오류: {log_err}")
        return False
