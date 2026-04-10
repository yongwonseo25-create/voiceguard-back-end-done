-- ══════════════════════════════════════════════════════════════════
-- Voice Guard — schema_v15_phase8_care_record.sql
-- Phase 8: 6대 의무기록 파이프라인 신규 테이블
--
-- [불변 원칙]
--   기존 스키마(evidence_ledger, outbox_events 등) 수정 없음
--   care_record_ledger: Append-Only 원장 (DELETE/UPDATE/TRUNCATE 트리거 봉인)
--   care_record_outbox: 비동기 처리 큐 (소비 후 status 전환만 허용)
--
-- [환경변수 세팅 안내 — 반드시 .env에 추가]
--   NOTION_CARE_RECORD_DB_ID=<노션 '일일 케어 기록 DB'의 데이터베이스 ID>
--   NOTION_HANDOVER_TITLE_PROP=보고서 제목   (기존 "이름" → "보고서 제목" 변경)
-- ══════════════════════════════════════════════════════════════════

-- ── 1. care_record_ledger (6대 의무기록 불변 원장) ─────────────────
CREATE TABLE IF NOT EXISTS care_record_ledger (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     TEXT        NOT NULL,
    beneficiary_id  TEXT        NOT NULL,
    caregiver_id    TEXT        NOT NULL,
    raw_voice_text  TEXT        NOT NULL,
    server_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recorded_at     TIMESTAMPTZ NOT NULL
);

-- 인덱스: 수급자·시설별 조회 최적화
CREATE INDEX IF NOT EXISTS idx_care_record_ledger_beneficiary
    ON care_record_ledger (beneficiary_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_care_record_ledger_facility
    ON care_record_ledger (facility_id, server_ts DESC);

-- ── 2. care_record_outbox (비동기 처리 큐) ─────────────────────────
CREATE TABLE IF NOT EXISTS care_record_outbox (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id       UUID        NOT NULL REFERENCES care_record_ledger(id),
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','processing','done','dlq')),
    attempts        INT         NOT NULL DEFAULT 0,
    payload         JSONB       NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    next_retry_at   TIMESTAMPTZ
);

-- 인덱스: 워커 폴링 최적화
CREATE INDEX IF NOT EXISTS idx_care_record_outbox_poll
    ON care_record_outbox (status, attempts, next_retry_at)
    WHERE status IN ('pending');

-- ── 3. Append-Only 봉인 트리거 (care_record_ledger) ───────────────
-- evidence_ledger와 동일한 방어 패턴 적용

CREATE OR REPLACE FUNCTION prevent_care_record_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE-GUARD] care_record_ledger는 Append-Only입니다. '
        'UPDATE/DELETE/TRUNCATE 금지. (TG_OP=%, id=%)',
        TG_OP, COALESCE(OLD.id::text, 'N/A');
END;
$$;

DROP TRIGGER IF EXISTS trg_care_record_no_update ON care_record_ledger;
CREATE TRIGGER trg_care_record_no_update
    BEFORE UPDATE ON care_record_ledger
    FOR EACH ROW EXECUTE FUNCTION prevent_care_record_mutation();

DROP TRIGGER IF EXISTS trg_care_record_no_delete ON care_record_ledger;
CREATE TRIGGER trg_care_record_no_delete
    BEFORE DELETE ON care_record_ledger
    FOR EACH ROW EXECUTE FUNCTION prevent_care_record_mutation();

DROP TRIGGER IF EXISTS trg_care_record_no_truncate ON care_record_ledger;
CREATE TRIGGER trg_care_record_no_truncate
    BEFORE TRUNCATE ON care_record_ledger
    EXECUTE FUNCTION prevent_care_record_mutation();

-- ── 4. 적용 확인 ──────────────────────────────────────────────────
-- 실행 후 아래 쿼리로 테이블·트리거 존재 여부를 확인하십시오.
--
-- SELECT table_name FROM information_schema.tables
--   WHERE table_name IN ('care_record_ledger','care_record_outbox');
--
-- SELECT trigger_name, event_manipulation, event_object_table
--   FROM information_schema.triggers
--   WHERE event_object_table = 'care_record_ledger';
