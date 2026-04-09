-- ============================================================
-- Voice Guard — schema_v11_phase4.sql
-- Phase 4: Unified Outbox + Event Router 단일 심장
--
-- [불변 원칙]
--   - 기존 outbox_events / notion_sync_outbox 테이블 수정 0
--   - unified_outbox: DELETE/TRUNCATE 원천 차단 트리거
--   - 상태 변경 = Append-Only 보상 행 INSERT (UPDATE 0)
--
-- 구성:
--   PART A: unified_outbox (단일 이벤트 원장, Event Sourcing 패턴)
--   PART B: v_unified_outbox_current (현재 상태 뷰)
--   PART C: worker_throughput_log (핸들러별 처리량 원장)
--   PART D: 인덱스 + 권한
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- PART A: unified_outbox — 단일 이벤트 원장 (Append-Only)
-- ══════════════════════════════════════════════════════════════
--
-- [Event Sourcing 패턴]
--   row_id:   이 물리 행의 고유 ID (PK, immutable)
--   event_id: 논리적 이벤트 단위 (여러 row가 동일 event_id 공유)
--
-- 상태 머신:
--   최초 발행 → INSERT(status=PENDING)
--   워커 수령 → INSERT(status=PROCESSING) ← UPDATE 아님!
--   처리 완료 → INSERT(status=DONE)
--   처리 실패 → INSERT(status=FAILED)
--
-- 현재 상태 = event_id별 가장 최근 row의 status
-- (v_unified_outbox_current 뷰로 조회)
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.unified_outbox (
    row_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      UUID         NOT NULL,             -- 논리 이벤트 식별자
    event_type    VARCHAR(30)  NOT NULL
                  CHECK (event_type IN (
                      'ingest', 'notion', 'command',
                      'access', 'reconcile', 'alert'
                  )),
    status        VARCHAR(20)  NOT NULL
                  CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    payload       JSONB        NOT NULL,
    attempt_num   INTEGER      NOT NULL DEFAULT 0,   -- 이 행이 몇 번째 시도인가
    worker_id     VARCHAR(100),                       -- 처리한 워커 프로세스 ID
    error_message TEXT,                               -- FAILED 시 오류 사유
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── Append-Only 강제: DELETE/TRUNCATE 트리거 ─────────────────

CREATE OR REPLACE FUNCTION fn_block_unified_outbox_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] unified_outbox DELETE/TRUNCATE 차단: '
        'Append-Only 이벤트 원장입니다. '
        '상태 변경은 새 row INSERT로만 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_unified_outbox_block_delete   ON public.unified_outbox;
DROP TRIGGER IF EXISTS trg_unified_outbox_block_truncate ON public.unified_outbox;

CREATE TRIGGER trg_unified_outbox_block_delete
    BEFORE DELETE ON public.unified_outbox
    FOR EACH ROW EXECUTE FUNCTION fn_block_unified_outbox_delete();

CREATE TRIGGER trg_unified_outbox_block_truncate
    BEFORE TRUNCATE ON public.unified_outbox
    EXECUTE FUNCTION fn_block_unified_outbox_delete();

-- ── UPDATE 전면 차단 트리거 ───────────────────────────────────

CREATE OR REPLACE FUNCTION fn_block_unified_outbox_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] unified_outbox UPDATE 차단: '
        'Append-Only 이벤트 원장입니다. '
        '상태 전이는 새 row INSERT(보상 트랜잭션)로만 처리하십시오.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_unified_outbox_block_update ON public.unified_outbox;

CREATE TRIGGER trg_unified_outbox_block_update
    BEFORE UPDATE ON public.unified_outbox
    FOR EACH ROW EXECUTE FUNCTION fn_block_unified_outbox_update();


