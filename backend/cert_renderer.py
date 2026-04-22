"""
Voice Guard 증거 검증서 렌더링 모듈 v1.0.0
- render_pdf_certificate(seal_data: dict) -> bytes
- render_json_certificate(seal_data: dict) -> bytes
- CERT_JSON_SCHEMA: dict  (jsonschema Draft-7)
- SAMPLE_SEAL_DATA: dict  (pre-commit 훅 + 테스트용 샘플)

[원자성 보장]
render_*() 함수가 예외를 raise하면, caller(worker.py)의 try 블록이
evidence_seal_event INSERT를 하지 않으므로 DB 트랜잭션 자체가 열리지 않는다.
즉, PDF/JSON 생성 실패 = 봉인 불가 = 증거 보장 우선.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import jsonschema
from fpdf import FPDF

ISSUER_VERSION = "vg-cert-v1.0.0"
KST = timezone(timedelta(hours=9))

# ── 한국어 폰트 탐색 + 자동 다운로드 ────────────────────────────────
_FONT_DIR        = os.path.join(os.path.dirname(__file__), "fonts")
_LOCAL_FONT_PATH = os.path.join(_FONT_DIR, "NanumGothic.ttf")
_FONT_SEARCH_PATHS = [
    _LOCAL_FONT_PATH,
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
]
# Google Fonts 공식 GitHub — 무료 라이선스 (OFL)
_NANUM_GOTHIC_URL = (
    "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Regular.ttf"
)


def _resolve_korean_font() -> str | None:
    found = next((p for p in _FONT_SEARCH_PATHS if os.path.isfile(p)), None)
    if found:
        return found
    # 자동 다운로드 시도 (Cloud Run 초기 기동 또는 로컬 개발 환경)
    try:
        import urllib.request
        os.makedirs(_FONT_DIR, exist_ok=True)
        urllib.request.urlretrieve(_NANUM_GOTHIC_URL, _LOCAL_FONT_PATH)  # nosec B310
        return _LOCAL_FONT_PATH
    except Exception:
        return None  # 오프라인 환경 → ASCII 폴백


KOREAN_FONT_PATH: str | None = _resolve_korean_font()

# ── JSON 스키마 (Draft-7) ────────────────────────────────────────────
CERT_JSON_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "cert_id", "cert_type", "issuer", "issuer_version", "issued_at",
        "subject", "worm", "integrity", "verification_instructions", "cert_self_hash",
    ],
    "additionalProperties": False,
    "properties": {
        "$schema":    {"type": "string"},
        "cert_id":    {"type": "string"},
        "cert_type":  {"type": "string", "enum": ["EVIDENCE_CERTIFICATE"]},
        "issuer":     {"type": "string"},
        "issuer_version": {"type": "string"},
        "issued_at":  {"type": "string"},
        "subject": {
            "type": "object",
            "required": ["ledger_id", "seal_event_id", "facility_id",
                         "beneficiary_id", "care_type", "ingested_at"],
            "properties": {
                "ledger_id":     {"type": "string"},
                "seal_event_id": {"type": "string"},
                "facility_id":   {"type": "string"},
                "beneficiary_id":{"type": "string"},
                "care_type":     {"type": "string"},
                "ingested_at":   {"type": "string"},
            },
        },
        "worm": {
            "type": "object",
            "required": ["bucket", "object_key", "lock_mode", "retain_until"],
            "properties": {
                "bucket":      {"type": "string"},
                "object_key":  {"type": "string"},
                "lock_mode":   {"type": "string", "enum": ["COMPLIANCE"]},
                "retain_until":{"type": "string"},
            },
        },
        "integrity": {
            "type": "object",
            "required": ["audio_sha256", "transcript_sha256", "chain_hash", "chain_algorithm"],
            "properties": {
                "audio_sha256":      {"type": "string", "minLength": 64, "maxLength": 64},
                "transcript_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                "chain_hash":        {"type": "string", "minLength": 64, "maxLength": 64},
                "chain_algorithm":   {"type": "string"},
                "chain_input_fields":{"type": "array"},
            },
        },
        "verification_instructions": {"type": "object"},
        "cert_self_hash": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}

# ── pre-commit 훅 + 테스트용 샘플 데이터 ────────────────────────────
SAMPLE_SEAL_DATA: dict = {
    "ledger_id":       "00000000-0000-0000-0000-000000000001",
    "seal_event_id":   "00000000-0000-0000-0000-000000000002",
    "facility_id":     "FAC-TEST-001",
    "beneficiary_id":  "BEN-TEST-001",
    "care_type":       "방문요양",
    "ingested_at":     "2026-04-22T05:30:15.000Z",
    "audio_sha256":    "a" * 64,
    "transcript_sha256":"b" * 64,
    "chain_hash":      "c" * 64,
    "transcript_text": "테스트 전사 내용입니다. 식사 보조 및 투약 확인 완료.",
    "worm_bucket":     "voice-guard-korea",
    "worm_object_key": "evidence/2026/04/22/00000000-0000-0000-0000-000000000001.wav",
    "worm_retain_until":"2031-04-22T05:30:15.000Z",
}


def to_kst_str(iso_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return iso_utc


def render_pdf_certificate(seal_data: dict) -> bytes:
    """
    PDF 증거 검증서 렌더링.
    실패 시 예외 전파 → caller의 try 블록이 DB INSERT를 건너뜀 (원자성).
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # PDF 메타데이터 — 압축 없이 raw bytes에 노출됨 (검증/감사용)
    chain_hash = seal_data.get("chain_hash", "")
    pdf.set_title("VoiceGuard Evidence Certificate")
    pdf.set_subject(f"chain:{chain_hash}")  # chain_hash → PDF 오브젝트 스트림에 raw 노출
    pdf.set_author("VoiceGuard-WORM-System")
    pdf.set_keywords(f"ledger:{seal_data.get('ledger_id','')}")

    pdf.add_page()

    font_registered = False
    if KOREAN_FONT_PATH:
        try:
            pdf.add_font("NanumGothic", fname=KOREAN_FONT_PATH)
            font_registered = True
        except Exception:
            pass

    def sf(size: int, bold: bool = False):
        if font_registered:
            # NanumGothic regular only — 크기 차이로 볼드 효과 대체
            pdf.set_font("NanumGothic", style="", size=size + (1 if bold else 0))
        else:
            pdf.set_font("Helvetica", style="B" if bold else "", size=size)

    def section_header(title: str):
        sf(10, bold=True)
        pdf.set_fill_color(41, 65, 122)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 7, f"  {title}", ln=True, fill=True)
        pdf.set_text_color(0, 0, 0)
        sf(9)

    def row(label: str, value: str):
        sf(9, bold=True)
        pdf.cell(55, 5, label)
        sf(9)
        pdf.cell(0, 5, value, ln=True)

    # cert_id와 issued_at: seal_event_id 기반 결정론적 생성 → 재시도 시 동일 출력 보장
    cert_id    = str(uuid.uuid5(uuid.NAMESPACE_URL, seal_data.get("seal_event_id", "") + "-pdf"))
    issued_kst = to_kst_str(seal_data.get("ingested_at", datetime.now(timezone.utc).isoformat()))

    # 한글/영문 레이블 선택 (폰트 등록 여부에 따라)
    L = {
        "title":     "Voice Guard  법적 증거 검증서"  if font_registered else "Voice Guard  Evidence Certificate",
        "subtitle":  "WORM 해시체인 무결성 인증 | 국민건강보험공단 현지조사 대응용" if font_registered
                     else "WORM Hash-Chain Integrity Certification | NHIS Inspection Ready",
        "cert_no":   "발급번호" if font_registered else "Cert No",
        "issued_at": "발급일시" if font_registered else "Issued At",
        "issuer":    "발급기관" if font_registered else "Issuer",
        "subj_hdr":  "[ 수급자 정보 ]"    if font_registered else "[ Subject Info ]",
        "ben_id":    "수급자 ID:"         if font_registered else "Beneficiary ID:",
        "fac_id":    "시설 ID:"           if font_registered else "Facility ID:",
        "care_type": "서비스 유형:"       if font_registered else "Care Type:",
        "rec_time":  "기록 시각(KST):"    if font_registered else "Recorded At (KST):",
        "worm_hdr":  "[ WORM 저장소 상태 ]" if font_registered else "[ WORM Storage Status ]",
        "lock_mode": "잠금 모드:"         if font_registered else "Lock Mode:",
        "lock_val":  "COMPLIANCE  [B2 head_object 검증 완료]" if font_registered
                     else "COMPLIANCE  [B2 head_object verified]",
        "retain":    "보존 기한:"         if font_registered else "Retain Until:",
        "obj_key":   "저장 경로:"         if font_registered else "Object Key:",
        "hash_hdr":  "[ 무결성 해시체인 ]" if font_registered else "[ Integrity Hash Chain ]",
        "audio_h":   "음성  SHA-256:"     if font_registered else "Audio   SHA-256:",
        "trans_h":   "전사  SHA-256:"     if font_registered else "Transcript SHA-256:",
        "chain_h":   "체인해시(HMAC):"    if font_registered else "Chain Hash (HMAC):",
        "full_audio":"전체 음성 해시"      if font_registered else "Full Audio Hash",
        "full_trans":"전체 전사 해시"      if font_registered else "Full Transcript Hash",
        "full_chain":"전체 체인 해시"      if font_registered else "Full Chain Hash",
        "tx_hdr":    "[ AI 전사 내용 (요약) ]" if font_registered else "[ AI Transcript (Preview) ]",
        "no_tx":     "(전사 내용 없음)"   if font_registered else "(No transcript)",
        "vfy_hdr":   "[ 검증 방법 (조사관용) ]" if font_registered else "[ Verification Steps (Inspector) ]",
        "vfy_steps": [
            ("1. WORM 저장소에서 위 저장 경로의 음성 파일을 수령합니다."
             if font_registered else "1. Retrieve audio file from the WORM object key above."),
            ("2. SHA-256(음성파일) 값이 위 음성 SHA-256과 일치하는지 확인합니다."
             if font_registered else "2. Verify SHA256(audio_file) == Audio SHA-256 above."),
            ("3. B2 head_object 조회 → ObjectLockMode = COMPLIANCE 확인합니다."
             if font_registered else "3. B2 head_object must return ObjectLockMode=COMPLIANCE."),
            ("4. 체인해시 = HMAC-SHA256(SECRET_KEY, SHA256(정렬_메타데이터_JSON))."
             if font_registered else "4. chain_hash = HMAC-SHA256(SECRET_KEY, SHA256(sorted_meta_json))."),
            ("5. 모든 값 일치 시 무결성 100% 보장. 불일치 시 조작 의심 즉시 신고."
             if font_registered else "5. All match = 100% integrity. Any mismatch = tampering suspected."),
        ],
        "legal": (
            "[법원 제출용] 본 문서는 WORM(Write Once Read Many) 불변 저장소에 봉인된 원본 음성 "
            "데이터의 법적 증거 검증서입니다. 국민건강보험공단 현지조사, 법원 제출 및 환수 조치 "
            "방어에 공식 사용될 수 있으며, 데이터 조작은 형사 처벌 대상입니다."
            if font_registered else
            "[For Court Submission] This document is a legal evidence certificate for audio data "
            "sealed in a WORM (Write Once Read Many) immutable storage. Valid for NHIS inspection, "
            "court submission and clawback defense. Data tampering is subject to criminal penalties."
        ),
    }

    # ── 헤더 ──────────────────────────────────────────────────────
    sf(14, bold=True)
    pdf.set_fill_color(41, 65, 122)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 12, L["title"], ln=True, fill=True, align="C")
    pdf.set_text_color(0, 0, 0)
    sf(8)
    pdf.cell(0, 4, L["subtitle"], ln=True, align="C")
    pdf.ln(3)

    sf(8)
    pdf.cell(60, 4, f"{L['cert_no']}: cert-{cert_id[:8].upper()}")
    pdf.cell(0, 4, f"{L['issued_at']}: {issued_kst}", ln=True)
    pdf.cell(0, 4, f"{L['issuer']}: VoiceGuard-WORM-System ({ISSUER_VERSION})", ln=True)
    pdf.ln(4)

    # ── 수급자 정보 ─────────────────────────────────────────────
    section_header(L["subj_hdr"])
    row(L["ben_id"],    seal_data.get("beneficiary_id", "-"))
    row(L["fac_id"],    seal_data.get("facility_id",    "-"))
    row(L["care_type"], seal_data.get("care_type",      "-"))
    row(L["rec_time"],  to_kst_str(seal_data.get("ingested_at", "")))
    pdf.ln(3)

    # ── WORM 저장소 상태 ─────────────────────────────────────────
    section_header(L["worm_hdr"])
    row(L["lock_mode"], L["lock_val"])
    row(L["retain"],    to_kst_str(seal_data.get("worm_retain_until", "")))
    obj_key = seal_data.get("worm_object_key", "")
    row(L["obj_key"],   obj_key[:70] + ("..." if len(obj_key) > 70 else ""))
    pdf.ln(3)

    # ── 무결성 해시체인 ──────────────────────────────────────────
    section_header(L["hash_hdr"])
    ah = seal_data.get("audio_sha256",      "")
    th = seal_data.get("transcript_sha256", "")
    ch = seal_data.get("chain_hash",        "")
    row(L["audio_h"], ah[:32] + "...")
    row(L["trans_h"], th[:32] + "...")
    row(L["chain_h"], ch[:32] + "...")
    sf(7)
    pdf.set_fill_color(248, 248, 248)
    pdf.cell(0, 3, f"  {L['full_audio']} : {ah}", ln=True, fill=True)
    pdf.cell(0, 3, f"  {L['full_trans']} : {th}", ln=True, fill=True)
    pdf.cell(0, 3, f"  {L['full_chain']} : {ch}", ln=True, fill=True)
    pdf.set_fill_color(255, 255, 255)
    pdf.ln(3)

    # ── AI 전사 내용 ──────────────────────────────────────────────
    section_header(L["tx_hdr"])
    sf(9)
    tx      = seal_data.get("transcript_text", "")
    if font_registered:
        preview = (tx[:400] + "...") if len(tx) > 400 else (tx or L["no_tx"])
    else:
        # 한글 폰트 없을 때 — transcript_text는 한글일 수 있으므로 길이만 표시
        preview = f"[{len(tx)} chars — Korean font required to display]" if tx else L["no_tx"]
    pdf.multi_cell(0, 5, preview)
    pdf.ln(3)

    # ── 검증 방법 ─────────────────────────────────────────────────
    section_header(L["vfy_hdr"])
    sf(9)
    for step in L["vfy_steps"]:
        pdf.cell(0, 5, step, ln=True)
    pdf.ln(4)

    # ── 법적 고지 ────────────────────────────────────────────────
    sf(7)
    pdf.set_fill_color(240, 240, 250)
    pdf.multi_cell(0, 4, L["legal"], fill=True)

    # fpdf2는 /CreationDate를 기본으로 현재 시각으로 설정 → 결정론적 고정 필요
    try:
        pdf.creation_date = datetime.fromisoformat(
            seal_data.get("ingested_at", "2000-01-01T00:00:00.000Z").replace("Z", "+00:00")
        )
    except Exception:
        pass
    return bytes(pdf.output())


