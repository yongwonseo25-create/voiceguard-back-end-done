-- ============================================================
-- Voice Guard — schema_v13_phase6.sql
-- Phase 6: 인수인계 4대 방어 로직 (Handover Defense Architecture)
--
-- [불변 원칙]
--   - Phase 1~5 기존 테이블 스키마 수정 0
--   - 3개 신규 원장: Append-Only + 트리거 봉인
--
-- 구성:
--   PART A: handover_utterance_ledger (수시 발화 기록, idempotency_key UNIQUE)
--   PART B: handover_report_ledger    (보고서 원장, gemini_json 등 봉인)
--   PART C: handover_ack_ledger       (법적 ACK 원장, 완전 Append-Only)
--   PART D: 트리거 — UPDATE 봉인 / DELETE / TRUNCATE 차단
--   PART E: 인덱스 + 권한
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART A: handover_utterance_ledger — 수시 발화 기록 원장
-- ══════════════════════════════════════════════════════════════
--
-- [설계 원칙]
--   - 클라이언트가 보내는 키를 절대 신뢰하지 않음
--   - 서버가 sha256(worker_id || shift_date || device_id || recorded_at_utc) 결정론적 생성
--   - UNIQUE NOT NULL → 재시도 멱등성 보장, 중복 INSERT 원천 차단
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.handover_utterance_ledger (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 서버 결정론적 멱등성 키 (클라이언트 제출 값 무시)
    idempotency_key  CHAR(64)     UNIQUE NOT NULL,   -- sha256 hex (64자)

    facility_id      TEXT         NOT NULL,
    worker_id        TEXT         NOT NULL,
    shift_date       DATE         NOT NULL,           -- 근무 날짜 (KST 기준)
    device_id        TEXT         NOT NULL,

    recorded_at      TIMESTAMPTZ  NOT NULL,           -- 발화 시각 (결함 2: recorded_at 고정)
    ingested_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    transcript_text  TEXT,
    audio_sha256     CHAR(64),
    care_type        VARCHAR(100) NOT NULL DEFAULT 'HANDOVER',
    beneficiary_id   TEXT
);

COMMENT ON TABLE public.handover_utterance_ledger IS
    'Voice Guard Phase 6 — 수시 발화 기록 원장 (Append-Only). '
    'idempotency_key = sha256(worker_id||shift_date||device_id||recorded_at_utc). '
    '클라이언트 키 무시, 서버 강제 생성으로 중복 차단.';

COMMENT ON COLUMN public.handover_utterance_ledger.idempotency_key IS
    '서버 결정론적 멱등성 키: sha256(worker_id||shift_date||device_id||recorded_at_utc). '
    '클라이언트가 보낸 키는 무시한다.';

COMMENT ON COLUMN public.handover_utterance_ledger.recorded_at IS
    '실제 발화 시각. ingested_at(수신 시각)이 아닌 이 컬럼 기준으로 집계한다. '
    '결함 2 방어: 오프라인 지연 업로드 시 ingested_at > shift_end 가 되어도 recorded_at은 정확.';


-- ══════════════════════════════════════════════════════════════
-- PART B: handover_report_ledger — 인수인계 보고서 원장
-- ══════════════════════════════════════════════════════════════
--
-- [봉인 필드 — 트리거로 일단 기록 후 변경 차단]
--   gemini_json, raw_fallback, notion_snapshot, notion_snapshot_sha256
--
-- [조건부 UPDATE 허용]
--   status: 상태 전이 (PENDING → COMPILING → DONE/FAILED/EXPIRED)
--   notion_page_id: Notion 동기화 완료 후 기록
--   gemini_failed, tamper_detected: 장애/위변조 플래그
--
-- [expires_at: trigger_at + 30분 — 무한 PENDING 고착 방지]
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.handover_report_ledger (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id      TEXT         NOT NULL,
    worker_id        TEXT         NOT NULL,
    shift_date       DATE         NOT NULL,

    -- 서버 결정론적 멱등성 키 (결함: 클라이언트 키 무시)
    idempotency_key  CHAR(64)     UNIQUE NOT NULL,   -- sha256(worker_id||shift_date)

    trigger_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- 무한 PENDING 고착 방지 (결함 방어: trigger_at + 30분 자동 만료)
    expires_at       TIMESTAMPTZ  NOT NULL
                     GENERATED ALWAYS AS (trigger_at + INTERVAL '30 minutes') STORED,

    status           VARCHAR(20)  NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','COMPILING','DONE','FAILED','EXPIRED')),

    -- ── 봉인 필드 (NULL → 값 SET은 허용, 이후 변경 차단) ────
    gemini_json              JSONB,           -- Gemini 구조화 JSON 응답
    raw_fallback             TEXT,            -- Gemini 장애 시 원문 폴백
    notion_snapshot          JSONB,           -- Notion 전송 당시 스냅샷
    notion_snapshot_sha256   CHAR(64),        -- 스냅샷 무결성 해시

    -- ── 허용 UPDATE 필드 ────────────────────────────────────
    notion_page_id   TEXT,                   -- Notion 동기화 완료 후 기록
    gemini_failed    BOOLEAN      NOT NULL DEFAULT FALSE,  -- Gemini 장애 플래그
    tamper_detected  BOOLEAN      NOT NULL DEFAULT FALSE   -- 위변조 감지 플래그
);

