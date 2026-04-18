"""
Voice Guard — backend/gemini_processor.py
Gemini API: 음성 트랜스크립트 → 구조화 JSON 정제기 v2.0

[파이프라인 A: 인수인계 (call_gemini)]
  1. incidents / todos null 반환 원천 차단 — 빈 배열 [] 강제
  2. care_checklist 8개 항목 항상 존재 보장 (누락 시 done=False, note=None 주입)
  3. schema_version 필드로 버전 불일치 조기 감지 (수신 레이어 방어)
  4. API 장애/빈 트랜스크립트 → 기본값 JSON 반환 (파이프라인 블로킹 금지)
  5. API 키 하드코딩 절대 금지 — 환경변수 전용

[파이프라인 B: 6대 의무기록 (call_gemini_care_record)]
  1. 6대 카테고리 키 누락/null → {"done": False, "detail": None} 자동 주입
  2. 부분 발화 ("점심만 드셨다") 시 나머지 5개 카테고리는 미수행으로 폴백
  3. API 장애 → 기본값 JSON 반환 (파이프라인 블로킹 금지)
"""

import json
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("voice_guard.gemini_processor")

# ── 설정 (환경변수 전용) ─────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
_GEMINI_BASE   = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_API_URL = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"

SCHEMA_VERSION = "1.0"

# 케어 체크리스트 고정 키 순서 (8개, 항상 보장)
_CHECKLIST_KEYS = (
    "meal_morning", "meal_lunch", "meal_dinner",
    "bowel", "medication", "hygiene", "activity", "sleep",
)

# ── 시스템 프롬프트 ───────────────────────────────────────────────────
_SYSTEM_PROMPT = """당신은 요양보호사의 음성 기록을 구조화된 JSON으로 변환하는 의료 기록 보조 시스템입니다.

[필수 지침]
1. 반드시 아래 JSON 스키마를 100% 준수하십시오. 임의 필드 추가 금지.
2. incidents와 todos는 음성에서 명확히 언급된 것만 포함하십시오.
   불확실한 내용은 포함하지 마십시오.
   내용이 없으면 반드시 빈 배열 []을 반환하십시오. 절대 null 반환 금지.
3. care_checklist의 8개 항목은 반드시 모두 존재해야 합니다.
   음성에서 언급이 없으면 done=false, note=null로 채우십시오.
4. 개인정보(이름, 연락처)는 음성에서 들린 그대로만 기재하십시오.
5. incidents[].summary는 80자, todos[]는 100자를 초과하지 마십시오.

[폴백 규칙 — 절대 준수]
- incidents 내용 없음 → [] (빈 배열, null 금지)
- todos 내용 없음    → [] (빈 배열, null 금지)
- 근무 시간 불명     → shift_start="00:00", shift_end="00:00"
- 수급자 이름 불명   → resident_name="미확인"
- 근무자 이름 불명   → worker_name="미확인"

[출력 JSON 스키마]
{
  "schema_version": "1.0",
  "report_date":   "YYYY-MM-DD",
  "worker_name":   "string",
  "resident_name": "string",
  "resident_id":   "string",
  "shift_start":   "HH:MM",
  "shift_end":     "HH:MM",
  "incidents": [
    {
      "type":     "FALL|MEDICATION|BEHAVIOR|OTHER",
      "severity": "CRITICAL|WARNING|INFO",
      "summary":  "string (max 80자)"
    }
  ],
  "care_checklist": {
    "meal_morning": { "done": true/false, "note": "string or null" },
    "meal_lunch":   { "done": true/false, "note": "string or null" },
    "meal_dinner":  { "done": true/false, "note": "string or null" },
    "bowel":        { "done": true/false, "note": "string or null" },
    "medication":   { "done": true/false, "note": "string or null" },
    "hygiene":      { "done": true/false, "note": "string or null" },
    "activity":     { "done": true/false, "note": "string or null" },
    "sleep":        { "done": true/false, "note": "string or null" }
  },
  "todos": ["string (max 100자)"]
}

[출력 형식]
순수 JSON만 반환하십시오. 마크다운 코드 블록(```), 설명 텍스트 일체 금지."""


