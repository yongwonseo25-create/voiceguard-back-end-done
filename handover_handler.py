"""
Voice Guard — Phase 5 HandoverHandler (handover_handler.py)
============================================================
자동 인수인계 브리핑 엔진의 핵심 처리 모듈.

[6대 결함 방어 매핑]
  결함 1 (대타/비정규 교대): trigger_mode MANUAL 지원 → shift_handover_ledger 에 기록
  결함 2 (오프라인 지연):    집계 쿼리 = recorded_at 기준 BETWEEN (ingested_at 불사용)
  결함 3 (LLM 장애):        try/except → generation_mode='RAW_FALLBACK' 자동 전환
  결함 4 (야간 날짜 경계):   집계 전 canonical_day_fact + canonical_time_fact REFRESH 강제
  결함 6 (미수령 알림):      check_undelivered_handovers() 주기 함수 제공

[핸들러 시그니처]
  handover_handler(event_id, payload, attempt_num) — event_router_worker 에 등록
  payload 필수 키: facility_id, shift_start (ISO-8601), shift_end (ISO-8601)
  payload 선택 키: trigger_mode ('SCHEDULED'|'MANUAL', 기본 'SCHEDULED')
                   caregiver_name (브리핑 텍스트에 표시)
"""

import hashlib
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

import boto3
import httpx
from botocore.client import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logger = logging.getLogger("handover_handler")
logging.basicConfig(
    level=logging.WARNING,          # 성공은 조용히, 실패만 시끄럽게
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── 설정 ─────────────────────────────────────────────────────
DATABASE_URL       = os.getenv("DATABASE_URL")
B2_KEY_ID          = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
B2_BUCKET_NAME     = os.getenv("B2_BUCKET_NAME", "voice-guard-korea")
B2_ENDPOINT_URL    = os.getenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
WORM_YEARS         = 5

# Haiku: CLAUDE.md §2 "Evaluator & Formatting" 비용 최적화 모델
LLM_MODEL          = os.getenv("HANDOVER_LLM_MODEL", "claude-haiku-4-5-20251001")
LLM_TIMEOUT_SEC    = int(os.getenv("HANDOVER_LLM_TIMEOUT", "30"))

# 미수령 알림 임계값 (분)
UNDELIVERED_ALERT_MIN = int(os.getenv("HANDOVER_UNDELIVERED_ALERT_MIN", "30"))

_engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
) if DATABASE_URL else None


def _get_b2():
    return boto3.client(
        "s3", endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"),
    )


# ══════════════════════════════════════════════════════════════
# Step 1: Materialized View REFRESH (결함 4 — 야간 날짜 경계 방어)
# ══════════════════════════════════════════════════════════════

def _refresh_materialized_views(conn) -> None:
    """
    집계 전 반드시 호출. canonical_day_fact/canonical_time_fact 를 최신화한다.
    야간 근무 날짜 경계(22:00~06:00) 에서 stale view 가 ANOMALY 집계를 누락하는
    결함 4를 물리적으로 차단.
    CONCURRENTLY 옵션: 조회 블로킹 없음 (해당 시점에 row 가 1개라도 있어야 동작).
    """
    try:
        conn.execute(text(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_day_fact"
        ))
    except Exception:
        # 첫 실행이라 row가 없을 경우 CONCURRENTLY 실패 → 일반 REFRESH 폴백
        conn.execute(text("REFRESH MATERIALIZED VIEW public.canonical_day_fact"))

    try:
        conn.execute(text(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_time_fact"
        ))
    except Exception:
        conn.execute(text("REFRESH MATERIALIZED VIEW public.canonical_time_fact"))


# ══════════════════════════════════════════════════════════════
# Step 2-A: HANDOVER 기록 집계 (결함 2 — recorded_at 기준 고정)
# ══════════════════════════════════════════════════════════════

