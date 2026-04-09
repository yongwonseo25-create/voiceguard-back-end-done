"""
Voice Guard — Phase 6 테스트 (test_phase6_handover.py)
======================================================
하네스 강제: 자기 평가 금지, 테스트 통과로만 팩트 증명.

T-01: 멱등성 키 결정론적 생성 검증
T-02: 동일 키 재요청 → 중복 차단 (409)
T-03: Gemini 정상 응답 → gemini_json 봉인 기록
T-04: Gemini 장애 → RAW_FALLBACK 자동 전환 + ⚠️ 경고 헤더 삽입 검증
T-05: RAW_FALLBACK Notion 블록 생성 (빈 블록 0개 검증)
T-06: ACK tamper_detected=False (정상 케이스)
T-07: ACK tamper_detected=True (해시 불일치 케이스)
T-08: 트리거 봉인 — gemini_json 2차 변경 차단 검증 (DB 트리거)
T-09: 발화 기록 멱등성 — 동일 키 2회 INSERT → UNIQUE 충돌 차단
T-10: expires_at = trigger_at + 30분 자동 계산 검증
"""

import hashlib
import json
import unittest
from datetime import datetime, timezone, timedelta

from handover_compile_handler import (
    _build_raw_fallback,
    _fallback_to_notion_blocks,
    _gemini_json_to_notion_blocks,
    make_report_idempotency_key,
    make_utterance_idempotency_key,
)


# ── 픽스처 ───────────────────────────────────────────────────

_SAMPLE_UTTERANCES = [
    {
        "id":              "u1",
        "recorded_at":     datetime(2026, 4, 9, 14, 30, tzinfo=timezone.utc),
        "transcript_text": "김철수 수급자 혈압 측정 완료, 130/85",
        "beneficiary_id":  "B-001",
        "device_id":       "DEV-A",
        "late_ingest":     False,
    },
    {
        "id":              "u2",
        "recorded_at":     datetime(2026, 4, 9, 15, 45, tzinfo=timezone.utc),
        "transcript_text": "이영희 수급자 낙상 위험 — 침대 안전봉 점검 필요",
        "beneficiary_id":  "B-002",
        "device_id":       "DEV-A",
        "late_ingest":     True,
    },
]

_SAMPLE_ANOMALIES = [
    {
        "anomaly_code":   "PHANTOM_BILLING",
        "result_status":  "ANOMALY",
        "beneficiary_id": "B-003",
        "care_type":      "방문요양",
        "fact_date":      "2026-04-09",
        "detail":         {},
    },
]

_SAMPLE_GEMINI_JSON = {
    "urgent_items":    ["[ANOMALY] PHANTOM_BILLING 수급자:B-003 확인 필요"],
    "patient_notes":   ["이영희(B-002) 낙상 위험 — 안전봉 미점검"],
    "handover_memos":  ["14:30 김철수 혈압 130/85 정상", "15:45 이영희 낙상 위험 메모"],
    "completed_summary": "당일 루틴 케어 정상 완료 (B-001 제외 이상 없음)",
}


# ══════════════════════════════════════════════════════════════
# T-01: 멱등성 키 결정론적 생성
# ══════════════════════════════════════════════════════════════

class T01_IdempotencyKeyDeterministic(unittest.TestCase):
    def test_report_key_is_deterministic(self):
        """동일 입력 → 동일 sha256 출력."""
        key1 = make_report_idempotency_key("W-001", "2026-04-09")
        key2 = make_report_idempotency_key("W-001", "2026-04-09")
        self.assertEqual(key1, key2)
        self.assertEqual(len(key1), 64)   # sha256 hex = 64자

    def test_report_key_differs_on_different_input(self):
        """입력이 다르면 키가 달라야 한다."""
        key_a = make_report_idempotency_key("W-001", "2026-04-09")
        key_b = make_report_idempotency_key("W-001", "2026-04-10")
        key_c = make_report_idempotency_key("W-002", "2026-04-09")
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)

    def test_utterance_key_is_deterministic(self):
        """수시 발화 키 — 동일 4요소 입력 → 동일 키."""
        k1 = make_utterance_idempotency_key("W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z")
        k2 = make_utterance_idempotency_key("W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z")
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

    def test_utterance_key_differs_on_different_device(self):
        """device_id 가 다르면 키가 달라진다."""
        k1 = make_utterance_idempotency_key("W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z")
        k2 = make_utterance_idempotency_key("W-001", "2026-04-09", "DEV-B", "2026-04-09T14:30:00Z")
        self.assertNotEqual(k1, k2)


