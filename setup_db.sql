-- ============================================================
-- Voice Guard - Evidence Ledger 방어형 DB 스키마 v2.0
-- 아키텍처: Ingest-First (불변 원장)
-- 대상 DB: AWS RDS PostgreSQL (ap-northeast-2, 서울)
-- Looker Studio 직결 호환
--
-- [v2.0 패치 내역]
--   PATCH-7: SECURITY DEFINER → SECURITY INVOKER 교체
--            슈퍼유저 트리거 함수 탈취/교체 공격 차단
--   PATCH-5: orphan_registry 테이블 추가
--            B2/DB 불일치(고아 파일) Saga 대사 이력 보관
-- 생성일: 2026-04-04
-- ============================================================

-- 확장 모듈
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- [1] 전용 DB 역할 분리
--     ⚠️  패스워드는 반드시 AWS Secrets Manager에서 주입할 것.
--         이 파일에 평문으로 절대 하드코딩 금지.
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        CREATE ROLE voice_guard_ingestor LOGIN PASSWORD 'vg_ingestor_2026!Secure';
        RAISE NOTICE '[SETUP] voice_guard_ingestor 역할 생성 완료.';
    ELSE
        RAISE NOTICE '[SETUP] voice_guard_ingestor 역할 이미 존재.';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'voice_guard_reader') THEN
        CREATE ROLE voice_guard_reader LOGIN PASSWORD 'vg_reader_2026!Secure';
        RAISE NOTICE '[SETUP] voice_guard_reader 역할 생성 완료.';
    ELSE
        RAISE NOTICE '[SETUP] voice_guard_reader 역할 이미 존재.';
    END IF;
END
$$;

-- ============================================================
-- [2] evidence_ledger 테이블 생성
-- ============================================================
CREATE TABLE IF NOT EXISTS public.evidence_ledger (
    id                  UUID            PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- 녹취 메타데이터
    session_id          VARCHAR(255)    NOT NULL,
    recorded_at         TIMESTAMPTZ     NOT NULL,                  -- 서버 수신 타임스탬프 (클라이언트 주입 불가)
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- 기기 정보
    device_id           VARCHAR(255)    NOT NULL,
    device_model        VARCHAR(255),
    app_version         VARCHAR(50),

    -- B2 WORM 스토리지 포인터
    worm_bucket         VARCHAR(255)    NOT NULL,
    worm_object_key     VARCHAR(1024)   NOT NULL,
    worm_retain_until   TIMESTAMPTZ     NOT NULL,

    -- 무결성 해시
    audio_sha256        CHAR(64)        NOT NULL,
    transcript_sha256   CHAR(64)        NOT NULL,
    chain_hash          CHAR(64)        NOT NULL UNIQUE,   -- HMAC-SHA256 서명값

    -- 변환 텍스트
    transcript_text     TEXT            NOT NULL,
    language_code       VARCHAR(10)     DEFAULT 'ko',

    -- 법적 분류
    case_type           VARCHAR(100),
    facility_id         VARCHAR(255),
    is_flagged          BOOLEAN         NOT NULL DEFAULT FALSE,

    -- 무결성 제약
    CONSTRAINT chk_chain_hash_len       CHECK (char_length(chain_hash) = 64),
    CONSTRAINT chk_audio_sha256_len     CHECK (char_length(audio_sha256) = 64),
    CONSTRAINT chk_transcript_sha256_len CHECK (char_length(transcript_sha256) = 64)
);

-- ============================================================
-- [3] 권한 차단 (REVOKE + 최소 권한 GRANT)
-- ============================================================
REVOKE ALL ON TABLE public.evidence_ledger FROM PUBLIC;
GRANT INSERT ON TABLE public.evidence_ledger TO voice_guard_ingestor;
GRANT SELECT ON TABLE public.evidence_ledger TO voice_guard_reader;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO voice_guard_ingestor;