# ══════════════════════════════════════════════════════════════════
# 내부 유틸리티
# ══════════════════════════════════════════════════════════════════

def _default_checklist() -> dict:
    """8개 케어 항목 기본값 — 누락 키 방어용"""
    return {key: {"done": False, "note": None} for key in _CHECKLIST_KEYS}


def _sanitize(raw: dict, metadata: dict) -> dict:
    """
    Gemini 응답 null-safe 정규화.

    - incidents / todos: null/누락 → []
    - care_checklist: 누락 키 → 기본값 주입
    - schema_version 불일치 → 경고 로그 (파이프라인은 계속)
    """
    schema_ver = raw.get("schema_version", "unknown")
    if schema_ver != SCHEMA_VERSION:
        logger.warning(
            f"[GEMINI] schema_version 불일치: "
            f"got={schema_ver!r} expected={SCHEMA_VERSION!r} — 수신 레이어 검증 실패"
        )

    # null-safe 배열 강제
    incidents_raw = raw.get("incidents") or []
    todos_raw     = raw.get("todos")     or []

    # incidents 항목별 필드 보장
    incidents = []
    for inc in incidents_raw:
        if not isinstance(inc, dict):
            continue
        incidents.append({
            "type":     str(inc.get("type",     "OTHER"))[:20],
            "severity": str(inc.get("severity", "INFO"))[:20],
            "summary":  str(inc.get("summary",  ""))[:80],
        })

    # todos 항목별 길이 보장
    todos = [str(t)[:100] for t in todos_raw if t]

    # care_checklist: 8개 키 완전 보장
    raw_cl   = raw.get("care_checklist") or {}
    checklist = _default_checklist()
    for key in _CHECKLIST_KEYS:
        item = raw_cl.get(key)
        if isinstance(item, dict):
            checklist[key] = {
                "done": bool(item.get("done", False)),
                "note": (str(item["note"])[:200] if item.get("note") else None),
            }

    return {
        "schema_version": SCHEMA_VERSION,
        "report_date":    str(raw.get("report_date")   or metadata.get("report_date", ""))[:10],
        "worker_name":    str(raw.get("worker_name")   or metadata.get("worker_name", "미확인"))[:50],
        "resident_name":  str(raw.get("resident_name") or metadata.get("resident_name", "미확인"))[:50],
        "resident_id":    str(raw.get("resident_id")   or metadata.get("beneficiary_id", "미확인"))[:100],
        "shift_start":    str(raw.get("shift_start")   or "00:00")[:5],
        "shift_end":      str(raw.get("shift_end")     or "00:00")[:5],
        "incidents":      incidents,
        "care_checklist": checklist,
        "todos":          todos,
    }


# ══════════════════════════════════════════════════════════════════
# 공개 API
# ══════════════════════════════════════════════════════════════════

