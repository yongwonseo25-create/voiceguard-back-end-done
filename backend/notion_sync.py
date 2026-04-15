"""
Voice Guard — backend/notion_sync.py
Rate-Limit 방어형 Notion 미러 동기화 워커 v2.0

[설계 원칙]
  1. Notion은 '읽기 중심 미러(System of Context)' — 실시간 운영은 Web Admin 전담
  2. Notion API 초당 3건 제한(429) 대응 → Token Bucket (2.5 req/s)
  3. 429 수신 시 Retry-After 헤더 준수 + 지수 백오프
  4. 연속 3회 실패 → DLQ 이관 (수동 재처리)
  5. 메인 DB 트랜잭션과 완벽 분리 (비동기 루프)

[파이프라인]
  notion_sync_outbox (pending)
      ↓ 폴링 (5초 주기)
  Token Bucket 대기
      ↓ 라우팅 분기
        gemini_json     → 5-Block 인수인계 페이지 (NOTION_HANDOVER_DB_ID)
        care_record_json → 일일 케어 기록 페이지 (NOTION_CARE_RECORD_DB_ID)
        둘 다 없음       → 증거 감사 DB (NOTION_DATABASE_ID)
  성공 → status='synced', notion_page_id 기록
  실패 → attempts++, 지수 백오프 대기
  3회 실패 → DLQ 이관 (dead_letter_queue INSERT + status='dlq')

[상태 파이프라인]
  INGESTED → SEALED → WORM_STORED → SYNCING → SYNCED

[환경변수]
  NOTION_API_KEY               — Notion Integration 토큰
  NOTION_DATABASE_ID           — 증거 감사 DB ID (기존)
  NOTION_HANDOVER_DB_ID        — 인수인계 1장 템플릿 DB ID
  NOTION_HANDOVER_TITLE_PROP   — 인수인계 DB 타이틀 속성명 (기본값: "보고서 제목")
  NOTION_CARE_RECORD_DB_ID     — 일일 케어 기록 DB ID (신규)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone as tz
from typing import Optional

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("voice_guard.notion_sync")

# ── 설정 ────────────────────────────────────────────────────────
DATABASE_URL             = os.getenv("DATABASE_URL")
NOTION_API_KEY           = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID       = os.getenv("NOTION_DATABASE_ID", "")      # 증거 감사 DB (기존)
NOTION_HANDOVER_DB_ID    = os.getenv("NOTION_HANDOVER_DB_ID", "")   # 인수인계 1장 템플릿 DB
NOTION_CARE_RECORD_DB_ID = os.getenv("NOTION_CARE_RECORD_DB_ID", "") # 일일 케어 기록 DB (신규)
NOTION_API_VERSION       = "2022-06-28"
NOTION_BASE_URL          = "https://api.notion.com/v1"

# Token Bucket 설정
TB_CAPACITY       = 3           # 버킷 최대 용량
TB_REFILL_RATE    = 2.5         # 초당 충전 속도 (Notion 제한 3 rps 미만)
TB_COST           = 1           # 요청당 소비

# DLQ 설정
MAX_ATTEMPTS      = 3           # 3회 실패 시 DLQ 이관
POLL_INTERVAL_SEC = 5           # 폴링 주기 (초)
BATCH_SIZE        = 5           # 1회 폴링 배치 크기

# 지수 백오프 단계 (초)
BACKOFF_SCHEDULE  = [5, 15, 60]

# ── DB 엔진 ─────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_size=3,
    max_overflow=5,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None


# ══════════════════════════════════════════════════════════════════
# Token Bucket (In-Memory, 단일 워커용)
# ══════════════════════════════════════════════════════════════════

class TokenBucket:
    """
    In-Memory Token Bucket — Notion API 초당 3건 제한 방어.
    단일 워커 프로세스 전용 (멀티 워커 시 Redis 기반으로 전환).
    """
    def __init__(self, capacity: float, refill_rate: float):
        self.capacity    = capacity
        self.refill_rate = refill_rate
        self.tokens      = capacity
        self.last_refill = time.monotonic()

    def _refill(self):
        now           = time.monotonic()
        elapsed       = now - self.last_refill
        self.tokens   = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    async def acquire(self, cost: float = 1.0, max_wait: float = 30.0) -> bool:
        waited = 0.0
        while waited < max_wait:
            self._refill()
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            deficit    = cost - self.tokens
            sleep_time = min(deficit / self.refill_rate + 0.05, 1.0)
            await asyncio.sleep(sleep_time)
            waited += sleep_time
        logger.warning("[TOKEN-BUCKET] 토큰 확보 타임아웃 — 작업 강행")
        return False


bucket = TokenBucket(capacity=TB_CAPACITY, refill_rate=TB_REFILL_RATE)


# ══════════════════════════════════════════════════════════════════
# Notion API 클라이언트 — 증거 감사 DB (기존)
# ══════════════════════════════════════════════════════════════════

async def notion_create_page(
    client: httpx.AsyncClient,
    payload: dict,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Notion 증거 감사 Database에 페이지 생성.

    Returns:
        (success, page_id_or_none, error_message_or_none)
    """
    properties = {
        "원장 ID":   {"title": [{"text": {"content": payload.get("ledger_id", "")[:36]}}]},
        "요양기관":  {"rich_text": [{"text": {"content": payload.get("facility_id", "")}}]},
        "수급자 ID": {"rich_text": [{"text": {"content": payload.get("beneficiary_id", "")}}]},
        "교대 ID":   {"rich_text": [{"text": {"content": payload.get("shift_id", "")}}]},
        "급여 유형": {"rich_text": [{"text": {"content": payload.get("care_type", "") or ""}}]},
        "수집 시각": {"rich_text": [{"text": {"content": payload.get("ingested_at", "")}}]},
        "해시 체인": {"rich_text": [{"text": {"content": payload.get("chain_hash", "")[:24] + "..."}}]},
        "WORM 키":   {"rich_text": [{"text": {"content": payload.get("worm_object_key", "")}}]},
        "동기화 상태": {"select": {"name": "SYNCED"}},
    }

    body = {
        "parent":     {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    try:
        resp = await client.post(
            f"{NOTION_BASE_URL}/pages",
            json=body,
            headers={
                "Authorization":  f"Bearer {NOTION_API_KEY}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type":   "application/json",
            },
            timeout=15.0,
        )

        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            return True, page_id, None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            return False, None, f"429_RATE_LIMIT:retry_after={retry_after}"

        error_body = resp.text[:500]
        return False, None, f"HTTP_{resp.status_code}: {error_body}"

    except httpx.TimeoutException:
        return False, None, "TIMEOUT: Notion API 응답 없음 (15초 초과)"
    except httpx.ConnectError as e:
        return False, None, f"CONNECT_ERROR: {str(e)[:200]}"
    except Exception as e:
        return False, None, f"UNKNOWN: {str(e)[:200]}"


# ══════════════════════════════════════════════════════════════════
# 5-Block 인수인계 1장 템플릿 헬퍼
# ══════════════════════════════════════════════════════════════════

# 케어 체크리스트 한글 라벨 맵 (순서 고정 — A4 인쇄 레이아웃 기준)
_CARE_LABELS: dict = {
    "meal_morning": "식사 (아침)",
    "meal_lunch":   "식사 (점심)",
    "meal_dinner":  "식사 (저녁)",
    "bowel":        "배변 활동",
    "medication":   "투약 확인",
    "hygiene":      "목욕/위생",
    "activity":     "활동/운동",
    "sleep":        "수면 상태",
}


def _rich(content: str, color: Optional[str] = None) -> list:
    """Notion rich_text 단일 항목 생성 헬퍼"""
    node: dict = {"type": "text", "text": {"content": content[:2000]}}
    if color:
        node["annotations"] = {"color": color}
    return [node]


def _infer_shift(shift_start: str) -> str:
    """
    shift_start "HH:MM" → 노션 select 값 결정론적 변환.

    06:00–13:59 → "아침조"
    14:00–21:59 → "오후조"
    22:00–05:59 → "야간조"
    파싱 실패 / "00:00" → "미정"
    """
    try:
        h = int(str(shift_start).split(":")[0])
    except (ValueError, AttributeError, IndexError):
        return "미정"
    if 6 <= h < 14:
        return "아침조"
    if 14 <= h < 22:
        return "오후조"
    if h >= 22 or h < 6:
        return "야간조"
    return "미정"


def _build_handover_blocks(data: dict) -> list:
    """
    Gemini JSON → Notion 5단 블록 구조 변환.

    BLOCK 1: 페이지 헤더 (Callout, gray)
    BLOCK 2: 긴급 콜아웃 (incidents → RED / 없음 → GREEN 폴백)
    BLOCK 3: 일상 케어 체크리스트 (heading_3 + to_do × 8)  ← to_do 블록
    BLOCK 4: 교대자 To-Do (heading_3 + to_do / 없으면 안내 텍스트)
    BLOCK 5: 구분선 + 서명 푸터
    """
    blocks: list = []

    # ── BLOCK 1: 페이지 헤더 ──────────────────────────────────────
    header = (
        f"날짜: {data.get('report_date', '미상')}  |  "
        f"근무자: {data.get('worker_name', '미확인')}\n"
        f"담당 수급자: {data.get('resident_name', '미확인')} "
        f"({data.get('resident_id', '')})\n"
        f"근무 시간: {data.get('shift_start', '00:00')} ~ {data.get('shift_end', '00:00')}"
    )
    blocks.append({
        "object": "block",
        "type":   "callout",
        "callout": {
            "rich_text": _rich(header),
            "icon":      {"type": "emoji", "emoji": "📋"},
            "color":     "gray_background",
        },
    })

    # ── BLOCK 2: 긴급 콜아웃 ─────────────────────────────────────
    incidents = data.get("incidents") or []
    if not incidents:
        blocks.append({
            "object": "block",
            "type":   "callout",
            "callout": {
                "rich_text": _rich("✅ 당일 특이사항 없음"),
                "icon":      {"type": "emoji", "emoji": "✅"},
                "color":     "green_background",
            },
        })
    else:
        lines = []
        for inc in incidents:
            icon = "🚨" if inc.get("severity") == "CRITICAL" else "⚠️"
            lines.append(f"{icon} [{inc.get('type', 'OTHER')}] {inc.get('summary', '')}")
        blocks.append({
            "object": "block",
            "type":   "callout",
            "callout": {
                "rich_text": _rich("\n".join(lines)),
                "icon":      {"type": "emoji", "emoji": "🚨"},
                "color":     "red_background",
            },
        })

    # ── BLOCK 3: 케어 체크리스트 헤더 + to_do 블록 ───────────────
    blocks.append({
        "object": "block",
        "type":   "heading_3",
        "heading_3": {"rich_text": _rich("📋 일상 케어 체크리스트")},
    })

    checklist = data.get("care_checklist") or {}
    for key, label in _CARE_LABELS.items():
        item = checklist.get(key) or {"done": False, "note": None}
        note = item.get("note") or ""
        line = label + (f"  —  {note}" if note else "")
        blocks.append({
            "object": "block",
            "type":   "to_do",
            "to_do":  {
                "rich_text": _rich(line),
                "checked":   bool(item.get("done", False)),
            },
        })

    # ── BLOCK 4: 교대자 To-Do ────────────────────────────────────
    blocks.append({
        "object": "block",
        "type":   "heading_3",
        "heading_3": {"rich_text": _rich("📝 교대자 지시사항 (To-Do)")},
    })

    todos = data.get("todos") or []
    if not todos:
        blocks.append({
            "object": "block",
            "type":   "paragraph",
            "paragraph": {"rich_text": _rich("교대 지시 없음")},
        })
    else:
        for todo in todos:
            blocks.append({
                "object": "block",
                "type":   "to_do",
                "to_do":  {
                    "rich_text": _rich(str(todo)[:100]),
                    "checked":   False,
                },
            })

    # ── BLOCK 5: 구분선 + 서명 푸터 ─────────────────────────────
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    footer = (
        f"작성자: {data.get('worker_name', '미확인')}  |  "
        f"서명: ____________  |  "
        f"AI 보조 작성  |  "
        f"생성: {datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    blocks.append({
        "object": "block",
        "type":   "paragraph",
        "paragraph": {"rich_text": _rich(footer, color="gray")},
    })

    return blocks


async def notion_create_handover_page(
    client: httpx.AsyncClient,
    payload: dict,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Gemini JSON → Notion 5-Block 인수인계 1장 템플릿 페이지 생성.

    노션 인수인계 DB 5개 한국어 속성 매핑:
      보고서 제목 (title)    — 페이지 제목
      근무조 (select)        — _infer_shift() 결과
      긴급 특이사항 (rich_text) — incidents 요약
      요약 (rich_text)       — care_checklist done=True 항목 나열
      상태 (status)          — "작성 완료" 고정

    환경변수:
      NOTION_HANDOVER_DB_ID        — 인수인계 DB ID
      NOTION_HANDOVER_TITLE_PROP   — 타이틀 속성명 (기본값: "보고서 제목")

    payload 필수 키:
        gemini_json  — _sanitize() 통과한 구조화 dict
        ledger_id    — 원장 ID (로깅용)

    Returns:
        (success, page_id_or_none, error_message_or_none)
    """
    if not NOTION_HANDOVER_DB_ID:
        return False, None, "NOTION_HANDOVER_DB_ID 미설정"

    gemini_json: dict = payload.get("gemini_json") or {}
    ledger_id:   str  = str(payload.get("ledger_id", ""))

    # ── 타이틀 생성 ───────────────────────────────────────────────
    title_prop = os.getenv("NOTION_HANDOVER_TITLE_PROP", "보고서 제목")
    page_title = (
        f"인수인계 {gemini_json.get('report_date', '날짜미상')}: "
        f"{gemini_json.get('worker_name', '미확인')} → "
        f"{gemini_json.get('resident_name', '미확인')}"
    )[:100]

    # ── 근무조 추론 ───────────────────────────────────────────────
    shift_select = _infer_shift(gemini_json.get("shift_start", "00:00"))

    # ── 긴급 특이사항 요약 ────────────────────────────────────────
    incidents = gemini_json.get("incidents") or []
    if incidents:
        incidents_summary = "\n".join(
            f"[{inc.get('type', 'OTHER')}] {inc.get('summary', '')}"
            for inc in incidents
        )
    else:
        incidents_summary = "특이사항 없음"

    # ── 요약: done=True 항목 나열 ─────────────────────────────────
    checklist = gemini_json.get("care_checklist") or {}
    done_items = [
        label
        for key, label in _CARE_LABELS.items()
        if bool((checklist.get(key) or {}).get("done", False))
    ]
    care_summary = ", ".join(done_items) if done_items else "기록 없음"

    # ── Notion API properties (5개 한국어 속성 정확 매핑) ─────────
    properties = {
        title_prop: {
            "title": [{"type": "text", "text": {"content": page_title}}]
        },
        "근무조": {
            "select": {"name": shift_select}
        },
        "긴급 특이사항": {
            "rich_text": [{"type": "text", "text": {"content": incidents_summary[:2000]}}]
        },
        "요약": {
            "rich_text": [{"type": "text", "text": {"content": care_summary[:2000]}}]
        },
        "상태": {
            "status": {"name": "작성 완료"}
        },
    }

    blocks = _build_handover_blocks(gemini_json)

    body = {
        "parent":     {"database_id": NOTION_HANDOVER_DB_ID},
        "properties": properties,
        "children":   blocks,
    }

    try:
        resp = await client.post(
            f"{NOTION_BASE_URL}/pages",
            json=body,
            headers={
                "Authorization":  f"Bearer {NOTION_API_KEY}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type":   "application/json",
            },
            timeout=20.0,
        )

        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            logger.info(
                f"[NOTION-TEMPLATE] ✅ 인수인계 페이지 생성: "
                f"ledger={ledger_id[:8]} page={page_id[:8]}"
            )
            return True, page_id, None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            return False, None, f"429_RATE_LIMIT:retry_after={retry_after}"

        error_body = resp.text[:500]
        return False, None, f"HTTP_{resp.status_code}: {error_body}"

    except httpx.TimeoutException:
        return False, None, "TIMEOUT: Notion API 응답 없음 (20초 초과)"
    except httpx.ConnectError as e:
        return False, None, f"CONNECT_ERROR: {str(e)[:200]}"
    except Exception as e:
        return False, None, f"UNKNOWN: {str(e)[:200]}"


# ══════════════════════════════════════════════════════════════════
# 일일 케어 기록 페이지 생성 (신규)
# ══════════════════════════════════════════════════════════════════

async def notion_create_care_record_page(
    client: httpx.AsyncClient,
    payload: dict,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    care_record_json → 노션 '일일 케어 기록 DB' 페이지 생성.

    6대 의무기록 속성 매핑:
      수급자명 (title)     — "{beneficiary_id} — {날짜}" 복합 타이틀 (갤러리 스캔 최적화)
      기록 일시 (date)     — recorded_at ISO-8601
      식사 (checkbox)      — bool() 강제 캐스팅
      투약 (checkbox)
      배설 (checkbox)
      체위변경 (checkbox)
      위생 (checkbox)
      특이사항 (rich_text) — 3-way 결정론적 텍스트 (빈 칸 원천 차단)

    [특이사항 3-way 로직]
      done=True  + detail 있음 → detail 원문
      done=True  + detail 없음 → "특이사항 있음 (내용 미입력)"  ← Gemini 추출 실패 방어
      done=False              → "특이사항 없음"                 ← 빈 칸 원천 차단

    [Craft 보강]
      - 페이지 아이콘: 특이사항 있음=🚨 / 없음=✅  (갤러리 카드 즉각 시각화)
      - children 블록: 헤더 callout + 5대 체크리스트 + 특이사항 callout
      - 색상 원칙: 특이사항 있음=red_background / 없음=green_background / 헤더=gray_background
        (보라색·그라데이션 등 AI식 과잉 색상 금지)

    환경변수:
      NOTION_CARE_RECORD_DB_ID — 일일 케어 기록 DB ID (필수)

    payload 필수 키:
        care_record_json  — _sanitize_care_record() 통과한 구조화 dict
        record_id         — care_record_ledger ID (로깅용)

    Returns:
        (success, page_id_or_none, error_message_or_none)
    """
    if not NOTION_CARE_RECORD_DB_ID:
        return False, None, "NOTION_CARE_RECORD_DB_ID 미설정"

    cr: dict       = payload.get("care_record_json") or {}
    record_id: str = str(payload.get("record_id", ""))

    beneficiary_name = str(cr.get("beneficiary_id") or "미확인")[:50]
    recorded_at_raw  = str(cr.get("recorded_at") or "")

    # recorded_at 폴백: 누락 시 현재 UTC
    if not recorded_at_raw:
        recorded_at_raw = datetime.now(tz.utc).isoformat()

    # Notion date 필드: 마이크로초 제거 후 +00:00 고정 (ISO 8601 완전 형식 보장)
    # 예) "2026-04-15T12:26:21.981242+00:00" → "2026-04-15T12:26:21+00:00"
    try:
        _parsed = datetime.fromisoformat(recorded_at_raw.replace("Z", "+00:00"))
        recorded_at = _parsed.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        recorded_at = recorded_at_raw[:19] + "+00:00"

    # 날짜 단축 표기 (갤러리 타이틀 가독성)
    date_short = recorded_at[:10]  # "2026-04-10"

    # ── 6대 의무기록 단일 헬퍼 ──────────────────────────────────────
    # 모든 카테고리는 {"done": bool, "detail": str|None} 구조 보장 (gemini_processor)
    def _care(key: str) -> dict:
        item = cr.get(key)
        if not isinstance(item, dict):
            return {"done": False, "detail": None}
        return {
            "done":   bool(item.get("done", False)),
            "detail": item.get("detail"),
        }

    meal          = _care("meal")
    medication    = _care("medication")
    excretion     = _care("excretion")
    repositioning = _care("repositioning")
    hygiene       = _care("hygiene")
    special_notes = _care("special_notes")

    # ── [BUG FIX] 특이사항 3-way 결정론적 텍스트 ─────────────────────
    # 구 코드: (detail or "")[:2000] → done=False 시 빈 칸 노출 버그
    if special_notes["done"]:
        _detail = (special_notes["detail"] or "").strip()
        special_notes_text = (_detail if _detail else "특이사항 있음 (내용 미입력)")[:2000]
    else:
        special_notes_text = "특이사항 없음"

    # ── 갤러리 아이콘: 특이사항 유무 즉각 시각화 ──────────────────────
    page_icon = "🚨" if special_notes["done"] else "✅"

    # ── 타이틀: "{수급자명} — {날짜}" (갤러리 카드 1줄 스캔 최적화) ──
    page_title = f"{beneficiary_name} — {date_short}"

    # ── Notion API properties (6대 의무기록 정확 매핑) ─────────────────
    # 5개 표준 카테고리: checkbox  /  특이사항: rich_text
    # 모든 done 값은 bool() 강제 — Gemini "true" 문자열 방어
    properties = {
        "수급자명":  {"title":    [{"type": "text", "text": {"content": page_title}}]},
        "기록 일시": {"date":     {"start": recorded_at}},
        "식사":      {"checkbox": meal["done"]},
        "투약":      {"checkbox": medication["done"]},
        "배설":      {"checkbox": excretion["done"]},
        "체위변경":  {"checkbox": repositioning["done"]},
        "위생":      {"checkbox": hygiene["done"]},
        "특이사항":  {
            "rich_text": [{"type": "text", "text": {"content": special_notes_text}}]
        },
    }

    # ── [Craft] 페이지 바디 블록 구성 ──────────────────────────────────
    # BLOCK 1: 헤더 callout (수급자 + 기록 일시) — gray_background (중립)
    # BLOCK 2: heading_3 + 5대 to_do 체크리스트
    # BLOCK 3: divider
    # BLOCK 4: 특이사항 callout — 내용 있음=red / 없음=green

    header_text = (
        f"수급자: {beneficiary_name}\n"
        f"기록 일시: {recorded_at[:16].replace('T', ' ')} UTC"
    )

    _CARE_ROWS = [
        ("식사",    meal),
        ("투약",    medication),
        ("배설",    excretion),
        ("체위변경", repositioning),
        ("위생",    hygiene),
    ]

    checklist_blocks: list = []
    for label, item in _CARE_ROWS:
        note = (item["detail"] or "").strip()
        line = label + (f"  —  {note}" if note else "")
        checklist_blocks.append({
            "object": "block",
            "type":   "to_do",
            "to_do":  {
                "rich_text": [{"type": "text", "text": {"content": line[:200]}}],
                "checked":   item["done"],
            },
        })

    sn_color = "red_background" if special_notes["done"] else "green_background"
    sn_icon  = "🚨"             if special_notes["done"] else "✅"

    children: list = [
        {
            "object": "block",
            "type":   "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": header_text}}],
                "icon":      {"type": "emoji", "emoji": "📋"},
                "color":     "gray_background",
            },
        },
        {
            "object": "block",
            "type":   "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "5대 의무기록"}}]
            },
        },
        *checklist_blocks,
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block",
            "type":   "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"특이사항: {special_notes_text}"[:2000]
                }}],
                "icon":  {"type": "emoji", "emoji": sn_icon},
                "color": sn_color,
            },
        },
    ]

    body = {
        "parent":     {"database_id": NOTION_CARE_RECORD_DB_ID},
        "icon":       {"type": "emoji", "emoji": page_icon},
        "properties": properties,
        "children":   children,
    }

    try:
        resp = await client.post(
            f"{NOTION_BASE_URL}/pages",
            json=body,
            headers={
                "Authorization":  f"Bearer {NOTION_API_KEY}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type":   "application/json",
            },
            timeout=15.0,
        )

        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            logger.info(
                f"[NOTION-CARE] ✅ 케어 기록 페이지 생성: "
                f"record={record_id[:8]} page={page_id[:8]} "
                f"special_notes.done={special_notes['done']}"
            )
            return True, page_id, None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            return False, None, f"429_RATE_LIMIT:retry_after={retry_after}"

        error_body = resp.text[:500]
        return False, None, f"HTTP_{resp.status_code}: {error_body}"

    except httpx.TimeoutException:
        return False, None, "TIMEOUT: Notion API 응답 없음 (15초 초과)"
    except httpx.ConnectError as e:
        return False, None, f"CONNECT_ERROR: {str(e)[:200]}"
    except Exception as e:
        return False, None, f"UNKNOWN: {str(e)[:200]}"


