-- ============================================================
-- Voice Guard — schema_v10_phase3.sql
-- Phase 3: 3각 검증 엔진 (Reconciliation Engine) DB 기반
--
-- [불변 원칙]
--   - care_plan_ledger / billing_ledger / evidence_ledger 스키마 수정 0
--   - 기존 트리거 함수명 변경 금지
--   - reconciliation_result 는 Append-Only (UPDATE/DELETE 차단)
--
-- 구성:
--   PART A: 2-Tier Canonical Service Fact (MATERIALIZED VIEW)
--     - Tier 1: canonical_day_fact  (3종 Day-Level 매칭)
--     - Tier 2: canonical_time_fact (Plan vs Record Time-Level)
--   PART B: Rule Engine 기초 테이블
--     - overlap_rule       (급여유형 겹침 허용 매트릭스)
--     - tolerance_ratio    (비율 기반 허용 오차)
--     - reconciliation_result (검증 결과 Append-Only 원장)
--   PART C: 인덱스 및 권한
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART A-1: Tier 1 — canonical_day_fact (3종 Day-Level 매칭)
-- ══════════════════════════════════════════════════════════════
--
-- 설계 의도:
--   청구(일 단위) ↔ 계획(plan_date) ↔ 기록(recorded_at::date) 해상도 통일.
--   3개 원장 각각을 day+care_type 단위로 집계한 뒤 FULL OUTER JOIN.
--   has_plan / has_record / has_billing 비트로 3각 매칭 상태 즉시 판별.
--
-- [Gotcha 방어]
--   FULL OUTER JOIN 3단 연쇄 시 COALESCE 체인으로 NULL 키 안전 처리.
-- ══════════════════════════════════════════════════════════════

DROP MATERIALIZED VIEW IF EXISTS public.canonical_day_fact CASCADE;

CREATE MATERIALIZED VIEW public.canonical_day_fact AS
WITH plan_agg AS (
    -- care_plan_ledger: 유효 계획만 (is_superseded = FALSE)
    SELECT
        facility_id,
        beneficiary_id,
        plan_date                        AS fact_date,
        care_type,
        caregiver_id                     AS planned_caregiver_id,
        SUM(planned_duration_min)        AS total_planned_min,
        COUNT(*)                         AS plan_count
    FROM public.care_plan_ledger
    WHERE is_superseded = FALSE
    GROUP BY facility_id, beneficiary_id, plan_date, care_type, caregiver_id
),
record_agg AS (
    -- evidence_ledger: care_type 있는 현장 기록만 집계
    SELECT
        facility_id,
        beneficiary_id,
        (recorded_at AT TIME ZONE 'Asia/Seoul')::date AS fact_date,
        care_type,
        COUNT(*)                         AS record_count,
        MIN(recorded_at)                 AS first_record_at,
        MAX(recorded_at)                 AS last_record_at
    FROM public.evidence_ledger
    WHERE care_type IS NOT NULL
      AND care_type <> ''
    GROUP BY facility_id, beneficiary_id, (recorded_at AT TIME ZONE 'Asia/Seoul')::date, care_type
),
billing_agg AS (
    -- billing_ledger: 청구 집계 (REJECTED 포함 — 전수 검증)
    SELECT
        facility_id,
        beneficiary_id,
        billing_date                     AS fact_date,
        care_type,
        SUM(billed_duration_min)         AS total_billed_min,
        SUM(billed_amount_krw)           AS total_billed_amount,
        COUNT(*)                         AS billing_count,
        ARRAY_AGG(DISTINCT claim_status) AS claim_statuses
    FROM public.billing_ledger
    GROUP BY facility_id, beneficiary_id, billing_date, care_type
)
SELECT
    COALESCE(p.facility_id,    r.facility_id,    b.facility_id)    AS facility_id,
    COALESCE(p.beneficiary_id, r.beneficiary_id, b.beneficiary_id) AS beneficiary_id,
    COALESCE(p.fact_date,      r.fact_date,      b.fact_date)      AS fact_date,
    COALESCE(p.care_type,      r.care_type,      b.care_type)      AS care_type,
    p.planned_caregiver_id,
    -- 3각 비트 (핵심 매칭 키)
    (p.facility_id IS NOT NULL)                                     AS has_plan,
    (r.facility_id IS NOT NULL)                                     AS has_record,
    (b.facility_id IS NOT NULL)                                     AS has_billing,
    -- 계획 집계
    COALESCE(p.total_planned_min, 0)                                AS total_planned_min,
    COALESCE(p.plan_count, 0)                                       AS plan_count,
    -- 기록 집계
    COALESCE(r.record_count, 0)                                     AS record_count,
    r.first_record_at,
    r.last_record_at,
    -- 청구 집계
    COALESCE(b.total_billed_min, 0)                                 AS total_billed_min,
    COALESCE(b.total_billed_amount, 0)                              AS total_billed_amount,
    COALESCE(b.billing_count, 0)                                    AS billing_count,
    b.claim_statuses,
    -- 뷰 갱신 시각 (배치 추적용)
    NOW()                                                           AS refreshed_at
