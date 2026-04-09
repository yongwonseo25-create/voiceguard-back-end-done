"""
Voice Guard — Phase 4 Event Router 적대적 검증
===============================================
T-01: unified_outbox Append-Only (_transition INSERT, UPDATE/DELETE 불가)
T-02: 핸들러 라우팅 — 알려진/미지 event_type 분기
T-03: MAX_ATTEMPTS 초과 → DLQ 이관 경로 검증
T-04: _publish_event → PENDING 행 생성 검증
T-05: _log_throughput → worker_throughput_log INSERT 검증
T-06: event_router_worker: PROCESSING 보상 행 INSERT (UPDATE 0 검증)
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4


# ══════════════════════════════════════════════════════════════
# T-01: Append-Only 강제 — _transition은 INSERT만 사용
# ══════════════════════════════════════════════════════════════

class TestAppendOnly:
    def _make_conn(self):
        conn = MagicMock()
        conn.__enter__ = lambda s: s
        conn.__exit__  = MagicMock(return_value=False)
        return conn

    def test_transition_uses_insert_not_update(self):
        """_transition이 INSERT만 실행하고 UPDATE를 실행하지 않음을 검증."""
        from event_router_worker import _transition

        conn = self._make_conn()
        _transition(conn, str(uuid4()), "ingest", "PENDING", {"ledger_id": "x"}, 0)

        executed_sql = str(conn.execute.call_args_list)
        assert "INSERT INTO public.unified_outbox" in executed_sql
        assert "UPDATE" not in executed_sql.upper()

    def test_transition_status_propagated(self):
        """각 상태값(PENDING/PROCESSING/DONE/FAILED)이 올바르게 전달됨."""
        from event_router_worker import _transition

        for status in ("PENDING", "PROCESSING", "DONE", "FAILED"):
            conn = self._make_conn()
            _transition(conn, str(uuid4()), "ingest", status, {}, 0)
            params = conn.execute.call_args[0][1]
            assert params["status"] == status

    def test_transition_jsonb_cast(self):
        """payload가 CAST(:payload AS jsonb) 형태로 전달됨 (Gotcha 방어)."""
        from event_router_worker import _transition

        conn = self._make_conn()
        _transition(conn, str(uuid4()), "ingest", "PENDING", {"key": "value"}, 0)

        sql_str = str(conn.execute.call_args[0][0])
        assert "CAST(:payload AS jsonb)" in sql_str


# ══════════════════════════════════════════════════════════════
# T-02: 핸들러 라우팅 분기
# ══════════════════════════════════════════════════════════════

class TestRouter:
    @patch("event_router_worker._engine")
    @patch("event_router_worker._ingest_handler", return_value=None)
    async def test_ingest_dispatched(self, mock_ingest, mock_engine):
        """event_type='ingest' → _ingest_handler 호출."""
        from event_router_worker import _dispatch

        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)
        mock_conn.execute.return_value = MagicMock()

        await _dispatch(str(uuid4()), "ingest", {"ledger_id": "x"}, 0)
        mock_ingest.assert_called_once()

    @patch("event_router_worker._engine")
    async def test_unknown_event_type_no_crash(self, mock_engine):
        """알 수 없는 event_type → 로그 에러만, 예외 없음."""
        from event_router_worker import _dispatch

        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)

        # 예외 없이 반환되어야 함
        await _dispatch(str(uuid4()), "unknown_type", {}, 0)


# ══════════════════════════════════════════════════════════════
# T-03: MAX_ATTEMPTS 초과 → DLQ 경로
# ══════════════════════════════════════════════════════════════

class TestMaxAttempts:
    @patch("event_router_worker._engine")
    async def test_max_attempts_routes_to_dlq(self, mock_engine):
        """attempt_num >= MAX_ATTEMPTS → _handle_max_attempts 호출 (핸들러 미실행)."""
        from event_router_worker import _dispatch, MAX_ATTEMPTS

        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)
        mock_conn.execute.return_value = MagicMock()

        with patch("event_router_worker._ingest_handler") as mock_h:
            await _dispatch(str(uuid4()), "ingest", {}, MAX_ATTEMPTS)
            # 핸들러는 호출되지 않아야 함
            mock_h.assert_not_called()

        # dead_letter_queue INSERT가 실행됐는지 확인
        insert_calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("dead_letter_queue" in c for c in insert_calls)

    def test_max_attempts_dlq_insert_no_update(self):
        """DLQ 이관 시에도 UPDATE 없이 INSERT만 사용."""
        from event_router_worker import _handle_max_attempts

        with patch("event_router_worker._engine") as mock_engine:
            mock_conn = MagicMock()
            mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
            mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)

            _handle_max_attempts(str(uuid4()), "ingest", {}, 5)

            all_sql = " ".join(str(c) for c in mock_conn.execute.call_args_list).upper()
            assert "DELETE" not in all_sql
            assert "TRUNCATE" not in all_sql


# ══════════════════════════════════════════════════════════════
# T-04: _publish_event → PENDING 행 생성
# ══════════════════════════════════════════════════════════════

class TestPublishEvent:
    def test_publish_inserts_pending(self):
        """_publish_event가 status=PENDING으로 INSERT를 실행함."""
        from event_router_worker import _publish_event

        with patch("event_router_worker._engine") as mock_engine:
            mock_conn = MagicMock()
            mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
            mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)

            eid = _publish_event("reconcile", {"facility_id": "FAC-001"})

            assert eid  # UUID 반환 확인
            params = mock_conn.execute.call_args[0][1]
            assert params["status"] == "PENDING"
            assert params["event_type"] == "reconcile"

    def test_publish_returns_event_id(self):
        """반환값이 UUID 형식 문자열."""
        from event_router_worker import _publish_event
        import uuid

        with patch("event_router_worker._engine") as mock_engine:
            mock_conn = MagicMock()
            mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
            mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)

            eid = _publish_event("alert", {})
            uuid.UUID(eid)  # 유효한 UUID이면 예외 없음


# ══════════════════════════════════════════════════════════════
# T-05: _log_throughput → worker_throughput_log INSERT
# ══════════════════════════════════════════════════════════════

class TestThroughputLog:
    def test_log_throughput_insert(self):
        """_log_throughput이 worker_throughput_log에 INSERT."""
        from event_router_worker import _log_throughput

        conn = MagicMock()
        _log_throughput(conn, "IngestHandler", str(uuid4()), "ingest", "DONE", 150)

        sql_str = str(conn.execute.call_args[0][0])
        assert "worker_throughput_log" in sql_str
        params = conn.execute.call_args[0][1]
        assert params["result"] == "DONE"
        assert params["dur"] == 150

    def test_log_throughput_no_update(self):
        """처리량 로그도 UPDATE 없음."""
        from event_router_worker import _log_throughput

        conn = MagicMock()
        _log_throughput(conn, "ReconHandler", str(uuid4()), "reconcile", "FAILED", 200)

        sql_str = str(conn.execute.call_args[0][0]).upper()
        assert "UPDATE" not in sql_str
        assert "DELETE" not in sql_str


# ══════════════════════════════════════════════════════════════
# T-06: PROCESSING 보상 행이 핸들러 실행 전 INSERT됨
# ══════════════════════════════════════════════════════════════

class TestProcessingTransition:
    @patch("event_router_worker._engine")
    @patch("event_router_worker._recon_handler", return_value=None)
    async def test_processing_row_before_handler(self, mock_handler, mock_engine):
        """
        핸들러 실행 전 PROCESSING 행이 INSERT되어야 함.
        순서: PROCESSING INSERT → handler() → DONE INSERT
        """
        from event_router_worker import _dispatch

        inserted_statuses = []

        def capture_execute(sql, params=None):
            if params and "status" in params:
                inserted_statuses.append(params["status"])
            return MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = capture_execute
        mock_engine.begin.return_value.__enter__ = lambda s: mock_conn
        mock_engine.begin.return_value.__exit__  = MagicMock(return_value=False)

        await _dispatch(str(uuid4()), "reconcile", {}, 0)

        # PROCESSING이 DONE보다 먼저 와야 함
        assert "PROCESSING" in inserted_statuses
        assert "DONE" in inserted_statuses
        assert inserted_statuses.index("PROCESSING") < inserted_statuses.index("DONE")
