-- ============================================================
-- Voice Guard — schema_v9_phase1.sql
-- Phase 1: 3각 검증 데이터 수집 파이프라인
--   care_plan_ledger  — 케어 계획 원장 (Append-Only + 조건부 UPDATE)
--   billing_ledger    — 청구 원장 (완전 Append-Only)
--
-- [불변 원칙] 기존 테이블 스키마 수정 0
-- [Blind Spot 4 해결] Plan + Billing 데이터 수집 인프라 구축
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART A: care_plan_ledger (케어 계획 원장)
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.care_plan_ledger (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id          VARCHAR(50)  NOT NULL,
    beneficiary_id       VARCHAR(50)  NOT NULL,
    caregiver_id         VARCHAR(50)  NOT NULL,
    plan_date            DATE         NOT NULL,
    care_type            VARCHAR(30)  NOT NULL,
    planned_start        TIMESTAMPTZ,           -- nullable: 야간/비정규 돌봄 허용
    planned_end          TIMESTAMPTZ,           -- nullable
    planned_duration_min INTEGER      NOT NULL,
    plan_source          VARCHAR(50)  NOT NULL DEFAULT 'MANUAL',
    plan_hash            CHAR(64)     NOT NULL UNIQUE,  -- SHA-256 Idempotency
    is_superseded        BOOLEAN      NOT NULL DEFAULT FALSE,
    superseded_by        UUID         REFERENCES public.care_plan_ledger(id),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_care_plan_facility_date
    ON public.care_plan_ledger (facility_id, plan_date DESC);

CREATE INDEX IF NOT EXISTS idx_care_plan_beneficiary
    ON public.care_plan_ledger (beneficiary_id, plan_date DESC);

CREATE INDEX IF NOT EXISTS idx_care_plan_hash
    ON public.care_plan_ledger (plan_hash);

-- ── A-1. 조건부 UPDATE 트리거 ──────────────────────────────────
-- is_superseded, superseded_by 외 컬럼 변경 시도 차단
CREATE OR REPLACE FUNCTION fn_care_plan_conditional_update()
RETURNS TRIGGER AS $$
BEGIN
    -- is_superseded / superseded_by 변경만 허용
    IF (OLD.id                   IS DISTINCT FROM NEW.id)
    OR (OLD.facility_id          IS DISTINCT FROM NEW.facility_id)
    OR (OLD.beneficiary_id       IS DISTINCT FROM NEW.beneficiary_id)
    OR (OLD.caregiver_id         IS DISTINCT FROM NEW.caregiver_id)
    OR (OLD.plan_date            IS DISTINCT FROM NEW.plan_date)
    OR (OLD.care_type            IS DISTINCT FROM NEW.care_type)
    OR (OLD.planned_start        IS DISTINCT FROM NEW.planned_start)
    OR (OLD.planned_end          IS DISTINCT FROM NEW.planned_end)
    OR (OLD.planned_duration_min IS DISTINCT FROM NEW.planned_duration_min)
    OR (OLD.plan_source          IS DISTINCT FROM NEW.plan_source)
    OR (OLD.plan_hash            IS DISTINCT FROM NEW.plan_hash)
    OR (OLD.created_at           IS DISTINCT FROM NEW.created_at)
    THEN
        RAISE EXCEPTION
            '[VOICE GUARD] care_plan_ledger UPDATE 차단: '
            'is_superseded, superseded_by 외 컬럼 수정 금지. '
            'Append-Only 원장입니다.';
        RETURN NULL;
    END IF;

    -- is_superseded FALSE→TRUE 전이만 허용 (역전이 차단)
    IF OLD.is_superseded = TRUE AND NEW.is_superseded = FALSE THEN
        RAISE EXCEPTION
            '[VOICE GUARD] care_plan_ledger: is_superseded 역전이(TRUE→FALSE) 금지.';
        RETURN NULL;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_care_plan_conditional_update
    ON public.care_plan_ledger;

CREATE TRIGGER trg_care_plan_conditional_update
    BEFORE UPDATE ON public.care_plan_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_care_plan_conditional_update();

-- ── A-2. DELETE 차단 트리거 ─────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_care_plan_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] care_plan_ledger DELETE/TRUNCATE 차단: '
        'Append-Only 원장입니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_care_plan_block_delete
    ON public.care_plan_ledger;