FROM plan_agg p
FULL OUTER JOIN record_agg r
    ON  p.facility_id    = r.facility_id
    AND p.beneficiary_id = r.beneficiary_id
    AND p.fact_date      = r.fact_date
    AND p.care_type      = r.care_type
FULL OUTER JOIN billing_agg b
    ON  COALESCE(p.facility_id,    r.facility_id)    = b.facility_id
    AND COALESCE(p.beneficiary_id, r.beneficiary_id) = b.beneficiary_id
    AND COALESCE(p.fact_date,      r.fact_date)      = b.fact_date
    AND COALESCE(p.care_type,      r.care_type)      = b.care_type
;

-- 갱신 명령 (배치 스케줄러 또는 수동):
-- REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_day_fact;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cdf_pk
    ON public.canonical_day_fact (facility_id, beneficiary_id, fact_date, care_type);

CREATE INDEX IF NOT EXISTS idx_cdf_facility_date
    ON public.canonical_day_fact (facility_id, fact_date DESC);

CREATE INDEX IF NOT EXISTS idx_cdf_anomaly_candidates
    ON public.canonical_day_fact (fact_date DESC)
    WHERE has_billing = TRUE AND has_record = FALSE;


-- ══════════════════════════════════════════════════════════════
-- PART A-2: Tier 2 — canonical_time_fact (Plan vs Record 시간 매칭)
-- ══════════════════════════════════════════════════════════════
--
-- 설계 의도:
--   청구는 일 단위라 시간 비교 불가. 계획(분 단위) ↔ 기록(TIMESTAMPTZ) 정밀 대조.
--   야간 케어 감지(22:00~06:00), 대타 감지(caregiver vs device) 지원.
--   FULL OUTER JOIN으로 plan-only / record-only 양쪽 모두 포착.
-- ══════════════════════════════════════════════════════════════

DROP MATERIALIZED VIEW IF EXISTS public.canonical_time_fact CASCADE;

CREATE MATERIALIZED VIEW public.canonical_time_fact AS
SELECT
    COALESCE(p.facility_id,    e.facility_id)    AS facility_id,
    COALESCE(p.beneficiary_id, e.beneficiary_id) AS beneficiary_id,
    COALESCE(p.plan_date,      (e.recorded_at AT TIME ZONE 'Asia/Seoul')::date) AS fact_date,
    COALESCE(p.care_type,      e.care_type)      AS care_type,
    -- 계획 쪽 컬럼
    p.id                                          AS plan_id,
    p.caregiver_id                                AS planned_caregiver_id,
    p.planned_start                               AS plan_start_time,
    p.planned_end                                 AS plan_end_time,
    p.planned_duration_min,
    -- 기록 쪽 컬럼
    e.id                                          AS record_id,
    e.device_id                                   AS record_device_id,
    e.shift_id                                    AS record_shift_id,
    e.recorded_at                                 AS record_time,
    -- 파생 컬럼
    (p.id IS NOT NULL)                            AS has_plan,
    (e.id IS NOT NULL)                            AS has_record,
    -- 야간 케어 여부 (22:00~06:00 KST)
    CASE
        WHEN e.recorded_at IS NOT NULL THEN
            EXTRACT(HOUR FROM e.recorded_at AT TIME ZONE 'Asia/Seoul') >= 22
            OR EXTRACT(HOUR FROM e.recorded_at AT TIME ZONE 'Asia/Seoul') < 6
        ELSE NULL
    END                                           AS is_night_care,
    -- 기록이 계획 시간 창 내에 있는지 (계획 시간 있을 때만)
    CASE
        WHEN p.planned_start IS NOT NULL AND e.recorded_at IS NOT NULL THEN
            e.recorded_at BETWEEN p.planned_start AND p.planned_end
        ELSE NULL
    END                                           AS within_plan_window,
    -- 담당자 불일치 여부 (대타 탐지)
    CASE
        WHEN p.caregiver_id IS NOT NULL AND e.device_id IS NOT NULL THEN
            p.caregiver_id <> e.device_id
        ELSE NULL
    END                                           AS caregiver_mismatch,
    NOW()                                         AS refreshed_at