async def call_gemini(
    transcript: str,
    metadata: dict,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """
    Gemini API 호출 → 인수인계 구조화 JSON 반환.

    API 장애 / 키 미설정 / 빈 트랜스크립트 모든 케이스에서
    파이프라인을 블로킹하지 않고 기본값 JSON 반환.

    Args:
        transcript: Whisper STT 결과 텍스트
        metadata:   {beneficiary_id, facility_id, shift_id, server_ts, ...}
        client:     호출자가 공유하는 httpx.AsyncClient (없으면 내부 생성)
    Returns:
        schema_version="1.0" 보장된 구조화 dict
    """
    if not GEMINI_API_KEY:
        logger.warning("[GEMINI] GEMINI_API_KEY 미설정 — 기본값 JSON 반환")
        return _sanitize({}, metadata)

    if not transcript or not transcript.strip():
        logger.warning("[GEMINI] 빈 트랜스크립트 — 기본값 JSON 반환")
        return _sanitize({}, metadata)

    body = {
        "contents": [
            {
                "parts": [
                    {"text": _SYSTEM_PROMPT},
                    {"text": f"[음성 기록]\n{transcript.strip()}"},
                ]
            }
        ],
        "generationConfig": {
            "temperature":       0.1,    # 결정론적 출력 (환각 최소화)
            "maxOutputTokens":   1024,
            "responseMimeType":  "application/json",
        },
    }

    url        = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        resp = await client.post(url, json=body, timeout=30.0)

        if resp.status_code != 200:
            logger.error(
                f"[GEMINI] API 오류 HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return _sanitize({}, metadata)

        # Gemini 응답 구조: candidates[0].content.parts[0].text
        raw_text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "{}")
        )

        try:
            raw_json = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(
                f"[GEMINI] JSON 파싱 실패: {e} | raw={raw_text[:300]}"
            )
            return _sanitize({}, metadata)

        result = _sanitize(raw_json, metadata)
        logger.info(
            f"[GEMINI] 정제 완료 — "
            f"incidents={len(result['incidents'])} "
            f"todos={len(result['todos'])} "
            f"worker={result['worker_name']!r}"
        )
        return result

    except httpx.TimeoutException:
        logger.error("[GEMINI] 타임아웃 (30초) — 기본값 JSON 반환")
        return _sanitize({}, metadata)
    except Exception as e:
        logger.error(f"[GEMINI] 예외: {e} — 기본값 JSON 반환")
        return _sanitize({}, metadata)
    finally:
        if own_client:
            await client.aclose()


# ══════════════════════════════════════════════════════════════════
# 파이프라인 B: 6대 의무기록 정제기 (신규)
# ══════════════════════════════════════════════════════════════════

# ── [TD-04] 6대 의무기록 고정 카테고리 키 ────────────────────────
# 식사 / 투약 / 배설 / 체위변경 / 위생 / 특이사항 — 정확히 6개.
# special_notes도 다른 5개와 동일한 {"done": bool, "detail": str|None} 구조를
# 강제하여 매핑 로직 일관성과 Notion 페이로드 정합성을 보장한다.
_CARE_RECORD_KEYS = (
    "meal",           # 식사
    "medication",     # 투약
    "excretion",      # 배설
    "repositioning",  # 체위변경
    "hygiene",        # 위생
    "special_notes",  # 특이사항 (← TD-04: 6번째 의무기록 복원)
)

CARE_RECORD_SCHEMA_VERSION = "2.2"