DROP TRIGGER IF EXISTS trg_care_plan_block_truncate
    ON public.care_plan_ledger;

CREATE TRIGGER trg_care_plan_block_delete
    BEFORE DELETE ON public.care_plan_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_block_care_plan_delete();

CREATE TRIGGER trg_care_plan_block_truncate
    BEFORE TRUNCATE ON public.care_plan_ledger
    EXECUTE FUNCTION fn_block_care_plan_delete();


-- ══════════════════════════════════════════════════════════════
-- PART B: billing_ledger (청구 원장)
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.billing_ledger (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id        VARCHAR(50)  NOT NULL,
    beneficiary_id     VARCHAR(50)  NOT NULL,
    billing_month      CHAR(7)      NOT NULL,  -- 'YYYY-MM' 형식
    billing_date       DATE         NOT NULL,
    care_type          VARCHAR(30)  NOT NULL,
    billed_duration_min INTEGER     NOT NULL,
    billing_code       VARCHAR(20),
    billed_amount_krw  BIGINT       NOT NULL DEFAULT 0,
    claim_status       VARCHAR(20)  NOT NULL DEFAULT 'PENDING'
                       CHECK (claim_status IN (
                           'PENDING', 'SUBMITTED', 'ACCEPTED',
                           'REJECTED', 'ADJUSTED'
                       )),
    upload_source      VARCHAR(50)  NOT NULL DEFAULT 'MANUAL',
    billing_hash       CHAR(64)     NOT NULL UNIQUE,  -- SHA-256 Idempotency
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_facility_month
    ON public.billing_ledger (facility_id, billing_month DESC);

CREATE INDEX IF NOT EXISTS idx_billing_beneficiary
    ON public.billing_ledger (beneficiary_id, billing_month DESC);

CREATE INDEX IF NOT EXISTS idx_billing_hash
    ON public.billing_ledger (billing_hash);

-- ── B-1. UPDATE 완전 차단 트리거 ────────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_billing_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] billing_ledger UPDATE 차단: '
        'Append-Only 원장입니다. 보정은 새 row INSERT로만 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_billing_block_update
    ON public.billing_ledger;

CREATE TRIGGER trg_billing_block_update
    BEFORE UPDATE ON public.billing_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_block_billing_update();

-- ── B-2. DELETE/TRUNCATE 차단 트리거 ────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_billing_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] billing_ledger DELETE/TRUNCATE 차단: '
        'Append-Only 원장입니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_billing_block_delete
    ON public.billing_ledger;
DROP TRIGGER IF EXISTS trg_billing_block_truncate
    ON public.billing_ledger;

CREATE TRIGGER trg_billing_block_delete
    BEFORE DELETE ON public.billing_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_block_billing_delete();

CREATE TRIGGER trg_billing_block_truncate
    BEFORE TRUNCATE ON public.billing_ledger
    EXECUTE FUNCTION fn_block_billing_delete();


-- ══════════════════════════════════════════════════════════════
-- PART C: 권한 설정
-- ══════════════════════════════════════════════════════════════

-- care_plan_ledger: INSERT + SELECT + 제한적 UPDATE (is_superseded/superseded_by)
REVOKE ALL ON TABLE public.care_plan_ledger FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT, UPDATE (is_superseded, superseded_by)
            ON TABLE public.care_plan_ledger TO voice_guard_ingestor;
    END IF;
END $$;

-- billing_ledger: INSERT + SELECT only
REVOKE ALL ON TABLE public.billing_ledger FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT
            ON TABLE public.billing_ledger TO voice_guard_ingestor;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 1] schema_v9 적용 완료.';
    RAISE NOTICE '  care_plan_ledger — 케어 계획 Append-Only 원장 (조건부 UPDATE 허용)';
    RAISE NOTICE '  billing_ledger   — 청구 Append-Only 원장 (완전 차단)';
    RAISE NOTICE '  UPDATE/DELETE/TRUNCATE 트리거 활성화';
END $$;
