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

CARE_RECORD_SCHEMA_VERSION = "2.1"

_CARE_RECORD_SYSTEM_PROMPT = """당신은 요양보호사의 현장 발화를 6대 의무기록 카테고리로 분류하는 의료 기록 보조 시스템입니다.

[필수 지침]
1. 반드시 아래 JSON 스키마를 100% 준수하십시오. 임의 필드 추가 금지.
2. 6대 카테고리(meal, medication, excretion, repositioning, hygiene, special_notes)는
   반드시 모두 존재해야 합니다.
   음성에서 언급이 없으면 반드시 done=false, detail=null로 채우십시오.
3. done 값은 반드시 boolean(true/false)만 사용하십시오. 문자열 "true" 금지.
4. detail은 음성에서 언급된 내용만 기재하십시오. 추측 금지.
5. special_notes(특이사항)는 5개 표준 카테고리(식사·투약·배설·체위변경·위생)
   어디에도 해당하지 않는 중요한 관찰/사건을 기재하십시오.
   특이사항이 있으면 done=true, detail=상세내용.
   없으면 done=false, detail=null.

[폴백 규칙 — 절대 준수]
- 언급 없는 카테고리 → done=false, detail=null (에러 금지, null 반환 금지)
- 수급자 ID 불명 → beneficiary_id="" (빈 문자열, null 금지)
- 기록 일시 불명 → recorded_at="" (빈 문자열, null 금지)

[출력 JSON 스키마 — 6대 의무기록 동일 구조]
{
  "schema_version": "2.1",
  "beneficiary_id": "string",
  "recorded_at":    "YYYY-MM-DDTHH:MM:SS",
  "meal":           { "done": true/false, "detail": "string or null" },
  "medication":     { "done": true/false, "detail": "string or null" },
  "excretion":      { "done": true/false, "detail": "string or null" },
  "repositioning":  { "done": true/false, "detail": "string or null" },
  "hygiene":        { "done": true/false, "detail": "string or null" },
  "special_notes":  { "done": true/false, "detail": "string or null" }
}

[출력 형식]
순수 JSON만 반환하십시오. 마크다운 코드 블록(```), 설명 텍스트 일체 금지."""


def _sanitize_care_record(raw: dict, metadata: dict) -> dict:
    """
    Gemini 응답 null-safe 정규화 (6대 의무기록 전용).

    안전망 원칙 (TD-04 반영):
    - 6대 카테고리(special_notes 포함) 키 누락/null → {"done":False,"detail":None}
    - done 값 bool() 강제 캐스팅 — Gemini "true" 문자열 방어
    - detail 길이 제한: 5개 표준은 200자, special_notes는 2000자
    - 구버전 응답(special_notes가 string 또는 null)도 호환 처리
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
        # special_notes는 detail 길이를 더 넉넉하게 (관찰/사건 서술 공간)
        max_detail = 2000 if key == "special_notes" else 200
        item = raw.get(key)

        if isinstance(item, dict) and "done" in item:
            # 정상 v2.1 구조
            result[key] = {
                "done":   bool(item.get("done", False)),
                "detail": (str(item["detail"])[:max_detail] if item.get("detail") else None),
            }
        elif key == "special_notes" and isinstance(item, str) and item.strip():
            # 구버전 v2.0 호환: special_notes가 문자열로 온 경우
            # → done=True, detail=문자열 로 자동 승격
            result[key] = {
                "done":   True,
                "detail": item.strip()[:max_detail],
            }
        else:
            # 키 누락 / null / 빈 값 → 기본값 주입 (파이프라인 블로킹 금지)
            result[key] = {"done": False, "detail": None}

    result["schema_version"] = CARE_RECORD_SCHEMA_VERSION
    result["beneficiary_id"] = str(
        raw.get("beneficiary_id") or metadata.get("beneficiary_id", "")
    )[:100]
    result["recorded_at"] = str(
        raw.get("recorded_at") or metadata.get("recorded_at", "")
    )[:30]

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
        metadata:       {beneficiary_id, facility_id, recorded_at, ...}
        client:         호출자가 공유하는 httpx.AsyncClient (없으면 내부 생성)
    Returns:
        schema_version="2.0" 보장된 구조화 dict
    """
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
            "maxOutputTokens":  512,
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