# v2.1 → v2.2 변경 요약:
#   1. corrected_transcript 신규 필드 (오탈자 교정 전체 발화)
#   2. detail: string → {situation, action, notes} 구조화 객체
#   3. 의료용어 표준화 매핑 + 3단 객관 요약 강제
_CARE_RECORD_SYSTEM_PROMPT = """당신은 요양보호사의 현장 발화를 6대 의무기록으로 정제하는 법적 증거 기록 시스템입니다.

[Step 1: 오탈자·발음오류 교정]
발화 원문에서 명백한 오탈자와 발음 오류를 먼저 교정하십시오.
교정된 전체 발화를 corrected_transcript 필드에 기재하십시오.
교정할 내용이 없으면 원문 그대로 기재하십시오.

[Step 2: 의료용어 표준화 — 아래 매핑 규칙을 반드시 적용하십시오]
- "이상 없음/별이상없음/별다른 없음" → "특이소견 없음"
- "약 드렸어요/약 먹었어요/약 챙겨드렸어요" → "투약 완료"
- "화장실 다녀오셨어요/볼일 봤어요/대소변" → "배설 수행"
- "자세 바꿔드렸어요/뒤집어드렸어요/자세 변경" → "체위변경 수행"
- "씻겨드렸어요/목욕시켜드렸어요/세면" → "위생관리 수행"
- "넘어지실 뻔/쓰러지려고/낙상 위험" → "낙상 위험 발생"
- "열 있어요/열나는 것 같아요/체온 높음" → "발열 증상 관찰"
- "밥 드셨어요/식사하셨어요/드셨어요" → "식사 수행"

[Step 3: 6대 카테고리 분류 + 3단 객관 요약]
각 카테고리별로 done 여부를 판단하고, done=true인 항목은 detail을 3단 구조로 작성하십시오.
- situation: 관찰된 사실 (예: "오전 9시 식사 보조 시행")
- action:    수행한 케어 내용 (예: "죽 200ml 전량 섭취 보조")
- notes:     비정상 소견 또는 "특이소견 없음"
done=false인 항목은 detail을 null로 설정하십시오.

[필수 지침]
1. 반드시 아래 JSON 스키마를 100% 준수하십시오. 임의 필드 추가 금지.
2. 6대 카테고리는 반드시 모두 존재해야 합니다. 언급 없으면 done=false, detail=null.
3. done 값은 반드시 boolean(true/false)만 사용하십시오. 문자열 금지.
4. detail 내용은 발화에서 확인된 사실만 기재하십시오. 추측·생성 절대 금지.
5. special_notes는 5개 표준 카테고리 외 중요 관찰/사건만 기재하십시오.

[폴백 규칙 — 절대 준수]
- 언급 없는 카테고리 → done=false, detail=null
- 수급자 ID 불명 → beneficiary_id="" (null 금지)
- 기록 일시 불명 → recorded_at="" (null 금지)
- corrected_transcript 교정 불가 → 원문 그대로 기재

[출력 JSON 스키마 v2.2]
{
  "schema_version": "2.2",
  "beneficiary_id": "string",
  "recorded_at":    "YYYY-MM-DDTHH:MM:SS",
  "corrected_transcript": "string",
  "meal":           { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null },
  "medication":     { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null },
  "excretion":      { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null },
  "repositioning":  { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null },
  "hygiene":        { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null },
  "special_notes":  { "done": true/false, "detail": {"situation": "string", "action": "string", "notes": "string"} or null }
}

[출력 형식]
순수 JSON만 반환하십시오. 마크다운 코드 블록(```), 설명 텍스트 일체 금지."""


def _sanitize_detail(raw_detail: object, is_long: bool = False) -> Optional[dict]:
    """
    v2.2 detail 필드 정규화 — {situation, action, notes} 구조화 객체 강제.

    하위 호환:
      - v2.1 string detail → situation에 승격, action/notes="특이소견 없음"
      - null / 빈값 → None 반환
    """
    if raw_detail is None:
        return None

    max_sub = 500 if is_long else 200

    if isinstance(raw_detail, dict):
        situation = str(raw_detail.get("situation") or "")[:max_sub].strip()
        action    = str(raw_detail.get("action")    or "")[:max_sub].strip()
        notes     = str(raw_detail.get("notes")     or "")[:max_sub].strip()
        if not situation and not action and not notes:
            return None
        return {
            "situation": situation or "미상",
            "action":    action    or "미상",
            "notes":     notes     or "특이소견 없음",
        }

    # v2.1 string 하위 호환: 문자열 → situation으로 승격
    if isinstance(raw_detail, str) and raw_detail.strip():
        return {
            "situation": raw_detail.strip()[:max_sub],
            "action":    "미상",
            "notes":     "특이소견 없음",
        }

    return None


