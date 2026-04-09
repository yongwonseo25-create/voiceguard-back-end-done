"""
Voice Guard — test_phase7_sharelink.py
Phase 7: sharelink 핵심 로직 단위 테스트

[검증 대상]
  T01: payload_hash SHA-256 결정론적 생성 및 정규화
  T02: ACK 토큰 생성 → 검증 성공
  T03: ACK 토큰 위조 → HTTPException(403)
  T04: ACK 토큰 만료 → HTTPException(403)
  T05: chain_hash 결정론적 생성 (prev_hash 포함)
  T06: chain_hash NULL prev_hash → 빈 문자열 대체
  T07: notifier _build_auth_header UTC Z suffix 강제
  T08: notifier salt 정확히 32자 hex (os.urandom(16) 기반)
  T09: Token Bucket acquire() 속도 제한 동작 확인
  T10: Solapi 에러 코드 분류 (1xxx→retry, 3xxx→lms_fallback)
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

# 경로 설정
sys.path.insert(0, os.path.dirname(__file__))

# SHARELINK_SECRET 환경변수 주입 (테스트 전용)
os.environ.setdefault("SHARELINK_SECRET", "test-secret-key-for-unit-tests")

from sharelink_api import (
    _build_ack_token,
    _sha256_chain,
    _sha256_payload,
    _verify_ack_token,
)
from sharelink_worker import TokenBucket, _classify_error, _extract_solapi_error_code


# ══════════════════════════════════════════════════════════════════
# T01: payload_hash 결정론적 생성
# ══════════════════════════════════════════════════════════════════

def test_payload_hash_deterministic():
    payload = {"worker_id": "W001", "shift_date": "2026-04-09", "type": "CARE_PLAN"}
    h1 = _sha256_payload(payload)
    h2 = _sha256_payload(payload)
    assert h1 == h2, "동일 payload → 동일 hash"
    assert len(h1) == 64, "SHA-256 hex = 64자"


def test_payload_hash_key_order_independent():
    """sort_keys=True → 키 순서 무관 동일 해시"""
    p1 = {"a": 1, "b": 2}
    p2 = {"b": 2, "a": 1}
    assert _sha256_payload(p1) == _sha256_payload(p2)


def test_payload_hash_different_payloads():
    p1 = {"worker_id": "W001"}
    p2 = {"worker_id": "W002"}
    assert _sha256_payload(p1) != _sha256_payload(p2)


# ══════════════════════════════════════════════════════════════════
# T02–T04: ACK 토큰 생성/검증/위조/만료
# ══════════════════════════════════════════════════════════════════

def test_ack_token_valid():
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
    token = _build_ack_token(dispatch_id, expires_at)

    returned_id, returned_expires = _verify_ack_token(token)
    assert returned_id == dispatch_id
    # 초 단위 비교 (microsecond 제외)
    assert abs((returned_expires - expires_at).total_seconds()) < 1


def test_ack_token_tampered_signature():
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
    token = _build_ack_token(dispatch_id, expires_at)

    # 서명 마지막 2자 변조
    tampered = token[:-2] + ("ab" if token[-2:] != "ab" else "cd")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _verify_ack_token(tampered)
    assert exc_info.value.status_code == 403
    assert "서명 불일치" in exc_info.value.detail


def test_ack_token_expired():
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    # 이미 만료된 토큰
    expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    token = _build_ack_token(dispatch_id, expires_at)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _verify_ack_token(token)
    assert exc_info.value.status_code == 403
    assert "만료" in exc_info.value.detail


def test_ack_token_malformed():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _verify_ack_token("malformed-token-no-dot")
    assert exc_info.value.status_code == 403


# ══════════════════════════════════════════════════════════════════
# T05–T06: chain_hash 결정론적 생성
# ══════════════════════════════════════════════════════════════════

def test_chain_hash_deterministic():
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    acked_at = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    prev_hash = "a" * 64

    h1 = _sha256_chain(dispatch_id, "link_clicked", acked_at, None, prev_hash)
    h2 = _sha256_chain(dispatch_id, "link_clicked", acked_at, None, prev_hash)
    assert h1 == h2
    assert len(h1) == 64


def test_chain_hash_null_prev_hash():
    """prev_hash=None → 빈 문자열 대체 → 결정론적"""
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    acked_at = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)

    h_none = _sha256_chain(dispatch_id, "link_clicked", acked_at, None, None)
    h_empty = _sha256_chain(dispatch_id, "link_clicked", acked_at, None, "")
    # None과 "" 모두 동일하게 처리되어야 함
    assert h_none == h_empty


def test_chain_hash_changes_with_dwell():
    """dwell_seconds 변경 → chain_hash 변경"""
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    acked_at = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)

    h_no_dwell = _sha256_chain(dispatch_id, "read_confirmed", acked_at, None, None)
    h_with_dwell = _sha256_chain(dispatch_id, "read_confirmed", acked_at, 30, None)
    assert h_no_dwell != h_with_dwell


def test_chain_hash_sequential_fork_detection():
    """prev_hash 참조로 포크 감지 가능 여부 확인"""
    dispatch_id = "550e8400-e29b-41d4-a716-446655440000"
    t1 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 9, 12, 1, 0, tzinfo=timezone.utc)

    h1 = _sha256_chain(dispatch_id, "link_clicked", t1, None, None)
    h2 = _sha256_chain(dispatch_id, "read_confirmed", t2, 45, h1)

    assert h1 != h2
    # h2 는 h1 에 의존 → h1이 달라지면 h2도 달라짐
    h1_alt = _sha256_chain(dispatch_id, "link_clicked", t1, None, "different")
    h2_alt = _sha256_chain(dispatch_id, "read_confirmed", t2, 45, h1_alt)
    assert h2 != h2_alt


# ══════════════════════════════════════════════════════════════════
# T07–T08: notifier _build_auth_header
# ══════════════════════════════════════════════════════════════════

def test_auth_header_utc_z_suffix():
    """date 필드가 UTC Z suffix 형식인지 확인"""
    import re
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

    os.environ["SOLAPI_API_KEY"] = "test_key"
    os.environ["SOLAPI_API_SECRET"] = "test_secret"

    from backend.notifier import _build_auth_header
    header = _build_auth_header()

    # date=2026-04-09T12:00:00Z 형식 확인
    m = re.search(r'date=(\S+),', header)
    assert m, f"date 필드 없음: {header}"
    date_str = m.group(1)
    assert date_str.endswith("Z"), f"UTC Z suffix 없음: {date_str}"
    # ISO 형식 파싱 가능 여부
    datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")


def test_auth_header_salt_32hex():
    """salt가 정확히 32자 hex 문자열인지 확인 (os.urandom(16).hex() 보장)"""
    import re
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
    from backend.notifier import _build_auth_header

    header = _build_auth_header()
    m = re.search(r'salt=([0-9a-f]+),', header)
    assert m, f"salt 필드 없음: {header}"
    salt = m.group(1)
    assert len(salt) == 32, f"salt 길이 {len(salt)} ≠ 32"
    assert re.fullmatch(r'[0-9a-f]{32}', salt), f"salt 형식 오류: {salt}"


def test_auth_header_salt_randomness():
    """연속 호출 시 salt가 달라야 함 (CSPRNG 보장)"""
    import re
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
    from backend.notifier import _build_auth_header

    salts = set()
    for _ in range(10):
        h = _build_auth_header()
        m = re.search(r'salt=([0-9a-f]+),', h)
        salts.add(m.group(1))
    assert len(salts) == 10, "salt 10회 호출 중 중복 발생 (CSPRNG 위반)"


# ══════════════════════════════════════════════════════════════════
# T09: Token Bucket Rate Limiter
# ══════════════════════════════════════════════════════════════════

def test_token_bucket_allows_burst():
    """초기 용량만큼 즉시 소비 가능"""
    bucket = TokenBucket(rate=10.0, capacity=10.0)
    start = time.monotonic()
    for _ in range(10):
        bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"버스트 10개 소비에 {elapsed:.2f}s 소요 (기대: <0.5s)"


def test_token_bucket_rate_limit():
    """rate=5/s → 5개 소비 후 추가 1개는 ~0.2s 이상 대기"""
    bucket = TokenBucket(rate=5.0, capacity=5.0)
    for _ in range(5):
        bucket.acquire()

    start = time.monotonic()
    bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15, f"rate limit 미작동: {elapsed:.3f}s < 0.15s"


# ══════════════════════════════════════════════════════════════════
# T10: Solapi 에러 코드 분류
# ══════════════════════════════════════════════════════════════════

def test_classify_1xxx_is_retry():
    assert _classify_error("1001") == "retry"
    assert _classify_error("1429") == "retry"


def test_classify_3xxx_is_lms_fallback():
    assert _classify_error("3001") == "lms_fallback"
    assert _classify_error("3010") == "lms_fallback"


def test_classify_none_is_retry():
    assert _classify_error(None) == "retry"
    assert _classify_error("") == "retry"


def test_extract_error_code_from_json():
    error_str = '{"code":"3001","message":"수신자 없음"}'
    assert _extract_solapi_error_code(error_str) == "3001"


def test_extract_error_code_from_status():
    error_str = "HTTP 400 StatusCode=1001 Bad Request"
    assert _extract_solapi_error_code(error_str) == "1001"


def test_extract_error_code_unknown():
    assert _extract_solapi_error_code("unknown error") is None


# ══════════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
