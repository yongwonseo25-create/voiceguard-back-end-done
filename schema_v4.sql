-- ============================================================
-- Voice Guard — schema_v4.sql
-- Phase 3: 원장 하향식 지시(Director Command) 원장
--
-- director_command:
--   - 원장 지시 불변 원장 (INSERT-ONLY WORM)
--   - 수급자별 조치 명령의 법적 증거
--
-- command_outbox:
--   - 알림톡/푸시 발행용 트랜잭셔널 아웃박스
--   - 워커가 소비하여 실발송 처리 (트랜잭션 외부)
-- ============================================================

-- ── 1. command_outbox (아웃박스 큐) ──────────────────────────
--   director_command 보다 먼저 생성 (FK 방향: outbox → command)
CREATE TABLE IF NOT EXISTS public.command_outbox (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    command_id      UUID        NOT NULL,   -- director_command.id (FK 후 추가)
    event_type      VARCHAR(50) NOT NULL,
    payload         JSONB       NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'dlq')),
    attempts        INT         NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cmd_outbox_status
    ON public.command_outbox (status, created_at ASC)
    WHERE status IN ('pending', 'processing');

-- ── 2. director_command (지시 원장) ──────────────────────────
CREATE TABLE IF NOT EXISTS public.director_command (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    beneficiary_id  VARCHAR(100) NOT NULL,
    action          VARCHAR(50)  NOT NULL,
                    -- 'field_check'  : 현장 확인 요청
                    -- 'freeze'       : 급여 지급 동결
                    -- 'escalate'     : 상급 기관 에스컬레이션
                    -- 'memo_only'    : 메모 기록만
    reason          TEXT         NOT NULL,
    memo            TEXT,
    commanded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    commanded_by    VARCHAR(100)          -- 원장(관리자) 식별자
);

-- ── 3. FK: command_outbox.command_id → director_command.id ──
ALTER TABLE public.command_outbox
    ADD CONSTRAINT fk_cmd_outbox_command
    FOREIGN KEY (command_id)
    REFERENCES public.director_command(id)
    DEFERRABLE INITIALLY DEFERRED;

-- ── 4. 인덱스 ────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_dir_cmd_beneficiary
    ON public.director_command (beneficiary_id, commanded_at DESC);

-- ── 5. 권한 ──────────────────────────────────────────────────
REVOKE ALL ON TABLE public.director_command FROM PUBLIC;
REVOKE ALL ON TABLE public.command_outbox   FROM PUBLIC;
GRANT INSERT, SELECT ON TABLE public.director_command TO voice_guard_ingestor;
GRANT INSERT, SELECT, UPDATE ON TABLE public.command_outbox TO voice_guard_ingestor;

-- ── 6. director_command DELETE/TRUNCATE 차단 트리거 ──────────
CREATE OR REPLACE FUNCTION fn_block_cmd_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] director_command 삭제 차단: '
        '원장 지시 이력은 증거 보존 정책에 의해 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

ALTER FUNCTION fn_block_cmd_delete() OWNER TO voice_guard_ingestor;
REVOKE ALL ON FUNCTION fn_block_cmd_delete() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_cmd_block_delete   ON public.director_command;
DROP TRIGGER IF EXISTS trg_cmd_block_truncate ON public.director_command;

CREATE TRIGGER trg_cmd_block_delete
    BEFORE DELETE ON public.director_command
    FOR EACH ROW EXECUTE FUNCTION fn_block_cmd_delete();

CREATE TRIGGER trg_cmd_block_truncate
    BEFORE TRUNCATE ON public.director_command
    EXECUTE FUNCTION fn_block_cmd_delete();

-- ── 완료 알림 ─────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 3] schema_v4 적용 완료.';
    RAISE NOTICE '  ✅ director_command  — 원장 하향식 지시 불변 원장';
    RAISE NOTICE '  ✅ command_outbox    — 알림톡/푸시 트랜잭셔널 아웃박스';
    RAISE NOTICE '  ✅ fk_cmd_outbox_command — FK (DEFERRABLE) 연결';
    RAISE NOTICE '  ✅ fn_block_cmd_delete — DELETE/TRUNCATE 차단 트리거';
END $$;
