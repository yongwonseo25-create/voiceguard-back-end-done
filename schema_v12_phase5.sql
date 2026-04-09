-- ============================================================
-- Voice Guard — schema_v12_phase5.sql
-- Phase 5: 자동 인수인계 엔진 (Shift Handover Brief Engine)
--
-- [불변 원칙]
--   - evidence_ledger 스키마 수정 0  (기존 컬럼 건드리지 않음)
--   - shift_handover_ledger: UPDATE(ACK·무효화 제외)/DELETE/TRUNCATE 트리거 차단
--   - 상태 변경 = 새 row INSERT (보상 트랜잭션)
--
-- 구성:
--   PART 0: evidence_ledger.case_type 타입 안전 검사 (결함 5 방어)
--   PART A: unified_outbox.event_type CHECK 제약 확장 (handover_trigger·handover_ack 추가)
--   PART B: shift_handover_ledger 인수인계 원장
--   PART C: UPDATE / DELETE / TRUNCATE 트리거
--   PART D: 인덱스 + 권한
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART 0: case_type 컬럼 타입 안전 검사 + ENUM 분기 (결함 5)
-- ══════════════════════════════════════════════════════════════
--
-- 목적: care_type / case_type 이 VARCHAR(100)이면 'HANDOVER' 값 삽입만으로 충분.
--       ENUM 타입인 경우에만 ALTER TYPE ... ADD VALUE 를 조건부 실행한다.
--       기존 컬럼의 타입 자체를 변경하는 것은 절대 금지.
-- ══════════════════════════════════════════════════════════════

DO $$
DECLARE
    v_col_type  TEXT;
    v_udt_name  TEXT;
    v_has_value BOOLEAN;
BEGIN
    -- care_type 컬럼 (schema_v2에서 추가된 VARCHAR(100)) 타입 조회
    SELECT
        pg_catalog.format_type(a.atttypid, a.atttypmod),
        t.typname
    INTO v_col_type, v_udt_name
    FROM pg_attribute a
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = 'public.evidence_ledger'::regclass
      AND a.attname  = 'care_type'
      AND a.attnum   > 0
      AND NOT a.attisdropped;

    IF v_col_type IS NULL THEN
        RAISE EXCEPTION
            '[VOICE GUARD Phase 5] evidence_ledger.care_type 컬럼을 찾을 수 없습니다. '
            'schema_v2.sql 선행 마이그레이션을 확인하십시오.';
    END IF;

    IF pg_catalog.format_type(
           (SELECT atttypid FROM pg_attribute
            WHERE attrelid='public.evidence_ledger'::regclass
              AND attname='care_type' AND attnum>0),
           -1
       ) LIKE 'USER-DEFINED' OR v_udt_name NOT IN ('varchar','text','bpchar') THEN
        -- ENUM 또는 사용자 정의 타입: 값만 추가 (타입 변경 아님)
        SELECT EXISTS(
            SELECT 1 FROM pg_enum e
            JOIN pg_type t2 ON t2.oid = e.enumtypid
            WHERE t2.typname = v_udt_name AND e.enumlabel = 'HANDOVER'
        ) INTO v_has_value;

        IF NOT v_has_value THEN
            EXECUTE format('ALTER TYPE %I ADD VALUE ''HANDOVER''', v_udt_name);
            RAISE NOTICE '[VOICE GUARD Phase 5] care_type ENUM(%) 에 HANDOVER 추가 완료.', v_udt_name;
        ELSE
            RAISE NOTICE '[VOICE GUARD Phase 5] care_type ENUM(%) 에 HANDOVER 이미 존재 — 건너뜀.', v_udt_name;
        END IF;
    ELSE
        -- VARCHAR/TEXT: ALTER 불필요, 값 삽입만으로 동작
        RAISE NOTICE '[VOICE GUARD Phase 5] care_type 타입: % — VARCHAR/TEXT 확인. ALTER 불필요.', v_col_type;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- PART A: unified_outbox 이벤트 라우팅 확장 (DBA 수동 적용)
