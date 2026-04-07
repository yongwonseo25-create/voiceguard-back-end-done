"use client";

import type { 알림카드데이터 } from "./types";

interface Props {
  알림목록: 알림카드데이터[];
  선택된알림아이디?: string;
  on알림선택: (item: 알림카드데이터) => void;
}

export function 미기록실시간알림패널({ 알림목록, 선택된알림아이디, on알림선택 }: Props) {
  return (
    <div className="flex flex-col gap-3 overflow-y-auto h-full">
      <div className="text-[11px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-1">
        실시간 미기록 알림
      </div>
      {알림목록.length === 0 ? (
        <div className="flex items-center justify-center h-32 text-[#4A5568] text-sm">
          현재 미기록 임박 건이 없습니다 ✅
        </div>
      ) : (
        알림목록.map((item) => {
          const isCritical = item.minutes_elapsed <= 2;
          const isSelected = item.id === 선택된알림아이디;
          return (
            <button
              key={item.id}
              onClick={() => on알림선택(item)}
              className={`w-full text-left p-4 rounded-xl border transition-all ${
                isSelected
                  ? "border-[#6366F1] bg-[#1E1B4B]/60"
                  : isCritical
                  ? "border-[#991B1B] bg-[#7F1D1D]/25 위험-점멸"
                  : "border-[#92400E] bg-[#78350F]/20 hover:border-[#B45309]"
              }`}
            >
              <div className="flex justify-between items-start mb-2">
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                    isCritical
                      ? "bg-[#7F1D1D] text-[#FCA5A5]"
                      : "bg-[#78350F] text-[#FCD34D]"
                  }`}
                >
                  {isCritical ? "🔴 긴급" : "🟡 주의"}
                </span>
                <span className="text-[#64748B] text-[11px]">
                  {item.minutes_elapsed.toFixed(1)}분 경과
                </span>
              </div>
              <div className="font-bold text-[14px] text-[#F1F5F9] mb-0.5">
                {item.beneficiary_id}
              </div>
              <div className="text-[#94A3B8] text-[11px]">{item.facility_id}</div>
              <div className="text-[#64748B] text-[11px]">
                {item.care_type ?? "미지정"} · {item.shift_id}
              </div>
              <div className="mt-2 flex justify-between items-center">
                <span className="text-[#F87171] text-[11px] font-mono font-semibold">
                  예상 환수 ₩{item.예상환수액.toLocaleString()}
                </span>
                <span className="text-[#818CF8] text-[11px] font-semibold">
                  조치하기 →
                </span>
              </div>
            </button>
          );
        })
      )}
    </div>
  );
}
