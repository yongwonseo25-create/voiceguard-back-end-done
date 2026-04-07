"""
Voice Guard — backend/notion_sync.py
Rate-Limit 방어형 Notion 미러 동기화 워커 v1.0

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
      ↓ Notion API POST (database item 생성)
  성공 → status='synced', notion_page_id 기록
  실패 → attempts++, 지수 백오프 대기
  3회 실패 → DLQ 이관 (dead_letter_queue INSERT + status='dlq')

[상태 파이프라인]
  INGESTED → SEALED → WORM_STORED → SYNCING → SYNCED
"""

import asyncio
import json
import logging
import os
import time
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
DATABASE_URL       = os.getenv("DATABASE_URL")
NOTION_API_KEY     = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL    = "https://api.notion.com/v1"

# Token Bucket 설정
TB_CAPACITY        = 3           # 버킷 최대 용량
TB_REFILL_RATE     = 2.5         # 초당 충전 속도 (Notion 제한 3 rps 미만)
TB_COST            = 1           # 요청당 소비

# DLQ 설정
MAX_ATTEMPTS       = 3           # 3회 실패 시 DLQ 이관
POLL_INTERVAL_SEC  = 5           # 폴링 주기 (초)
BATCH_SIZE         = 5           # 1회 폴링 배치 크기

# 지수 백오프 단계 (초)
BACKOFF_SCHEDULE   = [5, 15, 60]

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
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    async def acquire(self, cost: float = 1.0, max_wait: float = 30.0) -> bool:
        """
        토큰 확보까지 대기. max_wait 초과 시 False 반환.
        """
        waited = 0.0
        while waited < max_wait:
            self._refill()
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            # 부족한 토큰이 충전되는 예상 시간만큼 대기
            deficit = cost - self.tokens
            sleep_time = min(deficit / self.refill_rate + 0.05, 1.0)
            await asyncio.sleep(sleep_time)
            waited += sleep_time
        logger.warning("[TOKEN-BUCKET] 토큰 확보 타임아웃 — 작업 강행")
        return False


bucket = TokenBucket(capacity=TB_CAPACITY, refill_rate=TB_REFILL_RATE)


# ══════════════════════════════════════════════════════════════════
# Notion API 클라이언트
# ══════════════════════════════════════════════════════════════════

async def notion_create_page(
    client: httpx.AsyncClient,
    payload: dict,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Notion Database에 페이지 생성.

    Returns:
        (success, page_id_or_none, error_message_or_none)
    """
    properties = {
        "원장 ID": {"title": [{"text": {"content": payload.get("ledger_id", "")[:36]}}]},
        "요양기관": {"rich_text": [{"text": {"content": payload.get("facility_id", "")}}]},
        "수급자 ID": {"rich_text": [{"text": {"content": payload.get("beneficiary_id", "")}}]},
        "교대 ID": {"rich_text": [{"text": {"content": payload.get("shift_id", "")}}]},
        "급여 유형": {"rich_text": [{"text": {"content": payload.get("care_type", "") or ""}}]},
        "수집 시각": {"rich_text": [{"text": {"content": payload.get("ingested_at", "")}}]},
        "해시 체인": {"rich_text": [{"text": {"content": payload.get("chain_hash", "")[:24] + "..."}}]},
        "WORM 키": {"rich_text": [{"text": {"content": payload.get("worm_object_key", "")}}]},
        "동기화 상태": {"select": {"name": "SYNCED"}},
    }

    body = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    try:
        resp = await client.post(
            f"{NOTION_BASE_URL}/pages",
            json=body,
            headers={
                "Authorization": f"Bearer {NOTION_API_KEY}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            return True, page_id, None

        if resp.status_code == 429:
            # Rate Limit — Retry-After 헤더 준수
            retry_after = resp.headers.get("Retry-After", "")
            return False, None, f"429_RATE_LIMIT:retry_after={retry_after}"

        # 기타 오류
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
                "lid": ledger_id,
                "oid": outbox_id,
                "reason": f"[NOTION_SYNC] {reason[:2000]}",
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
    """notion_sync_outbox 1건 처리"""
    outbox_id  = str(row.id)
    ledger_id  = str(row.ledger_id)
    attempts   = row.attempts
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

    # Notion API 호출
    success, page_id, error = await notion_create_page(client, payload)

    if success:
        # ── 성공: synced 상태 + page_id 기록 ──
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
            "id": outbox_id,
            "err": (error or "")[:500],
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
                # ── pending 건 폴링 ──
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

                # 순차 처리 (Token Bucket이 rate 제어)
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
