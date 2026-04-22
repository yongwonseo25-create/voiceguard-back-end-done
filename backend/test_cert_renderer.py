"""
[GATE 2] cert_renderer 단위 테스트 — 8개 케이스 전수 통과 필수.
Evaluator 채점 기준 4대 지표(기능성/가독성/위변조방지/보안성) 검증.

실행: pytest backend/test_cert_renderer.py -v
"""

import hashlib
import json
import os
import sys

import jsonschema

# 로컬 임포트 경로 보정 (backend/ 디렉터리에서 실행)
sys.path.insert(0, os.path.dirname(__file__))
from cert_renderer import (
    CERT_JSON_SCHEMA,
    SAMPLE_SEAL_DATA,
    render_json_certificate,
    render_pdf_certificate,
    to_kst_str,
)


# ── TC-01: PDF 바이트 크기 검증 (기능성) ─────────────────────────────
def test_pdf_bytes_not_empty():
    pdf_bytes = render_pdf_certificate(SAMPLE_SEAL_DATA)
    assert isinstance(pdf_bytes, bytes), "PDF 결과가 bytes 타입이어야 한다"
    assert len(pdf_bytes) >= 1024, f"PDF 크기 비정상: {len(pdf_bytes)} bytes (최소 1KB 필요)"
    # PDF 매직 바이트 검증
    assert pdf_bytes[:4] == b"%PDF", "PDF 매직 바이트(%PDF) 없음"


# ── TC-02: PDF 메타데이터에 chain_hash 포함 (위변조방지) ────────────
def test_pdf_contains_chain_hash():
    """
    render_pdf_certificate()는 /Subject 메타데이터에 'chain:{hash}'를 raw 삽입.
    PDF 오브젝트 사전(비압축)에서 chain_hash를 검색하여 존재 여부 확인.
    """
    pdf_bytes = render_pdf_certificate(SAMPLE_SEAL_DATA)
    chain_hash = SAMPLE_SEAL_DATA["chain_hash"]

    # /Subject 메타데이터는 PDF info dict에 raw text로 저장됨 (FlateDecode 비적용)
    needle = f"chain:{chain_hash[:32]}".encode("latin-1")
    assert needle in pdf_bytes, (
        f"PDF /Subject 메타데이터에 chain_hash 앞 32자리가 없음: chain:{chain_hash[:32]}"
    )


# ── TC-03: JSON 스키마 완전성 검증 (기능성) ──────────────────────────
def test_json_schema_valid():
    json_bytes = render_json_certificate(SAMPLE_SEAL_DATA)
    doc = json.loads(json_bytes)
    # 예외 없이 통과해야 함
    jsonschema.validate(doc, CERT_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())


# ── TC-04: cert_self_hash 정합성 검증 (위변조방지) ────────────────────
def test_json_self_hash_correct():
    json_bytes = render_json_certificate(SAMPLE_SEAL_DATA)
    doc = json.loads(json_bytes)

    stored_hash  = doc["cert_self_hash"]
    doc_for_hash = {k: v for k, v in doc.items() if k != "cert_self_hash"}
    expected     = hashlib.sha256(
        json.dumps(doc_for_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    assert stored_hash == expected, (
        f"cert_self_hash 불일치\n  저장값: {stored_hash}\n  기대값: {expected}"
    )


# ── TC-05: JSON에 SECRET_KEY 미노출 (보안성) ─────────────────────────
def test_json_no_secret_key_leak():
    original_secret = os.environ.get("SECRET_KEY", "")
    os.environ["SECRET_KEY"] = "TEST_SECRET_SHOULD_NOT_APPEAR_IN_CERT_xK9mP2"

    try:
        json_bytes = render_json_certificate(SAMPLE_SEAL_DATA)
        content    = json_bytes.decode("utf-8")
        secret     = os.environ["SECRET_KEY"]

        assert secret not in content, (
            "SECRET_KEY가 JSON 검증서에 노출되었습니다! 보안 위반."
        )
    finally:
        if original_secret:
            os.environ["SECRET_KEY"] = original_secret
        else:
            os.environ.pop("SECRET_KEY", None)


# ── TC-06: PDF에 SECRET_KEY 미노출 (보안성) ──────────────────────────
def test_pdf_no_secret_key_leak():
    original_secret = os.environ.get("SECRET_KEY", "")
    test_secret     = "TEST_SECRET_SHOULD_NOT_APPEAR_IN_PDF_xK9mP2"
    os.environ["SECRET_KEY"] = test_secret

    try:
        pdf_bytes = render_pdf_certificate(SAMPLE_SEAL_DATA)
        # PDF 바이너리에서 ASCII 문자열로 검색
        assert test_secret.encode("ascii") not in pdf_bytes, (
            "SECRET_KEY가 PDF 검증서 바이트에 노출되었습니다! 보안 위반."
        )
    finally:
        if original_secret:
            os.environ["SECRET_KEY"] = original_secret
        else:
            os.environ.pop("SECRET_KEY", None)


# ── TC-07: KST 변환 정확성 (가독성) ─────────────────────────────────
def test_kst_conversion():
    utc_str   = "2026-04-22T05:30:15.000Z"
    kst_str   = to_kst_str(utc_str)
    assert "2026-04-22 14:30:15 KST" == kst_str, (
        f"KST 변환 오류: {utc_str} → {kst_str} (기대: 2026-04-22 14:30:15 KST)"
    )


# ── TC-08: 빈 전사 내용에서도 PDF/JSON 렌더링 성공 (기능성) ────────────
def test_render_with_empty_transcript():
    data = {**SAMPLE_SEAL_DATA, "transcript_text": ""}

    # PDF
    pdf_bytes = render_pdf_certificate(data)
    assert len(pdf_bytes) >= 1024, "빈 전사에서 PDF 크기 비정상"

    # JSON
    json_bytes = render_json_certificate(data)
    doc = json.loads(json_bytes)
    jsonschema.validate(doc, CERT_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())
