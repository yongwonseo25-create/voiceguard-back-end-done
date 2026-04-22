"""
pre-commit 훅 진입점 — cert_renderer.py JSON 스키마 검증기.

역할: cert_renderer.py가 수정될 때마다 SAMPLE_SEAL_DATA로 샘플 JSON을
      생성하여 CERT_JSON_SCHEMA 통과 여부를 검증한다.
      실패 시 sys.exit(1) → git commit 원천 차단.

사용:
  python backend/validate_cert_schema.py        # 직접 실행 (CI/CD)
  pre-commit run validate-cert-json-schema      # pre-commit 훅 실행
"""

import json
import sys

import jsonschema


def main() -> int:
    try:
        from cert_renderer import (
            CERT_JSON_SCHEMA,
            SAMPLE_SEAL_DATA,
            render_json_certificate,
        )
    except ImportError as e:
        print(f"[HOOK] ❌ cert_renderer 임포트 실패: {e}", file=sys.stderr)
        return 1

    try:
        json_bytes = render_json_certificate(SAMPLE_SEAL_DATA)
    except Exception as e:
        print(f"[HOOK] ❌ render_json_certificate 실패: {e}", file=sys.stderr)
        return 1

    try:
        doc = json.loads(json_bytes)
    except json.JSONDecodeError as e:
        print(f"[HOOK] ❌ JSON 파싱 실패: {e}", file=sys.stderr)
        return 1

    try:
        jsonschema.validate(doc, CERT_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as e:
        print(f"[HOOK] ❌ JSON schema FAILED: {e.message}", file=sys.stderr)
        print(f"       경로: {list(e.absolute_path)}", file=sys.stderr)
        return 1
    except jsonschema.SchemaError as e:
        print(f"[HOOK] ❌ 스키마 자체 오류: {e.message}", file=sys.stderr)
        return 1

    # cert_self_hash 정합성 재검증
    self_hash_in_doc  = doc.get("cert_self_hash", "")
    doc_for_hash      = {k: v for k, v in doc.items() if k != "cert_self_hash"}
    import hashlib
    expected_hash     = hashlib.sha256(
        json.dumps(doc_for_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if self_hash_in_doc != expected_hash:
        print("[HOOK] ❌ cert_self_hash 불일치 — 검증서 무결성 파손", file=sys.stderr)
        return 1

    print("[HOOK] OK JSON schema validation passed - cert_self_hash verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
