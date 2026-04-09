-- ============================================================
-- Voice Guard — schema_v14_sharelink.sql
-- Phase 7: 자료 발송·수신 확인 인프라 (ShareLink Architecture)
--
-- [불변 원칙]
--   - Phase 1~6 기존 테이블 스키마 수정 0
--   - 3개 신규 원장: 각각 목적별 Append-Only 수준 분리
--
-- 구성:
--   PART A: material_dispatch        — 발송 원장 (완전 Append-Only)
--   PART B: material_dispatch_outbox — 발송 대기열 (상태 컬럼만 UPDATE 허용)
--   PART C: material_ack_ledger      — 수신 확인 원장 (WORM 해시체인)
--   PART D: 트리거 — UPDATE 봉인 / DELETE / TRUNCATE 차단
--   PART E: 인덱스 + 권한
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART A: material_dispatch — 발송 원장 (완전 Append-Only)
-- ══════════════════════════════════════════════════════════════
--
-- [설계 원칙]
--   - payload_hash: SHA-256(payload JSON canonical form) 서버 강제 생성
--   - UNIQUE NOT NULL → 동일 payload 중복 발송 원천 차단
--   - UPDATE / DELETE / TRUNCATE 트리거로 완전 봉인
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.material_dispatch (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 발송 원장 식별
    facility_id      TEXT         NOT NULL,
    worker_id        TEXT         NOT NULL,
    dispatch_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- 수신자 정보
    recipient_phone  TEXT         NOT NULL,
    recipient_name   TEXT,

    -- 발송 내용
    material_type    VARCHAR(50)  NOT NULL,          -- e.g. 'CARE_PLAN', 'HANDOVER_REPORT'
    material_ref_id  UUID,                           -- 참조 원장 UUID (옵션)
    payload_json     JSONB        NOT NULL,          -- 발송 payload 전체
    payload_hash     CHAR(64)     UNIQUE NOT NULL,   -- SHA-256 hex (서버 강제 생성)

    -- 채널
    channel          VARCHAR(20)  NOT NULL DEFAULT 'kakao'
                     CHECK (channel IN ('kakao', 'lms', 'sms')),

    -- ACK 링크 메타
    ack_token        TEXT         UNIQUE NOT NULL,   -- HMAC-SHA256 Signed Token (72h 만료)
    ack_expires_at   TIMESTAMPTZ  NOT NULL,          -- dispatch_at + 72시간

    dispatched_by    TEXT         NOT NULL           -- API 호출자 식별 (worker_id or system)
);

COMMENT ON TABLE public.material_dispatch IS
    'Voice Guard Phase 7 — 자료 발송 원장 (완전 Append-Only). '
    'payload_hash = SHA-256(payload_json canonical). '
    'ack_token = HMAC-SHA256 Signed Token (72시간 만료). '
    'UPDATE/DELETE/TRUNCATE 트리거로 완전 봉인.';

COMMENT ON COLUMN public.material_dispatch.payload_hash IS
    '서버 강제 생성: SHA-256(json.dumps(payload_json, sort_keys=True, ensure_ascii=False)). '
    'UNIQUE 제약으로 동일 payload 중복 발송 원천 차단.';

COMMENT ON COLUMN public.material_dispatch.ack_token IS
    'HMAC-SHA256(dispatch_id + expires_at, SECRET_KEY). '
    '72시간 만료. GET /ack/{token} — link_clicked, POST /ack/{token} — read_confirmed.';

COMMENT ON COLUMN public.material_dispatch.ack_expires_at IS
    'dispatch_at + 72시간. 이 시각 이후 ACK 토큰은 유효하지 않음.';