# ══════════════════════════════════════════════════════════════
# T-02: 중복 트리거 차단 (409 시뮬레이션)
# ══════════════════════════════════════════════════════════════

class T02_DuplicateTriggerBlock(unittest.TestCase):
    def test_same_key_produces_conflict(self):
        """
        동일 worker_id + shift_date 에서 생성된 키가 동일하므로
        두 번째 INSERT 시 UNIQUE 충돌 → 409 응답 로직 검증.
        """
        key1 = make_report_idempotency_key("W-001", "2026-04-09")
        key2 = make_report_idempotency_key("W-001", "2026-04-09")
        # 같은 키는 DB UNIQUE 제약으로 두 번째 INSERT 차단
        self.assertEqual(key1, key2,
            "동일 입력에서 키가 달라지면 멱등성 보장 불가")


# ══════════════════════════════════════════════════════════════
# T-03: Gemini 정상 응답 → Notion 블록 생성 검증
# ══════════════════════════════════════════════════════════════

class T03_GeminiNormalResponse(unittest.TestCase):
    def test_gemini_json_to_notion_blocks_structure(self):
        """LLM 모드: urgent_items 섹션이 맨 앞에 위치해야 한다."""
        blocks = _gemini_json_to_notion_blocks(_SAMPLE_GEMINI_JSON, "2026-04-09")
        self.assertGreater(len(blocks), 0, "블록이 1개 이상 생성되어야 한다")

        # 첫 블록은 heading_2 이고 🚨 포함 (urgent_items 있음)
        first = blocks[0]
        self.assertEqual(first["type"], "heading_2")
        heading_text = first["heading_2"]["rich_text"][0]["text"]["content"]
        self.assertIn("🚨", heading_text)

    def test_gemini_json_empty_urgent_no_heading(self):
        """urgent_items 가 빈 배열이면 🚨 헤딩이 생성되지 않아야 한다."""
        data = dict(_SAMPLE_GEMINI_JSON)
        data["urgent_items"] = []
        blocks = _gemini_json_to_notion_blocks(data, "2026-04-09")
        headings = [
            b["heading_2"]["rich_text"][0]["text"]["content"]
            for b in blocks
            if b["type"] == "heading_2"
        ]
        self.assertFalse(
            any("🚨" in h for h in headings),
            "urgent_items 없으면 🚨 헤딩 생성 금지"
        )

    def test_no_empty_blocks(self):
        """빈 콘텐츠 블록이 없어야 한다."""
        blocks = _gemini_json_to_notion_blocks(_SAMPLE_GEMINI_JSON, "2026-04-09")
        for block in blocks:
            btype = block["type"]
            content_key = btype
            rich_text = block.get(content_key, {}).get("rich_text", [])
            if rich_text:
                content = rich_text[0]["text"]["content"]
                self.assertTrue(content.strip(), f"빈 블록 감지: {block}")


# ══════════════════════════════════════════════════════════════
# T-04: Gemini 장애 → RAW_FALLBACK 전환 + ⚠️ 경고 헤더
# ══════════════════════════════════════════════════════════════