def _aggregate_handover_records(
    conn,
    facility_id: str,
    shift_start: datetime,
    shift_end: datetime,
) -> list[dict]:
    """
    evidence_ledger 에서 care_type='HANDOVER' 기록을 집계.

    [결함 2 방어]
    WHERE 절은 반드시 recorded_at 을 기준으로 한다.
    ingested_at 은 오프라인 지연(네트워크 복구 후 업로드) 으로 인해
    shift_end 이후가 될 수 있어 기준 컬럼으로 부적합.
    """
    rows = conn.execute(text("""
        SELECT
            id,
            recorded_at,
            transcript_text,
            beneficiary_id,
            device_id,
            ingested_at
        FROM public.evidence_ledger
        WHERE facility_id = :fid
          AND care_type   = 'HANDOVER'
          AND recorded_at BETWEEN :shift_start AND :shift_end
        ORDER BY recorded_at ASC
    """), {
        "fid":         facility_id,
        "shift_start": shift_start,
        "shift_end":   shift_end,
    }).fetchall()

    records = []
    for r in rows:
        # 오프라인 지연 감지: ingested_at > shift_end 이면 늦은 수신
        late = r.ingested_at > shift_end if r.ingested_at else False
        records.append({
            "id":              str(r.id),
            "recorded_at":     r.recorded_at,
            "transcript_text": r.transcript_text or "",
            "beneficiary_id":  r.beneficiary_id or "",
            "device_id":       r.device_id or "",
            "late_ingest":     late,
        })
    return records


# ══════════════════════════════════════════════════════════════
# Step 2-B: ANOMALY 탐지 결과 집계 (결함 4 — fact_date 범위 쿼리)
# ══════════════════════════════════════════════════════════════

def _aggregate_anomalies(
    conn,
    facility_id: str,
    shift_start: datetime,
    shift_end: datetime,
) -> list[dict]:
    """
    reconciliation_result 에서 ANOMALY/PARTIAL 결과를 집계.

    [결함 4 방어]
    야간 근무(22:00~06:00)는 두 날짜에 걸치므로
    fact_date BETWEEN shift_start::DATE AND shift_end::DATE 로 양쪽 날짜를 포함.
    """
    rows = conn.execute(text("""
        SELECT
            anomaly_code,
            result_status,
            beneficiary_id,
            care_type,
            fact_date,
            anomaly_detail
        FROM public.reconciliation_result
        WHERE facility_id   = :fid
          AND result_status IN ('ANOMALY', 'PARTIAL')
          AND fact_date BETWEEN :date_start AND :date_end
        ORDER BY result_status DESC, fact_date ASC
    """), {
        "fid":        facility_id,
        "date_start": shift_start.date(),
        "date_end":   shift_end.date(),
    }).fetchall()

    return [
        {
            "anomaly_code":  r.anomaly_code or "",
            "result_status": r.result_status,
            "beneficiary_id":r.beneficiary_id,
            "care_type":     r.care_type,
            "fact_date":     str(r.fact_date),
            "detail":        r.anomaly_detail or {},
        }
        for r in rows
    ]


# ══════════════════════════════════════════════════════════════
# Step 3: LLM 브리핑 생성 (결함 3 — 실패 시 호출자가 RAW_FALLBACK 전환)
# ══════════════════════════════════════════════════════════════