-- ══════════════════════════════════════════════════════════════
-- PART B: material_dispatch_outbox — 발송 대기열
-- ══════════════════════════════════════════════════════════════
--
-- [설계 원칙]
--   - dispatch_id FK → material_dispatch (발송 원장)
--   - 상태/재시도 컬럼만 UPDATE 허용 (identity 컬럼 봉인)
--   - status 전이: PENDING → SENDING → SENT | FAILED | DLQ
--   - channel_fallback: 1xxx 재시도 실패 후 DLQ, 3xxx → lms 즉각 전환
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.material_dispatch_outbox (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id       UUID         NOT NULL
                      REFERENCES public.material_dispatch(id),

    -- 현재 발송 채널 (폴백 후 변경 가능)
    channel           VARCHAR(20)  NOT NULL DEFAULT 'kakao'
                      CHECK (channel IN ('kakao', 'lms', 'sms')),

    -- 상태 관리 (UPDATE 허용)
    status            VARCHAR(20)  NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING','SENDING','SENT','FAILED','DLQ')),

    -- 재시도 카운터 (UPDATE 허용)
    attempt_count     INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- 에러 정보 (UPDATE 허용)
    last_error_code   VARCHAR(20),
    last_error_msg    TEXT,

    -- 타임스탬프
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.material_dispatch_outbox IS
    'Voice Guard Phase 7 — 발송 대기열. '
    'SELECT FOR UPDATE SKIP LOCKED (batch_size=50) 로 워커가 소비. '
    'status, attempt_count, next_attempt_at, last_error_* 컬럼만 UPDATE 허용. '
    '1xxx 에러: 재시도 3회 후 DLQ. 3xxx 에러: channel=lms 즉각 폴백.';

COMMENT ON COLUMN public.material_dispatch_outbox.status IS
    '상태 전이: PENDING → SENDING → SENT | FAILED | DLQ. '
    '워커 루프가 PENDING/FAILED를 next_attempt_at 기준으로 소비.';

COMMENT ON COLUMN public.material_dispatch_outbox.last_error_code IS
    'Solapi 에러 코드 (e.g. "1001", "3001"). '
    '1xxx: 일시적 오류(재시도 대상). 3xxx: 수신자 문제(lms 폴백 대상).';


-- ══════════════════════════════════════════════════════════════
-- PART C: material_ack_ledger — 수신 확인 원장 (WORM 해시체인)
-- ══════════════════════════════════════════════════════════════
--
-- [설계 원칙]
--   - 직전 행의 chain_hash를 참조하는 prev_hash 필수 구성
--   - chain_hash = SHA-256(dispatch_id || ack_type || acked_at || dwell_seconds || prev_hash)
--   - 포크 방지: pg_advisory_xact_lock(ack_chain_lock_id) 트랜잭션 락 필수
--   - 2-Stage: link_clicked(GET) → read_confirmed(POST + dwell_seconds)
--   - UPDATE / DELETE / TRUNCATE 완전 차단
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.material_ack_ledger (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id      UUID         NOT NULL
                     REFERENCES public.material_dispatch(id),

    -- ACK 단계
    ack_type         VARCHAR(20)  NOT NULL
                     CHECK (ack_type IN ('link_clicked', 'read_confirmed')),

    -- 수신자 증거
    acked_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ip_address       TEXT,
    user_agent       TEXT,

    -- read_confirmed 전용 (GET 시 NULL)
    dwell_seconds    INTEGER
                     CHECK (dwell_seconds IS NULL OR dwell_seconds >= 0),

    -- WORM 해시체인 (포크 방지)
    prev_hash        CHAR(64),    -- 직전 행의 chain_hash (체인 첫 행: NULL)
    chain_hash       CHAR(64)     NOT NULL  -- SHA-256 서버 강제 생성
);

COMMENT ON TABLE public.material_ack_ledger IS
    'Voice Guard Phase 7 — 수신 확인 원장 (WORM 해시체인). '
    'chain_hash = SHA-256(dispatch_id||ack_type||acked_at||dwell_seconds||prev_hash). '
    'INSERT 전 반드시 pg_advisory_xact_lock(ack_chain_lock_id) 획득. '
    '2-Stage: GET→link_clicked(dwell_seconds NULL), POST→read_confirmed(dwell_seconds 필수). '
    'UPDATE/DELETE/TRUNCATE 완전 차단.';

COMMENT ON COLUMN public.material_ack_ledger.prev_hash IS
    '직전 행(동일 dispatch_id 기준 최신)의 chain_hash. '
    '체인 첫 행은 NULL. 포크 방지는 API 레벨 pg_advisory_xact_lock으로 보장.';

COMMENT ON COLUMN public.material_ack_ledger.chain_hash IS
    '서버 강제 생성: SHA-256(dispatch_id||ack_type||acked_at_iso||dwell_seconds||prev_hash). '
    'prev_hash가 NULL인 경우 빈 문자열로 대체하여 해시.';

COMMENT ON COLUMN public.material_ack_ledger.dwell_seconds IS
    'read_confirmed 전용 필드. POST /ack/{token} 에서 필수. '
    'GET link_clicked 시 NULL. 0 이상 정수만 허용.';


-- ══════════════════════════════════════════════════════════════
-- PART D: 트리거 — UPDATE 봉인 / DELETE / TRUNCATE 차단
-- ══════════════════════════════════════════════════════════════

-- ── D-1: material_dispatch — 완전 Append-Only ─────────────────

CREATE OR REPLACE FUNCTION fn_material_dispatch_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            '[VOICE GUARD P7] material_dispatch: UPDATE 차단. '
            '발송 원장은 Append-Only입니다.';
    END IF;
    RAISE EXCEPTION
        '[VOICE GUARD P7] material_dispatch: DELETE/TRUNCATE 차단. '
        '발송 원장은 Append-Only입니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_dispatch_update   ON public.material_dispatch;
DROP TRIGGER IF EXISTS trg_dispatch_delete   ON public.material_dispatch;
DROP TRIGGER IF EXISTS trg_dispatch_truncate ON public.material_dispatch;

CREATE TRIGGER trg_dispatch_update
    BEFORE UPDATE ON public.material_dispatch
    FOR EACH ROW EXECUTE FUNCTION fn_material_dispatch_block_mutation();

CREATE TRIGGER trg_dispatch_delete
    BEFORE DELETE ON public.material_dispatch
    FOR EACH ROW EXECUTE FUNCTION fn_material_dispatch_block_mutation();

CREATE TRIGGER trg_dispatch_truncate
    BEFORE TRUNCATE ON public.material_dispatch
    EXECUTE FUNCTION fn_material_dispatch_block_mutation();


-- ── D-2: material_dispatch_outbox — 상태 컬럼만 UPDATE 허용 ───

CREATE OR REPLACE FUNCTION fn_outbox_update_guard()
RETURNS TRIGGER AS $$
BEGIN
    -- 구조 식별자 봉인 (변경 불가)
    IF (OLD.id          IS DISTINCT FROM NEW.id)          OR
       (OLD.dispatch_id IS DISTINCT FROM NEW.dispatch_id) OR
       (OLD.created_at  IS DISTINCT FROM NEW.created_at)
    THEN
        RAISE EXCEPTION
            '[VOICE GUARD P7] material_dispatch_outbox: '
            '구조 식별자 변경 차단 (id / dispatch_id / created_at).';
    END IF;
    -- updated_at 자동 갱신
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

CREATE OR REPLACE FUNCTION fn_outbox_delete_guard()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD P7] material_dispatch_outbox: DELETE/TRUNCATE 차단. '
        '처리 완료 행은 status=SENT|DLQ로 유지합니다.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_outbox_update   ON public.material_dispatch_outbox;
