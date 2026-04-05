-- ============================================================
-- Voice Guard - Phase 2 스키마 확장 (schema_v2.sql)
-- 추가 테이블:
--   1. outbox_events       — Transactional Outbox (비동기 큐)
--   2. dead_letter_queue   — DLQ (최종 실패 이관소)
-- evidence_ledger 컬럼 추가:
--   beneficiary_id, shift_id, idempotency_key,
--   care_type, gps_lat, gps_lon, audio_size_kb,
--   resolution_cause, resolution_memo
-- ============================================================

-- ── evidence_ledger 컬럼 추가 (WORM 원장 확장) ─────────────
ALTER TABLE public.evidence_ledger
    ADD COLUMN IF NOT EXISTS beneficiary_id   VARCHAR(255),
    ADD COLUMN IF NOT EXISTS shift_id         VARCHAR(255),
    ADD COLUMN IF NOT EXISTS idempotency_key  CHAR(64),
    ADD COLUMN IF NOT EXISTS care_type        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS gps_lat          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS gps_lon          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS audio_size_kb    INTEGER,
    ADD COLUMN IF NOT EXISTS resolution_cause VARCHAR(255),
    ADD COLUMN IF NOT EXISTS resolution_memo  TEXT;

-- Idempotency Key UNIQUE 제약 (중복 요청 DB 레벨 차단)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_evidence_idempotency_key'
    ) THEN
        ALTER TABLE public.evidence_ledger
            ADD CONSTRAINT uq_evidence_idempotency_key
            UNIQUE (idempotency_key);
    END IF;
END $$;

-- 인덱스 추가
CREATE INDEX IF NOT EXISTS idx_evidence_beneficiary
    ON public.evidence_ledger (beneficiary_id);
CREATE INDEX IF NOT EXISTS idx_evidence_shift
    ON public.evidence_ledger (shift_id);
CREATE INDEX IF NOT EXISTS idx_evidence_idempotency
    ON public.evidence_ledger (idempotency_key);

-- ── [1] outbox_events — Transactional Outbox 패턴 ─────────
--
-- 역할: evidence_ledger와 동일 트랜잭션으로 INSERT됨.
--   이중 쓰기(Dual-write) 불일치를 원천 차단.
--   워커가 이 테이블을 폴링하며 비동기 작업 처리.
--
-- status 상태 머신:
--   pending → processing → done
--                       ↘ dlq (MAX_ATTEMPTS 초과)
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.outbox_events (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id       UUID            NOT NULL REFERENCES public.evidence_ledger(id),
    status          VARCHAR(20)     NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'dlq')),
    attempts        INTEGER         NOT NULL DEFAULT 0,
    payload         JSONB           NOT NULL,       -- 처리에 필요한 메타데이터
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,                    -- 완료 또는 DLQ 이관 시각
    error_message   TEXT,                           -- 마지막 실패 메시지
    next_retry_at   TIMESTAMPTZ                     -- 다음 재시도 예약 시각
);

