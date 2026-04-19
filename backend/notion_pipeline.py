"""
Voice Guard — backend/notion_pipeline.py
5대 마스터 DB 아키텍처 파이프라인 v3.0

[3대 설계 원칙]
  제1원칙: Notion API 2026-03-11 / Retry-After 준수 / Exponential Backoff (Transactional Outbox)
  제2원칙: Hot Path (1행 운영관제) vs Cold Path (원자 케어이벤트 비동기 큐)
  제3원칙: 관계형 ID 직접 매핑 (RESIDENT_DB_ID / CAREGIVER_DB_ID 환경변수 고정)

[Hot Path 흐름]
  Gemini care_record JSON → Pydantic 검증 →
  Resident/Caregiver Page ID 조회 (In-Memory 캐시) →
  VG_운영_대시보드_DB 단 1행 생성 (Relation + 5대 체크박스)

[Cold Path 흐름]
  비동기 큐 폴링 (NOTION_ATOMIC_EVENTS_DB_ID) →
  원자 이벤트 단위 분해 → VG_Atomic_Care_Events DB 적재

[환경변수]
  NOTION_API_KEY                — Notion Integration 토큰
  RESIDENT_DB_ID                — 수급자 마스터 DB ID (하드코딩 기본값 제공)
  CAREGIVER_DB_ID               — 요양보호사 마스터 DB ID (하드코딩 기본값 제공)
  NOTION_OPS_DASHBOARD_DB_ID    — VG_운영_대시보드_DB ID (사령관 입력 대기)
  NOTION_ATOMIC_EVENTS_DB_ID    — VG_Atomic_Care_Events DB ID (사령관 입력 대기)
  NOTION_API_VERSION            — Notion API 버전 (기본값: 2022-06-28, 최신: 2026-03-11)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()

logger = logging.getLogger("voice_guard.notion_pipeline")

# ══════════════════════════════════════════════════════════════════
# 설정 상수 (환경변수 전용 — 하드코딩 키 절대 금지)
# ══════════════════════════════════════════════════════════════════

NOTION_API_KEY             = os.getenv("NOTION_API_KEY", "")
# NOTE: Notion API 2026-03-11은 /query 엔드포인트에서 invalid_request_url 반환.
#       실증 검증된 안정 버전 2022-06-28 고정 사용 (2026-03-11 공식 지원 시 업그레이드 예정).
NOTION_API_VERSION         = "2022-06-28"
NOTION_BASE_URL            = "https://api.notion.com/v1"

# 제3원칙: 마스터 DB ID — 환경변수 우선, 기본값은 사령관 지정 고정값
RESIDENT_DB_ID             = os.getenv("RESIDENT_DB_ID",  "3fcdbdd0e3b383e1adf681ca3574b913")
CAREGIVER_DB_ID            = os.getenv("CAREGIVER_DB_ID", "ac8dbdd0e3b382f9988681d686aca5f0")
NOTION_OPS_DASHBOARD_DB_ID = os.getenv("NOTION_OPS_DASHBOARD_DB_ID", "")   # 사령관 입력 대기
NOTION_ATOMIC_EVENTS_DB_ID = os.getenv("NOTION_ATOMIC_EVENTS_DB_ID", "")   # 사령관 입력 대기

# 마스터 DB 조회 속성명 (실증 확인 값)
RESIDENT_LOOKUP_PROP  = os.getenv("RESIDENT_LOOKUP_PROP",  "수급자번호")
CAREGIVER_LOOKUP_PROP = os.getenv("CAREGIVER_LOOKUP_PROP", "보호사번호")

# 제1원칙: Rate Limit 방어 상수
_MAX_RETRIES     = 4        # 최대 재시도 횟수
_BACKOFF_BASE    = 2.0      # 지수 백오프 베이스 (초)
_MAX_BACKOFF     = 60.0     # 최대 단일 대기 상한 (초)
_DEFAULT_TIMEOUT = 15.0     # Notion API 타임아웃

# Page ID 캐시 (In-Memory, Hot Path 지연 최소화)
_page_id_cache: dict[str, tuple[str, float]] = {}  # key → (page_id, expire_ts)
_CACHE_TTL_SEC  = 3600.0    # 1시간 캐시 TTL


# ══════════════════════════════════════════════════════════════════
# SECTION 1: Pydantic 스키마 — 서버 단 입력 검증 레이어
# (제1원칙: Zod 역할을 Python Pydantic으로 구현)
# ══════════════════════════════════════════════════════════════════

class CareFlag(BaseModel):
    """6대 의무기록 단일 카테고리 — done 플래그 + 3단 요약 객체 (v2.2)"""
    done:   bool             = False
    detail: Optional[dict]   = None

    @field_validator("detail", mode="before")
    @classmethod
    def normalize_detail(cls, v: object) -> Optional[dict]:
        """v2.2 dict 정상 경로 + v2.1 string 하위 호환."""
        if v is None:
            return None
        if isinstance(v, dict):
            if not any(v.get(k) for k in ("situation", "action", "notes")):
                return None
            return {
                "situation": str(v.get("situation") or "")[:500],
                "action":    str(v.get("action")    or "")[:500],
                "notes":     str(v.get("notes")     or "특이소견 없음")[:500],
            }
        # v2.1 string 하위 호환 — situation으로 승격
        s = str(v).strip()[:500]
        return {"situation": s, "action": "미상", "notes": "특이소견 없음"} if s else None


class CareRecordInput(BaseModel):
    """
    Hot Path 입력 스키마 검증.
    Gemini care_record JSON + 라우팅 메타 → Notion 1행 생성 인자.
    """
    # ── 라우팅 메타 (필수)
    facility_id:    str = Field(..., min_length=1, max_length=100)
    beneficiary_id: str = Field(..., min_length=1, max_length=100)
    caregiver_id:   str = Field(..., min_length=1, max_length=100)

    # ── 발화 원문 (필수)
    raw_voice_text: str = Field(..., min_length=1, max_length=10_000)

    # ── 6대 의무기록 체크박스 (기본값 미수행)
    meal:           CareFlag = Field(default_factory=CareFlag)
    medication:     CareFlag = Field(default_factory=CareFlag)
    excretion:      CareFlag = Field(default_factory=CareFlag)
    repositioning:  CareFlag = Field(default_factory=CareFlag)
    hygiene:        CareFlag = Field(default_factory=CareFlag)
    special_notes:  CareFlag = Field(default_factory=CareFlag)

    # ── Gemini 교정 발화 (v2.2 신규 — 없으면 raw_voice_text 폴백)
    corrected_transcript: Optional[str] = None

    # ── 타임스탬프 (선택, 없으면 서버 현재 시각 사용)
    recorded_at: Optional[str] = None

    @field_validator("facility_id", "beneficiary_id", "caregiver_id", mode="before")
    @classmethod
    def strip_whitespace(cls, v: object) -> str:
        return str(v).strip()

    @model_validator(mode="after")
    def at_least_one_care_done(self) -> "CareRecordInput":
        """최소 1개 의무기록 항목은 done=True여야 유효한 케어 기록으로 인정."""
        flags = [
            self.meal.done, self.medication.done, self.excretion.done,
            self.repositioning.done, self.hygiene.done, self.special_notes.done,
        ]
        if not any(flags):
            logger.warning("[PYDANTIC] 모든 케어 플래그 False — 빈 발화 가능성")
            # 파이프라인 블로킹 금지: 경고만 발행하고 통과
        return self


class AtomicCareEvent(BaseModel):
    """Cold Path 원자 이벤트 단위 — VG_Atomic_Care_Events DB 1행 대응"""
    care_record_id: str
    facility_id:    str
    beneficiary_id: str
    caregiver_id:   str
    category:       str   # meal / medication / excretion / repositioning / hygiene / special_notes
    done:           bool
    detail:         Optional[dict]   # v2.2: {situation, action, notes}
    recorded_at:    str


# ══════════════════════════════════════════════════════════════════
# SECTION 2: Notion API 클라이언트 — Rate Limit 방어 + 재시도
# (제1원칙: 429 Retry-After + 500/503 Exponential Backoff)
# ══════════════════════════════════════════════════════════════════

def _notion_headers() -> dict[str, str]:
    return {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type":   "application/json",
    }


async def _api_request_with_retry(
    client: httpx.AsyncClient,
    method:  str,
    url:     str,
    *,
    json:    Optional[dict] = None,
    params:  Optional[dict] = None,
) -> tuple[bool, Optional[dict], Optional[str]]:
    """
    Notion API 요청 — 제1원칙 Rate Limit 완전 방어.

    429 → Retry-After 헤더 준수 (없으면 5초 fallback)
    500/503 → 지수 백오프 (2^attempt 초, 최대 _MAX_BACKOFF)
    4xx (429 제외) → 즉시 실패 (재시도 무의미)
    타임아웃/접속오류 → 지수 백오프

    Returns:
        (success, response_dict_or_none, error_message_or_none)
    """
    last_error: Optional[str] = None

    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.request(
                method,
                url,
                json=json,
                params=params,
                headers=_notion_headers(),
                timeout=_DEFAULT_TIMEOUT,
            )

            # ── 성공 ──────────────────────────────────────────────
            if resp.status_code == 200:
                return True, resp.json(), None

            # ── 429 Rate Limit: Retry-After 헤더 완전 준수 ────────
            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After", "")
                try:
                    wait = float(retry_after_raw)
                except (ValueError, TypeError):
                    wait = 5.0   # 헤더 없을 때 fallback
                logger.warning(
                    f"[NOTION] 429 Rate Limit — Retry-After={wait:.1f}s "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})"
                )
                await asyncio.sleep(wait)
                last_error = f"429_RATE_LIMIT retry_after={wait}"
                continue

            # ── 500 / 503 서버 오류: Exponential Backoff ──────────
            if resp.status_code in (500, 503):
                wait = min(_BACKOFF_BASE ** attempt, _MAX_BACKOFF)
                logger.warning(
                    f"[NOTION] HTTP {resp.status_code} — backoff={wait:.1f}s "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})"
                )
                await asyncio.sleep(wait)
                last_error = f"HTTP_{resp.status_code} backoff={wait}"
                continue

            # ── 기타 4xx: 즉시 실패 (재시도 불필요) ─────────────────
            error_body = resp.text[:500]
            return False, None, f"HTTP_{resp.status_code}: {error_body}"

        except httpx.TimeoutException:
            wait = min(_BACKOFF_BASE ** attempt, _MAX_BACKOFF)
            logger.warning(f"[NOTION] 타임아웃 — backoff={wait:.1f}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)
            last_error = "TIMEOUT"

        except httpx.ConnectError as exc:
            wait = min(_BACKOFF_BASE ** attempt, _MAX_BACKOFF)
            logger.warning(f"[NOTION] 접속 실패: {exc} — backoff={wait:.1f}s")
            await asyncio.sleep(wait)
            last_error = f"CONNECT_ERROR: {str(exc)[:200]}"

    return False, None, f"MAX_RETRIES_EXCEEDED last_error={last_error}"


# ══════════════════════════════════════════════════════════════════
# SECTION 3: Page ID 조회 캐시 (제3원칙 관계형 ID 매핑)
# ══════════════════════════════════════════════════════════════════

def _cache_get(key: str) -> Optional[str]:
    entry = _page_id_cache.get(key)
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    return None


def _cache_set(key: str, page_id: str) -> None:
    _page_id_cache[key] = (page_id, time.monotonic() + _CACHE_TTL_SEC)


async def lookup_page_id(
    client:    httpx.AsyncClient,
    db_id:     str,
    prop_name: str,
    prop_val:  str,
) -> Optional[str]:
    """
    Notion 마스터 DB를 조회하여 내부 ID에 해당하는 Page ID 반환.
    캐시 HIT 시 API 호출 없음 (Hot Path 지연 0 추가).

    Args:
        db_id:     조회 대상 Notion DB ID (RESIDENT_DB_ID or CAREGIVER_DB_ID)
        prop_name: 매칭 속성명 (e.g. "수급자 ID", "보호사 ID")
        prop_val:  매칭 속성값 (e.g. beneficiary_id, caregiver_id)

    Returns:
        Notion Page ID (UUID 형식) 또는 None (미발견 / API 오류)
    """
    cache_key = f"{db_id}::{prop_name}::{prop_val}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    url  = f"{NOTION_BASE_URL}/databases/{db_id}/query"
    body = {
        "filter": {
            "property": prop_name,
            "rich_text": {"equals": prop_val},
        },
        "page_size": 1,
    }

    success, data, err = await _api_request_with_retry(client, "POST", url, json=body)
    if not success or not data:
        logger.error(f"[LOOKUP] DB 조회 실패 db={db_id} val={prop_val!r}: {err}")
        return None

    results = data.get("results", [])
    if not results:
        logger.warning(f"[LOOKUP] Page 미발견 db={db_id} prop={prop_name} val={prop_val!r}")
        return None

    page_id = results[0].get("id", "")
    if page_id:
        _cache_set(cache_key, page_id)
        logger.info(f"[LOOKUP] 캐시 저장 — val={prop_val!r} → page_id={page_id[:8]}…")
    return page_id or None


# ══════════════════════════════════════════════════════════════════
# SECTION 4: Hot Path — VG_운영_대시보드_DB 단 1행 생성
# (제2원칙 Hot Path: 1행, 5대 체크박스, Relation 연결)
# ══════════════════════════════════════════════════════════════════

_CATEGORY_EMOJI = {
    "meal":          "🍚",
    "medication":    "💊",
    "excretion":     "🚽",
    "repositioning": "🔄",
    "hygiene":       "🧼",
    "special_notes": "📋",
}
_CATEGORY_KO = {
    "meal":          "식사",
    "medication":    "투약",
    "excretion":     "배설",
    "repositioning": "체위변경",
    "hygiene":       "위생",
    "special_notes": "특이사항",
}


def _detail_to_rich_text(detail: Optional[dict], max_chars: int = 2000) -> str:
    """detail dict → Notion rich_text용 단일 문자열."""
    if not detail:
        return "특이소견 없음"
    parts = []
    if detail.get("situation"):
        parts.append(f"상황: {detail['situation']}")
    if detail.get("action"):
        parts.append(f"조치: {detail['action']}")
    if detail.get("notes"):
        parts.append(f"특이: {detail['notes']}")
    return " | ".join(parts)[:max_chars] if parts else "특이소견 없음"


def _build_care_detail_blocks(record: "CareRecordInput") -> list[dict]:
    """
    done=True 카테고리의 detail을 Notion 페이지 본문 블록으로 변환.
    각 카테고리 → callout 블록 (이모지 + 3단 요약 텍스트).
    """
    blocks: list[dict] = []

    categories = [
        ("meal",          record.meal),
        ("medication",    record.medication),
        ("excretion",     record.excretion),
        ("repositioning", record.repositioning),
        ("hygiene",       record.hygiene),
        ("special_notes", record.special_notes),
    ]

    done_items = [(cat, flag) for cat, flag in categories if flag.done]
    if not done_items:
        return blocks

    # 섹션 헤더
    blocks.append({
        "object": "block",
        "type":   "heading_3",
        "heading_3": {
            "rich_text": [{"type": "text", "text": {"content": "케어 상세 기록"}}],
            "color": "default",
        },
    })

    for cat, flag in done_items:
        emoji   = _CATEGORY_EMOJI.get(cat, "📌")
        cat_ko  = _CATEGORY_KO.get(cat, cat)
        content = _detail_to_rich_text(flag.detail, max_chars=1800)
        blocks.append({
            "object": "block",
            "type":   "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"[{cat_ko}] {content}"},
                    }
                ],
                "icon":  {"type": "emoji", "emoji": emoji},
                "color": "gray_background",
            },
        })

    return blocks


async def create_ops_dashboard_row(
    record:            "CareRecordInput",
    resident_page_id:  Optional[str],
    caregiver_page_id: Optional[str],
    care_record_id:    str,
    client:            httpx.AsyncClient,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    VG_운영_대시보드_DB에 단 1행 생성.

    제2원칙 Hot Path 규칙:
      - 행 1개만 생성 (복수 행 생성 금지)
      - 5대 체크박스(식사·투약·배설·체위변경·위생) 완전 매핑
      - Resident / Caregiver Relation 속성에 Page ID 직접 매핑
      - detail 3단 요약 → 페이지 본문 callout 블록으로 렌더링

    Returns:
        (success, notion_page_id_or_none, error_message_or_none)
    """
    if not NOTION_OPS_DASHBOARD_DB_ID:
        return False, None, "NOTION_OPS_DASHBOARD_DB_ID 미설정 — 사령관 입력 필요"

    ts         = record.recorded_at or ""
    title_text = (
        f"[{ts[:10] or 'now'}] {record.beneficiary_id} / {record.facility_id}"
    )[:100]

    # ── 5대 체크박스 + 정제발화 + 특이사항 rich_text ─────────────
    corrected = (
        record.corrected_transcript or record.raw_voice_text or ""
    )[:2000]
    special_text = _detail_to_rich_text(record.special_notes.detail)

    properties: dict = {
        "보고서 제목": {"title": [{"text": {"content": title_text}}]},
        # ── 5대 체크박스 완전 매핑 (G-02 해소) ────────────────────
        "식사":     {"checkbox": record.meal.done},
        "투약":     {"checkbox": record.medication.done},
        "배설":     {"checkbox": record.excretion.done},
        "체위 변경": {"checkbox": record.repositioning.done},
        "위생":     {"checkbox": record.hygiene.done},
        # ── 텍스트 필드 (G-04 해소) ───────────────────────────────
        "정제발화": {
            "rich_text": [{"type": "text", "text": {"content": corrected}}]
        },
        "특이사항 ": {
            "rich_text": [{"type": "text", "text": {"content": special_text}}]
        },
    }

    if record.recorded_at:
        properties["발생일시"] = {"date": {"start": record.recorded_at}}

    # ── 제3원칙: Relation — Page ID 직접 매핑 ────────────────────
    if resident_page_id:
        properties["입소자 연결"] = {"relation": [{"id": resident_page_id}]}
    else:
        logger.warning(
            f"[HOT-PATH] Resident page_id 미발견 — beneficiary_id={record.beneficiary_id!r} "
            "Relation 속성 생략"
        )

    if caregiver_page_id:
        properties["담당 보호사 연결"] = {"relation": [{"id": caregiver_page_id}]}
    else:
        logger.warning(
            f"[HOT-PATH] Caregiver page_id 미발견 — caregiver_id={record.caregiver_id!r} "
            "Relation 속성 생략"
        )

    # ── 페이지 본문: done=True 카테고리 callout 블록 ──────────────
    children = _build_care_detail_blocks(record)

    body: dict = {"parent": {"database_id": NOTION_OPS_DASHBOARD_DB_ID}, "properties": properties}
    if children:
        body["children"] = children

    url = f"{NOTION_BASE_URL}/pages"
    success, data, err = await _api_request_with_retry(client, "POST", url, json=body)
    if not success:
        return False, None, err

    page_id: str = (data or {}).get("id", "")
    logger.info(
        f"[HOT-PATH] 1행 생성 완료 — care_record_id={care_record_id[:8]}… "
        f"page_id={page_id[:8]}… "
        f"meal={record.meal.done} med={record.medication.done} exc={record.excretion.done} "
        f"repo={record.repositioning.done} hyg={record.hygiene.done} "
        f"blocks={len(children)}"
    )
    return True, page_id, None


