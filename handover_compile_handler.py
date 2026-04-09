"""
Voice Guard — Phase 6 HandoverCompileHandler (handover_compile_handler.py)
==========================================================================
event_type='handover_compile' 처리기.

[Phase 6 핵심 방어]
  ① 결정론적 멱등성 키: sha256(worker_id + shift_date) — 클라이언트 키 무시
  ② Gemini API JSON 스키마 강제 응답 (structured output)
  ③ Gemini 장애(Timeout/5xx) → 즉각 RAW_FALLBACK + ⚠️ 경고 헤더 (Notion 빈 블록 차단)
  ④ Gemini 장애 → 관리자 카카오 알림톡 NT-5 트리거
  ⑤ Notion 스냅샷 sha256 기록 → ACK 시 위변조 감지 기반 제공

[TTS 제거]
  pyttsx3 / gTTS / OpenAI TTS: 백엔드 파이프라인에서 완전 제거.
  인수인계는 텍스트(Notion) + 알림톡으로 전달한다.

[Notion 2-mode 템플릿]
  LLM 모드:      구조화된 섹션 블록 (urgent/patient/memo/completed)
  Fallback 모드: ⚠️ 경고 헤더 + 원문 텍스트 블록 (빈 페이지 절대 금지)
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logger = logging.getLogger("handover_compile")
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── 설정 ─────────────────────────────────────────────────────
DATABASE_URL      = os.getenv("DATABASE_URL")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL        = (
    f"https://generativelanguage.googleapis.com/v1beta/models"
    f"/{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT    = int(os.getenv("GEMINI_TIMEOUT_SEC", "30"))

NOTION_API_KEY    = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID= os.getenv("NOTION_HANDOVER_DB_ID", "")
NOTION_API_URL    = "https://api.notion.com/v1"
NOTION_VERSION    = "2022-06-28"

_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None


# ══════════════════════════════════════════════════════════════
# 멱등성 키 생성 (결함 방어: 클라이언트 키 무시, 서버 결정론적 생성)
# ══════════════════════════════════════════════════════════════

def make_report_idempotency_key(worker_id: str, shift_date: str) -> str:
    """
    POST /api/v6/handover/trigger 에서 클라이언트 키를 무시하고
    서버가 이 함수로 결정론적 키를 생성한다.

    sha256(worker_id || ":" || shift_date) → CHAR(64)
    """
    raw = f"{worker_id}:{shift_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_utterance_idempotency_key(
    worker_id: str,
    shift_date: str,
    device_id: str,
    recorded_at_utc: str,
) -> str:
    """
    handover_utterance_ledger 수시 발화 기록 멱등성 키.
    sha256(worker_id||shift_date||device_id||recorded_at_utc)
    """
    raw = f"{worker_id}:{shift_date}:{device_id}:{recorded_at_utc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════
# Step 1: 발화 기록 집계 (recorded_at 기준 — 결함 2 방어)
# ══════════════════════════════════════════════════════════════

def _aggregate_utterances(conn, facility_id: str, shift_date: str) -> list[dict]:
    """
    handover_utterance_ledger 에서 해당 근무일 발화 기록 집계.
    recorded_at 기준: ingested_at > shift_end 오프라인 지연 레코드도 정확히 포착.
    """
    rows = conn.execute(text("""
        SELECT
            id, recorded_at, ingested_at,
            transcript_text, beneficiary_id, device_id
        FROM public.handover_utterance_ledger
        WHERE facility_id = :fid
          AND shift_date  = :sdate
        ORDER BY recorded_at ASC
    """), {"fid": facility_id, "sdate": shift_date}).fetchall()

    return [
        {
            "id":              str(r.id),
            "recorded_at":     r.recorded_at,
            "transcript_text": r.transcript_text or "",
            "beneficiary_id":  r.beneficiary_id or "",
            "device_id":       r.device_id or "",
            "late_ingest":     (r.ingested_at - r.recorded_at).total_seconds() > 3600
                               if r.ingested_at and r.recorded_at else False,
        }
        for r in rows
    ]


def _aggregate_anomalies(conn, facility_id: str, shift_date: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT anomaly_code, result_status, beneficiary_id, care_type, fact_date, anomaly_detail
        FROM public.reconciliation_result
        WHERE facility_id   = :fid
          AND result_status IN ('ANOMALY', 'PARTIAL')
          AND fact_date      = :sdate::DATE
        ORDER BY result_status DESC, fact_date ASC
    """), {"fid": facility_id, "sdate": shift_date}).fetchall()

    return [
        {
            "anomaly_code":   r.anomaly_code or "",
            "result_status":  r.result_status,
            "beneficiary_id": r.beneficiary_id,
            "care_type":      r.care_type,
            "fact_date":      str(r.fact_date),
            "detail":         r.anomaly_detail or {},
        }
        for r in rows
    ]


