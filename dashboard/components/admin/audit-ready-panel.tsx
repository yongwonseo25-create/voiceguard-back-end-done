"use client";

import { useState, useEffect } from "react";
import type { DashboardEvidence } from "@/lib/dashboard-types";

interface AuditRow {
  id: string;
  recorded_at: string;
  beneficiary_id: string;
  recorder_id: string;
  gps_lat: number | null;
  gps_lon: number | null;
  chain_hash: string;
  worm_status: "sealed" | "pending" | "none";
  is_flagged: boolean;
}

interface Props {
  evidenceRows: DashboardEvidence[];
  selectedEvidence: DashboardEvidence | null;
  onSelectEvidence: (row: DashboardEvidence) => void;
}

/** 현지조사 방어 뷰 — 테이블 형식 (시각·수급자·기록자·위치·해시·WORM) */
export function AuditReadyPanel({ evidenceRows, selectedEvidence, onSelectEvidence }: Props) {
  const [auditRows, setAuditRows] = useState<AuditRow[]>([]);
  const [loading, setLoading] = useState(true);

  // 백엔드 /api/v2/audit에서 실제 데이터 시도, 실패 시 evidenceRows 폴백
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("http://localhost:8000/api/v2/audit");
        if (res.ok) {
          const json = await res.json();
          if (!cancelled && json.records?.length > 0) {
            setAuditRows(json.records.map((r: any) => ({
              id: r.id ?? r.ledger_id,
              recorded_at: r.recorded_at ?? r.ingested_at,
              beneficiary_id: r.beneficiary_id,
              recorder_id: r.shift_id ?? r.recorder_id ?? "-",
              gps_lat: r.gps_lat ?? null,
              gps_lon: r.gps_lon ?? null,
              chain_hash: r.chain_hash ?? "pending",
              worm_status: r.is_sealed ? "sealed" : r.worm_bucket ? "pending" : "none",
              is_flagged: r.is_flagged ?? false,
            })));
            setLoading(false);
            return;
          }
        }
      } catch {}

      // 폴백: SSE에서 변환된 evidenceRows 사용
      if (!cancelled) {
        setAuditRows(evidenceRows.map((e) => ({
          id: e.id,
          recorded_at: e.recorded_at,
          beneficiary_id: e.beneficiary_id,
          recorder_id: "-",
          gps_lat: null,
          gps_lon: null,
          chain_hash: e.chain_hash,
          worm_status: e.is_sealed ? "sealed" as const : "pending" as const,
          is_flagged: e.is_flagged,
        })));
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [evidenceRows]);

  const formatTime = (iso: string) => {
    try {
      return new Date(iso).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch { return "-"; }
  };

  const formatGps = (lat: number | null, lon: number | null) => {
    if (lat == null || lon == null) return "-";
    return `${lat.toFixed(4)},${lon.toFixed(4)}`;
  };

  const wormBadge = (status: string) => {
    switch (status) {
      case "sealed":
        return <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-900/70 text-indigo-300 font-bold border border-indigo-700/50">SEALED</span>;
      case "pending":
        return <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-300 font-bold border border-amber-700/50">PENDING</span>;
      default:
        return <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-400 font-bold">NONE</span>;
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-40 text-slate-500 text-sm">
        <div className="w-2 h-2 rounded-full bg-slate-600 animate-pulse mr-2" />
        증거 원장 로딩 중...
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="text-[10px] uppercase tracking-[0.15em] text-slate-500 mb-2 font-semibold">
        조사관 제출용 증거 테이블 ({auditRows.length}건)
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-[11px] text-left">
          <thead className="sticky top-0 bg-slate-900 z-10">
            <tr className="text-[10px] text-slate-500 uppercase tracking-wider border-b border-slate-700">
              <th className="py-2 px-1.5">시각</th>
              <th className="py-2 px-1.5">수급자</th>
              <th className="py-2 px-1.5">기록자</th>
              <th className="py-2 px-1.5">위치</th>
              <th className="py-2 px-1.5">해시</th>
              <th className="py-2 px-1.5">WORM</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/50">
            {auditRows.map((row) => {
              const isSelected = row.id === selectedEvidence?.id;
              return (
                <tr
                  key={row.id}
                  onClick={() => {
                    const ev = evidenceRows.find((e) => e.id === row.id);
                    if (ev) onSelectEvidence(ev);
                  }}
                  className={`cursor-pointer transition-colors ${
                    isSelected
                      ? "bg-emerald-950/40"
                      : row.is_flagged
                      ? "bg-rose-950/20 hover:bg-rose-950/30"
                      : "hover:bg-slate-800/40"
                  }`}
                >
                  <td className="py-2 px-1.5 font-mono text-slate-300 whitespace-nowrap">
                    {formatTime(row.recorded_at)}
                  </td>
                  <td className="py-2 px-1.5 text-slate-200 font-medium">
                    {row.beneficiary_id}
                    {row.is_flagged && <span className="ml-1 text-rose-400 text-[9px]">FLAG</span>}
                  </td>
                  <td className="py-2 px-1.5 text-slate-400 font-mono">
                    {row.recorder_id}
                  </td>
                  <td className="py-2 px-1.5 text-slate-400 font-mono text-[10px]">
                    {formatGps(row.gps_lat, row.gps_lon)}
                  </td>
                  <td className="py-2 px-1.5 font-mono text-[10px]">
                    {row.chain_hash === "pending" ? (
                      <span className="text-amber-400 animate-pulse">생성중</span>
                    ) : (
                      <span className="text-slate-400">{row.chain_hash.slice(0, 12)}…</span>
                    )}
                  </td>
                  <td className="py-2 px-1.5">
                    {wormBadge(row.worm_status)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
