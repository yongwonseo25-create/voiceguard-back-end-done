-- ============================================================
-- Voice Guard — schema_v5.sql
-- Phase 4: Notion 미러 동기화 전용 아웃박스
--
-- notion_sync_outbox:
--   - evidence_ledger 봉인 완료 건의 Notion 동기화 추적
--   - 상태: pending → syncing → synced / dlq
--   - 기존 outbox_events (B2/Whisper)와 완전 분리
-- ============================================================

-- ── 1. notion_sync_outbox (Notion 동기화 큐) ────────────────
CREATE TABLE IF NOT EXISTS public.notion_sync_outbox (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id       UUID        NOT NULL REFERENCES public.evidence_ledger(id),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'syncing', 'synced', 'dlq')),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    payload         JSONB       NOT NULL,
    notion_page_id  VARCHAR(255),           -- 성공 시 Notion page ID 기록
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    error_message   TEXT,
    next_retry_at   TIMESTAMPTZ
);

-- 인덱스: 워커 폴링 최적화
CREATE INDEX IF NOT EXISTS idx_notion_outbox_pending
    ON public.notion_sync_outbox (created_at ASC)
    WHERE status IN ('pending', 'syncing');

CREATE INDEX IF NOT EXISTS idx_notion_outbox_ledger
    ON public.notion_sync_outbox (ledger_id);

-- 중복 방지: 동일 ledger_id 2건 이상 금지
CREATE UNIQUE INDEX IF NOT EXISTS uq_notion_outbox_ledger
    ON public.notion_sync_outbox (ledger_id);

-- ── 2. 권한 ──────────────────────────────────────────────────
REVOKE ALL ON TABLE public.notion_sync_outbox FROM PUBLIC;
GRANT INSERT, SELECT, UPDATE ON TABLE public.notion_sync_outbox TO voice_guard_ingestor;

-- ── 3. 인수인계 트리거: evidence_ledger 봉인 완료 시 자동 큐잉 ─
--   outbox_events.status='done' 전환 시 → notion_sync_outbox INSERT
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_queue_notion_sync()
RETURNS TRIGGER AS $$
BEGIN
    -- outbox_events 상태가 'done'으로 변경될 때만 트리거
    IF NEW.status = 'done' AND (OLD.status IS NULL OR OLD.status != 'done') THEN
        INSERT INTO public.notion_sync_outbox (ledger_id, payload, created_at)
        SELECT
            NEW.ledger_id,
            jsonb_build_object(
                'ledger_id',      NEW.ledger_id,
                'facility_id',    e.facility_id,
                'beneficiary_id', e.beneficiary_id,
                'shift_id',       e.shift_id,
                'care_type',      e.care_type,
                'ingested_at',    e.ingested_at,
                'chain_hash',     e.chain_hash,
                'audio_sha256',   e.audio_sha256,
                'worm_object_key',e.worm_object_key
            ),
            NOW()
        FROM evidence_ledger e
        WHERE e.id = NEW.ledger_id
        ON CONFLICT (ledger_id) DO NOTHING;   -- 멱등성
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_queue_notion_sync ON public.outbox_events;
CREATE TRIGGER trg_queue_notion_sync
    AFTER UPDATE ON public.outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_queue_notion_sync();

-- ── 완료 알림 ─────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 4] schema_v5 적용 완료.';
    RAISE NOTICE '  ✅ notion_sync_outbox — Notion 미러 동기화 큐';
    RAISE NOTICE '  ✅ fn_queue_notion_sync — 봉인 완료 시 자동 큐잉 트리거';
    RAISE NOTICE '  ✅ uq_notion_outbox_ledger — 중복 동기화 방지';
END $$;