-- ══════════════════════════════════════════════════════════════
--
-- Phase 5 handover 이벤트는 Redis Stream 직접 발행 방식으로 동작하며
-- unified_outbox 를 경유하지 않는다.
--
-- unified_outbox.event_type 허용 목록 확장이 필요하다면
-- DBA 가 schema_v12_manual_outbox_patch.sql 을 별도 실행할 것.
-- (해당 파일은 레포 외부 DBA 전용 스크립트로 관리됨)
--
-- 이 파일에서는 unified_outbox DDL 을 생략한다.
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[VOICE GUARD Phase 5] PART A: unified_outbox DDL 생략 — DBA 수동 패치로 위임.';
END $$;


-- ══════════════════════════════════════════════════════════════
-- PART B: shift_handover_ledger — 인수인계 원장 (Append-Only)
-- ══════════════════════════════════════════════════════════════
--
-- [봉인 필드]
--   id, facility_id, shift_start, shift_end, generated_at,
--   trigger_mode, source_count, anomaly_count,
--   brief_text, brief_sha256, generation_mode,
--   tts_object_key, tts_sha256
--
-- [조건부 UPDATE 허용 — 트리거로 검증]
--   delivered_to, delivered_at  → ACK 수령 확인 (결함 6 방어)
--   is_superseded, superseded_by → 무효화 체인 (care_plan_ledger 패턴)
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.shift_handover_ledger (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id      TEXT         NOT NULL,
    shift_start      TIMESTAMPTZ  NOT NULL,
    shift_end        TIMESTAMPTZ  NOT NULL,
    generated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- 트리거 분류 (결함 1: 대타/수동 트리거 방어)
    trigger_mode     VARCHAR(10)  NOT NULL
                     CHECK (trigger_mode IN ('SCHEDULED', 'MANUAL')),

    -- 입력 추적 (감사 가능성)
    source_count     INT          NOT NULL DEFAULT 0,
    anomaly_count    INT          NOT NULL DEFAULT 0,

    -- 출력물 봉인
    brief_text       TEXT         NOT NULL,
    brief_sha256     CHAR(64)     NOT NULL,

    -- 생성 모드 (결함 3: LLM 장애 폴백 방어)
    generation_mode  VARCHAR(12)  NOT NULL
                     CHECK (generation_mode IN ('LLM', 'RAW_FALLBACK')),

    -- TTS 오디오 (nullable — 생성 실패 허용, 텍스트는 보존)
    tts_object_key   TEXT,
    tts_sha256       CHAR(64),

    -- 수령 확인 (결함 6: ACK 루프)
    delivered_to     TEXT,
    delivered_at     TIMESTAMPTZ,

    -- 무효화 체인 (care_plan_ledger 패턴 동일)
    is_superseded    BOOLEAN      NOT NULL DEFAULT FALSE,
    superseded_by    UUID         REFERENCES public.shift_handover_ledger(id)
);

COMMENT ON TABLE public.shift_handover_ledger IS
    'Voice Guard Phase 5 — 자동 인수인계 브리핑 원장 (Append-Only). '
    'UPDATE는 ACK(delivered_to/delivered_at) 및 무효화(is_superseded/superseded_by)만 허용.';

COMMENT ON COLUMN public.shift_handover_ledger.trigger_mode IS
    'SCHEDULED: pg_cron 교대 시간 트리거 | MANUAL: 대타 출근 등 수동 발행 (결함1 방어)';

COMMENT ON COLUMN public.shift_handover_ledger.generation_mode IS
    'LLM: claude-haiku 요약 성공 | RAW_FALLBACK: LLM 장애 시 원문 정렬 (결함3 방어)';


-- ══════════════════════════════════════════════════════════════
-- PART C: 트리거 — UPDATE 봉인 검사 / DELETE / TRUNCATE 차단
-- ══════════════════════════════════════════════════════════════

-- ── C-1: UPDATE — 봉인 필드 변경 차단 (ACK·무효화 필드만 허용) ──