# ══════════════════════════════════════════════════════════════
# Step 2: Gemini API 호출 (JSON 스키마 강제)
# ══════════════════════════════════════════════════════════════

# Gemini structured output 강제 스키마
_HANDOVER_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "urgent_items": {
            "type":  "ARRAY",
            "items": {"type": "STRING"},
            "description": "즉시 대응 필요 항목 (ANOMALY/PARTIAL). 없으면 빈 배열.",
        },
        "patient_notes": {
            "type":  "ARRAY",
            "items": {"type": "STRING"},
            "description": "수급자 특이사항. 없으면 빈 배열.",
        },
        "handover_memos": {
            "type":  "ARRAY",
            "items": {"type": "STRING"},
            "description": "인수인계 음성 메모 요약 (시간순). 없으면 빈 배열.",
        },
        "completed_summary": {
            "type":        "STRING",
            "description": "정상 처리된 루틴 한 줄 요약.",
        },
    },
    "required": ["urgent_items", "patient_notes", "handover_memos", "completed_summary"],
}

_SYSTEM_INSTRUCTION = (
    "너는 요양 현장의 베테랑 수간호사다. "
    "제공된 데이터만을 근거로 다음 근무자를 위한 인수인계 브리핑을 작성하라. "
    "입력에 없는 내용은 절대 추론하지 말 것. "
    "urgent_items 에는 ANOMALY/PARTIAL 이상 탐지 항목만 기록한다. "
    "총 문자 수 300자 이내. 환자 ID는 원본 그대로 표기."
)