def _build_llm_prompt(
    records:     list[dict],
    anomalies:   list[dict],
    shift_info:  dict,
) -> tuple[str, str]:
    """LLM system/user 프롬프트를 구성하고 반환한다."""

    system_prompt = (
        "너는 요양 현장의 베테랑 수간호사다. "
        "아래 데이터를 바탕으로 다음 근무자를 위한 1분 브리핑서를 작성하라.\n\n"
        "출력 형식 (우선순위 순):\n"
        "1. [즉시 대응 필요] — ANOMALY/PARTIAL 항목 (없으면 생략)\n"
        "2. [환자 특이사항] — 케어 기록 중 주목할 내용\n"
        "3. [인수 메모] — 근무 중 남긴 음성 메모 요약 (시간순)\n"
        "4. [완료 사항] — 정상 처리된 루틴 (한 줄 요약)\n\n"
        "규칙:\n"
        "- [즉시 대응 필요] 항목이 있으면 반드시 맨 앞에 배치\n"
        "- 환자 이름/ID는 수신 데이터 그대로 표기\n"
        "- 총 길이 300자 이내\n"
        "- 입력 데이터에 없는 내용은 절대 추론하지 말 것"
    )

    # 오프라인 지연 경고 문구 추가
    late_count = sum(1 for r in records if r.get("late_ingest"))
    late_notice = ""
    if late_count > 0:
        late_notice = f"\n※ 주의: {late_count}건의 메모가 네트워크 지연으로 늦게 수신되었습니다."

    anomaly_lines = "\n".join(
        f"  - [{r['result_status']}] {r['anomaly_code']} | "
        f"수급자:{r['beneficiary_id']} | 날짜:{r['fact_date']}"
        for r in anomalies
    ) or "  (없음)"

    memo_lines = "\n".join(
        f"  {r['recorded_at'].strftime('%H:%M') if hasattr(r['recorded_at'], 'strftime') else str(r['recorded_at'])[:16]}"
        f" [{r['beneficiary_id']}] {r['transcript_text']}"
        for r in records
        if r["transcript_text"].strip()
    ) or "  (없음)"

    user_content = (
        f"[교대 정보]\n"
        f"  기관: {shift_info.get('facility_id', '')}\n"
        f"  교대 시간: {shift_info.get('shift_start', '')} ~ {shift_info.get('shift_end', '')}\n"
        f"  담당자: {shift_info.get('caregiver_name', '미지정')}\n"
        f"  트리거: {shift_info.get('trigger_mode', 'SCHEDULED')}"
        f"{late_notice}\n\n"
        f"[이상 탐지 결과]\n{anomaly_lines}\n\n"
        f"[인수인계 음성 메모 전사]\n{memo_lines}"
    )

    return system_prompt, user_content