DROP TRIGGER IF EXISTS trg_outbox_delete   ON public.material_dispatch_outbox;
DROP TRIGGER IF EXISTS trg_outbox_truncate ON public.material_dispatch_outbox;

CREATE TRIGGER trg_outbox_update
    BEFORE UPDATE ON public.material_dispatch_outbox
    FOR EACH ROW EXECUTE FUNCTION fn_outbox_update_guard();

CREATE TRIGGER trg_outbox_delete
    BEFORE DELETE ON public.material_dispatch_outbox
    FOR EACH ROW EXECUTE FUNCTION fn_outbox_delete_guard();

CREATE TRIGGER trg_outbox_truncate
    BEFORE TRUNCATE ON public.material_dispatch_outbox
    EXECUTE FUNCTION fn_outbox_delete_guard();


-- ── D-3: material_ack_ledger — 완전 Append-Only ───────────────

CREATE OR REPLACE FUNCTION fn_ack_ledger_p7_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            '[VOICE GUARD P7] material_ack_ledger: UPDATE 차단. '
            'WORM 해시체인 원장 — 수신 확인 기록 변경 불가.';
    END IF;
    RAISE EXCEPTION
        '[VOICE GUARD P7] material_ack_ledger: DELETE/TRUNCATE 차단. '
        '수신 확인은 법적 증거 보존 원칙 적용.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_ack_p7_update   ON public.material_ack_ledger;