-- 인덱스: 워커 폴링 최적화
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON public.outbox_events (created_at ASC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_outbox_ledger
    ON public.outbox_events (ledger_id);

-- outbox 권한 (voice_guard_ingestor가 INSERT/UPDATE)
REVOKE ALL ON TABLE public.outbox_events FROM PUBLIC;
GRANT INSERT, SELECT, UPDATE ON TABLE public.outbox_events TO voice_guard_ingestor;

-- ── [2] dead_letter_queue — DLQ (최종 실패 이관소) ─────────
--
-- 역할: MAX_ATTEMPTS 초과 시 워커가 이 테이블에 INSERT.
--   관리자가 원인 분석 후 수동 재처리.
--   INSERT-only (삭제 차단 트리거 적용).
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.dead_letter_queue (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id        UUID        REFERENCES public.evidence_ledger(id),
    outbox_id        UUID        REFERENCES public.outbox_events(id),
    failure_reason   TEXT        NOT NULL,
    original_payload JSONB,
    detected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_resolved      BOOLEAN     NOT NULL DEFAULT FALSE,
    resolved_at      TIMESTAMPTZ,
    resolved_note    TEXT
);

-- DLQ 인덱스
CREATE INDEX IF NOT EXISTS idx_dlq_unresolved
    ON public.dead_letter_queue (detected_at DESC)
    WHERE is_resolved = FALSE;

-- DLQ 권한
REVOKE ALL ON TABLE public.dead_letter_queue FROM PUBLIC;
GRANT INSERT, SELECT ON TABLE public.dead_letter_queue TO voice_guard_ingestor;

-- DLQ 삭제 차단 트리거
CREATE OR REPLACE FUNCTION fn_block_dlq_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '[VOICE GUARD] DLQ 삭제 차단: 증거 원장 보존 정책에 의해 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

ALTER FUNCTION fn_block_dlq_delete() OWNER TO voice_guard_ingestor;
REVOKE ALL ON FUNCTION fn_block_dlq_delete() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_dlq_block_delete   ON public.dead_letter_queue;
DROP TRIGGER IF EXISTS trg_dlq_block_truncate ON public.dead_letter_queue;

CREATE TRIGGER trg_dlq_block_delete
    BEFORE DELETE ON public.dead_letter_queue
    FOR EACH ROW EXECUTE FUNCTION fn_block_dlq_delete();

CREATE TRIGGER trg_dlq_block_truncate
    BEFORE TRUNCATE ON public.dead_letter_queue
    EXECUTE FUNCTION fn_block_dlq_delete();

-- ── [3] Alert View를 위한 DB 뷰 ────────────────────────────
--
-- 미처리(음성변환 미완료) + 입수 후 N분 이내 건
-- Next.js Alert View가 이 뷰로 실시간 폴링
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW public.v_pending_alerts AS
SELECT
    e.id,
    e.facility_id,
    e.beneficiary_id,
    e.shift_id,
    e.care_type,
    e.ingested_at,
    e.gps_lat,
    e.gps_lon,
    e.is_flagged,
    o.status         AS sync_status,
    o.attempts       AS sync_attempts,
    EXTRACT(EPOCH FROM (NOW() - e.ingested_at)) / 60 AS minutes_elapsed
FROM public.evidence_ledger e
LEFT JOIN public.outbox_events o ON o.ledger_id = e.id
WHERE e.transcript_text = ''          -- Whisper 미완료
   OR e.chain_hash = 'pending'        -- 해시 체인 미생성
ORDER BY e.ingested_at ASC;

-- ── [4] Audit-Ready 뷰 (AG Grid용) ─────────────────────────
CREATE OR REPLACE VIEW public.v_audit_ready AS
SELECT
    e.id,
    e.facility_id,
    e.beneficiary_id,
    e.shift_id,
    e.care_type,
    e.recorded_at,
    e.ingested_at,
    e.audio_sha256,
    e.chain_hash,
    e.worm_bucket,
    e.worm_object_key,
    e.worm_retain_until,
    e.transcript_text != '' AS has_audio,
    e.chain_hash != 'pending' AS is_sealed,
    e.is_flagged,
    o.status AS outbox_status
FROM public.evidence_ledger e
LEFT JOIN public.outbox_events o ON o.ledger_id = e.id
ORDER BY e.recorded_at DESC;

-- ── 완료 알림 ───────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 1 Fix] schema_v2 적용 완료.';
    RAISE NOTICE '  ✅ evidence_ledger 컬럼 확장 (beneficiary/shift/idempotency/resolution)';
    RAISE NOTICE '  ✅ outbox_events — Transactional Outbox 테이블 생성 (notion_sync_outbox 명칭 통일)';
    RAISE NOTICE '  ✅ dead_letter_queue — DLQ 테이블 + DELETE 차단 트리거';
    RAISE NOTICE '  ✅ v_pending_alerts / v_audit_ready 뷰 생성';
END $$;
