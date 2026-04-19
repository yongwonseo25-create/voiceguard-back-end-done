"""
Voice Guard — backend/dlq_recovery_worker.py
DLQ 자동 재처리 워커 (UPGRADE-03)

[설계 원칙]
  - Ledger 절대 불변: evidence_ledger / care_record_ledger 수정 금지.
    outbox_events 상태만 'pending'으로 복원.
  - 알림 실패 무중단: send_alimtalk 예외 시 로그만 기록, 워커 계속 진행.
  - 환경변수 전용: MAX_DLQ_RETRY, ADMIN_PHONE 하드코딩 금지.
  - is_resolved 기반: 실제 DB 스키마(retry_count/alerted_at 없음) 기준으로 동작.

[실행 방법]
  python dlq_recovery_worker.py            ← 직접 실행
  POST /internal/dlq-recovery              ← Cloud Scheduler 트리거

[DLQ 처리 흐름]
  dead_letter_queue(is_resolved=FALSE) 조회
  → outbox_events.status='pending' 복원 (원장 무수정)
  → dead_letter_queue.is_resolved=TRUE 마킹 (소프트 완료)
  → 관리자 알림톡 발행 (실패해도 계속 진행)
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from notifier import (
    ADMIN_PHONE,
    ALIMTALK_TPL_NT2,
    send_alimtalk,
)

load_dotenv()

logger = logging.getLogger("voice_guard.dlq_recovery")

DATABASE_URL  = os.getenv("DATABASE_URL")
# DLQ 1회 실행당 처리 최대 건수 (환경변수 전용 — 하드코딩 금지)
MAX_DLQ_RETRY = int(os.getenv("DLQ_MAX_RETRY", "10"))

engine = create_engine(
    DATABASE_URL,
    pool_size=3,
    max_overflow=5,
    pool_pre_ping=True,
) if DATABASE_URL else None


# ══════════════════════════════════════════════════════════════════
# 핵심 재처리 루프
# ══════════════════════════════════════════════════════════════════

async def run_recovery() -> dict:
    """
    DLQ 전체 재처리 루프 — 1회 실행 후 종료 (Cloud Scheduler 호출 방식).

    Returns:
        {"processed": int, "recovered": int, "failed": int}
    """
    stats = {"processed": 0, "recovered": 0, "failed": 0}

    if not engine:
        logger.error("[DLQ-RECOVERY] DATABASE_URL 미설정 — 종료")
        return stats

    # ── 미처리 DLQ 항목 조회 (실제 스키마 기반: is_resolved=FALSE) ──
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, ledger_id, outbox_id, failure_reason,
                       original_payload, detected_at
                FROM dead_letter_queue
                WHERE is_resolved = FALSE
                ORDER BY detected_at ASC
                LIMIT :limit
            """), {"limit": MAX_DLQ_RETRY}).fetchall()
    except Exception as exc:
        logger.error(f"[DLQ-RECOVERY] DLQ 조회 실패: {exc}")
        return stats

    logger.info(f"[DLQ-RECOVERY] 재처리 대상: {len(rows)}건 (limit={MAX_DLQ_RETRY})")
    stats["processed"] = len(rows)

    for row in rows:
        item = dict(row._mapping)
        ok = await _process_dlq_item(item)
        if ok:
            stats["recovered"] += 1
        else:
            stats["failed"] += 1

    logger.info(
        f"[DLQ-RECOVERY] 완료 — processed={stats['processed']} "
        f"recovered={stats['recovered']} failed={stats['failed']}"
    )
    return stats