# ══════════════════════════════════════════════════════════════════
# SECTION 5: Cold Path — VG_Atomic_Care_Events 원자 이벤트 적재
# (제2원칙 Cold Path: 비동기 큐, 카테고리별 1행씩 분해 적재)
# ══════════════════════════════════════════════════════════════════

_HOT_CATEGORIES = ("meal", "medication", "excretion", "repositioning", "hygiene")


def decompose_to_atomic_events(
    record:         CareRecordInput,
    care_record_id: str,
) -> list[AtomicCareEvent]:
    """
    CareRecordInput → 원자 이벤트 목록 분해 (done=True 항목만).
    Cold Path 비동기 큐에 넣기 전 단계.
    """
    events: list[AtomicCareEvent] = []
    categories = {
        "meal":          record.meal,
        "medication":    record.medication,
        "excretion":     record.excretion,
        "repositioning": record.repositioning,
        "hygiene":       record.hygiene,
        "special_notes": record.special_notes,
    }
    for cat, flag in categories.items():
        if flag.done:
            events.append(AtomicCareEvent(
                care_record_id=care_record_id,
                facility_id=record.facility_id,
                beneficiary_id=record.beneficiary_id,
                caregiver_id=record.caregiver_id,
                category=cat,
                done=flag.done,
                detail=flag.detail,
                recorded_at=record.recorded_at or "",
            ))
    return events