COMMENT ON TABLE public.handover_report_ledger IS
    'Voice Guard Phase 6 — 인수인계 보고서 원장. '
    'gemini_json / raw_fallback / notion_snapshot / notion_snapshot_sha256 는 '
    '최초 기록 후 트리거로 변경 차단. status 및 플래그 컬럼만 UPDATE 허용.';

COMMENT ON COLUMN public.handover_report_ledger.idempotency_key IS
    '서버 결정론적 멱등성 키: sha256(worker_id||shift_date). '
    'POST /api/v6/handover/trigger 에서 클라이언트 키를 무시하고 서버가 생성.';

COMMENT ON COLUMN public.handover_report_ledger.expires_at IS
    'trigger_at + 30분 자동 만료. 무한 PENDING 고착을 방지하는 물리적 TTL.';

COMMENT ON COLUMN public.handover_report_ledger.gemini_json IS
    '봉인 필드: Gemini API JSON 스키마 강제 응답. 최초 SET 후 변경 차단.';

COMMENT ON COLUMN public.handover_report_ledger.notion_snapshot IS
    '봉인 필드: Notion 전송 당시 스냅샷. ACK 시 재조회 값과 비교하여 위변조 감지.';


-- ══════════════════════════════════════════════════════════════
-- PART C: handover_ack_ledger — 법적 수신 확인 원장 (완전 Append-Only)
-- ══════════════════════════════════════════════════════════════
--
-- [설계 원칙]
--   - 다음 근무자의 법적 수신 확인 기록
--   - device_id + ack_at 기록 — UPDATE/DELETE 완전 차단
--   - ACK 시 Notion 재조회 → 스냅샷 해시 비교 → tamper_detected 플래그
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.handover_ack_ledger (
    id                     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

    report_id              UUID         NOT NULL
                           REFERENCES public.handover_report_ledger(id),

    -- 수신자 식별 (법적 증거)
    device_id              TEXT         NOT NULL,
    ack_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ip_address             TEXT,

    -- ACK 시점 무결성 검증 결과
    tamper_detected        BOOLEAN      NOT NULL DEFAULT FALSE,
    snapshot_sha256_at_ack CHAR(64)              -- ACK 시점 Notion 재조회 해시
);

COMMENT ON TABLE public.handover_ack_ledger IS
    'Voice Guard Phase 6 — 법적 수신 확인 원장 (완전 Append-Only). '
    'device_id, ack_at 기록 후 UPDATE/DELETE 트리거로 완전 차단. '
    'ACK 시 Notion 재조회 해시 비교 → tamper_detected 법적 무결성 증명.';

COMMENT ON COLUMN public.handover_ack_ledger.tamper_detected IS
    'ACK 시점 Notion 재조회 sha256 ≠ notion_snapshot_sha256(전송 당시) → TRUE. '
    '위변조 감지 시 PATCH /api/v6/handover/{id}/ack 가 tamper_detected:true 플래그 반환.';


-- ══════════════════════════════════════════════════════════════
-- PART D: 트리거 — UPDATE 봉인 / DELETE / TRUNCATE 차단
-- ══════════════════════════════════════════════════════════════

-- ── D-1: handover_utterance_ledger — 완전 Append-Only ────────

CREATE OR REPLACE FUNCTION fn_utterance_ledger_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_utterance_ledger: UPDATE 차단. '
            'Append-Only 원장입니다.';
    END IF;
    RAISE EXCEPTION
        '[VOICE GUARD P6] handover_utterance_ledger: DELETE/TRUNCATE 차단. '
        'Append-Only 원장입니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_utterance_update   ON public.handover_utterance_ledger;
