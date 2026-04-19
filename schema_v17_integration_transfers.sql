-- schema_v17_integration_transfers.sql
-- Universal ERP Integration — Mutable Ops DB 상태 머신 테이블
--
-- 설계도 결단 1: WORM(불변 증거) ≠ Ops DB(mutable 상태)
-- 이 테이블들은 mutable 운영 상태만 관리한다.
-- WORM 원장(evidence_ledger)과 절대 혼용 금지.
-- 기존 WORM 원장 스키마(schema_v16_*) 수정 없음 — Append-Only 불변 원칙 준수.

BEGIN;

-- ─────────────────────────────────────────────────────────────────
-- [1] integration_transfers — 이관 요청 및 상태 머신
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS integration_transfers (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key         CHAR(64)        NOT NULL,   -- SHA-256 64자 강제
    tenant_id               VARCHAR(128)    NOT NULL,
    facility_id             VARCHAR(128)    NOT NULL,
    internal_record_id      VARCHAR(256)    NOT NULL,
    record_version          INTEGER         NOT NULL CHECK (record_version >= 1),
    target_system           VARCHAR(64)     NOT NULL,   -- angel|carefo|wiseman
    target_adapter_version  VARCHAR(64)     NOT NULL,
    current_state           VARCHAR(64)     NOT NULL
                            DEFAULT 'APPROVED'
                            CHECK (current_state IN (
                                'APPROVED', 'QUEUED', 'DISPATCHED',
                                'AUTHENTICATED', 'WRITING', 'SUBMITTED',
                                'VERIFYING', 'COMMITTED',
                                'RETRYABLE_FAILED', 'UNKNOWN_OUTCOME',
                                'TERMINAL_FAILED', 'MANUAL_REVIEW_REQUIRED'
                            )),
    approved_at             TIMESTAMPTZ     NOT NULL,
    approved_by             VARCHAR(256)    NOT NULL,
    legal_hash              CHAR(64)        NOT NULL,   -- WORM 원장 hash 참조
    external_ref            VARCHAR(512),               -- ERP 측 레코드 ID
    committed_at            TIMESTAMPTZ,
    last_error              TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_it_tenant_state
    ON integration_transfers (tenant_id, current_state);
CREATE INDEX IF NOT EXISTS idx_it_target_system
    ON integration_transfers (target_system, current_state);

-- ─────────────────────────────────────────────────────────────────
-- [2] transfer_attempts — 재시도 이력
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transfer_attempts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id         UUID            NOT NULL
                        REFERENCES integration_transfers(id),
    attempt_number      INTEGER         NOT NULL CHECK (attempt_number >= 1),
    from_state          VARCHAR(64)     NOT NULL,
    to_state            VARCHAR(64)     NOT NULL,
    adapter_id          VARCHAR(128),
    error_code          VARCHAR(64),
    error_message       TEXT,
    trace_artifact_path TEXT,
    attempted_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ta_transfer_id
    ON transfer_attempts (transfer_id, attempted_at DESC);

-- ─────────────────────────────────────────────────────────────────
-- [3] adapter_versions — 어댑터 레지스트리 버전 추적
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS adapter_versions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    adapter_id              VARCHAR(128)    NOT NULL,
    erp_system              VARCHAR(64)     NOT NULL,
    adapter_type            VARCHAR(32)     NOT NULL,   -- api|file|ui|desktop_vnc
    mapping_version         VARCHAR(32)     NOT NULL,
    selector_profile_version VARCHAR(64)    NOT NULL,
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    deployed_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_adapter_version UNIQUE (adapter_id, mapping_version)
);

-- ─────────────────────────────────────────────────────────────────
-- [4] credential_refs — Secret Manager 키 참조 (평문 저장 절대 금지)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS credential_refs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           VARCHAR(128)    NOT NULL,
    erp_system          VARCHAR(64)     NOT NULL,
    secret_manager_ref  VARCHAR(512)    NOT NULL,   -- 참조값만 (평문 절대 금지)
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    rotated_at          TIMESTAMPTZ,

    CONSTRAINT uq_credential_ref UNIQUE (tenant_id, erp_system)
);

-- ─────────────────────────────────────────────────────────────────
-- [5] reconciliation_jobs — UNKNOWN 상태 재조사 작업
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reconciliation_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id         UUID            NOT NULL
                        REFERENCES integration_transfers(id),
    status              VARCHAR(32)     NOT NULL
                        DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'RUNNING', 'RESOLVED', 'FAILED')),
    resolution          VARCHAR(64),    -- CONFIRMED|NOT_FOUND|AMBIGUOUS
    resolution_note     TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────────
-- [6] updated_at 자동 갱신 트리거 (integration_transfers만)
-- ─────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_it_updated_at ON integration_transfers;
CREATE TRIGGER trg_it_updated_at
    BEFORE UPDATE ON integration_transfers
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

COMMIT;