async def _call_llm(
    records:    list[dict],
    anomalies:  list[dict],
    shift_info: dict,
) -> str:
    """
    Anthropic API 를 통해 브리핑 텍스트를 생성한다.
    실패(타임아웃·HTTP 오류·키 미설정) 시 RuntimeError 를 발생시킨다.
    호출자(handover_handler)가 except 로 잡아 RAW_FALLBACK 으로 전환.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정 — LLM 호출 불가")

    system_prompt, user_content = _build_llm_prompt(records, anomalies, shift_info)

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SEC) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":           ANTHROPIC_API_KEY,
                "anthropic-version":   "2023-06-01",
                "content-type":        "application/json",
            },
            json={
                "model":      LLM_MODEL,
                "max_tokens": 512,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_content}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ══════════════════════════════════════════════════════════════
# Step 3-B: RAW_FALLBACK 브리핑 생성 (결함 3 — LLM 장애 방어)
# ══════════════════════════════════════════════════════════════

def _build_raw_fallback_brief(records: list[dict], anomalies: list[dict]) -> str:
    """
    LLM 호출 실패 시 호출되는 폴백 포매터.
    정렬된 원문 메모를 타임스탬프와 함께 이어붙인다.
    """
    lines = ["[AI 요약 불가 — 원문 전달]", ""]

    if anomalies:
        lines.append("[이상 탐지]")
        for a in anomalies:
            lines.append(
                f"  ! [{a['result_status']}] {a['anomaly_code']} "
                f"수급자:{a['beneficiary_id']}"
            )
        lines.append("")

    lines.append("[인수인계 메모]")
    memo_found = False
    # recorded_at 오름차순 정렬 — 결함 2 방어: 오프라인 메모는 recorded_at 기준이 옳음
    for r in sorted(records, key=lambda x: x["recorded_at"]):
        text = (r["transcript_text"] or "").strip()
        if not text:
            continue
        ts = (
            r["recorded_at"].strftime("%H:%M")
            if hasattr(r["recorded_at"], "strftime")
            else str(r["recorded_at"])[:16]
        )
        late_mark = " [지연수신]" if r.get("late_ingest") else ""
        lines.append(f"  {ts}{late_mark} [{r['beneficiary_id']}] {text}")
        memo_found = True

    if not memo_found:
        lines.append("  (인수인계 메모 없음)")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# Step 4: TTS 오디오 생성 + B2 WORM 업로드 (선택적)
# ══════════════════════════════════════════════════════════════

async def _upload_tts_to_b2(brief_text: str, handover_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    OpenAI TTS 로 오디오를 생성하고 B2 WORM 에 업로드한다.
    OPENAI_API_KEY 미설정 또는 모든 오류 → (None, None) 반환.
    TTS 실패는 브리핑 전체를 막지 않는다.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 미설정 — TTS 생략")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model": "tts-1",
                "input": brief_text[:4096],
                "voice": "nova",
            },
        )
        resp.raise_for_status()
        audio_bytes = resp.content

    tts_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    date_pfx   = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    b2_key     = f"handover_tts/{date_pfx}/{handover_id}.mp3"
    retain     = datetime.now(timezone.utc) + timedelta(days=365 * WORM_YEARS)

    b2 = _get_b2()
    b2.put_object(
        Bucket=B2_BUCKET_NAME,
        Key=b2_key,
        Body=audio_bytes,
        ContentType="audio/mpeg",
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=retain,
    )

    return b2_key, tts_sha256


# ══════════════════════════════════════════════════════════════
# 메인 핸들러 — event_router_worker 에서 호출
# ══════════════════════════════════════════════════════════════

async def handover_handler(event_id: str, payload: dict, attempt_num: int) -> None:
    """
    event_type='handover_trigger' 이벤트 처리기.

    payload 필수:
        facility_id  (str)
        shift_start  (ISO-8601 문자열)
        shift_end    (ISO-8601 문자열)

    payload 선택:
        trigger_mode   ('SCHEDULED' | 'MANUAL', 기본 'SCHEDULED')
        caregiver_name (str, 브리핑 텍스트 표시용)

    [처리 순서]
      1. REFRESH MATERIALIZED VIEW  (결함 4)
      2. evidence_ledger 집계  — recorded_at 기준  (결함 2)
      3. reconciliation_result 집계  (결함 4)
      4. LLM 브리핑 생성  → 실패 시 RAW_FALLBACK  (결함 3)
      5. TTS 생성 + B2 업로드  → 실패 허용 (tts 필드 NULL)
      6. shift_handover_ledger INSERT  (결함 1, 6)
    """
    if _engine is None:
        raise RuntimeError("[HandoverHandler] DB 미연결")

    # ── payload 파싱 ─────────────────────────────────────────
    facility_id    = payload.get("facility_id", "")
    shift_start_s  = payload.get("shift_start", "")
    shift_end_s    = payload.get("shift_end", "")
    trigger_mode   = payload.get("trigger_mode", "SCHEDULED").upper()
    caregiver_name = payload.get("caregiver_name", "미지정")

    if not facility_id or not shift_start_s or not shift_end_s:
        raise ValueError(
            f"[HandoverHandler] payload 필수 키 누락: facility_id={facility_id!r}"
        )

    if trigger_mode not in ("SCHEDULED", "MANUAL"):
        trigger_mode = "SCHEDULED"

    shift_start = datetime.fromisoformat(shift_start_s)
    shift_end   = datetime.fromisoformat(shift_end_s)

    shift_info = {
        "facility_id":   facility_id,
        "shift_start":   shift_start_s,
        "shift_end":     shift_end_s,
        "trigger_mode":  trigger_mode,
        "caregiver_name":caregiver_name,
    }

    # ── 1. REFRESH (결함 4: 야간 날짜 경계 방어) ─────────────
    with _engine.begin() as conn:
        _refresh_materialized_views(conn)

    # ── 2 + 3. 집계 (단일 REPEATABLE READ 스냅샷) ────────────
    with _engine.connect() as conn:
        conn.execute(text(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        ))
        records   = _aggregate_handover_records(conn, facility_id, shift_start, shift_end)
        anomalies = _aggregate_anomalies(conn, facility_id, shift_start, shift_end)

    # ── 4. LLM 브리핑 생성 (결함 3: RAW_FALLBACK) ────────────
    generation_mode = "LLM"
    try:
        brief_text = await _call_llm(records, anomalies, shift_info)
    except Exception as e:
        logger.warning(
            "[HandoverHandler] LLM 호출 실패 → RAW_FALLBACK 전환: %s", e
        )
        generation_mode = "RAW_FALLBACK"
        brief_text = _build_raw_fallback_brief(records, anomalies)

    brief_sha256 = hashlib.sha256(brief_text.encode("utf-8")).hexdigest()

    # ── 5. TTS 생성 + B2 업로드 (실패 허용) ──────────────────
    handover_id  = str(uuid4())
    tts_key      = None
    tts_sha256   = None
    try:
        tts_key, tts_sha256 = await _upload_tts_to_b2(brief_text, handover_id)
    except Exception as e:
        logger.warning("[HandoverHandler] TTS 생성 생략: %s", e)

    # ── 6. shift_handover_ledger INSERT (WORM 봉인) ───────────
    with _engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.shift_handover_ledger (
                id, facility_id, shift_start, shift_end,
                trigger_mode, source_count, anomaly_count,
                brief_text, brief_sha256, generation_mode,
                tts_object_key, tts_sha256
            ) VALUES (
                :id, :fid, :sstart, :send,
                :tmode, :src_cnt, :ano_cnt,
                :brief, :sha256, :gmode,
                :tts_key, :tts_sha256
            )
        """), {
            "id":        handover_id,
            "fid":       facility_id,
            "sstart":    shift_start,
            "send":      shift_end,
            "tmode":     trigger_mode,
            "src_cnt":   len(records),
            "ano_cnt":   len(anomalies),
            "brief":     brief_text,
            "sha256":    brief_sha256,
            "gmode":     generation_mode,
            "tts_key":   tts_key,
            "tts_sha256":tts_sha256,
        })