DROP TRIGGER IF EXISTS trg_utterance_delete   ON public.handover_utterance_ledger;
DROP TRIGGER IF EXISTS trg_utterance_truncate ON public.handover_utterance_ledger;

CREATE TRIGGER trg_utterance_update
    BEFORE UPDATE ON public.handover_utterance_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_utterance_ledger_block_mutation();

CREATE TRIGGER trg_utterance_delete
    BEFORE DELETE ON public.handover_utterance_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_utterance_ledger_block_mutation();

CREATE TRIGGER trg_utterance_truncate
    BEFORE TRUNCATE ON public.handover_utterance_ledger
    EXECUTE FUNCTION fn_utterance_ledger_block_mutation();


-- ── D-2: handover_report_ledger — 봉인 필드 UPDATE 차단 ──────
--
-- 허용: status, notion_page_id, gemini_failed, tamper_detected
-- 차단: gemini_json, raw_fallback, notion_snapshot, notion_snapshot_sha256
--       id, facility_id, worker_id, shift_date, idempotency_key,
--       trigger_at, expires_at (GENERATED)

CREATE OR REPLACE FUNCTION fn_report_ledger_update_guard()
RETURNS TRIGGER AS $$
BEGIN
    -- 구조 식별자 봉인
    IF (OLD.id               IS DISTINCT FROM NEW.id)              OR
       (OLD.facility_id      IS DISTINCT FROM NEW.facility_id)     OR
       (OLD.worker_id        IS DISTINCT FROM NEW.worker_id)       OR
       (OLD.shift_date       IS DISTINCT FROM NEW.shift_date)      OR
       (OLD.idempotency_key  IS DISTINCT FROM NEW.idempotency_key) OR
       (OLD.trigger_at       IS DISTINCT FROM NEW.trigger_at)
    THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_report_ledger: 구조 식별자 변경 차단. '
            '(id / facility_id / worker_id / shift_date / idempotency_key / trigger_at)';
    END IF;

    -- 콘텐츠 봉인: NULL → 값 SET은 허용, 이후 변경 차단
    IF OLD.gemini_json IS NOT NULL AND
       OLD.gemini_json IS DISTINCT FROM NEW.gemini_json THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_report_ledger.gemini_json: '
            '최초 기록 후 변경 차단 (Immutable after first write).';
    END IF;

    IF OLD.raw_fallback IS NOT NULL AND
       OLD.raw_fallback IS DISTINCT FROM NEW.raw_fallback THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_report_ledger.raw_fallback: '
            '최초 기록 후 변경 차단.';
    END IF;

    IF OLD.notion_snapshot IS NOT NULL AND
       OLD.notion_snapshot IS DISTINCT FROM NEW.notion_snapshot THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_report_ledger.notion_snapshot: '
            '최초 기록 후 변경 차단. 위변조 감지는 tamper_detected 플래그를 사용하십시오.';
    END IF;

    IF OLD.notion_snapshot_sha256 IS NOT NULL AND
       OLD.notion_snapshot_sha256 IS DISTINCT FROM NEW.notion_snapshot_sha256 THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_report_ledger.notion_snapshot_sha256: '
            '최초 기록 후 변경 차단.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_report_ledger_update   ON public.handover_report_ledger;
DROP TRIGGER IF EXISTS trg_report_ledger_delete   ON public.handover_report_ledger;
DROP TRIGGER IF EXISTS trg_report_ledger_truncate ON public.handover_report_ledger;

CREATE TRIGGER trg_report_ledger_update
    BEFORE UPDATE ON public.handover_report_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_report_ledger_update_guard();

CREATE OR REPLACE FUNCTION fn_report_ledger_delete_guard()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD P6] handover_report_ledger: DELETE/TRUNCATE 차단. '
        '만료는 status=EXPIRED + expires_at TTL로 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

CREATE TRIGGER trg_report_ledger_delete
    BEFORE DELETE ON public.handover_report_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_report_ledger_delete_guard();

CREATE TRIGGER trg_report_ledger_truncate
    BEFORE TRUNCATE ON public.handover_report_ledger
    EXECUTE FUNCTION fn_report_ledger_delete_guard();


-- ── D-3: handover_ack_ledger — 완전 Append-Only ──────────────