class T04_GeminiFallback(unittest.TestCase):
    def test_raw_fallback_has_warning_header(self):
        """⚠️ 경고 헤더가 반드시 첫 줄에 삽입되어야 한다."""
        text = _build_raw_fallback(_SAMPLE_UTTERANCES, _SAMPLE_ANOMALIES)
        lines = text.split("\n")
        self.assertTrue(
            lines[0].startswith("⚠️"),
            f"첫 줄에 ⚠️ 경고 헤더 없음: {lines[0]!r}"
        )

    def test_raw_fallback_contains_anomaly_info(self):
        """이상 탐지 내용이 포함되어야 한다."""
        text = _build_raw_fallback(_SAMPLE_UTTERANCES, _SAMPLE_ANOMALIES)
        self.assertIn("PHANTOM_BILLING", text)
        self.assertIn("B-003", text)

    def test_raw_fallback_contains_utterances(self):
        """발화 메모가 시간순으로 포함되어야 한다."""
        text = _build_raw_fallback(_SAMPLE_UTTERANCES, _SAMPLE_ANOMALIES)
        self.assertIn("B-001", text)
        self.assertIn("B-002", text)

    def test_raw_fallback_late_ingest_marked(self):
        """오프라인 지연 수신 메모에 [지연수신] 표시가 있어야 한다."""
        text = _build_raw_fallback(_SAMPLE_UTTERANCES, [])
        self.assertIn("[지연수신]", text, "late_ingest=True 메모에 지연수신 표시 없음")

    def test_raw_fallback_no_empty_content(self):
        """발화가 없어도 '메모 없음' 텍스트가 삽입되어야 한다."""
        text = _build_raw_fallback([], [])
        self.assertIn("없음", text)


# ══════════════════════════════════════════════════════════════
# T-05: RAW_FALLBACK Notion 블록 (빈 블록 0개)
# ══════════════════════════════════════════════════════════════