# ══════════════════════════════════════════════════════════════
# 미수령 알림 주기 점검 (결함 6 방어 — event_router_worker 에서 주기 호출)
# ══════════════════════════════════════════════════════════════

def check_undelivered_handovers(engine=None) -> list[dict]:
    """
    delivered_at IS NULL 이고 generated_at 으로부터
    UNDELIVERED_ALERT_MIN 분 이상 경과한 브리핑을 반환한다.

    event_router_worker 의 주기 점검 루프에서 호출:
      → 반환된 항목마다 'alert' 이벤트(NT-4) 를 발행할 것.
    """
    eng = engine or _engine
    if eng is None:
        return []

    try:
        with eng.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, facility_id, shift_end, generated_at
                FROM public.shift_handover_ledger
                WHERE delivered_at  IS NULL
                  AND is_superseded = FALSE
                  AND generated_at <= NOW() - (:m || ' minutes')::INTERVAL
                ORDER BY generated_at ASC
                LIMIT 20
            """), {"m": UNDELIVERED_ALERT_MIN}).fetchall()
        return [
            {
                "handover_id": str(r.id),
                "facility_id": r.facility_id,
                "shift_end":   r.shift_end.isoformat(),
                "generated_at":r.generated_at.isoformat(),
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("[HandoverHandler] 미수령 점검 실패: %s", e)
        return []