CREATE OR REPLACE FUNCTION fn_ack_ledger_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            '[VOICE GUARD P6] handover_ack_ledger: UPDATE 차단. '
            '법적 수신 확인 원장 — 일단 기록된 ACK는 변경 불가.';
    END IF;
    RAISE EXCEPTION
        '[VOICE GUARD P6] handover_ack_ledger: DELETE/TRUNCATE 차단. '
        '법적 증거 보존 원칙.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_ack_update   ON public.handover_ack_ledger;
DROP TRIGGER IF EXISTS trg_ack_delete   ON public.handover_ack_ledger;
DROP TRIGGER IF EXISTS trg_ack_truncate ON public.handover_ack_ledger;

CREATE TRIGGER trg_ack_update
    BEFORE UPDATE ON public.handover_ack_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_ack_ledger_block_mutation();

CREATE TRIGGER trg_ack_delete
    BEFORE DELETE ON public.handover_ack_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_ack_ledger_block_mutation();

CREATE TRIGGER trg_ack_truncate
    BEFORE TRUNCATE ON public.handover_ack_ledger
    EXECUTE FUNCTION fn_ack_ledger_block_mutation();


-- ══════════════════════════════════════════════════════════════
-- PART E: 인덱스 + 권한
-- ══════════════════════════════════════════════════════════════

-- handover_utterance_ledger
CREATE INDEX IF NOT EXISTS idx_utterance_facility_shift
    ON public.handover_utterance_ledger (facility_id, shift_date, recorded_at);

CREATE INDEX IF NOT EXISTS idx_utterance_worker_shift
    ON public.handover_utterance_ledger (worker_id, shift_date);

-- handover_report_ledger
CREATE INDEX IF NOT EXISTS idx_report_facility_shift
    ON public.handover_report_ledger (facility_id, shift_date DESC);

CREATE INDEX IF NOT EXISTS idx_report_idempotency
    ON public.handover_report_ledger (idempotency_key);

-- 만료 점검: status=PENDING + expires_at < NOW()
CREATE INDEX IF NOT EXISTS idx_report_pending_expired
    ON public.handover_report_ledger (expires_at)
    WHERE status = 'PENDING';

-- handover_ack_ledger
CREATE INDEX IF NOT EXISTS idx_ack_report_id
    ON public.handover_ack_ledger (report_id, ack_at DESC);

CREATE INDEX IF NOT EXISTS idx_ack_tamper
    ON public.handover_ack_ledger (report_id)
    WHERE tamper_detected = TRUE;

-- 권한
REVOKE ALL ON TABLE public.handover_utterance_ledger FROM PUBLIC;
REVOKE ALL ON TABLE public.handover_report_ledger    FROM PUBLIC;
REVOKE ALL ON TABLE public.handover_ack_ledger       FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT ON TABLE public.handover_utterance_ledger TO voice_guard_ingestor;
        GRANT INSERT, SELECT, UPDATE ON TABLE public.handover_report_ledger TO voice_guard_ingestor;
        GRANT INSERT, SELECT ON TABLE public.handover_ack_ledger TO voice_guard_ingestor;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_reader') THEN
        GRANT SELECT ON TABLE public.handover_utterance_ledger TO voice_guard_reader;
        GRANT SELECT ON TABLE public.handover_report_ledger    TO voice_guard_reader;
        GRANT SELECT ON TABLE public.handover_ack_ledger       TO voice_guard_reader;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 6] schema_v13 적용 완료.';
    RAISE NOTICE '  PART A: handover_utterance_ledger (idempotency_key UNIQUE NOT NULL)';
    RAISE NOTICE '  PART B: handover_report_ledger (gemini_json/raw_fallback/notion_snapshot 봉인 + expires_at 30분)';
    RAISE NOTICE '  PART C: handover_ack_ledger (법적 ACK 원장, 완전 Append-Only)';
    RAISE NOTICE '  PART D: 트리거 9개 (UPDATE 봉인 + DELETE/TRUNCATE 차단)';
    RAISE NOTICE '  PART E: 인덱스 7개 + 역할 권한';
    RAISE NOTICE '  [방어 체계] 멱등성 키: sha256(worker_id||shift_date)';
    RAISE NOTICE '             expires_at: trigger_at + 30분 (무한 PENDING 고착 차단)';
    RAISE NOTICE '             tamper_detected: ACK 시 Notion 재조회 해시 비교';
END $$;
