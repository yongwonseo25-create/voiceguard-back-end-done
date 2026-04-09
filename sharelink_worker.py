"""
Voice Guard — sharelink_worker.py
Phase 7: Outbox 워커 — Rate Limit + 2-Layer Fallback

[설계 원칙]
  ① SELECT FOR UPDATE SKIP LOCKED (batch_size=50) — 동시 실행 안전
  ② Token Bucket Rate Limiter — 최대 80 req/s (Solapi 한도 방어)
  ③ 1xxx 에러: 최대 3회 재시도 → DLQ
  ④ 3xxx 에러: channel=lms 즉각 폴백 재발송 (수신자 문제)
  ⑤ 폴백 재발송도 Outbox에 새 행 INSERT (이중 노동 제로)
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.notifier import _call_solapi_raw

logger = logging.getLogger("voice_guard.sharelink_worker")

# ── 환경변수 ──────────────────────────────────────────────────────
DATABASE_URL    = os.getenv("DATABASE_URL", "")
WORKER_INTERVAL = float(os.getenv("SHARELINK_WORKER_INTERVAL_SEC", "5"))
BATCH_SIZE      = int(os.getenv("SHARELINK_BATCH_SIZE", "50"))
MAX_ATTEMPTS    = int(os.getenv("SHARELINK_MAX_ATTEMPTS", "3"))
RETRY_DELAY_SEC = int(os.getenv("SHARELINK_RETRY_DELAY_SEC", "60"))

# Rate Limiter 설정
RATE_LIMIT_RPS  = float(os.getenv("SHARELINK_RATE_LIMIT_RPS", "80"))


# ══════════════════════════════════════════════════════════════════
# Token Bucket Rate Limiter
# ══════════════════════════════════════════════════════════════════

class TokenBucket:
    """
    Token Bucket Rate Limiter.
    최대 rate req/s 를 보장. acquire() 호출마다 필요 시 대기.
    """

    def __init__(self, rate: float, capacity: Optional[float] = None):
        self.rate = rate                          # tokens/second
        self.capacity = capacity or rate          # 버킷 최대 용량 (burst)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        """토큰 1개 소비. 부족 시 대기."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.rate,
            )
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # 부족한 토큰 채우는 데 걸리는 시간만큼 대기
            wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# ══════════════════════════════════════════════════════════════════
# 에러 코드 분류
# ══════════════════════════════════════════════════════════════════

def _classify_error(error_code: Optional[str]) -> str:
    """
    Solapi 에러 코드 분류.
    - "1xxx": 일시적 오류 → 재시도 대상
    - "3xxx": 수신자 문제 → lms 즉각 폴백
    - 기타:   재시도 대상
    """
    if not error_code:
        return "retry"
    prefix = error_code[:1]
    if prefix == "3":
        return "lms_fallback"
    return "retry"  # 1xxx 포함 기타 모두 재시도


# ══════════════════════════════════════════════════════════════════
# 단일 Outbox 행 처리
# ══════════════════════════════════════════════════════════════════