FROM public.care_plan_ledger p
FULL OUTER JOIN public.evidence_ledger e
    ON  p.facility_id    = e.facility_id
    AND p.beneficiary_id = e.beneficiary_id
    AND p.plan_date      = (e.recorded_at AT TIME ZONE 'Asia/Seoul')::date
    AND p.care_type      = e.care_type
WHERE COALESCE(p.is_superseded, FALSE) = FALSE
;

CREATE INDEX IF NOT EXISTS idx_ctf_facility_date
    ON public.canonical_time_fact (facility_id, fact_date DESC);

CREATE INDEX IF NOT EXISTS idx_ctf_night
    ON public.canonical_time_fact (fact_date DESC)
    WHERE is_night_care = TRUE;

CREATE INDEX IF NOT EXISTS idx_ctf_mismatch
    ON public.canonical_time_fact (fact_date DESC)
    WHERE caregiver_mismatch = TRUE;


-- ══════════════════════════════════════════════════════════════
-- PART B-1: overlap_rule — 급여유형 겹침 허용 매트릭스
-- ══════════════════════════════════════════════════════════════
--
-- 요양 도메인 특화: 같은 날 동일 수급자에게 두 급여유형이 동시 청구될 때
-- 법적으로 허용되는지 여부를 DB로 관리 (하드코딩 방지).
--
-- care_type_a ≤ care_type_b (알파벳 순 정렬) 로 중복 행 방지.
-- is_overlap_allowed = FALSE → 동시 청구 시 ANOMALY (ILLEGAL_OVERLAP).
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.overlap_rule (
    id                  SERIAL       PRIMARY KEY,
    care_type_a         VARCHAR(30)  NOT NULL,
    care_type_b         VARCHAR(30)  NOT NULL,
    is_overlap_allowed  BOOLEAN      NOT NULL,
    rule_note           TEXT,
    effective_from      DATE         NOT NULL DEFAULT '2024-01-01',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_overlap_pair UNIQUE (care_type_a, care_type_b),
    CONSTRAINT chk_overlap_order CHECK (care_type_a <= care_type_b)
);

-- 동일 유형 자기 자신끼리 겹침: 전부 불허 (ANOMALY)
INSERT INTO public.overlap_rule
    (care_type_a, care_type_b, is_overlap_allowed, rule_note)
VALUES
    ('BATH_CARE',    'BATH_CARE',    FALSE, '같은 날 방문목욕 2회 = 중복 청구 불허'),
    ('NURSING',      'NURSING',      FALSE, '같은 날 방문간호 2회 = 중복 청구 불허'),
    ('VISIT_CARE',   'VISIT_CARE',   FALSE, '같은 날 방문요양 2회 = 중복 청구 불허'),
    ('DAYCARE',      'DAYCARE',      FALSE, '주야간보호 동일일 2회 불허'),
    ('SHORT_TERM',   'SHORT_TERM',   FALSE, '단기보호 동일일 2회 불허'),
    ('WELFARE_EQUIP','WELFARE_EQUIP',FALSE, '복지용구 동일일 2회 불허')
ON CONFLICT (care_type_a, care_type_b) DO NOTHING;

-- 유형 간 조합: 요양 도메인 NHIS 기준
INSERT INTO public.overlap_rule
    (care_type_a, care_type_b, is_overlap_allowed, rule_note)
