-- ============================================================
-- Voice Guard — schema_v7.sql
-- Phase 5-2: 증거 브리지 Export 배치 불변 원장
--
-- [불변 원칙] evidence_ledger / outbox_events 수정 0
-- bridge_export_batch: Export ZIP 생성 시 배치 메타 고정
-- ============================================================

-- ── 1. bridge_export_batch (Export 배치 불변 원장) ───────────────
-- INSERT-ONLY: 공단 감사 시 "그때 엔젤에 넣으려고 만든 파일" 증명용
CREATE TABLE IF NOT EXISTS public.bridge_export_batch (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     VARCHAR(100) NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN ('CREATED', 'DOWNLOADED', 'UPLOADED')),
    -- 포함된 ledger_id 목록 (불변 스냅샷)
    ledger_ids      UUID[]       NOT NULL,
    item_count      INTEGER      NOT NULL,
    -- 파일 해시 (무결성 증빙)
    zip_sha256      CHAR(64)     NOT NULL,
    angel_csv_sha256    CHAR(64) NOT NULL,
    proof_csv_sha256    CHAR(64) NOT NULL,
    -- 메타
    export_range_start  TIMESTAMPTZ,
    export_range_end    TIMESTAMPTZ,
    exported_by     VARCHAR(100),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    downloaded_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_export_batch_facility
    ON public.bridge_export_batch (facility_id, created_at DESC);

-- ── 2. DELETE/TRUNCATE 차단 트리거 ──────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_export_batch_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] bridge_export_batch 삭제 차단: '
        'Export 배치 이력은 감사 증빙으로 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_export_batch_block_delete
    ON public.bridge_export_batch;
DROP TRIGGER IF EXISTS trg_export_batch_block_truncate
    ON public.bridge_export_batch;

CREATE TRIGGER trg_export_batch_block_delete
    BEFORE DELETE ON public.bridge_export_batch
    FOR EACH ROW EXECUTE FUNCTION fn_block_export_batch_delete();

CREATE TRIGGER trg_export_batch_block_truncate
    BEFORE TRUNCATE ON public.bridge_export_batch
    EXECUTE FUNCTION fn_block_export_batch_delete();

-- ── 3. 권한 ──────────────────────────────────────────────────────
REVOKE ALL ON TABLE public.bridge_export_batch FROM PUBLIC;
GRANT INSERT, SELECT, UPDATE
    ON TABLE public.bridge_export_batch TO voice_guard_ingestor;

-- ── 완료 알림 ─────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 5-2] schema_v7 적용 완료.';
    RAISE NOTICE '  bridge_export_batch — Export ZIP 배치 불변 원장';
    RAISE NOTICE '  DELETE/TRUNCATE 차단 트리거 활성화';
END $$;