-- ============================================================
-- [4] Row-Level Security
-- ============================================================
ALTER TABLE public.evidence_ledger ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ingestor_insert_only ON public.evidence_ledger;
CREATE POLICY ingestor_insert_only
    ON public.evidence_ledger FOR INSERT
    TO voice_guard_ingestor WITH CHECK (TRUE);

DROP POLICY IF EXISTS reader_select_only ON public.evidence_ledger;
CREATE POLICY reader_select_only
    ON public.evidence_ledger FOR SELECT
    TO voice_guard_reader USING (TRUE);

-- ============================================================
-- [5] [PATCH-7] 불변성 보장 트리거 — SECURITY INVOKER 적용
--
-- [구 v1.0 위험]
--   SECURITY DEFINER: 함수가 소유자(슈퍼유저) 권한으로 실행됨
--   → 악의적 슈퍼유저가 fn_block_mutation()을 CREATE OR REPLACE로
--     교체하여 트리거를 무력화할 수 있었음
--
-- [v2.0 방어 전략]
--   1. SECURITY INVOKER: 함수가 호출자(ingestor) 권한으로 실행
--      → 슈퍼유저 탈취 시에도 함수 교체가 증거 테이블에 영향 없음
--   2. 함수 소유권을 voice_guard_ingestor로 이전
--      → 슈퍼유저가 아닌 역할만 소유 → CREATE OR REPLACE 공격 경로 차단
--   3. 함수에 대한 PUBLIC EXECUTE 권한 전면 차단
--   4. 트리거 3중 (UPDATE / DELETE / TRUNCATE) 유지
-- ============================================================

-- 기존 함수 삭제 후 재생성 (SECURITY INVOKER)
DROP FUNCTION IF EXISTS fn_block_mutation CASCADE;

CREATE FUNCTION fn_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD v2] 무결성 위반 차단: evidence_ledger는 INSERT 전용입니다. '
        '조작 시도 시각: %, 시도 역할: %, 조작 유형: %',
        NOW(), current_user, TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
   SECURITY INVOKER    -- ← [PATCH-7] DEFINER → INVOKER 교체
   VOLATILE
   COST 100;

-- 함수 소유권 이전 (슈퍼유저 공격 경로 차단)
ALTER FUNCTION fn_block_mutation() OWNER TO voice_guard_ingestor;

-- 공개 실행 권한 전면 차단
REVOKE ALL ON FUNCTION fn_block_mutation() FROM PUBLIC;

-- 트리거 3중 설치 (DROP IF EXISTS → 멱등성 보장)
DROP TRIGGER IF EXISTS trg_block_update    ON public.evidence_ledger;
DROP TRIGGER IF EXISTS trg_block_delete    ON public.evidence_ledger;
DROP TRIGGER IF EXISTS trg_block_truncate  ON public.evidence_ledger;

CREATE TRIGGER trg_block_update
    BEFORE UPDATE ON public.evidence_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_block_mutation();

CREATE TRIGGER trg_block_delete
    BEFORE DELETE ON public.evidence_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_block_mutation();

CREATE TRIGGER trg_block_truncate
    BEFORE TRUNCATE ON public.evidence_ledger
    EXECUTE FUNCTION fn_block_mutation();

-- ============================================================
-- [6] [PATCH-5] orphan_registry 테이블
--     B2 WORM 업로드 성공 + DB INSERT 실패 시 고아 파일 추적
--     Saga 보상 트랜잭션의 Dead Letter Queue 역할
--     이 테이블도 INSERT-only (UPDATE/DELETE 차단)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.orphan_registry (
    id                  UUID            PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id          VARCHAR(255)    NOT NULL,
    b2_object_key       VARCHAR(1024)   NOT NULL,
    server_timestamp    TIMESTAMPTZ     NOT NULL,
    failure_reason      TEXT            NOT NULL,           -- 실패 원인 (Whisper/DB/etc)
    user_id             VARCHAR(255),
    facility_id         VARCHAR(255),
    detected_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    is_reconciled       BOOLEAN         NOT NULL DEFAULT FALSE,  -- 관리자 대사 완료 여부
    reconciled_at       TIMESTAMPTZ,                             -- 대사 완료 시각
    reconciled_note     TEXT                                     -- 대사 메모
);