VALUES
    -- 방문요양 + 방문목욕: 이동 지원 후 목욕 서비스 — 허용
    ('BATH_CARE',    'VISIT_CARE',   TRUE,  '방문요양+목욕: 이동지원 후 목욕 서비스 동시 허용'),
    -- 방문요양 + 방문간호: 요양+간호 조합 — 허용
    ('NURSING',      'VISIT_CARE',   TRUE,  '방문요양+간호: 복합 서비스 동시 허용'),
    -- 방문목욕 + 방문간호: 목욕 후 간호 처치 — 허용
    ('BATH_CARE',    'NURSING',      TRUE,  '방문목욕+간호: 처치 목욕 동시 허용'),
    -- 방문요양 + 주야간보호: 당일 병행 불허 (이중 청구 위험)
    ('DAYCARE',      'VISIT_CARE',   FALSE, '방문요양+주야간보호: 동일일 이중 청구 불허'),
    -- 방문요양 + 단기보호: 불허
    ('SHORT_TERM',   'VISIT_CARE',   FALSE, '방문요양+단기보호: 동일일 이중 청구 불허'),
    -- 주야간보호 + 단기보호: 불허
    ('DAYCARE',      'SHORT_TERM',   FALSE, '주야간+단기보호: 동일일 이중 청구 불허'),
    -- 복지용구 + 기타: 별도 급여라 겹침 허용
    ('BATH_CARE',    'WELFARE_EQUIP',TRUE,  '복지용구는 타 급여와 병행 허용'),
    ('NURSING',      'WELFARE_EQUIP',TRUE,  '복지용구는 타 급여와 병행 허용'),
    ('VISIT_CARE',   'WELFARE_EQUIP',TRUE,  '복지용구는 타 급여와 병행 허용'),
    ('DAYCARE',      'WELFARE_EQUIP',TRUE,  '복지용구는 타 급여와 병행 허용'),
    ('SHORT_TERM',   'WELFARE_EQUIP',TRUE,  '복지용구는 타 급여와 병행 허용')
ON CONFLICT (care_type_a, care_type_b) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_overlap_type_a ON public.overlap_rule (care_type_a);
CREATE INDEX IF NOT EXISTS idx_overlap_type_b ON public.overlap_rule (care_type_b);


-- ══════════════════════════════════════════════════════════════
-- PART B-2: tolerance_ratio — 급여유형별 비율 기반 허용 오차
-- ══════════════════════════════════════════════════════════════
--
-- 절대값이 아닌 비율로 오차 허용.
--   예: 식사 20% → 계획 60분 기준 ±12분 허용 (48~72분).
--   요양 도메인 특성상 이동시간·대기 편차를 반영.
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.tolerance_ratio (
    id              SERIAL       PRIMARY KEY,
    care_type       VARCHAR(30)  NOT NULL UNIQUE,
    tolerance_ratio NUMERIC(5,4) NOT NULL CHECK (tolerance_ratio >= 0 AND tolerance_ratio <= 1),
    tolerance_note  TEXT,
    effective_from  DATE         NOT NULL DEFAULT '2024-01-01',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

INSERT INTO public.tolerance_ratio
    (care_type, tolerance_ratio, tolerance_note)
VALUES
    ('VISIT_CARE',    0.2000, '방문요양: 이동시간 편차 감안 20% 허용'),
    ('BATH_CARE',     0.1500, '방문목욕: 고정 시간 서비스 15% 허용'),
    ('NURSING',       0.1000, '방문간호: 처치 정밀도 요구 10% 허용'),
    ('DAYCARE',       0.0500, '주야간보호: 입퇴소 기록 정밀 5% 허용'),
    ('SHORT_TERM',    0.0500, '단기보호: 입퇴소 기록 정밀 5% 허용'),
    ('WELFARE_EQUIP', 0.0000, '복지용구: 건수 기반 청구 — 오차 불허')
ON CONFLICT (care_type) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- PART B-3: reconciliation_result — 검증 결과 Append-Only 원장
-- ══════════════════════════════════════════════════════════════
--
-- 상태 체계:
--   MATCH    — 3각 완전 일치 (오차 범위 내 포함)
--   PARTIAL  — 부분 일치 (미청구, 계획 미실행 등)
--   ANOMALY  — 이상 탐지 (환수 위험)
--   UNPLANNED — 무계획 케어 (야간/긴급은 정상 분류)
--
-- anomaly_code 체계 (anomaly_detail JSONB에 상세):
--   PHANTOM_BILLING      — 기록 없이 청구 (최고 위험)
--   UNPLANNED_BILLING    — 계획/기록 없이 청구
--   OVER_BILLING         — 청구 시간 > 계획+허용 오차
--   UNDER_BILLING        — 청구 시간 < 계획-허용 오차
--   PLANNED_NOT_EXECUTED — 계획만 있고 실행/청구 없음
--   UNBILLED_CARE        — 케어 실행했으나 미청구
--   UNPLANNED_NIGHT      — 야간 무계획 돌봄 (정상)
--   UNPLANNED_EMERGENCY  — 긴급 무계획 돌봄 (정상)
--   UNPLANNED_DAYTIME    — 주간 무계획 돌봄 (이상)
--   ILLEGAL_OVERLAP      — 동시 청구 불허 급여유형 조합
--   SUBSTITUTION         — 대타 담당자 (정상 처리)
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.reconciliation_result (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     VARCHAR(50)  NOT NULL,
    beneficiary_id  VARCHAR(50)  NOT NULL,
    fact_date       DATE         NOT NULL,
    care_type       VARCHAR(30)  NOT NULL,
    result_status   VARCHAR(20)  NOT NULL
                    CHECK (result_status IN ('MATCH', 'PARTIAL', 'ANOMALY', 'UNPLANNED')),
    anomaly_code    VARCHAR(50),
    anomaly_detail  JSONB,
    -- 3각 비트 스냅샷
    has_plan        BOOLEAN      NOT NULL DEFAULT FALSE,
    has_record      BOOLEAN      NOT NULL DEFAULT FALSE,
    has_billing     BOOLEAN      NOT NULL DEFAULT FALSE,
    -- 수치 스냅샷 (원장 값 변경 방어)
    planned_min     INTEGER      NOT NULL DEFAULT 0,
    recorded_count  INTEGER      NOT NULL DEFAULT 0,
    billed_min      INTEGER      NOT NULL DEFAULT 0,
    -- 엔진 메타
    engine_version  VARCHAR(10)  NOT NULL DEFAULT '1.0',
    run_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recon_facility_date
    ON public.reconciliation_result (facility_id, fact_date DESC);

CREATE INDEX IF NOT EXISTS idx_recon_status
    ON public.reconciliation_result (result_status, fact_date DESC);

CREATE INDEX IF NOT EXISTS idx_recon_anomaly
    ON public.reconciliation_result (fact_date DESC)
    WHERE result_status = 'ANOMALY';

CREATE INDEX IF NOT EXISTS idx_recon_beneficiary
    ON public.reconciliation_result (beneficiary_id, fact_date DESC);

-- ── reconciliation_result Append-Only 트리거 ────────────────

CREATE OR REPLACE FUNCTION fn_block_recon_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] reconciliation_result UPDATE 차단: '
        'Append-Only 검증 원장입니다. 재검증은 새 run으로만 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_recon_block_update ON public.reconciliation_result;
CREATE TRIGGER trg_recon_block_update
    BEFORE UPDATE ON public.reconciliation_result
    FOR EACH ROW EXECUTE FUNCTION fn_block_recon_update();

CREATE OR REPLACE FUNCTION fn_block_recon_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] reconciliation_result DELETE/TRUNCATE 차단: '
        'Append-Only 검증 원장입니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_recon_block_delete   ON public.reconciliation_result;
