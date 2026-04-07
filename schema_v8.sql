-- ============================================================
-- Voice Guard — schema_v8.sql
-- Phase 5-3: RPA 실행 증거 원장 + Export Batch 상태 확장
--
-- [불변 원칙] 기존 테이블 스키마 수정 0
-- bridge_rpa_execution_log: RPA 봇 실행 결과 Append-Only 원장
-- bridge_export_batch.status 확장: APPLIED_CONFIRMED, APPLY_FAILED
-- ============================================================

-- ── 1. bridge_export_batch status CHECK 확장 ────────────────────
-- 기존 CHECK 제약 교체 (컬럼 자체는 미수정, 허용값만 추가)
ALTER TABLE public.bridge_export_batch
    DROP CONSTRAINT IF EXISTS bridge_export_batch_status_check;

ALTER TABLE public.bridge_export_batch
    ADD CONSTRAINT bridge_export_batch_status_check
    CHECK (status IN (
        'CREATED', 'DOWNLOADED', 'UPLOADED',
        'RPA_IN_PROGRESS',
        'APPLIED_CONFIRMED', 'APPLY_FAILED'
    ));

-- ── 2. bridge_rpa_execution_log (RPA 실행 증거 원장) ────────────
-- INSERT-ONLY: 공단 실사 시 "RPA가 정확히 이렇게 입력했다" 증명
CREATE TABLE IF NOT EXISTS public.bridge_rpa_execution_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        UUID         NOT NULL
                    REFERENCES public.bridge_export_batch(id),
    status          VARCHAR(20)  NOT NULL
                    CHECK (status IN ('SUCCESS', 'FAILED', 'PARTIAL')),
    screenshot_hash CHAR(64),              -- 스크린샷 SHA-256
    angel_receipt   JSONB,                 -- 엔젤시스템 응답 메타
    error_msg       TEXT,
    items_applied   INTEGER      NOT NULL DEFAULT 0,
    items_failed    INTEGER      NOT NULL DEFAULT 0,
    executed_by     VARCHAR(100) NOT NULL,  -- RPA 봇 식별자
    executed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rpa_log_batch
    ON public.bridge_rpa_execution_log (batch_id, executed_at DESC);

CREATE INDEX IF NOT EXISTS idx_rpa_log_status
    ON public.bridge_rpa_execution_log (status, executed_at DESC);

-- ── 3. UPDATE 차단 트리거 (Append-Only 강제) ────────────────────
CREATE OR REPLACE FUNCTION fn_block_rpa_log_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] bridge_rpa_execution_log UPDATE 차단: '
        'Append-Only 원장입니다. 새 INSERT로만 기록하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_rpa_log_block_update
    ON public.bridge_rpa_execution_log;

CREATE TRIGGER trg_rpa_log_block_update
    BEFORE UPDATE ON public.bridge_rpa_execution_log
    FOR EACH ROW EXECUTE FUNCTION fn_block_rpa_log_update();

-- ── 4. DELETE/TRUNCATE 차단 트리거 ──────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_rpa_log_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] bridge_rpa_execution_log 삭제 차단: '
        'RPA 실행 이력은 감사 증빙으로 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_rpa_log_block_delete
    ON public.bridge_rpa_execution_log;
DROP TRIGGER IF EXISTS trg_rpa_log_block_truncate
    ON public.bridge_rpa_execution_log;

CREATE TRIGGER trg_rpa_log_block_delete
    BEFORE DELETE ON public.bridge_rpa_execution_log
    FOR EACH ROW EXECUTE FUNCTION fn_block_rpa_log_delete();

CREATE TRIGGER trg_rpa_log_block_truncate
    BEFORE TRUNCATE ON public.bridge_rpa_execution_log
    EXECUTE FUNCTION fn_block_rpa_log_delete();

-- ── 5. 권한 ──────────────────────────────────────────────────────
REVOKE ALL ON TABLE public.bridge_rpa_execution_log FROM PUBLIC;
GRANT INSERT, SELECT
    ON TABLE public.bridge_rpa_execution_log TO voice_guard_ingestor;

-- ── 완료 알림 ─────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 5-3] schema_v8 적용 완료.';
    RAISE NOTICE '  bridge_rpa_execution_log — RPA 실행 Append-Only 원장';
    RAISE NOTICE '  bridge_export_batch status — APPLIED_CONFIRMED/APPLY_FAILED 추가';
    RAISE NOTICE '  UPDATE/DELETE/TRUNCATE 3중 차단 트리거 활성화';
END $$;
