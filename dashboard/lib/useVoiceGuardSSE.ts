"use client";
/**
 * useVoiceGuardSSE — 백엔드 SSE 실시간 구독 훅
 *
 * 백엔드 이벤트명 ↔ 리스너 매핑 (onmessage 절대 사용 금지):
 *   "connected"         → 연결 확인
 *   "new_evidence"      → 신규 미기록 건 감지 → alerts 추가
 *   "evidence_resolved" → 처리 완료 → alerts에서 제거
 *
 * 백엔드 JSON 키 (backend/main.py sse_event_data 완전 일치):
 *   ledger_id, facility_id, beneficiary_id, shift_id,
 *   care_type, ingested_at, gps_lat, gps_lon,
 *   is_flagged, sync_status, sync_attempts, minutes_elapsed
 */

import { useEffect, useRef, useState } from "react";
import type { 알림카드데이터 } from "@/components/admin/types";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// 예상 환수액 산정 기준 (케어 유형별 단가 × 1건)
const 케어별환수단가: Record<string, number> = {
  "식사 보조":  450_000,
  "배변 보조":  520_000,
  "체위 변경":  380_000,
  "구강 위생":  310_000,
  "목욕 보조":  680_000,
  "이동 보조":  420_000,
};
const DEFAULT_환수액 = 450_000;

export function useVoiceGuardSSE() {
  const [alerts, setAlerts]       = useState<알림카드데이터[]>([]);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const url = `${API_BASE}/api/sse/stream`;
    const es  = new EventSource(url);
    esRef.current = es;

    // ── 연결 확인 ─────────────────────────────────────────────
    es.addEventListener("connected", () => {
      setConnected(true);
    });

    // ── 신규 미기록 건 수신 ────────────────────────────────────
    // 백엔드 JSON 키와 1:1 매핑 (backend/main.py:sse_event_data 기준)
    es.addEventListener("new_evidence", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as {
          ledger_id:     string;
          facility_id:   string;
          beneficiary_id: string;
          shift_id:      string;
          care_type:     string | null;
          ingested_at:   string;
          gps_lat:       number | null;
          gps_lon:       number | null;
          is_flagged:    boolean;
          sync_status:   string;
          sync_attempts: number;
          minutes_elapsed: number;
        };

        const card: 알림카드데이터 = {
          id:              d.ledger_id,
          beneficiary_id:  d.beneficiary_id ?? "미상",
          facility_id:     d.facility_id    ?? "미상",
          shift_id:        d.shift_id       ?? "-",
          care_type:       d.care_type      ?? null,
          ingested_at:     d.ingested_at,
          minutes_elapsed: d.minutes_elapsed ?? 0,
          예상환수액:       케어별환수단가[d.care_type ?? ""] ?? DEFAULT_환수액,
          gps_lat:         d.gps_lat  ?? null,
          gps_lon:         d.gps_lon  ?? null,
        };

        // 최신 건을 맨 위에, 최대 50건 유지
        setAlerts((prev) => [card, ...prev].slice(0, 50));
      } catch (err) {
        console.error("[SSE] new_evidence 파싱 실패:", err);
      }
    });

    // ── 처리 완료 건 제거 ──────────────────────────────────────
    es.addEventListener("evidence_resolved", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { ledger_id: string };
        setAlerts((prev) => prev.filter((a) => a.id !== d.ledger_id));
      } catch {}
    });

    // ── 연결 오류 ──────────────────────────────────────────────
    es.onerror = () => {
      setConnected(false);
      // EventSource 는 자동으로 재연결을 시도한다 (브라우저 내장)
    };

    return () => {
      es.close();
    };
  }, []);

  return { alerts, connected };
}