DROP TRIGGER IF EXISTS trg_recon_block_truncate ON public.reconciliation_result;

CREATE TRIGGER trg_recon_block_delete
    BEFORE DELETE ON public.reconciliation_result
    FOR EACH ROW EXECUTE FUNCTION fn_block_recon_delete();

CREATE TRIGGER trg_recon_block_truncate
    BEFORE TRUNCATE ON public.reconciliation_result
    EXECUTE FUNCTION fn_block_recon_delete();


-- ══════════════════════════════════════════════════════════════
-- PART C: 권한 설정
-- ══════════════════════════════════════════════════════════════

-- overlap_rule, tolerance_ratio: 읽기 전용 (런타임 수정 금지)
REVOKE ALL ON TABLE public.overlap_rule    FROM PUBLIC;
REVOKE ALL ON TABLE public.tolerance_ratio FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT SELECT ON TABLE public.overlap_rule    TO voice_guard_ingestor;
        GRANT SELECT ON TABLE public.tolerance_ratio TO voice_guard_ingestor;
        GRANT INSERT, SELECT ON TABLE public.reconciliation_result TO voice_guard_ingestor;
    END IF;
END $$;

-- MATERIALIZED VIEW 읽기 권한
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT SELECT ON public.canonical_day_fact  TO voice_guard_ingestor;
        GRANT SELECT ON public.canonical_time_fact TO voice_guard_ingestor;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 3] schema_v10 적용 완료.';
    RAISE NOTICE '  Tier 1: canonical_day_fact MATERIALIZED VIEW (3종 Day-Level 매칭)';
    RAISE NOTICE '  Tier 2: canonical_time_fact MATERIALIZED VIEW (Plan vs Record 시간 매칭)';
    RAISE NOTICE '  overlap_rule: 6x6 급여유형 겹침 허용 매트릭스 (11행 초기화)';
    RAISE NOTICE '  tolerance_ratio: 6개 급여유형 비율 기반 허용 오차';
    RAISE NOTICE '  reconciliation_result: Append-Only 검증 원장 + UPDATE/DELETE 차단 트리거';
    RAISE NOTICE '  갱신: REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_day_fact;';
END $$;