async def _process_dlq_item(item: dict) -> bool:
    """
    DLQ 단건 재처리.

    [불변 원칙]
    - evidence_ledger / care_record_ledger 절대 수정 금지.
    - outbox_events.status='pending' 복원 → 기존 워커가 재처리.
    - DLQ 행 is_resolved=TRUE 마킹 (소프트 완료).
    - 알림톡 실패 시 예외 흡수 후 계속 진행 (무중단 보장).

    Returns:
        True  — outbox 복원 성공 (알림 실패는 True 유지)
        False — outbox 복원 실패
    """
    dlq_id        = str(item.get("id", ""))
    ledger_id     = str(item.get("ledger_id") or "")
    outbox_id     = str(item.get("outbox_id") or "")
    failure_reason = str(item.get("failure_reason") or "원인 미상")

    logger.info(
        f"[DLQ-RECOVERY] 처리 시작 dlq_id={dlq_id[:8]}… "
        f"outbox_id={outbox_id[:8] if outbox_id else 'None'}… "
        f"ledger_id={ledger_id[:8] if ledger_id else 'None'}…"
    )

    # ── 1. outbox_events 복원 (원장 무수정) ──────────────────────
    if not outbox_id:
        logger.warning(f"[DLQ-RECOVERY] outbox_id 없음 dlq_id={dlq_id[:8]}… — DLQ 마킹 후 스킵")
        _mark_resolved(dlq_id, note="outbox_id 없음 — 수동 확인 필요")
        _notify_admin(ledger_id, failure_reason, "outbox_id 없음 — 수동 개입 필요")
        return False

    try:
        with engine.begin() as conn:
            # outbox_events만 복원 — evidence_ledger/care_record_ledger 절대 수정 금지
            result = conn.execute(text("""
                UPDATE outbox_events
                SET status      = 'pending',
                    attempts    = 0,
                    error_message = NULL,
                    next_retry_at = NULL
                WHERE id = :outbox_id
                  AND status IN ('failed', 'dlq')
            """), {"outbox_id": outbox_id})
            restored = result.rowcount

        if restored == 0:
            logger.warning(
                f"[DLQ-RECOVERY] outbox 복원 대상 없음 (이미 처리됨?) "
                f"outbox_id={outbox_id[:8]}…"
            )
        else:
            logger.info(
                f"[DLQ-RECOVERY] outbox 복원 완료 outbox_id={outbox_id[:8]}… "
                f"(워커 재처리 대기)"
            )

    except Exception as exc:
        logger.error(f"[DLQ-RECOVERY] outbox 복원 실패: {exc}")
        _notify_admin(ledger_id, failure_reason, f"outbox 복원 오류: {str(exc)[:100]}")
        return False

    # ── 2. DLQ 행 is_resolved=TRUE 마킹 ──────────────────────────
    _mark_resolved(dlq_id, note="auto-recovery: outbox pending 복원 완료")

    # ── 3. 관리자 알림 (실패해도 True 유지 — 알림 실패 무중단) ──
    _notify_admin(ledger_id, failure_reason, "auto-recovery 완료 — 워커 재처리 대기")

    return True


def _mark_resolved(dlq_id: str, note: str) -> None:
    """DLQ 행을 is_resolved=TRUE로 마킹. 실패 시 로그만 기록."""
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE dead_letter_queue
                SET is_resolved = TRUE,
                    resolved_at = NOW(),
                    resolved_note = :note
                WHERE id = :dlq_id
            """), {"dlq_id": dlq_id, "note": note[:500]})
        logger.info(f"[DLQ-RECOVERY] DLQ 마킹 완료 dlq_id={dlq_id[:8]}…")
    except Exception as exc:
        logger.error(f"[DLQ-RECOVERY] DLQ 마킹 실패: {exc}")


def _notify_admin(ledger_id: str, failure_reason: str, extra: str) -> None:
    """
    관리자 알림톡 발행. 실패해도 예외 전파 없이 로그만 기록.
    알림 실패로 DLQ 워커 프로세스가 뻗어서는 안 된다 (무중단 보장).
    """
    if not ADMIN_PHONE or not ALIMTALK_TPL_NT2:
        logger.info("[DLQ-RECOVERY] 알림 생략 (ADMIN_PHONE 또는 ALIMTALK_TPL_NT2 미설정)")
        return

    try:
        lid_short = (ledger_id[:12] + "…") if ledger_id else "N/A"
        reason_short = failure_reason[:80]
        send_alimtalk(
            engine=engine,
            phone=ADMIN_PHONE,
            template_code=ALIMTALK_TPL_NT2,
            variables={
                "#{원장ID}":   lid_short,
                "#{실패사유}": f"{reason_short} | {extra[:60]}",
            },
            trigger_type="NT-2",
            ledger_id=ledger_id or None,
        )
        logger.info(f"[DLQ-RECOVERY] 관리자 알림 발행 완료 ledger_id={lid_short}")
    except Exception as exc:
        # 알림 실패는 워커를 멈추지 않는다 — 로그만 기록
        logger.warning(f"[DLQ-RECOVERY] 관리자 알림 실패 (무시, 계속 진행): {exc}")


# ══════════════════════════════════════════════════════════════════
# CLI 직접 실행
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    result = asyncio.run(run_recovery())
    print(f"\n[DLQ-RECOVERY] 결과: {result}")