def _process_outbox_row(db, row, rate_limiter: TokenBucket) -> None:
    """
    Outbox 행 1개 처리.
    rate_limiter.acquire() → Solapi 발송 → 성공/실패 UPDATE.
    """
    outbox_id   = str(row.id)
    dispatch_id = str(row.dispatch_id)
    channel     = row.channel
    attempt     = row.attempt_count + 1

    # SENDING 상태로 선점
    db.execute(
        text("""
            UPDATE material_dispatch_outbox
            SET status = 'SENDING', attempt_count = :attempt, updated_at = NOW()
            WHERE id = :oid::uuid
        """),
        {"attempt": attempt, "oid": outbox_id},
    )
    db.commit()

    # 발송 payload 조회
    dispatch_row = db.execute(
        text("""
            SELECT recipient_phone, payload_json, channel
            FROM material_dispatch
            WHERE id = :did::uuid
        """),
        {"did": dispatch_id},
    ).fetchone()

    if not dispatch_row:
        logger.error(f"[WORKER] dispatch_id={dispatch_id} 원장 없음 — DLQ 처리")
        db.execute(
            text("""
                UPDATE material_dispatch_outbox
                SET status = 'DLQ', last_error_code = 'MISSING_DISPATCH',
                    last_error_msg = 'material_dispatch 원장 없음', updated_at = NOW()
                WHERE id = :oid::uuid
            """),
            {"oid": outbox_id},
        )
        db.commit()
        return

    # Rate Limiter 토큰 소비 (Solapi 호출 전)
    rate_limiter.acquire()

    try:
        _call_solapi_raw(
            phone=dispatch_row.recipient_phone,
            payload_json=dispatch_row.payload_json,
            channel=channel,
        )
        # 성공
        db.execute(
            text("""
                UPDATE material_dispatch_outbox
                SET status = 'SENT', updated_at = NOW()
                WHERE id = :oid::uuid
            """),
            {"oid": outbox_id},
        )
        db.commit()
        logger.info(
            f"[WORKER] SENT: outbox_id={outbox_id} dispatch_id={dispatch_id} "
            f"channel={channel} attempt={attempt}"
        )

    except Exception as exc:
        error_str = str(exc)
        # Solapi 에러 코드 추출 (응답 바디에 포함된 경우)
        error_code = _extract_solapi_error_code(error_str)
        error_class = _classify_error(error_code)

        logger.warning(
            f"[WORKER] 발송 실패: outbox_id={outbox_id} dispatch_id={dispatch_id} "
            f"channel={channel} attempt={attempt} error_code={error_code} "
            f"class={error_class}"
        )

        if error_class == "lms_fallback":
            # 3xxx: 수신자 문제 → channel=lms 새 Outbox 행 INSERT
            db.execute(
                text("""
                    UPDATE material_dispatch_outbox
                    SET status = 'FAILED',
                        last_error_code = :code,
                        last_error_msg  = :msg,
                        updated_at      = NOW()
                    WHERE id = :oid::uuid
                """),
                {"code": error_code, "msg": error_str[:500], "oid": outbox_id},
            )
            # 폴백 행 INSERT (새 채널 시도)
            db.execute(
                text("""
                    INSERT INTO material_dispatch_outbox
                        (dispatch_id, channel, status, attempt_count, next_attempt_at)
                    VALUES
                        (:did::uuid, 'lms', 'PENDING', 0, NOW())
                """),
                {"did": dispatch_id},
            )
            db.commit()
            logger.info(
                f"[WORKER] LMS 폴백 큐잉: dispatch_id={dispatch_id} "
                f"원인={error_code}"
            )

        elif attempt >= MAX_ATTEMPTS:
            # 재시도 한도 초과 → DLQ
            db.execute(
                text("""
                    UPDATE material_dispatch_outbox
                    SET status = 'DLQ',
                        last_error_code = :code,
                        last_error_msg  = :msg,
                        updated_at      = NOW()
                    WHERE id = :oid::uuid
                """),
                {"code": error_code, "msg": error_str[:500], "oid": outbox_id},
            )
            db.commit()
            logger.error(
                f"[WORKER] DLQ: outbox_id={outbox_id} dispatch_id={dispatch_id} "
                f"attempt={attempt}/{MAX_ATTEMPTS}"
            )

        else:
            # 재시도 예약
            next_at = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY_SEC)
            db.execute(
                text("""
                    UPDATE material_dispatch_outbox
                    SET status          = 'FAILED',
                        last_error_code = :code,
                        last_error_msg  = :msg,
                        next_attempt_at = :next_at,
                        updated_at      = NOW()
                    WHERE id = :oid::uuid
                """),
                {
                    "code":    error_code,
                    "msg":     error_str[:500],
                    "next_at": next_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "oid":     outbox_id,
                },
            )
            db.commit()
            logger.info(
                f"[WORKER] 재시도 예약: outbox_id={outbox_id} "
                f"attempt={attempt}/{MAX_ATTEMPTS} next_at={next_at.isoformat()}"
            )


def _extract_solapi_error_code(error_str: str) -> Optional[str]:
    """
    에러 문자열에서 Solapi 에러 코드 추출.
    e.g. "StatusCode=1001" or '"code":"3001"'
    """
    import re
    # JSON 응답에서 code 필드 추출 시도
    m = re.search(r'"code"\s*:\s*"(\d{4})"', error_str)
    if m:
        return m.group(1)
    # StatusCode 형식
    m = re.search(r'[Ss]tatus[Cc]ode[=:]\s*(\d{4})', error_str)
    if m:
        return m.group(1)
    return None


# ══════════════════════════════════════════════════════════════════
# 메인 워커 루프
# ══════════════════════════════════════════════════════════════════

def run_worker_loop(engine) -> None:
    """
    Outbox 워커 메인 루프.
    SELECT FOR UPDATE SKIP LOCKED (batch_size=50) 로 대기열 소비.
    Token Bucket(80 req/s) 으로 Solapi 한도 방어.
    """
    Session_ = sessionmaker(bind=engine)
    rate_limiter = TokenBucket(rate=RATE_LIMIT_RPS)

    logger.info(
        f"[WORKER] 시작: batch_size={BATCH_SIZE} "
        f"rate_limit={RATE_LIMIT_RPS}rps "
        f"max_attempts={MAX_ATTEMPTS}"
    )

    while True:
        try:
            with Session_() as db:
                rows = db.execute(
                    text("""
                        SELECT id, dispatch_id, channel, attempt_count
                        FROM material_dispatch_outbox
                        WHERE status IN ('PENDING', 'FAILED')
                          AND next_attempt_at <= NOW()
                        ORDER BY next_attempt_at ASC
                        LIMIT :batch
                        FOR UPDATE SKIP LOCKED
                    """),
                    {"batch": BATCH_SIZE},
                ).fetchall()

            if not rows:
                time.sleep(WORKER_INTERVAL)
                continue

            logger.info(f"[WORKER] 배치 처리 시작: {len(rows)}건")
            for row in rows:
                with Session_() as db:
                    _process_outbox_row(db, row, rate_limiter)

        except Exception as e:
            logger.error(f"[WORKER] 루프 오류 (재시작): {e}")
            time.sleep(WORKER_INTERVAL)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    from sqlalchemy import create_engine as _ce
    _engine = _ce(DATABASE_URL, pool_pre_ping=True)
    run_worker_loop(_engine)