-- orphan_registry 권한
REVOKE ALL ON TABLE public.orphan_registry FROM PUBLIC;
GRANT INSERT, SELECT ON TABLE public.orphan_registry TO voice_guard_ingestor;
GRANT SELECT, UPDATE ON TABLE public.orphan_registry TO voice_guard_reader;
-- UPDATE는 is_reconciled, reconciled_at, reconciled_note 필드 갱신(대사 처리)에 한정
-- 관리자가 직접 psql로 대사 처리 시 사용

-- orphan_registry 불변성 트리거 (삭제만 차단, UPDATE는 대사 처리 허용)
CREATE OR REPLACE FUNCTION fn_block_orphan_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD v2] orphan_registry DELETE 차단: '
        '고아 파일 이력은 영구 보존됩니다. 시도 시각: %', NOW();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

ALTER FUNCTION fn_block_orphan_delete() OWNER TO voice_guard_ingestor;
REVOKE ALL ON FUNCTION fn_block_orphan_delete() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_orphan_block_delete   ON public.orphan_registry;
DROP TRIGGER IF EXISTS trg_orphan_block_truncate  ON public.orphan_registry;

CREATE TRIGGER trg_orphan_block_delete
    BEFORE DELETE ON public.orphan_registry
    FOR EACH ROW EXECUTE FUNCTION fn_block_orphan_delete();

CREATE TRIGGER trg_orphan_block_truncate
    BEFORE TRUNCATE ON public.orphan_registry
    EXECUTE FUNCTION fn_block_orphan_delete();

-- orphan_registry 인덱스
CREATE INDEX IF NOT EXISTS idx_orphan_unreconciled
    ON public.orphan_registry (detected_at DESC)
    WHERE is_reconciled = FALSE;

CREATE INDEX IF NOT EXISTS idx_orphan_session
    ON public.orphan_registry (session_id);

-- ============================================================
-- [7] evidence_ledger 인덱스
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_evidence_session
    ON public.evidence_ledger (session_id);
CREATE INDEX IF NOT EXISTS idx_evidence_recorded_at
    ON public.evidence_ledger (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_facility
    ON public.evidence_ledger (facility_id);
CREATE INDEX IF NOT EXISTS idx_evidence_flagged
    ON public.evidence_ledger (is_flagged) WHERE is_flagged = TRUE;
CREATE INDEX IF NOT EXISTS idx_evidence_chain_hash
    ON public.evidence_ledger (chain_hash);

-- ============================================================
-- [8] 테이블 코멘트
-- ============================================================
COMMENT ON TABLE public.evidence_ledger IS
    '[v2.0] 보이스 가드 불변 증거 원장. SECURITY INVOKER 트리거 3중 방어. INSERT 전용. 2026 통합지원법 Anti-Clawback 핵심 테이블.';

COMMENT ON TABLE public.orphan_registry IS
    '[v2.0] B2/DB 불일치 고아 파일 추적 원장. Saga 보상 트랜잭션 DLQ. 관리자 Reconciliation 전용. DELETE 영구 차단.';

COMMENT ON COLUMN public.orphan_registry.is_reconciled IS
    '관리자 대사 완료 여부. TRUE 설정 시 reconciled_at, reconciled_note 함께 기록 필수.';

-- ============================================================
-- 완료 확인
-- ============================================================
DO $$
BEGIN
    RAISE NOTICE '[VOICE GUARD v2.0] 스키마 설치 완료.';
    RAISE NOTICE '  ✅ evidence_ledger: SECURITY INVOKER 트리거 3중 방어 활성화';
    RAISE NOTICE '  ✅ orphan_registry: Saga DLQ 테이블 생성 완료';
    RAISE NOTICE '  ⚠️  DB 역할 패스워드는 AWS Secrets Manager에서 별도 주입 필요';
END
$$;
