"""
Voice Guard — Phase 5 인수인계 엔진 적대적 검증 (test_phase5_handover.py)
=========================================================================

T-01: [결함 3] LLM 장애 → RAW_FALLBACK 자동 전환 + generation_mode 검증
T-02: [결함 2] 집계 쿼리가 recorded_at 사용, ingested_at 절대 불사용
T-03: [결함 1] trigger_mode=MANUAL 이 shift_handover_ledger 에 정확히 기록
T-04: [결함 5] UPDATE 봉인 트리거 함수 SQL — 봉인 필드 변경 시 EXCEPTION 발생
T-05: [결함 5] DELETE 트리거 함수 SQL 존재 확인 (Append-Only 강제)
T-06: [결함 6] ACK PATCH — delivered_to/delivered_at 만 UPDATE (봉인 필드 미변경)
T-07: [결함 2] _build_raw_fallback_brief — recorded_at 오름차순 정렬 + 타임스탬프 포함
T-08: [결함 4] handover_handler 가 집계 전 반드시 REFRESH 를 먼저 실행
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ════════════════════════════════════════════════════════════════
# T-01: LLM 장애 → RAW_FALLBACK 자동 전환 (결함 3 방어)
# ════════════════════════════════════════════════════════════════

class TestLlmFallback:
    """_call_llm 이 예외를 던지면 generation_mode='RAW_FALLBACK' 으로 전환됨을 검증."""

    @pytest.mark.asyncio
    @patch("handover_handler._engine")
    @patch("handover_handler._call_llm", new_callable=AsyncMock)
    @patch("handover_handler._upload_tts_to_b2", new_callable=AsyncMock)
    async def test_llm_timeout_triggers_raw_fallback(
        self, mock_tts, mock_llm, mock_engine
    ):
        """LLM 타임아웃 → generation_mode=RAW_FALLBACK, brief_text 에 '[AI 요약 불가]' 포함."""
        import httpx
        from handover_handler import handover_handler

        # LLM: 타임아웃 예외
        mock_llm.side_effect = httpx.ReadTimeout("timeout")
        # TTS: 정상 생략 (None, None)
        mock_tts.side_effect = RuntimeError("OPENAI_API_KEY 미설정")

        # DB mock: context manager 를 올바르게 설정
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_begin_ctx.__exit__  = MagicMock(return_value=False)

        mock_connect_ctx = MagicMock()
        mock_connect_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect_ctx.__exit__  = MagicMock(return_value=False)

        mock_engine.begin.return_value   = mock_begin_ctx
        mock_engine.connect.return_value = mock_connect_ctx

        payload = {
            "facility_id": "F001",
            "shift_start": "2026-04-09T06:00:00+09:00",
            "shift_end":   "2026-04-09T14:00:00+09:00",
            "trigger_mode":"SCHEDULED",
        }

        await handover_handler("evt-001", payload, 0)

        # INSERT 호출 검사 — params 딕셔너리의 "gmode" 키로 식별
        # (SQLAlchemy text() repr 이 SQL 텍스트를 노출하지 않으므로 params 로 판단)
        insert_calls = [
            c for c in mock_conn.execute.call_args_list
            if len(c[0]) > 1 and isinstance(c[0][1], dict) and "gmode" in c[0][1]
        ]
        assert len(insert_calls) >= 1, "shift_handover_ledger INSERT 파라미터(gmode 키)가 호출되어야 함"

        insert_params = insert_calls[-1][0][1]
        assert insert_params["gmode"] == "RAW_FALLBACK", (
            f"LLM 장애 시 generation_mode 는 RAW_FALLBACK 이어야 함. 실제: {insert_params['gmode']}"
        )
        assert "[AI 요약 불가" in insert_params["brief"], (
            "RAW_FALLBACK brief_text 에 '[AI 요약 불가' 문구가 포함되어야 함"
        )

    @pytest.mark.asyncio
    @patch("handover_handler._engine")
    @patch("handover_handler._call_llm", new_callable=AsyncMock)
    @patch("handover_handler._upload_tts_to_b2", new_callable=AsyncMock)
    async def test_llm_http_503_triggers_raw_fallback(
        self, mock_tts, mock_llm, mock_engine
    ):
        """LLM HTTP 503 → RAW_FALLBACK."""
        import httpx
        from handover_handler import handover_handler

        mock_llm.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        mock_tts.side_effect = RuntimeError("skip")

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_begin_ctx.__exit__  = MagicMock(return_value=False)
        mock_connect_ctx = MagicMock()
        mock_connect_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect_ctx.__exit__  = MagicMock(return_value=False)
        mock_engine.begin.return_value   = mock_begin_ctx
        mock_engine.connect.return_value = mock_connect_ctx

        payload = {
            "facility_id": "F002",
            "shift_start": "2026-04-09T14:00:00+09:00",
            "shift_end":   "2026-04-09T22:00:00+09:00",
        }
        await handover_handler("evt-002", payload, 0)

        insert_params = mock_conn.execute.call_args_list[-1][0][1]
        assert insert_params["gmode"] == "RAW_FALLBACK"


# ════════════════════════════════════════════════════════════════
# T-02: recorded_at 기준 집계, ingested_at 절대 불사용 (결함 2 방어)
# ════════════════════════════════════════════════════════════════

class TestRecordedAtAggregation:
    """집계 SQL 이 recorded_at BETWEEN 을 쓰고 ingested_at 을 기준으로 쓰지 않음을 검증."""

    def test_aggregate_handover_records_uses_recorded_at(self):
        """_aggregate_handover_records SQL 에 recorded_at BETWEEN 존재."""
        import inspect
        from handover_handler import _aggregate_handover_records

        source = inspect.getsource(_aggregate_handover_records)

        assert "recorded_at BETWEEN" in source, (
            "_aggregate_handover_records 는 recorded_at BETWEEN 을 사용해야 함 (결함2)"
        )

    def test_aggregate_handover_records_no_ingested_at_filter(self):
        """_aggregate_handover_records SQL WHERE 절에 ingested_at 필터가 없음."""
        import re
        import inspect
        from handover_handler import _aggregate_handover_records

        source = inspect.getsource(_aggregate_handover_records)

        # SQL 블록만 추출 (text(""" ... """) 또는 text(''' ... ''') 삼중따옴표 블록)
        sql_blocks = re.findall(r'text\s*\(\s*"""(.*?)"""', source, re.DOTALL)
        assert sql_blocks, "_aggregate_handover_records 에 SQL text() 블록이 없음"

        for sql in sql_blocks:
            # WHERE 절과 ORDER BY 사이만 검사
            w_idx = sql.find("WHERE")
            o_idx = sql.find("ORDER BY")
            if w_idx == -1:
                continue
            where_block = sql[w_idx:o_idx] if o_idx != -1 else sql[w_idx:]

            assert "ingested_at" not in where_block, (
                f"집계 SQL WHERE 절에 ingested_at 이 포함되면 안 됨 (오프라인 지연 결함2)\n"
                f"WHERE 블록:\n{where_block}"
            )

    def test_late_ingest_flag_set_when_ingested_after_shift_end(self):
        """오프라인 지연(ingested_at > shift_end) 레코드에 late_ingest=True 플래그 설정."""
        from unittest.mock import MagicMock
        from handover_handler import _aggregate_handover_records

        shift_end   = datetime(2026, 4, 9, 14, 0, tzinfo=timezone.utc)
        shift_start = datetime(2026, 4, 9, 6,  0, tzinfo=timezone.utc)

        late_row = MagicMock()
        late_row.id              = uuid4()
        late_row.recorded_at     = datetime(2026, 4, 9, 13, 55, tzinfo=timezone.utc)
        late_row.transcript_text = "소화제 투약"
        late_row.beneficiary_id  = "P001"
        late_row.device_id       = "DEV-1"
        late_row.ingested_at     = datetime(2026, 4, 9, 14, 25, tzinfo=timezone.utc)  # 지연!

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [late_row]

        records = _aggregate_handover_records(conn, "F001", shift_start, shift_end)

        assert len(records) == 1
        assert records[0]["late_ingest"] is True, (
            "ingested_at > shift_end 이면 late_ingest=True 이어야 함 (결함2 경고 표시)"
        )


# ════════════════════════════════════════════════════════════════
# T-03: trigger_mode=MANUAL 기록 검증 (결함 1 방어)
# ════════════════════════════════════════════════════════════════

class TestManualTriggerMode:
    """대타 출근 시 MANUAL 트리거 모드가 DB 에 정확히 기록됨을 검증."""

    @pytest.mark.asyncio
    @patch("handover_handler._engine")
    @patch("handover_handler._call_llm", new_callable=AsyncMock)
    @patch("handover_handler._upload_tts_to_b2", new_callable=AsyncMock)
    async def test_manual_trigger_mode_persisted(
        self, mock_tts, mock_llm, mock_engine
    ):
        """payload trigger_mode=MANUAL → INSERT 파라미터에 tmode='MANUAL'."""
        from handover_handler import handover_handler

        mock_llm.return_value = "정상 브리핑 텍스트"
        mock_tts.return_value = (None, None)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_begin_ctx.__exit__  = MagicMock(return_value=False)
        mock_connect_ctx = MagicMock()
        mock_connect_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect_ctx.__exit__  = MagicMock(return_value=False)
        mock_engine.begin.return_value   = mock_begin_ctx
        mock_engine.connect.return_value = mock_connect_ctx

        payload = {
            "facility_id":  "F003",
            "shift_start":  "2026-04-09T13:20:00+09:00",  # 대타 출근 시각
            "shift_end":    "2026-04-09T14:00:00+09:00",
            "trigger_mode": "MANUAL",                       # 결함 1 방어
            "caregiver_name": "이대타",
        }
        await handover_handler("evt-003", payload, 0)

        insert_params = mock_conn.execute.call_args_list[-1][0][1]
        assert insert_params["tmode"] == "MANUAL", (
            f"MANUAL 트리거 모드가 DB 에 기록되어야 함. 실제: {insert_params['tmode']}"
        )

    @pytest.mark.asyncio
    @patch("handover_handler._engine")
    @patch("handover_handler._call_llm", new_callable=AsyncMock)
    @patch("handover_handler._upload_tts_to_b2", new_callable=AsyncMock)
    async def test_invalid_trigger_mode_defaults_to_scheduled(
        self, mock_tts, mock_llm, mock_engine
    ):
        """알 수 없는 trigger_mode 값은 SCHEDULED 로 안전하게 폴백."""
        from handover_handler import handover_handler

        mock_llm.return_value = "브리핑"
        mock_tts.return_value = (None, None)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_begin_ctx.__exit__  = MagicMock(return_value=False)
        mock_connect_ctx = MagicMock()
        mock_connect_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect_ctx.__exit__  = MagicMock(return_value=False)
        mock_engine.begin.return_value   = mock_begin_ctx
        mock_engine.connect.return_value = mock_connect_ctx

        payload = {
            "facility_id":  "F004",
            "shift_start":  "2026-04-09T06:00:00+09:00",
            "shift_end":    "2026-04-09T14:00:00+09:00",
            "trigger_mode": "UNKNOWN_MODE",
        }
        await handover_handler("evt-004", payload, 0)

        insert_params = mock_conn.execute.call_args_list[-1][0][1]
        assert insert_params["tmode"] == "SCHEDULED"


# ════════════════════════════════════════════════════════════════
# T-04: UPDATE 봉인 트리거 — 봉인 필드 변경 감지 로직 검증 (결함 5)
# ════════════════════════════════════════════════════════════════

class TestUpdateTriggerGuard:
    """fn_handover_ledger_update_guard SQL 이 스키마에 존재하고
    봉인 필드를 모두 감시함을 검증."""

    def _load_schema(self) -> str:
        import os
        sql_path = os.path.join(
            os.path.dirname(__file__), "schema_v12_phase5.sql"
        )
        with open(sql_path, encoding="utf-8") as f:
            return f.read()

    def test_update_trigger_function_exists_in_schema(self):
        """schema_v12 에 fn_handover_ledger_update_guard 함수 정의가 존재."""
        schema = self._load_schema()
        assert "fn_handover_ledger_update_guard" in schema

    def test_update_trigger_guards_all_immutable_fields(self):
        """트리거가 봉인 대상 13개 필드 전부를 IS DISTINCT FROM 으로 감시."""
        schema = self._load_schema()
        immutable_fields = [
            "id", "facility_id", "shift_start", "shift_end",
            "generated_at", "trigger_mode", "source_count",
            "anomaly_count", "brief_text", "brief_sha256",
            "generation_mode", "tts_object_key", "tts_sha256",
        ]
        for field in immutable_fields:
            assert f"OLD.{field}" in schema and f"NEW.{field}" in schema, (
                f"봉인 필드 {field} 가 트리거 감시 목록에 없음"
            )

    def test_ack_fields_not_in_guard_block(self):
        """ACK 허용 필드(delivered_to, delivered_at)는 트리거 차단 목록에 없음."""
        schema = self._load_schema()
        # 트리거 함수 본문만 추출
        guard_start = schema.find("fn_handover_ledger_update_guard")
        guard_end   = schema.find("$$ LANGUAGE plpgsql", guard_start) + 20
        guard_body  = schema[guard_start:guard_end]

        assert "OLD.delivered_to" not in guard_body, (
            "delivered_to 는 ACK 허용 필드이므로 트리거 차단에 포함되면 안 됨"
        )
        assert "OLD.delivered_at" not in guard_body, (
            "delivered_at 는 ACK 허용 필드이므로 트리거 차단에 포함되면 안 됨"
        )


# ════════════════════════════════════════════════════════════════
# T-05: DELETE 트리거 존재 확인 (결함 5 — Append-Only 강제)
# ════════════════════════════════════════════════════════════════

class TestDeleteTriggerExists:
    """schema_v12 에 DELETE/TRUNCATE 차단 트리거가 존재함을 검증."""

    def _load_schema(self) -> str:
        import os
        sql_path = os.path.join(
            os.path.dirname(__file__), "schema_v12_phase5.sql"
        )
        with open(sql_path, encoding="utf-8") as f:
            return f.read()

    def test_delete_trigger_function_defined(self):
        schema = self._load_schema()
        assert "fn_handover_ledger_delete_guard" in schema

    def test_delete_trigger_attached_to_table(self):
        schema = self._load_schema()
        assert "trg_handover_ledger_delete" in schema

    def test_truncate_trigger_attached_to_table(self):
        schema = self._load_schema()
        assert "trg_handover_ledger_truncate" in schema

    def test_delete_trigger_raises_exception(self):
        """트리거 함수 본문에 RAISE EXCEPTION 이 포함됨 (Append-Only 보호)."""
        schema = self._load_schema()
        delete_fn_start = schema.find("fn_handover_ledger_delete_guard")
        delete_fn_end   = schema.find("$$ LANGUAGE plpgsql", delete_fn_start) + 20
        delete_fn_body  = schema[delete_fn_start:delete_fn_end]
        assert "RAISE EXCEPTION" in delete_fn_body


# ════════════════════════════════════════════════════════════════
# T-06: ACK PATCH — delivered_to/delivered_at 만 UPDATE (결함 6)
# ════════════════════════════════════════════════════════════════

class TestAckEndpoint:
    """PATCH /api/v5/handover/{id}/ack 가 delivered_to/delivered_at 만 변경함을 검증."""

    def test_ack_update_sql_only_touches_ack_fields(self):
        """handover_api.py ack_handover 함수 소스에서 UPDATE SET 절 검증."""
        import inspect
        from handover_api import ack_handover

        source = inspect.getsource(ack_handover)

        assert "delivered_to" in source
        assert "delivered_at" in source

        # 봉인 필드가 UPDATE SET 에 나타나면 안 됨
        forbidden_in_set = [
            "brief_text", "brief_sha256", "trigger_mode",
            "generation_mode", "source_count", "anomaly_count",
        ]
        # UPDATE SET 블록만 추출 (SET 부터 WHERE 앞까지)
        set_start = source.find("UPDATE public.shift_handover_ledger")
        set_end   = source.find("WHERE id", set_start)
        set_block = source[set_start:set_end] if set_start != -1 else ""

        for field in forbidden_in_set:
            assert field not in set_block, (
                f"ACK UPDATE 에 봉인 필드 {field!r} 가 포함되어서는 안 됨"
            )

    def test_ack_publishes_handover_ack_event(self):
        """ACK 성공 후 handover_ack 이벤트가 unified_outbox 에 발행됨."""
        import inspect
        from handover_api import ack_handover

        source = inspect.getsource(ack_handover)
        assert "handover_ack" in source, (
            "ACK 처리 후 handover_ack 이벤트를 outbox 에 발행해야 함 (NT-4 취소 신호)"
        )


# ════════════════════════════════════════════════════════════════
# T-07: _build_raw_fallback_brief — recorded_at 오름차순 정렬 (결함 2)
# ════════════════════════════════════════════════════════════════

class TestRawFallbackBrief:
    """_build_raw_fallback_brief 가 recorded_at 오름차순으로 정렬하고
    타임스탬프와 [AI 요약 불가] 문구를 포함함을 검증."""

    def _make_record(self, hour: int, minute: int, text: str, late: bool = False) -> dict:
        return {
            "recorded_at":     datetime(2026, 4, 9, hour, minute, tzinfo=timezone.utc),
            "transcript_text": text,
            "beneficiary_id":  "P001",
            "late_ingest":     late,
        }

    def test_output_starts_with_fallback_header(self):
        from handover_handler import _build_raw_fallback_brief
        records = [self._make_record(9, 30, "소화제 투약")]
        brief   = _build_raw_fallback_brief(records, [])
        # "[AI 요약 불가 — 원문 전달]" 헤더가 포함되어야 함
        assert "[AI 요약 불가" in brief

    def test_records_sorted_by_recorded_at_ascending(self):
        """나중에 들어온 레코드가 먼저 정렬되면 안 됨."""
        from handover_handler import _build_raw_fallback_brief
        records = [
            self._make_record(11, 0, "점심 식사 완료"),      # 나중
            self._make_record(9,  30, "소화제 투약"),         # 먼저
            self._make_record(10, 15, "화장실 보조"),         # 중간
        ]
        brief = _build_raw_fallback_brief(records, [])
        idx_090 = brief.index("09:30")
        idx_100 = brief.index("10:15")
        idx_110 = brief.index("11:00")
        assert idx_090 < idx_100 < idx_110, (
            "recorded_at 오름차순 정렬이 깨짐 — 결함2 방어 실패"
        )

    def test_late_ingest_marker_shown(self):
        """오프라인 지연 레코드에 [지연수신] 마커가 표시됨."""
        from handover_handler import _build_raw_fallback_brief
        records = [self._make_record(13, 55, "기저귀 교체", late=True)]
        brief   = _build_raw_fallback_brief(records, [])
        assert "[지연수신]" in brief

    def test_anomaly_section_prepended(self):
        """ANOMALY 가 있으면 [이상 탐지] 섹션이 메모 앞에 나타남."""
        from handover_handler import _build_raw_fallback_brief
        records   = [self._make_record(9, 0, "세면 보조")]
        anomalies = [{"result_status": "ANOMALY", "anomaly_code": "PHANTOM_BILLING",
                       "beneficiary_id": "P001"}]
        brief = _build_raw_fallback_brief(records, anomalies)
        assert "[이상 탐지]" in brief
        assert brief.index("[이상 탐지]") < brief.index("[인수인계 메모]")

    def test_empty_records_shows_no_memo_message(self):
        """메모가 없으면 '(인수인계 메모 없음)' 출력."""
        from handover_handler import _build_raw_fallback_brief
        brief = _build_raw_fallback_brief([], [])
        assert "(인수인계 메모 없음)" in brief


# ════════════════════════════════════════════════════════════════
# T-08: REFRESH 먼저 실행 순서 강제 검증 (결함 4 방어)
# ════════════════════════════════════════════════════════════════

class TestRefreshBeforeAggregation:
    """handover_handler 가 집계 쿼리 전에 반드시 REFRESH 를 실행함을 호출 순서로 검증."""

    @pytest.mark.asyncio
    @patch("handover_handler._engine")
    @patch("handover_handler._call_llm", new_callable=AsyncMock)
    @patch("handover_handler._upload_tts_to_b2", new_callable=AsyncMock)
    async def test_refresh_called_before_select(
        self, mock_tts, mock_llm, mock_engine
    ):
        """
        execute() 호출 목록에서 REFRESH MATERIALIZED VIEW 가
        evidence_ledger SELECT 보다 먼저 나타남.
        """
        from handover_handler import handover_handler

        mock_llm.return_value = "브리핑"
        mock_tts.return_value = (None, None)

        call_order: list[str] = []

        def side_effect_execute(sql, *args, **kwargs):
            sql_str = str(sql)
            if "REFRESH MATERIALIZED VIEW" in sql_str:
                call_order.append("REFRESH")
            elif "FROM public.evidence_ledger" in sql_str:
                call_order.append("SELECT_EVIDENCE")
            elif "FROM public.reconciliation_result" in sql_str:
                call_order.append("SELECT_RECON")
            result = MagicMock()
            result.fetchall.return_value = []
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = side_effect_execute

        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_begin_ctx.__exit__  = MagicMock(return_value=False)
        mock_connect_ctx = MagicMock()
        mock_connect_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect_ctx.__exit__  = MagicMock(return_value=False)
        mock_engine.begin.return_value   = mock_begin_ctx
        mock_engine.connect.return_value = mock_connect_ctx

        payload = {
            "facility_id": "F005",
            "shift_start": "2026-04-09T22:00:00+09:00",   # 야간 교대
            "shift_end":   "2026-04-10T06:00:00+09:00",
        }
        await handover_handler("evt-008", payload, 0)

        assert "REFRESH" in call_order, "REFRESH 가 한 번도 호출되지 않음 — 결함4 방어 실패"

        refresh_idx = call_order.index("REFRESH")
        for select_label in ("SELECT_EVIDENCE", "SELECT_RECON"):
            if select_label in call_order:
                select_idx = call_order.index(select_label)
                assert refresh_idx < select_idx, (
                    f"REFRESH({refresh_idx}) 가 {select_label}({select_idx}) 보다 나중에 실행됨 — 결함4"
                )

    def test_refresh_function_exists_in_source(self):
        """handover_handler.py 소스에 _refresh_materialized_views 호출이 존재."""
        import inspect
        from handover_handler import handover_handler as _fn

        source = inspect.getsource(_fn)
        assert "_refresh_materialized_views" in source, (
            "handover_handler 에 _refresh_materialized_views 호출이 없음 — 결함4"
        )