# ══════════════════════════════════════════════════════════════════
# DLQ 이관
# ══════════════════════════════════════════════════════════════════

def send_to_dlq(ledger_id: str, outbox_id: str, reason: str, payload: str):
    """3회 실패 → dead_letter_queue 이관 + notion_sync_outbox status='dlq'"""
    if engine is None:
        logger.critical(f"[DLQ] DB 미연결로 DLQ 이관 불가! ledger={ledger_id}")
        return

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO dead_letter_queue
                    (id, ledger_id, outbox_id, failure_reason, original_payload, detected_at)
                VALUES
                    (gen_random_uuid(), :lid, :oid, :reason, :payload::jsonb, NOW())
            """), {
                "lid":     ledger_id,
                "oid":     outbox_id,
                "reason":  f"[NOTION_SYNC] {reason[:2000]}",
                "payload": payload,
            })

            conn.execute(text("""
                UPDATE notion_sync_outbox
                SET status = 'dlq', processed_at = NOW(), error_message = :reason
                WHERE id = :id
            """), {"id": outbox_id, "reason": reason[:500]})

        logger.critical(
            f"[DLQ] Notion 동기화 DLQ 이관 완료: ledger={ledger_id} | reason={reason[:80]}"
        )
    except Exception as e:
        logger.critical(f"[DLQ] 이관 자체 실패: {e} | ledger={ledger_id}")


# ══════════════════════════════════════════════════════════════════
# 단일 레코드 처리
# ══════════════════════════════════════════════════════════════════

async def process_one(client: httpx.AsyncClient, row) -> None:
    """
    notion_sync_outbox 1건 처리.

    라우팅 분기 (3개):
      1. gemini_json 있음      → notion_create_handover_page()
      2. care_record_json 있음 → notion_create_care_record_page()
      3. 둘 다 없음            → notion_create_page() (증거 감사 DB)
    """
    outbox_id   = str(row.id)
    ledger_id   = str(row.ledger_id)
    attempts    = row.attempts
    payload_str = row.payload if isinstance(row.payload, str) else json.dumps(row.payload)
    payload     = json.loads(payload_str) if isinstance(payload_str, str) else payload_str

    # DLQ 임계값 확인
    if attempts >= MAX_ATTEMPTS:
        send_to_dlq(ledger_id, outbox_id, f"MAX_ATTEMPTS({MAX_ATTEMPTS}) 초과", payload_str)
        return

    # syncing 상태 전환 + attempts 증가
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE notion_sync_outbox
            SET status = 'syncing', attempts = attempts + 1
            WHERE id = :id
        """), {"id": outbox_id})

    logger.info(
        f"[NOTION-SYNC] 처리 시작: ledger={ledger_id} | "
        f"attempt={attempts + 1}/{MAX_ATTEMPTS}"
    )

    # Token Bucket 대기
    await bucket.acquire(cost=TB_COST)

    # ── 3-way 라우팅 ──────────────────────────────────────────────
    if payload.get("gemini_json"):
        # 경로 1: 인수인계 1장 템플릿
        success, page_id, error = await notion_create_handover_page(client, payload)
    elif payload.get("care_record_json"):
        # 경로 2: 일일 케어 기록 DB
        success, page_id, error = await notion_create_care_record_page(client, payload)
    else:
        # 경로 3: 증거 감사 DB (기존)
        success, page_id, error = await notion_create_page(client, payload)

    if success:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE notion_sync_outbox
                SET status = 'synced',
                    notion_page_id = :pid,
                    processed_at = NOW(),
                    error_message = NULL
                WHERE id = :id
            """), {"id": outbox_id, "pid": page_id})

        logger.info(
            f"[NOTION-SYNC] SYNCED: ledger={ledger_id} | page={page_id}"
        )
        return

    # ── 실패 처리 ──
    logger.warning(
        f"[NOTION-SYNC] 실패 attempt={attempts + 1}: "
        f"ledger={ledger_id} | {error}"
    )

    # 429 Rate Limit → Retry-After 헤더 추출 후 대기
    retry_wait = 0
    if error and "429_RATE_LIMIT" in error:
        retry_str = error.split("retry_after=")[-1] if "retry_after=" in error else ""
        try:
            retry_wait = max(int(float(retry_str)), 1) if retry_str else 2
        except ValueError:
            retry_wait = 2
        logger.info(f"[NOTION-SYNC] 429 Rate Limit — {retry_wait}초 대기")
        await asyncio.sleep(retry_wait)

    # 지수 백오프 계산
    backoff_idx = min(attempts, len(BACKOFF_SCHEDULE) - 1)
    backoff_sec = BACKOFF_SCHEDULE[backoff_idx] + retry_wait

    # DLQ 판단 (다음 시도에서 MAX 초과)
    if attempts + 1 >= MAX_ATTEMPTS:
        send_to_dlq(ledger_id, outbox_id, error or "UNKNOWN", payload_str)
        return

    # pending으로 롤백 + 다음 재시도 예약
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE notion_sync_outbox
            SET status = 'pending',
                error_message = :err,
                next_retry_at = NOW() + (:delay || ' seconds')::INTERVAL
            WHERE id = :id AND status = 'syncing'
        """), {
            "id":    outbox_id,
            "err":   (error or "")[:500],
            "delay": backoff_sec,
        })

    logger.info(
        f"[NOTION-SYNC] 재시도 예약: {backoff_sec}초 후 | ledger={ledger_id}"
    )


