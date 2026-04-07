-- ============================================================
-- Voice Guard — schema_v6.sql
-- Phase 5: 엔젤시스템 기생형 브리지 (Bounded Context)
--
-- [불변 원칙] evidence_ledger / outbox_events 일체 미수정
-- 신규 테이블만 추가하여 행정 반영 상태를 독립 관리
--
-- angel_review_event (Append-Only 판정 이벤트 원장):
--   DETECTED → REVIEW_REQUIRED → APPROVED_FOR_EXPORT → EXPORTED
--   원본 증거는 evidence_ledger에 보존, 판정만 여기에 적재
-- ============================================================

-- ── 1. angel_review_event (판정 이벤트 원장) ────────────────────
-- INSERT-ONLY: 상태 전이마다 새 row 추가 (UPDATE 금지)
CREATE TABLE IF NOT EXISTS public.angel_review_event (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id       UUID         NOT NULL REFERENCES public.evidence_ledger(id),
    status          VARCHAR(30)  NOT NULL
                    CHECK (status IN (
                        'DETECTED',
                        'REVIEW_REQUIRED',
                        'APPROVED_FOR_EXPORT',
                        'REJECTED',
                        'RECLASSIFIED',
                        'EXPORTED'
                    )),
    -- 판정 메타데이터
    reviewer_id     VARCHAR(100),          -- 검수 관리자 ID
    decision_note   TEXT,                  -- 사유/메모
    reclassified_to VARCHAR(50),           -- RECLASSIFIED 시 새 care_type
    export_batch_id UUID,                  -- EXPORTED 시 배치 ID
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 인덱스: ledger_id별 최신 상태 조회 최적화
CREATE INDEX IF NOT EXISTS idx_angel_review_ledger
    ON public.angel_review_event (ledger_id, created_at DESC);

-- 인덱스: 상태별 목록 조회 (관리자 대시보드)
CREATE INDEX IF NOT EXISTS idx_angel_review_status
    ON public.angel_review_event (status, created_at DESC);

-- ── 2. angel_review_event DELETE/TRUNCATE 차단 트리거 ───────────
CREATE OR REPLACE FUNCTION fn_block_angel_review_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] angel_review_event 삭제 차단: '
        '판정 이벤트는 증거 보존 정책에 의해 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_angel_review_block_delete   ON public.angel_review_event;
DROP TRIGGER IF EXISTS trg_angel_review_block_truncate ON public.angel_review_event;

CREATE TRIGGER trg_angel_review_block_delete
    BEFORE DELETE ON public.angel_review_event
    FOR EACH ROW EXECUTE FUNCTION fn_block_angel_review_delete();

CREATE TRIGGER trg_angel_review_block_truncate
    BEFORE TRUNCATE ON public.angel_review_event
    EXECUTE FUNCTION fn_block_angel_review_delete();

-- ── 3. angel_review_event UPDATE 차단 트리거 (Append-Only 강제) ─
CREATE OR REPLACE FUNCTION fn_block_angel_review_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] angel_review_event UPDATE 차단: '
        'Append-Only 원장입니다. 상태 전이는 새 INSERT로만 기록하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_angel_review_block_update ON public.angel_review_event;

CREATE TRIGGER trg_angel_review_block_update
    BEFORE UPDATE ON public.angel_review_event
    FOR EACH ROW EXECUTE FUNCTION fn_block_angel_review_update();

-- ── 4. 자동 큐잉: outbox_events 봉인 완료 시 DETECTED 자동 INSERT ─
-- 기존 fn_queue_notion_sync와 동일 패턴, 별도 트리거
CREATE OR REPLACE FUNCTION fn_queue_angel_detected()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'done' AND (OLD.status IS NULL OR OLD.status != 'done') THEN
        INSERT INTO public.angel_review_event (ledger_id, status, created_at)
        VALUES (NEW.ledger_id, 'DETECTED', NOW())
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_queue_angel_detected ON public.outbox_events;
CREATE TRIGGER trg_queue_angel_detected
    AFTER UPDATE ON public.outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_queue_angel_detected();

-- ── 5. 권한 ──────────────────────────────────────────────────────
REVOKE ALL ON TABLE public.angel_review_event FROM PUBLIC;
GRANT INSERT, SELECT ON TABLE public.angel_review_event TO voice_guard_ingestor;

-- ── 완료 알림 ─────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 5] schema_v6 적용 완료.';
    RAISE NOTICE '  ✅ angel_review_event      — 판정 이벤트 Append-Only 원장';
    RAISE NOTICE '  ✅ DELETE/UPDATE/TRUNCATE   — 3중 차단 트리거';
    RAISE NOTICE '  ✅ trg_queue_angel_detected — 봉인 완료 시 자동 DETECTED 큐잉';
END $$;
