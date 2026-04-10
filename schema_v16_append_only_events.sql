-- ══════════════════════════════════════════════════════════════════
-- Voice Guard — schema_v16_append_only_events.sql
-- Phase 9: WORM 원장 Append-Only 절대 원칙 복구
--
-- [배경]
--   기존 worker.py / main.py가 evidence_ledger를 직접 UPDATE하여
--   "법적 증거 무효화" 위험. (audio_sha256/chain_hash/transcript_text/
--    worm_*/is_flagged/resolution_*)
--
-- [해결]
--   1. evidence_seal_event  — 워커 봉인 결과 INSERT 전용
--   2. evidence_flag_event  — 현장 확인 요청(NT-3) 이력 INSERT 전용
--   3. v_evidence_sealed    — 봉인 값 우선 노출 편의 VIEW
--
-- [불변 원칙]
--   기존 evidence_ledger 컬럼은 단 하나도 변경하지 않음.
--   이벤트 테이블은 UPDATE/DELETE/TRUNCATE 트리거로 봉인.
-- ══════════════════════════════════════════════════════════════════

-- ── 1. evidence_seal_event ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evidence_seal_event (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id           UUID        NOT NULL REFERENCES evidence_ledger(id),
    audio_sha256        CHAR(64)    NOT NULL,
    transcript_sha256   CHAR(64)    NOT NULL,
    chain_hash          CHAR(64)    NOT NULL,
    transcript_text     TEXT        NOT NULL DEFAULT '',
    worm_bucket         TEXT        NOT NULL,
    worm_object_key     TEXT        NOT NULL,
    worm_retain_until   TIMESTAMPTZ NOT NULL,
    sealed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 단일 ledger_id에 대해 단 한 번만 봉인 (워커 재시도 멱등)
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_seal_event_ledger
    ON evidence_seal_event (ledger_id);

CREATE INDEX IF NOT EXISTS idx_evidence_seal_event_sealed_at
    ON evidence_seal_event (sealed_at DESC);

-- Append-Only 트리거
CREATE OR REPLACE FUNCTION prevent_seal_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE-GUARD] evidence_seal_event는 Append-Only입니다. '
        'UPDATE/DELETE/TRUNCATE 금지. (TG_OP=%)', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_seal_event_no_update ON evidence_seal_event;
CREATE TRIGGER trg_seal_event_no_update
    BEFORE UPDATE ON evidence_seal_event
    FOR EACH ROW EXECUTE FUNCTION prevent_seal_event_mutation();

DROP TRIGGER IF EXISTS trg_seal_event_no_delete ON evidence_seal_event;
CREATE TRIGGER trg_seal_event_no_delete
    BEFORE DELETE ON evidence_seal_event
    FOR EACH ROW EXECUTE FUNCTION prevent_seal_event_mutation();

DROP TRIGGER IF EXISTS trg_seal_event_no_truncate ON evidence_seal_event;
CREATE TRIGGER trg_seal_event_no_truncate
    BEFORE TRUNCATE ON evidence_seal_event
    EXECUTE FUNCTION prevent_seal_event_mutation();


-- ── 2. evidence_flag_event ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evidence_flag_event (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id         UUID        NOT NULL REFERENCES evidence_ledger(id),
    resolution_cause  TEXT        NOT NULL,
    resolution_memo   TEXT,
    flagged_by        TEXT,
    flagged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_flag_event_ledger
    ON evidence_flag_event (ledger_id, flagged_at DESC);

CREATE OR REPLACE FUNCTION prevent_flag_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE-GUARD] evidence_flag_event는 Append-Only입니다. '
        'UPDATE/DELETE/TRUNCATE 금지. (TG_OP=%)', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_flag_event_no_update ON evidence_flag_event;
CREATE TRIGGER trg_flag_event_no_update
    BEFORE UPDATE ON evidence_flag_event
    FOR EACH ROW EXECUTE FUNCTION prevent_flag_event_mutation();

DROP TRIGGER IF EXISTS trg_flag_event_no_delete ON evidence_flag_event;
CREATE TRIGGER trg_flag_event_no_delete
    BEFORE DELETE ON evidence_flag_event
    FOR EACH ROW EXECUTE FUNCTION prevent_flag_event_mutation();

DROP TRIGGER IF EXISTS trg_flag_event_no_truncate ON evidence_flag_event;
CREATE TRIGGER trg_flag_event_no_truncate
    BEFORE TRUNCATE ON evidence_flag_event
    EXECUTE FUNCTION prevent_flag_event_mutation();


-- ── 3. v_evidence_sealed (편의 VIEW) ───────────────────────────────
-- 봉인된 값을 우선 노출, 없으면 ledger의 placeholder 그대로
-- 기존 SELECT 쿼리에서 evidence_ledger를 v_evidence_sealed로 교체만 하면
-- 동일 컬럼 + is_sealed/is_flagged 자동 노출.
CREATE OR REPLACE VIEW v_evidence_sealed AS
SELECT
    e.id,
    e.session_id,
    e.facility_id,
    e.beneficiary_id,
    e.shift_id,
    e.care_type,
    e.case_type,
    e.recorded_at,
    e.ingested_at,
    e.device_id,
    e.idempotency_key,
    e.gps_lat,
    e.gps_lon,
    e.audio_size_kb,
    e.language_code,
    COALESCE(s.audio_sha256,      e.audio_sha256)      AS audio_sha256,
    COALESCE(s.transcript_sha256, e.transcript_sha256) AS transcript_sha256,
    COALESCE(s.chain_hash,        e.chain_hash)        AS chain_hash,
    COALESCE(s.transcript_text,   e.transcript_text)   AS transcript_text,
    COALESCE(s.worm_bucket,       e.worm_bucket)       AS worm_bucket,
    COALESCE(s.worm_object_key,   e.worm_object_key)   AS worm_object_key,
    COALESCE(s.worm_retain_until, e.worm_retain_until) AS worm_retain_until,
    (s.id IS NOT NULL)            AS is_sealed,
    s.sealed_at,
    EXISTS (
        SELECT 1 FROM evidence_flag_event f WHERE f.ledger_id = e.id
    )                             AS is_flagged,
    (
        SELECT f.resolution_cause
        FROM evidence_flag_event f
        WHERE f.ledger_id = e.id
        ORDER BY f.flagged_at DESC
        LIMIT 1
    )                             AS resolution_cause,
    (
        SELECT f.resolution_memo
        FROM evidence_flag_event f
        WHERE f.ledger_id = e.id
        ORDER BY f.flagged_at DESC
        LIMIT 1
    )                             AS resolution_memo
FROM evidence_ledger e
LEFT JOIN evidence_seal_event s ON s.ledger_id = e.id;


-- ── 4. 적용 확인 ──────────────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables
--   WHERE table_name IN ('evidence_seal_event','evidence_flag_event');
-- SELECT trigger_name, event_manipulation, event_object_table
--   FROM information_schema.triggers
--   WHERE event_object_table IN ('evidence_seal_event','evidence_flag_event');
-- SELECT * FROM v_evidence_sealed LIMIT 1;
