-- ══════════════════════════════════════════════════════════════════
-- Voice Guard — schema_v18_certificate_ledger.sql
-- Phase 10: 증거 검증서 자동 발급 — certificate_ledger (Append-Only)
--
-- [목적]
--   evidence_seal_event 봉인 직후 PDF/JSON 검증서를 자동 발급하고
--   그 메타데이터(B2 키, 해시)를 불변 원장에 기록한다.
--
-- [불변 원칙]
--   evidence_ledger/evidence_seal_event 스키마 수정 없음 — 절대 규칙 준수.
--   certificate_ledger는 INSERT 전용. UPDATE/DELETE/TRUNCATE 트리거 봉인.
-- ══════════════════════════════════════════════════════════════════

BEGIN;

-- ── 1. certificate_ledger ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS certificate_ledger (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id         UUID        NOT NULL REFERENCES evidence_ledger(id),
    seal_event_id     UUID        NOT NULL REFERENCES evidence_seal_event(id),
    cert_type         VARCHAR(4)  NOT NULL CHECK (cert_type IN ('PDF', 'JSON')),
    cert_hash         CHAR(64)    NOT NULL,          -- SHA-256 of certificate content
    storage_key       TEXT        NOT NULL,           -- B2 object key
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issuer_version    VARCHAR(20) NOT NULL DEFAULT 'vg-cert-v1.0.0',
    worm_retain_until TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_cert_per_seal UNIQUE (seal_event_id, cert_type)
);

CREATE INDEX IF NOT EXISTS idx_certificate_ledger_ledger_id
    ON certificate_ledger (ledger_id, issued_at DESC);

CREATE INDEX IF NOT EXISTS idx_certificate_ledger_seal_event_id
    ON certificate_ledger (seal_event_id);

-- ── 2. Append-Only 봉인 트리거 ────────────────────────────────────
CREATE OR REPLACE FUNCTION prevent_certificate_ledger_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE-GUARD] certificate_ledger는 Append-Only입니다. '
        'UPDATE/DELETE/TRUNCATE 금지. (TG_OP=%)', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_cert_ledger_no_update ON certificate_ledger;
CREATE TRIGGER trg_cert_ledger_no_update
    BEFORE UPDATE ON certificate_ledger
    FOR EACH ROW EXECUTE FUNCTION prevent_certificate_ledger_mutation();

DROP TRIGGER IF EXISTS trg_cert_ledger_no_delete ON certificate_ledger;
CREATE TRIGGER trg_cert_ledger_no_delete
    BEFORE DELETE ON certificate_ledger
    FOR EACH ROW EXECUTE FUNCTION prevent_certificate_ledger_mutation();

DROP TRIGGER IF EXISTS trg_cert_ledger_no_truncate ON certificate_ledger;
CREATE TRIGGER trg_cert_ledger_no_truncate
    BEFORE TRUNCATE ON certificate_ledger
    EXECUTE FUNCTION prevent_certificate_ledger_mutation();

-- ── 3. 편의 VIEW: 봉인 + 검증서 통합 조회 ──────────────────────────
CREATE OR REPLACE VIEW v_sealed_with_certs AS
SELECT
    s.ledger_id,
    s.id          AS seal_event_id,
    s.chain_hash,
    s.audio_sha256,
    s.transcript_sha256,
    s.worm_bucket,
    s.worm_object_key,
    s.worm_retain_until,
    s.sealed_at,
    MAX(CASE WHEN c.cert_type = 'PDF'  THEN c.storage_key END) AS pdf_storage_key,
    MAX(CASE WHEN c.cert_type = 'JSON' THEN c.storage_key END) AS json_storage_key,
    MAX(CASE WHEN c.cert_type = 'PDF'  THEN c.cert_hash  END) AS pdf_cert_hash,
    MAX(CASE WHEN c.cert_type = 'JSON' THEN c.cert_hash  END) AS json_cert_hash,
    MAX(c.issued_at) AS cert_issued_at,
    COUNT(c.id)   AS cert_count
FROM evidence_seal_event s
LEFT JOIN certificate_ledger c ON c.seal_event_id = s.id
GROUP BY s.ledger_id, s.id, s.chain_hash, s.audio_sha256, s.transcript_sha256,
         s.worm_bucket, s.worm_object_key, s.worm_retain_until, s.sealed_at;

COMMIT;

-- ── 적용 확인 쿼리 ───────────────────────────────────────────────
-- \d certificate_ledger
-- SELECT trigger_name, event_manipulation FROM information_schema.triggers
--   WHERE event_object_table = 'certificate_ledger';
-- SELECT * FROM v_sealed_with_certs LIMIT 1;
