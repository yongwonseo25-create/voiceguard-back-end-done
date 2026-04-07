"use client";

import type { DashboardEvidence } from "@/lib/dashboard-types";
import { X, Shield } from "lucide-react";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  evidence: DashboardEvidence | null;
}

export function EvidenceDrawer({ open, onOpenChange, evidence }: Props) {
  if (!open || !evidence) return null;

  return (
    <>
      <div
        className="fixed inset-0 bg-black/60 z-40 backdrop-blur-sm"
        onClick={() => onOpenChange(false)}
      />
      <aside className="fixed right-0 top-0 h-full w-full max-w-md bg-[#0E1728] border-l border-[#223049] z-50 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#223049]">
          <div className="flex items-center gap-2">
            <Shield size={18} className="text-[#4ADE80]" />
            <span className="text-[#F8FAFC] font-bold text-[15px]">증거 원장 상세</span>
          </div>
          <button onClick={() => onOpenChange(false)} className="text-[#4A5568] hover:text-[#CBD5E1]">
            <X size={20} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
          <div className="bg-[#121C2F] rounded-lg p-4 space-y-3 border border-[#1E2D45]">
            <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">
              증거 메타데이터
            </div>
            <Row label="원장 ID"     value={evidence.id} mono />
            <Row label="수급자 ID"   value={evidence.beneficiary_id} />
            <Row label="요양기관"    value={evidence.facility_id} />
            <Row label="급여 유형"   value={evidence.care_type ?? "미지정"} />
            <Row label="기록 시각"   value={new Date(evidence.recorded_at).toLocaleString("ko-KR")} suppressHydrationWarning />
          </div>

          <div className="bg-[#121C2F] rounded-lg p-4 space-y-3 border border-[#1E2D45]">
            <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">
              WORM 봉인 상태
            </div>
            <div className="flex items-center gap-2">
              {evidence.is_sealed ? (
                <span className="px-3 py-1 rounded-full bg-[#1E1B4B] text-[#A5B4FC] text-[11px] font-bold">
                  🔒 봉인 완료
                </span>
              ) : (
                <span className="px-3 py-1 rounded-full bg-[#78350F]/60 text-[#FCD34D] text-[11px] font-bold">
                  ⏳ 처리 중
                </span>
              )}
              {evidence.is_flagged && (
                <span className="px-3 py-1 rounded-full bg-[#7F1D1D]/60 text-[#FCA5A5] text-[11px] font-bold">
                  🚩 플래그됨
                </span>
              )}
            </div>
            <div className="text-[11px] text-[#64748B] font-mono break-all">
              {evidence.chain_hash === "pending" ? "해시 생성 중…" : evidence.chain_hash}
            </div>
          </div>
        </div>
      </aside>
    </>
  );
}

function Row({ label, value, mono, suppressHydrationWarning }: { label: string; value: string; mono?: boolean; suppressHydrationWarning?: boolean }) {
  return (
    <div className="flex gap-2 text-[12px]">
      <span className="text-[#64748B] w-24 flex-shrink-0">{label}:</span>
      <span className={`text-[#E2E8F0] break-all ${mono ? "font-mono text-[10px]" : ""}`} suppressHydrationWarning={suppressHydrationWarning}>{value}</span>
    </div>
  );
}