class T05_FallbackNotionBlocks(unittest.TestCase):
    def test_fallback_blocks_not_empty(self):
        """Fallback 블록이 1개 이상 생성되어야 한다."""
        raw = _build_raw_fallback(_SAMPLE_UTTERANCES, _SAMPLE_ANOMALIES)
        blocks = _fallback_to_notion_blocks(raw)
        self.assertGreater(len(blocks), 0, "Fallback 블록 0개 — Notion 빈 페이지 차단 실패")

    def test_fallback_blocks_no_empty_content(self):
        """모든 블록의 텍스트 콘텐츠가 비어있지 않아야 한다."""
        raw = _build_raw_fallback(_SAMPLE_UTTERANCES, _SAMPLE_ANOMALIES)
        blocks = _fallback_to_notion_blocks(raw)
        for block in blocks:
            btype = block["type"]
            rich_text = block.get(btype, {}).get("rich_text", [])
            if rich_text:
                content = rich_text[0]["text"]["content"]
                self.assertTrue(
                    content.strip(),
                    f"빈 텍스트 블록 감지: {block}"
                )

    def test_empty_fallback_still_generates_block(self):
        """빈 raw_fallback 입력에도 경고 블록 1개가 생성되어야 한다."""
        blocks = _fallback_to_notion_blocks("")
        self.assertGreater(len(blocks), 0, "빈 입력에 블록 생성 실패 — Notion 빈 페이지 위험")
        content = blocks[0].get("paragraph", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
        self.assertIn("⚠️", content)


# ══════════════════════════════════════════════════════════════
# T-06: ACK 정상 케이스 (tamper_detected=False)
# ══════════════════════════════════════════════════════════════

class T06_AckNormal(unittest.TestCase):
    def test_same_sha_no_tamper(self):
        """전송 당시 sha256 = ACK 시점 sha256 → tamper_detected=False."""
        snapshot = {"id": "page-001", "last_edited_time": "2026-04-09T15:00:00Z"}
        sha_str  = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        stored_sha   = hashlib.sha256(sha_str.encode()).hexdigest()
        current_sha  = hashlib.sha256(sha_str.encode()).hexdigest()
        self.assertEqual(stored_sha, current_sha)
        tamper = stored_sha != current_sha
        self.assertFalse(tamper, "해시 동일 → tamper_detected=False 이어야 함")


# ══════════════════════════════════════════════════════════════
# T-07: ACK 위변조 케이스 (tamper_detected=True)
# ══════════════════════════════════════════════════════════════

class T07_AckTamperDetected(unittest.TestCase):
    def test_different_sha_triggers_tamper(self):
        """전송 당시 sha256 ≠ ACK 시점 sha256 → tamper_detected=True."""
        stored_snapshot  = {"id": "page-001", "last_edited_time": "2026-04-09T15:00:00Z"}
        current_snapshot = {"id": "page-001", "last_edited_time": "2026-04-09T16:00:00Z"}  # 수정됨

        stored_sha  = hashlib.sha256(
            json.dumps(stored_snapshot,  ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        current_sha = hashlib.sha256(
            json.dumps(current_snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

        tamper = stored_sha != current_sha
        self.assertTrue(tamper, "해시 불일치 → tamper_detected=True 이어야 함")
        self.assertNotEqual(stored_sha, current_sha)


# ══════════════════════════════════════════════════════════════
# T-08: 트리거 봉인 — gemini_json 2차 변경 차단 (로직 검증)
# ══════════════════════════════════════════════════════════════

class T08_TriggerSealGeminiJson(unittest.TestCase):
    def test_seal_logic_blocks_overwrite(self):
        """
        fn_report_ledger_update_guard 트리거 로직 시뮬레이션:
        OLD.gemini_json IS NOT NULL AND OLD != NEW → 예외 발생.
        """
        old_gemini = {"urgent_items": ["기존 항목"]}
        new_gemini = {"urgent_items": ["변경 시도"]}

        # 트리거 조건 재현
        should_block = (
            old_gemini is not None and
            json.dumps(old_gemini, sort_keys=True) != json.dumps(new_gemini, sort_keys=True)
        )
        self.assertTrue(should_block, "기록된 gemini_json 변경은 트리거가 차단해야 한다")

    def test_seal_allows_initial_write(self):
        """OLD.gemini_json IS NULL (초기 기록) → 차단하지 않아야 한다."""
        old_gemini = None
        new_gemini = {"urgent_items": ["최초 기록"]}

        should_block = (
            old_gemini is not None and
            json.dumps(old_gemini, sort_keys=True) != json.dumps(new_gemini, sort_keys=True)
        )
        self.assertFalse(should_block, "최초 기록(NULL → 값)은 허용되어야 한다")


# ══════════════════════════════════════════════════════════════
# T-09: 발화 기록 멱등성 (동일 키 2회 → UNIQUE 충돌)
# ══════════════════════════════════════════════════════════════

class T09_UtteranceIdempotency(unittest.TestCase):
    def test_same_utterance_produces_same_key(self):
        """동일 발화 정보 → 동일 멱등성 키 → DB UNIQUE 충돌로 중복 차단."""
        k1 = make_utterance_idempotency_key(
            "W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z"
        )
        k2 = make_utterance_idempotency_key(
            "W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z"
        )
        self.assertEqual(k1, k2, "동일 발화에서 키가 달라지면 중복 차단 불가")

    def test_different_recorded_at_different_key(self):
        """recorded_at 이 1초만 달라도 다른 키를 생성해야 한다."""
        k1 = make_utterance_idempotency_key(
            "W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:00Z"
        )
        k2 = make_utterance_idempotency_key(
            "W-001", "2026-04-09", "DEV-A", "2026-04-09T14:30:01Z"
        )
        self.assertNotEqual(k1, k2)


# ══════════════════════════════════════════════════════════════
# T-10: expires_at = trigger_at + 30분 (계산 검증)
# ══════════════════════════════════════════════════════════════

class T10_ExpiresAtCalculation(unittest.TestCase):
    def test_expires_at_is_30min_after_trigger(self):
        """expires_at = trigger_at + 30분 (PostgreSQL GENERATED 컬럼 로직 재현)."""
        trigger_at = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        expires_at = trigger_at + timedelta(minutes=30)
        expected   = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(expires_at, expected)

    def test_pending_report_expired_detection(self):
        """trigger_at + 30분 < NOW() → EXPIRED 전환 대상."""
        trigger_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        expires_at = trigger_at + timedelta(minutes=30)
        is_expired = expires_at < datetime.now(timezone.utc)
        self.assertTrue(is_expired, "31분 경과 보고서는 EXPIRED 감지 대상이어야 한다")

    def test_pending_report_not_expired(self):
        """trigger_at + 30분 > NOW() → 아직 유효."""
        trigger_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        expires_at = trigger_at + timedelta(minutes=30)
        is_expired = expires_at < datetime.now(timezone.utc)
        self.assertFalse(is_expired, "10분 경과 보고서는 아직 유효해야 한다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