async def _call_gemini(
    utterances: list[dict],
    anomalies:  list[dict],
    facility_id: str,
    shift_date:  str,
) -> dict:
    """
    Gemini API 를 호출하여 JSON 스키마 강제 응답을 반환한다.
    Timeout / HTTP 5xx → RuntimeError 발생 (호출자가 Fallback 전환).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 미설정 — Gemini 호출 불가")

    anomaly_lines = "\n".join(
        f"  [{a['result_status']}] {a['anomaly_code']} "
        f"수급자:{a['beneficiary_id']} 날짜:{a['fact_date']}"
        for a in anomalies
    ) or "  (이상 없음)"

    memo_lines = "\n".join(
        f"  {r['recorded_at'].strftime('%H:%M') if hasattr(r['recorded_at'], 'strftime') else str(r['recorded_at'])[:16]}"
        f" [{r['beneficiary_id']}] {r['transcript_text']}"
        for r in utterances
        if r["transcript_text"].strip()
    ) or "  (메모 없음)"

    user_text = (
        f"[근무 정보]\n기관:{facility_id} 날짜:{shift_date}\n\n"
        f"[이상 탐지 결과]\n{anomaly_lines}\n\n"
        f"[인수인계 음성 메모]\n{memo_lines}"
    )

    request_body = {
        "system_instruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema":   _HANDOVER_RESPONSE_SCHEMA,
            "temperature":      0.2,
            "maxOutputTokens":  1024,
        },
    }

    async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as client:
        resp = await client.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=request_body,
        )
        resp.raise_for_status()

    data = resp.json()
    text_raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text_raw)


# ══════════════════════════════════════════════════════════════
# Step 3: RAW_FALLBACK 생성 (Gemini 장애 시 — 빈 Notion 블록 방지)
# ══════════════════════════════════════════════════════════════

def _build_raw_fallback(utterances: list[dict], anomalies: list[dict]) -> str:
    """
    Gemini 장애 시 즉각 전환하는 폴백 포매터.
    ⚠️ 경고 헤더를 삽입하여 수신자가 AI 요약 실패를 인지하게 한다.
    Notion 빈 블록 생성 절대 금지.
    """
    lines = [
        "⚠️ [AI 요약 장애 — 원문 자동 전달]",
        "담당자 확인 필수: 아래 내용은 AI 미처리 원문입니다.",
        "",
    ]

    if anomalies:
        lines.append("■ [이상 탐지 결과]")
        for a in anomalies:
            lines.append(
                f"  ! [{a['result_status']}] {a['anomaly_code']} "
                f"수급자:{a['beneficiary_id']}"
            )
        lines.append("")

    lines.append("■ [인수인계 메모 원문]")
    memo_found = False
    for r in sorted(utterances, key=lambda x: x["recorded_at"]):
        txt = (r["transcript_text"] or "").strip()
        if not txt:
            continue
        ts = (
            r["recorded_at"].strftime("%H:%M")
            if hasattr(r["recorded_at"], "strftime")
            else str(r["recorded_at"])[:16]
        )
        late = " [지연수신]" if r.get("late_ingest") else ""
        lines.append(f"  {ts}{late} [{r['beneficiary_id']}] {txt}")
        memo_found = True

    if not memo_found:
        lines.append("  (인수인계 메모 없음)")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# Step 4: Notion 2-mode 템플릿 (LLM / FALLBACK)
# ══════════════════════════════════════════════════════════════

def _gemini_json_to_notion_blocks(gemini_json: dict, shift_date: str) -> list[dict]:
    """LLM 모드: Gemini 구조화 JSON → Notion 블록 배열."""

    def heading(text: str) -> dict:
        return {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def bullet(text: str) -> dict:
        return {
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def paragraph(text: str) -> dict:
        return {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    blocks: list[dict] = []

    urgent = gemini_json.get("urgent_items", [])
    if urgent:
        blocks.append(heading("🚨 즉시 대응 필요"))
        blocks.extend(bullet(item) for item in urgent)

    patient = gemini_json.get("patient_notes", [])
    if patient:
        blocks.append(heading("👤 수급자 특이사항"))
        blocks.extend(bullet(item) for item in patient)

    memos = gemini_json.get("handover_memos", [])
    if memos:
        blocks.append(heading("📝 인수인계 메모"))
        blocks.extend(bullet(item) for item in memos)

    completed = gemini_json.get("completed_summary", "")
    if completed:
        blocks.append(heading("✅ 완료 사항"))
        blocks.append(paragraph(completed))

    if not blocks:
        blocks.append(paragraph(f"[{shift_date}] 인수인계 항목 없음."))

    return blocks


def _fallback_to_notion_blocks(raw_fallback: str) -> list[dict]:
    """Fallback 모드: ⚠️ 경고 헤더 포함 원문 → Notion 블록. 빈 블록 절대 생성 금지."""
    lines = raw_fallback.split("\n")
    blocks = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("⚠️") or stripped.startswith("■"):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": stripped}}]},
            })
        elif stripped.startswith("  "):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped.strip()}}]},
            })
        else:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped}}]},
            })
    if not blocks:
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "⚠️ 인수인계 내용 없음 — 담당자 확인 필요."}}]},
        })
    return blocks


async def _write_to_notion(
    facility_id: str,
    shift_date:  str,
    blocks:      list[dict],
    generation_mode: str,
) -> tuple[Optional[str], dict, str]:
    """
    Notion 데이터베이스에 인수인계 페이지를 생성한다.
    반환: (notion_page_id, notion_snapshot, notion_snapshot_sha256)
    Notion API 키 미설정 또는 실패 → (None, {}, "")
    """
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        logger.warning("[HandoverCompile] Notion API 키 또는 DB ID 미설정 — Notion 동기화 생략")
        return None, {}, ""

    title = f"[{generation_mode}] 인수인계 {facility_id} {shift_date}"
    properties = {
        "Name": {
            "title": [{"text": {"content": title}}]
        },
    }

    body = {
        "parent":     {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
        "children":   blocks[:100],  # Notion API 블록 한도 100
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{NOTION_API_URL}/pages",
            headers={
                "Authorization":  f"Bearer {NOTION_API_KEY}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type":   "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        page_data = resp.json()

    page_id       = page_data.get("id", "")
    snapshot_str  = json.dumps(page_data, ensure_ascii=False, sort_keys=True)
    snapshot_sha  = hashlib.sha256(snapshot_str.encode("utf-8")).hexdigest()

    return page_id, page_data, snapshot_sha


# ══════════════════════════════════════════════════════════════
# 메인 핸들러 — event_router_worker 에서 호출
# ══════════════════════════════════════════════════════════════

async def handover_compile_handler(
    event_id:    str,
    payload:     dict,
    attempt_num: int,
) -> None:
    """
    event_type='handover_compile' 처리기.

    payload 필수:
        report_id    (UUID — handover_report_ledger.id)
        facility_id  (str)
        worker_id    (str)
        shift_date   (str, 'YYYY-MM-DD')

    [처리 순서]
      1. DB에서 report 상태 확인 (중복 처리 방지)
      2. status=COMPILING 으로 전환
      3. 발화 기록 + 이상 탐지 집계 (recorded_at 기준)
      4. Gemini JSON 스키마 강제 호출 → 실패 시 RAW_FALLBACK
      5. Notion 2-mode 템플릿 동기화 (빈 블록 절대 금지)
      6. handover_report_ledger UPDATE (gemini_json/raw_fallback/notion_snapshot 봉인)
      7. Gemini 장애 → 관리자 카카오 알림톡 NT-5 트리거
    """
    if _engine is None:
        raise RuntimeError("[HandoverCompile] DB 미연결")

    report_id   = payload.get("report_id", "")
    facility_id = payload.get("facility_id", "")
    worker_id   = payload.get("worker_id", "")
    shift_date  = payload.get("shift_date", "")

    if not all([report_id, facility_id, worker_id, shift_date]):
        raise ValueError(
            f"[HandoverCompile] payload 필수 키 누락: "
            f"report_id={report_id!r} facility_id={facility_id!r} "
            f"worker_id={worker_id!r} shift_date={shift_date!r}"
        )

    # ── 1. 상태 확인 (중복 처리 방지) ────────────────────────
    with _engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, expires_at FROM public.handover_report_ledger
            WHERE id = :rid
        """), {"rid": report_id}).fetchone()

    if row is None:
        raise ValueError(f"[HandoverCompile] report_id={report_id!r} 존재하지 않음")

    if row.status not in ("PENDING",):
        logger.warning(
            "[HandoverCompile] report_id=%s 이미 처리됨 (status=%s) — 건너뜀",
            report_id, row.status,
        )
        return

    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        with _engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.handover_report_ledger
                SET status = 'EXPIRED'
                WHERE id = :rid AND status = 'PENDING'
            """), {"rid": report_id})
        logger.warning("[HandoverCompile] report_id=%s 만료됨 (expires_at=%s)", report_id, row.expires_at)
        return

    # ── 2. COMPILING 전환 ─────────────────────────────────────
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.handover_report_ledger
            SET status = 'COMPILING'
            WHERE id = :rid AND status = 'PENDING'
        """), {"rid": report_id})

    # ── 3. 집계 (REPEATABLE READ 스냅샷) ─────────────────────
    with _engine.connect() as conn:
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        utterances = _aggregate_utterances(conn, facility_id, shift_date)
        anomalies  = _aggregate_anomalies(conn, facility_id, shift_date)

    # ── 4. Gemini 호출 → RAW_FALLBACK 전환 ───────────────────
    gemini_json:     Optional[dict] = None
    raw_fallback:    Optional[str]  = None
    generation_mode: str            = "LLM"
    gemini_failed:   bool           = False

    try:
        gemini_json     = await _call_gemini(utterances, anomalies, facility_id, shift_date)
        generation_mode = "LLM"
    except Exception as e:
        logger.warning(
            "[HandoverCompile] Gemini 장애 → RAW_FALLBACK 전환: %s", e
        )
        generation_mode = "RAW_FALLBACK"
        gemini_failed   = True
        raw_fallback    = _build_raw_fallback(utterances, anomalies)

    # ── 5. Notion 2-mode 동기화 (빈 블록 절대 금지) ──────────
    if generation_mode == "LLM" and gemini_json:
        blocks = _gemini_json_to_notion_blocks(gemini_json, shift_date)
    else:
        blocks = _fallback_to_notion_blocks(raw_fallback or "⚠️ 내용 없음")

    notion_page_id:  Optional[str]  = None
    notion_snapshot: dict           = {}
    snapshot_sha256: str            = ""
    try:
        notion_page_id, notion_snapshot, snapshot_sha256 = await _write_to_notion(
            facility_id, shift_date, blocks, generation_mode
        )
    except Exception as e:
        logger.error("[HandoverCompile] Notion 동기화 실패: %s", e)

    # ── 6. report_ledger 봉인 기록 ────────────────────────────
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.handover_report_ledger
            SET
                status                 = 'DONE',
                gemini_json            = CAST(:gemini_json AS jsonb),
                raw_fallback           = :raw_fallback,
                notion_snapshot        = CAST(:notion_snapshot AS jsonb),
                notion_snapshot_sha256 = :snapshot_sha256,
                notion_page_id         = :page_id,
                gemini_failed          = :gemini_failed
            WHERE id = :rid
        """), {
            "gemini_json":    json.dumps(gemini_json, ensure_ascii=False) if gemini_json else None,
            "raw_fallback":   raw_fallback,
            "notion_snapshot":json.dumps(notion_snapshot, ensure_ascii=False) if notion_snapshot else None,
            "snapshot_sha256":snapshot_sha256 or None,
            "page_id":        notion_page_id,
            "gemini_failed":  gemini_failed,
            "rid":            report_id,
        })

    # ── 7. Gemini 장애 시 관리자 카카오 알림톡 NT-5 ──────────
    if gemini_failed:
        _trigger_admin_alert(report_id, facility_id, shift_date)


def _trigger_admin_alert(report_id: str, facility_id: str, shift_date: str) -> None:
    """
    Gemini 장애 감지 → 관리자 카카오 알림톡 NT-5 트리거.
    unified_outbox에 'alert' 이벤트 INSERT (Append-Only 보상 트랜잭션).
    """
    from uuid import uuid4 as _uuid4
    import json as _json

    if not _engine:
        return

    event_id = str(_uuid4())
    payload  = {
        "trigger_type":  "NT-5",
        "report_id":     report_id,
        "facility_id":   facility_id,
        "shift_date":    shift_date,
        "phone":         os.getenv("ADMIN_PHONE", ""),
        "template_code": os.getenv("ALIMTALK_TPL_NT5", ""),
        "variables": {
            "#{요양기관}":    facility_id,
            "#{근무일}":      shift_date,
            "#{장애유형}":    "Gemini API 타임아웃/장애",
            "#{폴백모드}":    "RAW_FALLBACK 자동 전환 완료",
        },
    }

    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.unified_outbox
                    (row_id, event_id, event_type, status, payload,
                     attempt_num, worker_id, error_message, created_at)
                VALUES
                    (gen_random_uuid(), :eid, 'alert', 'PENDING',
                     CAST(:payload AS jsonb), 0, 'handover_compile', NULL, NOW())
            """), {
                "eid":     event_id,
                "payload": _json.dumps(payload, ensure_ascii=False, default=str),
            })
        logger.warning(
            "[HandoverCompile] NT-5 관리자 알림 발행: report_id=%s facility=%s",
            report_id, facility_id,
        )
    except Exception as e:
        logger.error("[HandoverCompile] NT-5 알림 발행 실패: %s", e)