# ══════════════════════════════════════════════════════════════════
# 메인 워커 루프
# ══════════════════════════════════════════════════════════════════

async def main():
    if engine is None:
        logger.critical("[NOTION-SYNC] DATABASE_URL 미설정. 종료.")
        return

    if not NOTION_API_KEY:
        logger.warning(
            "[NOTION-SYNC] NOTION_API_KEY 미설정 — "
            "Notion API 호출은 실패하지만 큐 폴링 + DLQ 로직은 정상 동작"
        )

    if not NOTION_DATABASE_ID:
        logger.warning("[NOTION-SYNC] NOTION_DATABASE_ID 미설정")

    if not NOTION_HANDOVER_DB_ID:
        logger.warning("[NOTION-SYNC] NOTION_HANDOVER_DB_ID 미설정 — 인수인계 페이지 생성 불가")

    if not NOTION_CARE_RECORD_DB_ID:
        logger.warning("[NOTION-SYNC] NOTION_CARE_RECORD_DB_ID 미설정 — 케어 기록 페이지 생성 불가")

    logger.info(
        f"[NOTION-SYNC] 워커 시작 | "
        f"rate_limit={TB_REFILL_RATE} req/s | "
        f"max_attempts={MAX_ATTEMPTS} | "
        f"poll_interval={POLL_INTERVAL_SEC}s | "
        f"batch={BATCH_SIZE}"
    )

    async with httpx.AsyncClient() as client:
        while True:
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text("""
                        SELECT id, ledger_id, attempts, payload, status
                        FROM notion_sync_outbox
                        WHERE status IN ('pending')
                          AND attempts < :max_a
                          AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                        ORDER BY created_at ASC
                        LIMIT :batch
                    """), {
                        "max_a": MAX_ATTEMPTS,
                        "batch": BATCH_SIZE,
                    }).fetchall()

                if not rows:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue

                logger.info(f"[NOTION-SYNC] {len(rows)}건 폴링됨")

                for row in rows:
                    await process_one(client, row)

            except asyncio.CancelledError:
                logger.info("[NOTION-SYNC] 워커 종료 요청")
                break
            except Exception as e:
                logger.error(f"[NOTION-SYNC] 루프 오류: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