async def push_atomic_event(
    event:                 AtomicCareEvent,
    client:                httpx.AsyncClient,
    ops_dashboard_page_id: Optional[str] = None,
    resident_page_id:      Optional[str] = None,
    caregiver_page_id:     Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    VG_상세_행위_원장_DB에 원자 이벤트 1행 적재.
    Cold Path 비동기 큐 워커에서 호출 — 제1원칙 재시도 적용.

    실증 확인 속성명 (VG_상세_행위_원장_DB / 344dbdd0...):
      '이름'        → title
      '상위 보고서' → relation → VG_운영_대시보드_DB  ← Hot Path page_id Relation 직접 연결
      '카테고리'    → select   (meal / medication / excretion / repositioning / hygiene)
      '입소자 연결' → relation → RESIDENT_DB (선택)
      '담당자 연결' → relation → CAREGIVER_DB (선택)

    Args:
        ops_dashboard_page_id: Hot Path VG_운영_대시보드_DB page_id → '상위 보고서' Relation
        resident_page_id:      RESIDENT_DB page_id → '입소자 연결' Relation (선택)
        caregiver_page_id:     CAREGIVER_DB page_id → '담당자 연결' Relation (선택)
    """
    if not NOTION_ATOMIC_EVENTS_DB_ID:
        return False, None, "NOTION_ATOMIC_EVENTS_DB_ID 미설정 — 사령관 입력 필요"

    # 제목: [카테고리] YYYY-MM-DD HH:MM
    ts = event.recorded_at[:16] if event.recorded_at else ""
    title_text = f"[{event.category}] {ts}"[:100]

    care_content = _detail_to_rich_text(event.detail, max_chars=2000)

    properties: dict = {
        "이름": {
            "title": [{"text": {"content": title_text}}]
        },
        "카테고리": {
            "select": {"name": event.category}
        },
        # ── G-04: detail 3단 요약 텍스트 전달 ────────────────────
        "케어 내용": {
            "rich_text": [{"type": "text", "text": {"content": care_content}}]
        },
    }

    # '상위 보고서' — Relation 객체 1배열 (텍스트 입력 절대 금지)
    if ops_dashboard_page_id:
        properties["상위 보고서"] = {
            "relation": [{"id": ops_dashboard_page_id}]
        }
    else:
        logger.warning(
            f"[COLD-PATH] ops_dashboard_page_id 없음 — "
            f"cat={event.category} '상위 보고서' Relation 생략"
        )

    # '입소자 연결' — Relation (선택)
    if resident_page_id:
        properties["입소자 연결"] = {"relation": [{"id": resident_page_id}]}

    # '담당자 연결' — Relation (선택)
    if caregiver_page_id:
        properties["담당자 연결"] = {"relation": [{"id": caregiver_page_id}]}

    body = {
        "parent":     {"database_id": NOTION_ATOMIC_EVENTS_DB_ID},
        "properties": properties,
    }
    url = f"{NOTION_BASE_URL}/pages"

    success, data, err = await _api_request_with_retry(client, "POST", url, json=body)
    if not success:
        logger.error(
            f"[COLD-PATH] 적재 실패 — cat={event.category} err={err}"
        )
        return False, None, err

    page_id = (data or {}).get("id", "")
    logger.info(
        f"[COLD-PATH] 적재 완료 — cat={event.category} "
        f"page_id={page_id[:8]}… 상위={ops_dashboard_page_id[:8] if ops_dashboard_page_id else 'None'}…"
    )
    return True, page_id, None


# ══════════════════════════════════════════════════════════════════
# SECTION 6: 통합 진입점 — Hot + Cold Path 순차 실행
# ══════════════════════════════════════════════════════════════════

async def run_pipeline(
    gemini_care_json: dict,
    facility_id:      str,
    beneficiary_id:   str,
    caregiver_id:     str,
    care_record_id:   str,
    raw_voice_text:   str = "",
    recorded_at:      Optional[str] = None,
) -> dict:
    """
    Voice Guard Notion 파이프라인 통합 진입점.

    1. Pydantic 검증 (입력 방어막)
    2. Resident / Caregiver Page ID 조회 (마스터 DB Relation 매핑)
    3. Hot Path: VG_운영_대시보드_DB 1행 생성
    4. Cold Path: 원자 이벤트 분해 → 비동기 큐 적재

    Args:
        gemini_care_json: call_gemini_care_record() 반환 dict
        facility_id:      기관 ID
        beneficiary_id:   수급자 ID
        caregiver_id:     요양보호사 ID
        care_record_id:   PostgreSQL UUID
        raw_voice_text:   원본 발화 텍스트
        recorded_at:      ISO-8601 타임스탬프 (없으면 현재 시각)

    Returns:
        {
          "hot_path": {"success": bool, "page_id": str|None, "error": str|None},
          "cold_path": {"total": int, "success": int, "failed": int},
        }
    """
    result: dict = {
        "hot_path":  {"success": False, "page_id": None, "error": None},
        "cold_path": {"total": 0, "success": 0, "failed": 0},
    }

    # ── Step 1: Pydantic 검증 ────────────────────────────────────
    try:
        record = CareRecordInput(
            facility_id=facility_id,
            beneficiary_id=beneficiary_id,
            caregiver_id=caregiver_id,
            raw_voice_text=raw_voice_text or "미확인",
            corrected_transcript=gemini_care_json.get("corrected_transcript") or raw_voice_text,
            meal=         CareFlag(**gemini_care_json.get("meal",          {"done": False})),
            medication=   CareFlag(**gemini_care_json.get("medication",    {"done": False})),
            excretion=    CareFlag(**gemini_care_json.get("excretion",     {"done": False})),
            repositioning=CareFlag(**gemini_care_json.get("repositioning", {"done": False})),
            hygiene=      CareFlag(**gemini_care_json.get("hygiene",       {"done": False})),
            special_notes=CareFlag(**gemini_care_json.get("special_notes", {"done": False})),
            recorded_at=recorded_at or gemini_care_json.get("recorded_at"),
        )
    except Exception as exc:
        logger.error(f"[PIPELINE] Pydantic 검증 실패: {exc}")
        result["hot_path"]["error"] = f"VALIDATION_ERROR: {exc}"
        return result

    async with httpx.AsyncClient() as client:
        # ── Step 2: Relation Page ID 조회 (병렬, 캐시 우선) ──────
        resident_page_id, caregiver_page_id = await asyncio.gather(
            lookup_page_id(client, RESIDENT_DB_ID,  RESIDENT_LOOKUP_PROP,  beneficiary_id),
            lookup_page_id(client, CAREGIVER_DB_ID, CAREGIVER_LOOKUP_PROP, caregiver_id),
        )

        # ── Step 3: Hot Path (1행 생성) ──────────────────────────
        hot_ok, hot_page_id, hot_err = await create_ops_dashboard_row(
            record=record,
            resident_page_id=resident_page_id,
            caregiver_page_id=caregiver_page_id,
            care_record_id=care_record_id,
            client=client,
        )
        result["hot_path"] = {
            "success": hot_ok,
            "page_id": hot_page_id,
            "error":   hot_err,
        }

        # ── Step 4: Cold Path (원자 이벤트 비동기 적재) ──────────
        if NOTION_ATOMIC_EVENTS_DB_ID:
            events = decompose_to_atomic_events(record, care_record_id)
            result["cold_path"]["total"] = len(events)
            hot_page_id = result["hot_path"].get("page_id")

            for event in events:
                ok, _, _ = await push_atomic_event(
                    event, client,
                    ops_dashboard_page_id=hot_page_id,
                    resident_page_id=resident_page_id,
                    caregiver_page_id=caregiver_page_id,
                )
                if ok:
                    result["cold_path"]["success"] += 1
                else:
                    result["cold_path"]["failed"] += 1
        else:
            logger.info(
                "[PIPELINE] NOTION_ATOMIC_EVENTS_DB_ID 미설정 — Cold Path 건너뜀"
            )

    logger.info(
        f"[PIPELINE] 완료 — hot={result['hot_path']['success']} "
        f"cold={result['cold_path']['success']}/{result['cold_path']['total']} "
        f"care_record_id={care_record_id[:8]}…"
    )
    return result