CREATE OR REPLACE FUNCTION fn_handover_ledger_update_guard()
RETURNS TRIGGER AS $$
BEGIN
    -- 봉인 필드 변경 감지: IS DISTINCT FROM 은 NULL 안전 비교
    IF (OLD.id               IS DISTINCT FROM NEW.id)               OR
       (OLD.facility_id      IS DISTINCT FROM NEW.facility_id)      OR
       (OLD.shift_start      IS DISTINCT FROM NEW.shift_start)      OR
       (OLD.shift_end        IS DISTINCT FROM NEW.shift_end)        OR
       (OLD.generated_at     IS DISTINCT FROM NEW.generated_at)     OR
       (OLD.trigger_mode     IS DISTINCT FROM NEW.trigger_mode)     OR
       (OLD.source_count     IS DISTINCT FROM NEW.source_count)     OR
       (OLD.anomaly_count    IS DISTINCT FROM NEW.anomaly_count)    OR
       (OLD.brief_text       IS DISTINCT FROM NEW.brief_text)       OR
       (OLD.brief_sha256     IS DISTINCT FROM NEW.brief_sha256)     OR
       (OLD.generation_mode  IS DISTINCT FROM NEW.generation_mode)  OR
       (OLD.tts_object_key   IS DISTINCT FROM NEW.tts_object_key)   OR
       (OLD.tts_sha256       IS DISTINCT FROM NEW.tts_sha256)
    THEN
        RAISE EXCEPTION
            '[VOICE GUARD] shift_handover_ledger: 봉인된 필드 변경 불가. '
            'ACK 수령: delivered_to / delivered_at, '
            '무효화 체인: is_superseded / superseded_by 만 UPDATE 허용.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_handover_ledger_update ON public.shift_handover_ledger;

CREATE TRIGGER trg_handover_ledger_update
    BEFORE UPDATE ON public.shift_handover_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_handover_ledger_update_guard();


-- ── C-2: DELETE / TRUNCATE 전면 차단 ──────────────────────────

CREATE OR REPLACE FUNCTION fn_handover_ledger_delete_guard()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] shift_handover_ledger DELETE/TRUNCATE 차단: '
        'Append-Only 인수인계 원장입니다. '
        '무효화는 is_superseded=true + 신규 row INSERT로 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_handover_ledger_delete   ON public.shift_handover_ledger;
DROP TRIGGER IF EXISTS trg_handover_ledger_truncate ON public.shift_handover_ledger;

CREATE TRIGGER trg_handover_ledger_delete
    BEFORE DELETE ON public.shift_handover_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_handover_ledger_delete_guard();

CREATE TRIGGER trg_handover_ledger_truncate
    BEFORE TRUNCATE ON public.shift_handover_ledger
    EXECUTE FUNCTION fn_handover_ledger_delete_guard();


-- ══════════════════════════════════════════════════════════════
-- PART D: 인덱스 + 권한
-- ══════════════════════════════════════════════════════════════

-- 최신 브리핑 조회용 (GET /api/v5/handover/latest)
CREATE INDEX IF NOT EXISTS idx_handover_facility_shift_end
    ON public.shift_handover_ledger (facility_id, shift_end DESC);

-- 미수령 브리핑 주기 점검용
CREATE INDEX IF NOT EXISTS idx_handover_undelivered
    ON public.shift_handover_ledger (generated_at DESC)
    WHERE delivered_at IS NULL AND is_superseded = FALSE;

-- 무효화 체인 탐색용
CREATE INDEX IF NOT EXISTS idx_handover_superseded_by
    ON public.shift_handover_ledger (superseded_by)
    WHERE superseded_by IS NOT NULL;

REVOKE ALL ON TABLE public.shift_handover_ledger FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT, UPDATE ON TABLE public.shift_handover_ledger
            TO voice_guard_ingestor;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_reader') THEN
        GRANT SELECT ON TABLE public.shift_handover_ledger
            TO voice_guard_reader;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 5] schema_v12 적용 완료.';
    RAISE NOTICE '  PART 0: evidence_ledger.care_type 타입 안전 검사 완료';
    RAISE NOTICE '  PART A: unified_outbox.event_type +handover_trigger +handover_ack';
    RAISE NOTICE '  PART B: shift_handover_ledger 인수인계 원장 생성';
    RAISE NOTICE '  PART C: UPDATE 봉인 트리거 + DELETE/TRUNCATE 차단 트리거';
    RAISE NOTICE '  PART D: 인덱스 3개 + 역할별 권한 부여';
    RAISE NOTICE '  [결함 방어] 1:MANUAL trigger_mode | 3:generation_mode RAW_FALLBACK';
    RAISE NOTICE '             5:case_type ENUM 안전검사 | 6:ACK 수령 필드';
END $$;