DROP TRIGGER IF EXISTS trg_ack_p7_delete   ON public.material_ack_ledger;
DROP TRIGGER IF EXISTS trg_ack_p7_truncate ON public.material_ack_ledger;

CREATE TRIGGER trg_ack_p7_update
    BEFORE UPDATE ON public.material_ack_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_ack_ledger_p7_block_mutation();

CREATE TRIGGER trg_ack_p7_delete
    BEFORE DELETE ON public.material_ack_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_ack_ledger_p7_block_mutation();

CREATE TRIGGER trg_ack_p7_truncate
    BEFORE TRUNCATE ON public.material_ack_ledger
    EXECUTE FUNCTION fn_ack_ledger_p7_block_mutation();


-- ══════════════════════════════════════════════════════════════
-- PART E: 인덱스 + 권한
-- ══════════════════════════════════════════════════════════════

-- material_dispatch
CREATE INDEX IF NOT EXISTS idx_dispatch_facility_at
    ON public.material_dispatch (facility_id, dispatch_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispatch_payload_hash
    ON public.material_dispatch (payload_hash);

CREATE INDEX IF NOT EXISTS idx_dispatch_ack_token
    ON public.material_dispatch (ack_token);

CREATE INDEX IF NOT EXISTS idx_dispatch_ack_expires
    ON public.material_dispatch (ack_expires_at)
    WHERE ack_expires_at > NOW();

-- material_dispatch_outbox
CREATE INDEX IF NOT EXISTS idx_outbox_status_next
    ON public.material_dispatch_outbox (status, next_attempt_at)
    WHERE status IN ('PENDING', 'FAILED');

CREATE INDEX IF NOT EXISTS idx_outbox_dispatch_id
    ON public.material_dispatch_outbox (dispatch_id);

-- material_ack_ledger
CREATE INDEX IF NOT EXISTS idx_ack_p7_dispatch_id
    ON public.material_ack_ledger (dispatch_id, acked_at DESC);

CREATE INDEX IF NOT EXISTS idx_ack_p7_dispatch_type
    ON public.material_ack_ledger (dispatch_id, ack_type);

-- 권한
REVOKE ALL ON TABLE public.material_dispatch         FROM PUBLIC;
REVOKE ALL ON TABLE public.material_dispatch_outbox  FROM PUBLIC;
REVOKE ALL ON TABLE public.material_ack_ledger       FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT ON TABLE public.material_dispatch        TO voice_guard_ingestor;
        GRANT INSERT, SELECT, UPDATE ON TABLE public.material_dispatch_outbox TO voice_guard_ingestor;
        GRANT INSERT, SELECT ON TABLE public.material_ack_ledger      TO voice_guard_ingestor;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_reader') THEN
        GRANT SELECT ON TABLE public.material_dispatch         TO voice_guard_reader;
        GRANT SELECT ON TABLE public.material_dispatch_outbox  TO voice_guard_reader;
        GRANT SELECT ON TABLE public.material_ack_ledger       TO voice_guard_reader;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 7] schema_v14_sharelink 적용 완료.';
    RAISE NOTICE '  PART A: material_dispatch (payload_hash UNIQUE, ack_token UNIQUE, 완전 Append-Only)';
    RAISE NOTICE '  PART B: material_dispatch_outbox (상태/재시도 컬럼 UPDATE 허용, 구조 식별자 봉인)';
    RAISE NOTICE '  PART C: material_ack_ledger (prev_hash + chain_hash WORM 해시체인, 완전 Append-Only)';
    RAISE NOTICE '  PART D: 트리거 9개 (UPDATE 봉인 + DELETE/TRUNCATE 차단)';
    RAISE NOTICE '  PART E: 인덱스 8개 + 역할 권한';
    RAISE NOTICE '  [방어 체계] payload_hash: SHA-256(canonical JSON) 중복 발송 차단';
    RAISE NOTICE '             ack_token: HMAC-SHA256 72시간 만료 서명 토큰';
    RAISE NOTICE '             WORM 체인: prev_hash → chain_hash 포크 방지';
    RAISE NOTICE '             포크 방지: API 레벨 pg_advisory_xact_lock 필수';
END $$;