def render_json_certificate(seal_data: dict) -> bytes:
    """
    JSON 증거 검증서 렌더링 + 스키마 검증.
    ValidationError 시 예외 전파 → 봉인 ROLLBACK.
    SECRET_KEY는 절대 포함하지 않는다.
    """
    # cert_id와 issued_at: seal_event_id 기반 결정론적 생성 → 재시도 시 동일 출력 보장
    cert_id   = str(uuid.uuid5(uuid.NAMESPACE_URL, seal_data.get("seal_event_id", "") + "-json"))
    issued_at = seal_data.get("ingested_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    doc: dict = {
        "$schema":        "https://voiceguard.kr/schemas/cert/v1.0.0",
        "cert_id":        cert_id,
        "cert_type":      "EVIDENCE_CERTIFICATE",
        "issuer":         "VoiceGuard-WORM-System",
        "issuer_version": ISSUER_VERSION,
        "issued_at":      issued_at,
        "subject": {
            "ledger_id":     seal_data["ledger_id"],
            "seal_event_id": seal_data["seal_event_id"],
            "facility_id":   seal_data.get("facility_id",   ""),
            "beneficiary_id":seal_data.get("beneficiary_id",""),
            "care_type":     seal_data.get("care_type",     ""),
            "ingested_at":   seal_data.get("ingested_at",   ""),
        },
        "worm": {
            "bucket":      seal_data.get("worm_bucket",       ""),
            "object_key":  seal_data.get("worm_object_key",   ""),
            "lock_mode":   "COMPLIANCE",
            "retain_until":seal_data.get("worm_retain_until", ""),
        },
        "integrity": {
            "audio_sha256":      seal_data["audio_sha256"],
            "transcript_sha256": seal_data["transcript_sha256"],
            "chain_hash":        seal_data["chain_hash"],
            "chain_algorithm":   "HMAC-SHA256",
            "chain_input_fields": [
                "ledger_id", "facility_id", "beneficiary_id",
                "shift_id", "server_ts", "audio_sha256",
                "transcript_sha256", "b2_key",
            ],
        },
        "verification_instructions": {
            "step1": "Retrieve audio file from worm.object_key in B2 WORM bucket",
            "step2": "SHA256(audio_bytes) must equal integrity.audio_sha256",
            "step3": "B2 head_object ObjectLockMode must equal 'COMPLIANCE'",
            "step4": "HMAC-SHA256(SECRET_KEY, SHA256(sorted_json_payload)) must equal integrity.chain_hash",
            "step5": "SHA256(this_document_excluding_cert_self_hash_field) must equal cert_self_hash",
        },
        "cert_self_hash": "",
    }

    # cert_self_hash 계산 (자기 자신 필드 제외)
    doc_for_hash = {k: v for k, v in doc.items() if k != "cert_self_hash"}
    self_hash = hashlib.sha256(
        json.dumps(doc_for_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    doc["cert_self_hash"] = self_hash

    # 스키마 검증 — 실패 시 ValidationError 전파
    jsonschema.validate(doc, CERT_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())

    # SECRET_KEY 노출 방어 (이중 안전장치)
    secret = os.environ.get("SECRET_KEY", "")
    serialized = json.dumps(doc, ensure_ascii=False, indent=2)
    if secret and secret != "추후입력" and len(secret) > 4 and secret in serialized:
        raise RuntimeError("SECURITY: SECRET_KEY leaked into JSON certificate — ABORT")

    return serialized.encode("utf-8")