def _sanitize_care_record(raw: dict, metadata: dict) -> dict:
    """
    Gemini 응답 null-safe 정규화 (6대 의무기록 전용) v2.2.

    안전망 원칙:
    - 6대 카테고리 키 누락/null → {"done": False, "detail": None}
    - done 값 bool() 강제 캐스팅 — Gemini "true" 문자열 방어
    - detail: v2.2 구조화 객체 강제, v2.1 string 하위 호환
    - corrected_transcript 누락 → raw_voice_text 폴백
    - schema_version 불일치 → 경고 로그 (파이프라인은 계속)
    """
    schema_ver = raw.get("schema_version", "unknown")
    if schema_ver != CARE_RECORD_SCHEMA_VERSION:
        logger.warning(
            f"[GEMINI-CARE] schema_version 불일치: "
            f"got={schema_ver!r} expected={CARE_RECORD_SCHEMA_VERSION!r}"
        )

    result: dict = {}

    for key in _CARE_RECORD_KEYS:
        is_long = (key == "special_notes")
        item    = raw.get(key)

        if isinstance(item, dict) and "done" in item:
            result[key] = {
                "done":   bool(item.get("done", False)),
                "detail": _sanitize_detail(item.get("detail"), is_long=is_long),
            }
        else:
            result[key] = {"done": False, "detail": None}

    result["schema_version"] = CARE_RECORD_SCHEMA_VERSION
    result["beneficiary_id"] = str(
        raw.get("beneficiary_id") or metadata.get("beneficiary_id", "")
    )[:100]
    result["recorded_at"] = str(
        raw.get("recorded_at") or metadata.get("recorded_at", "")
    )[:30]
    # corrected_transcript: Gemini 교정 발화, 없으면 원문 폴백
    raw_voice = metadata.get("raw_voice_text", "")
    result["corrected_transcript"] = str(
        raw.get("corrected_transcript") or raw_voice
    )[:5000]

    return result


async def call_gemini_care_record(
    raw_voice_text: str,
    metadata: dict,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """
    Gemini API 호출 → 6대 의무기록 구조화 JSON 반환.

    API 장애 / 키 미설정 / 빈 발화 모든 케이스에서
    파이프라인을 블로킹하지 않고 기본값 JSON 반환.

    Args:
        raw_voice_text: 현장 발화 원문 텍스트
        metadata:       {beneficiary_id, facility_id, recorded_at, raw_voice_text, ...}
        client:         호출자가 공유하는 httpx.AsyncClient (없으면 내부 생성)
    Returns:
        schema_version="2.2" 보장된 구조화 dict
    """
    # metadata에 raw_voice_text 주입 — corrected_transcript 폴백용
    if "raw_voice_text" not in metadata:
        metadata = {**metadata, "raw_voice_text": raw_voice_text}

    if not GEMINI_API_KEY:
        logger.warning("[GEMINI-CARE] GEMINI_API_KEY 미설정 — 기본값 JSON 반환")
        return _sanitize_care_record({}, metadata)

    if not raw_voice_text or not raw_voice_text.strip():
        logger.warning("[GEMINI-CARE] 빈 발화 텍스트 — 기본값 JSON 반환")
        return _sanitize_care_record({}, metadata)

    body = {
        "contents": [
            {
                "parts": [
                    {"text": _CARE_RECORD_SYSTEM_PROMPT},
                    {"text": f"[현장 발화]\n{raw_voice_text.strip()}"},
                ]
            }
        ],
        "generationConfig": {
            "temperature":      0.1,
            "maxOutputTokens":  1024,   # v2.2: 3단 구조화 detail로 토큰 증가
            "responseMimeType": "application/json",
        },
    }

    url        = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        resp = await client.post(url, json=body, timeout=30.0)

        if resp.status_code != 200:
            logger.error(
                f"[GEMINI-CARE] API 오류 HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return _sanitize_care_record({}, metadata)

        raw_text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "{}")
        )

        try:
            raw_json = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(
                f"[GEMINI-CARE] JSON 파싱 실패: {e} | raw={raw_text[:300]}"
            )
            return _sanitize_care_record({}, metadata)

        result = _sanitize_care_record(raw_json, metadata)
        done_count = sum(
            1 for k in _CARE_RECORD_KEYS if result.get(k, {}).get("done", False)
        )
        logger.info(
            f"[GEMINI-CARE] 정제 완료 — "
            f"수행 항목={done_count}/{len(_CARE_RECORD_KEYS)} "
            f"beneficiary={result['beneficiary_id']!r}"
        )
        return result

    except httpx.TimeoutException:
        logger.error("[GEMINI-CARE] 타임아웃 (30초) — 기본값 JSON 반환")
        return _sanitize_care_record({}, metadata)
    except Exception as e:
        logger.error(f"[GEMINI-CARE] 예외: {e} — 기본값 JSON 반환")
        return _sanitize_care_record({}, metadata)
    finally:
        if own_client:
            await client.aclose()
