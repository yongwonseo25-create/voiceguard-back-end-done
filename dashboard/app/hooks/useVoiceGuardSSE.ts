"use client";
/**
 * useVoiceGuardSSE — 백엔드 SSE 실시간 구독 훅
 * 경로: app/hooks/useVoiceGuardSSE.ts
 *
 * 백엔드 이벤트명 ↔ 리스너 매핑 (onmessage 절대 사용 금지):
 *   "connected"         → 연결 확인
 *   "new_evidence"      → 신규 미기록 건 감지 → alerts 추가
 *   "evidence_resolved" → 처리 완료 → alerts에서 제거
 *   "evidence_sealed"   → 파이프라인 SEALED 상태 전이
 *   "notion_synced"     → 파이프라인 SYNCED 상태 전이
 *
 * 백엔드 JSON 키 (backend/main.py sse_event_data 완전 일치):
 *   ledger_id, facility_id, beneficiary_id, shift_id,
 *   care_type, ingested_at, gps_lat, gps_lon,
 *   is_flagged, sync_status, sync_attempts, minutes_elapsed
 */

import { useEffect, useRef, useState, useCallback } from "react";
import type { 알림카드데이터 } from "@/components/admin/types";

// 로컬 백엔드 URL 하드코딩 — 외부 터널 사용 금지
const SSE_URL = "http://localhost:8000/api/sse/stream";

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

/** 파이프라인 상태 */
export type PipelineStage =
  | "INGESTED"
  | "SEALED"
  | "WORM_STORED"
  | "SYNCING"
  | "SYNCED";

export interface PipelineStatus {
  ledger_id: string;
  stage: PipelineStage;
  updated_at: string;
}

export function useVoiceGuardSSE() {
  const [alerts, setAlerts]               = useState<알림카드데이터[]>([]);
  const [connected, setConnected]         = useState(false);
  const [pipeline, setPipeline]           = useState<PipelineStatus[]>([]);
  const esRef = useRef<EventSource | null>(null);
  // 중복 방지 Set (ID 기반)
  const seenIdsRef = useRef<Set<string>>(new Set());

  const updatePipeline = useCallback((ledger_id: string, stage: PipelineStage) => {
    setPipeline((prev) => {
      const existing = prev.findIndex((p) => p.ledger_id === ledger_id);
      const entry: PipelineStatus = {
        ledger_id,
        stage,
        updated_at: new Date().toISOString(),
      };
      if (existing >= 0) {
        const next = [...prev];
        next[existing] = entry;
        return next;
      }
      return [entry, ...prev].slice(0, 100);
    });
  }, []);

  useEffect(() => {
    const es = new EventSource(SSE_URL);
    esRef.current = es;

    // ── 연결 확인 ─────────────────────────────────────────────
    es.addEventListener("connected", () => {
      setConnected(true);
    });

    // ── 신규 미기록 건 수신 ────────────────────────────────────
    es.addEventListener("new_evidence", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as {
          ledger_id:       string;
          facility_id:     string;
          beneficiary_id:  string;
          shift_id:        string;
          care_type:       string | null;
          ingested_at:     string;
          gps_lat:         number | null;
          gps_lon:         number | null;
          is_flagged:      boolean;
          sync_status:     string;
          sync_attempts:   number;
          minutes_elapsed: number;
        };

        // ── 중복 렌더링 방지: ID 기반 ──
        if (seenIdsRef.current.has(d.ledger_id)) return;
        seenIdsRef.current.add(d.ledger_id);

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

        // 최신 건 맨 위, 최대 50건 유지 (state 레벨 이중 체크)
        setAlerts((prev) => {
          if (prev.some((a) => a.id === card.id)) return prev;
          return [card, ...prev].slice(0, 50);
        });

        // 파이프라인: INGESTED
        updatePipeline(d.ledger_id, "INGESTED");
      } catch (err) {
        console.error("[SSE] new_evidence 파싱 실패:", err);
      }
    });

    // ── 처리 완료 건 제거 ──────────────────────────────────────
    es.addEventListener("evidence_resolved", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { ledger_id: string };
        setAlerts((prev) => prev.filter((a) => a.id !== d.ledger_id));
        seenIdsRef.current.delete(d.ledger_id);
      } catch {}
    });

    // ── 봉인 완료 → 파이프라인 SEALED ─────────────────────────
    es.addEventListener("evidence_sealed", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as {
          ledger_id: string;
          chain_hash: string;
          is_sealed: boolean;
          worm_key?: string;
        };
        updatePipeline(d.ledger_id, d.worm_key ? "WORM_STORED" : "SEALED");
      } catch {}
    });

    // ── Notion 동기화 완료 → 파이프라인 SYNCED ─────────────────
    es.addEventListener("notion_synced", (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data) as { ledger_id: string };
        updatePipeline(d.ledger_id, "SYNCED");
      } catch {}
    });

    // ── 연결 오류 — EventSource는 브라우저가 자동 재연결 ─────────
    es.onerror = () => {
      setConnected(false);
    };

    return () => {
      es.close();
    };
  }, [updatePipeline]);

  return { alerts, connected, pipeline };
}
