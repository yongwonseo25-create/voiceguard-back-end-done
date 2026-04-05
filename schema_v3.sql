-- ============================================================
-- Voice Guard — schema_v3.sql
-- Phase 2: 카카오 알림톡 발송 이력 테이블
--
-- notification_log:
--   - 알림톡 발송 감사 추적 (언제·누구에게·어떤 건을 알렸는가)
--   - 중복 발송 방지 키 역할 (ledger_id + trigger_type + status)
--   - INSERT-only WORM 보존 (DELETE 차단 트리거 적용)
-- ============================================================

-- ── notification_log 테이블 생성 ─────────────────────────────
CREATE TABLE IF NOT EXISTS public.notification_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id       UUID        REFERENCES public.evidence_ledger(id),
    trigger_type    VARCHAR(10) NOT NULL
                    CHECK (trigger_type IN ('NT-1', 'NT-2', 'NT-3')),
    recipient_phone VARCHAR(20) NOT NULL,
    template_code   VARCHAR(50) NOT NULL,
    status          VARCHAR(10) NOT NULL
                    CHECK (status IN ('sent', 'failed')),
    error_msg       TEXT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 인덱스 ────────────────────────────────────────────────────
-- 중복 방지 조회 최적화 (ledger_id + trigger_type + status)
CREATE INDEX IF NOT EXISTS idx_notif_dedup
    ON public.notification_log (ledger_id, trigger_type, status);

-- 시계열 조회 최적화 (대시보드 발송 이력 뷰)
CREATE INDEX IF NOT EXISTS idx_notif_sent_at
    ON public.notification_log (sent_at DESC);

-- ── 권한 ──────────────────────────────────────────────────────
REVOKE ALL ON TABLE public.notification_log FROM PUBLIC;
GRANT INSERT, SELECT ON TABLE public.notification_log TO voice_guard_ingestor;

-- ── DELETE/TRUNCATE 차단 트리거 (DLQ와 동일 패턴) ────────────
--
-- 알림 발송 이력은 "언제 누구에게 알렸는가"의 법적 증거.
-- 환수 분쟁 시 삭제·변조 시도 차단.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_block_notif_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '[VOICE GUARD] notification_log 삭제 차단: '
        '알림 발송 이력은 증거 원장 보존 정책에 의해 DELETE/TRUNCATE 금지.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER;

ALTER FUNCTION fn_block_notif_delete() OWNER TO voice_guard_ingestor;
REVOKE ALL ON FUNCTION fn_block_notif_delete() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_notif_block_delete   ON public.notification_log;
DROP TRIGGER IF EXISTS trg_notif_block_truncate ON public.notification_log;

CREATE TRIGGER trg_notif_block_delete
    BEFORE DELETE ON public.notification_log
    FOR EACH ROW EXECUTE FUNCTION fn_block_notif_delete();

CREATE TRIGGER trg_notif_block_truncate
    BEFORE TRUNCATE ON public.notification_log
    EXECUTE FUNCTION fn_block_notif_delete();

-- ── 완료 알림 ─────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE '[Voice Guard Phase 2] schema_v3 적용 완료.';
    RAISE NOTICE '  ✅ notification_log — 알림톡 발송 이력 테이블 생성';
    RAISE NOTICE '  ✅ idx_notif_dedup  — 중복 발송 방지 인덱스';
    RAISE NOTICE '  ✅ idx_notif_sent_at — 시계열 조회 인덱스';
    RAISE NOTICE '  ✅ fn_block_notif_delete — DELETE/TRUNCATE 차단 트리거 적용';
END $$;