-- ══════════════════════════════════════════════════════════════
-- PART B: v_unified_outbox_current — 이벤트별 현재 상태 뷰
-- ══════════════════════════════════════════════════════════════
--
-- Event Sourcing의 "현재 상태 프로젝션".
-- DISTINCT ON (event_id): event_id별 가장 최근 row만 반환.
-- 워커는 이 뷰를 기준으로 PENDING 이벤트를 선택.
-- ══════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW public.v_unified_outbox_current AS
SELECT DISTINCT ON (event_id)
    row_id,
    event_id,
    event_type,
    status,
    payload,
    attempt_num,
    worker_id,
    error_message,
    created_at
FROM public.unified_outbox
ORDER BY event_id, created_at DESC;


-- ══════════════════════════════════════════════════════════════
-- PART C: worker_throughput_log — 핸들러별 처리량 Append-Only 원장
-- ══════════════════════════════════════════════════════════════
--
-- 매 처리 완료/실패 시 1행 INSERT.
-- GET /api/v2/worker/health에서 집계 조회.
-- DELETE/TRUNCATE 차단.
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.worker_throughput_log (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    handler_name VARCHAR(50) NOT NULL,      -- 'IngestHandler', 'NotionHandler' 등
    event_id     UUID        NOT NULL,
    event_type   VARCHAR(30) NOT NULL,
    result       VARCHAR(20) NOT NULL
                 CHECK (result IN ('DONE', 'FAILED')),
    duration_ms  INTEGER,                   -- 처리 소요 시간
    worker_id    VARCHAR(100),
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_throughput_handler_time
    ON public.worker_throughput_log (handler_name, logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_throughput_recent
    ON public.worker_throughput_log (logged_at DESC);

CREATE OR REPLACE FUNCTION fn_block_throughput_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '[VOICE GUARD] worker_throughput_log DELETE/TRUNCATE 차단.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

DROP TRIGGER IF EXISTS trg_throughput_block_delete   ON public.worker_throughput_log;
DROP TRIGGER IF EXISTS trg_throughput_block_truncate ON public.worker_throughput_log;

CREATE TRIGGER trg_throughput_block_delete
    BEFORE DELETE ON public.worker_throughput_log
    FOR EACH ROW EXECUTE FUNCTION fn_block_throughput_delete();

CREATE TRIGGER trg_throughput_block_truncate
    BEFORE TRUNCATE ON public.worker_throughput_log
    EXECUTE FUNCTION fn_block_throughput_delete();


-- ══════════════════════════════════════════════════════════════
-- PART D: 인덱스 + 권한
-- ══════════════════════════════════════════════════════════════

-- unified_outbox 인덱스
CREATE INDEX IF NOT EXISTS idx_uo_event_id
    ON public.unified_outbox (event_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_uo_pending
    ON public.unified_outbox (event_type, created_at ASC)
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_uo_failed
    ON public.unified_outbox (created_at DESC)
    WHERE status = 'FAILED';

-- 권한
REVOKE ALL ON TABLE public.unified_outbox         FROM PUBLIC;
REVOKE ALL ON TABLE public.worker_throughput_log  FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_guard_ingestor') THEN
        GRANT INSERT, SELECT ON TABLE public.unified_outbox        TO voice_guard_ingestor;
        GRANT INSERT, SELECT ON TABLE public.worker_throughput_log TO voice_guard_ingestor;
        GRANT SELECT ON public.v_unified_outbox_current            TO voice_guard_ingestor;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 완료 알림
-- ══════════════════════════════════════════════════════════════

DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 4] schema_v11 적용 완료.';
    RAISE NOTICE '  unified_outbox: Append-Only 단일 이벤트 원장 (UPDATE/DELETE/TRUNCATE 차단)';
    RAISE NOTICE '  v_unified_outbox_current: event_id별 현재 상태 프로젝션 뷰';
    RAISE NOTICE '  worker_throughput_log: 핸들러별 처리량 Append-Only 원장';
    RAISE NOTICE '  기존 outbox_events / notion_sync_outbox 유지 (하위 호환)';
END $$;
