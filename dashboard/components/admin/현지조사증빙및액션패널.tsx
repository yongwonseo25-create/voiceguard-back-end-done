"use client";

import { useState } from "react";
import { postDirective } from "@/lib/api";
import type { 알림카드데이터 } from "./types";
import { AlertTriangle, MapPin, Clock, X } from "lucide-react";

interface Props {
  선택알림: 알림카드데이터;
  닫기: () => void;
}

const ACTION_OPTIONS: { value: "field_check" | "freeze" | "escalate" | "memo_only"; label: string }[] = [
  { value: "field_check", label: "현장 확인 요청" },
  { value: "freeze",      label: "급여 지급 동결" },
  { value: "escalate",    label: "상급 기관 에스컬레이션" },
  { value: "memo_only",   label: "메모 기록만 (내부 처리)" },
];

const REASON_OPTIONS = [
  "정상 기록 (보호자 증빙 확보)",
  "요양보호사 앱 오류",
  "GPS 신호 불량",
  "현장 확인 중",
  "기타 사유 (메모 필수)",
];

export function 현지조사증빙및액션패널({ 선택알림, 닫기 }: Props) {
  const [selectedAction, setSelectedAction] = useState<"field_check" | "freeze" | "escalate" | "memo_only">("field_check");
  const [selectedReason, setSelectedReason] = useState("");
  const [memo, setMemo]       = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted,  setSubmitted]  = useState(false);
  const [error, setError]           = useState<string | null>(null);

  const isCritical = 선택알림.minutes_elapsed <= 2;

  const handleSubmit = async () => {
    if (!selectedReason) return alert("사유를 선택해 주세요.");
    setSubmitting(true);
    setError(null);
    try {
      await postDirective({
        beneficiary_id: 선택알림.beneficiary_id,
        action:         selectedAction,
        reason:         selectedReason,
        memo:           memo || undefined,
        commanded_by:   "admin",
      });
      setSubmitted(true);
      setTimeout(닫기, 1800);
    } catch (e) {
      setError(e instanceof Error ? e.message : "전송 실패. 다시 시도해 주세요.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-[#0E1728] rounded-xl border border-[#223049] overflow-hidden">
      {/* 헤더 */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-[#223049]">
        <div className="flex items-center gap-2">
          <AlertTriangle
            size={16}
            className={isCritical ? "text-[#F87171]" : "text-[#FCD34D]"}
          />
          <span className="text-[#F8FAFC] font-bold text-[14px]">현지조사 증빙 및 조치</span>
        </div>
        <button onClick={닫기} className="text-[#4A5568] hover:text-[#CBD5E1] transition">
          <X size={18} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        {/* 수급자 정보 */}
        <div className="bg-[#121C2F] rounded-lg p-4 space-y-2 border border-[#1E2D45]">
          <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">수급자 정보</div>
          <Row label="수급자 ID"  value={선택알림.beneficiary_id} />
          <Row label="요양기관"   value={선택알림.facility_id} />
          <Row label="급여 유형"  value={선택알림.care_type ?? "미지정"} />
          <Row label="교대 ID"    value={선택알림.shift_id} />
          <div className="flex items-center gap-2 text-[12px]">
            <Clock size={12} className="text-[#64748B]" />
            <span className="text-[#64748B]">수신:</span>
            <span className="text-[#E2E8F0]" suppressHydrationWarning>
              {new Date(선택알림.ingested_at).toLocaleString("ko-KR")}
            </span>
            <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
              isCritical ? "bg-[#7F1D1D]/60 text-[#FCA5A5]" : "bg-[#78350F]/60 text-[#FCD34D]"
            }`}>
              {선택알림.minutes_elapsed.toFixed(1)}분 경과
            </span>
          </div>
          {선택알림.gps_lat && (
            <div className="flex items-center gap-2 text-[12px]">
              <MapPin size={12} className="text-[#64748B]" />
              <span className="text-[#64748B]">GPS:</span>
              <span className="text-[#94A3B8] font-mono text-[11px]">
                {선택알림.gps_lat.toFixed(5)}, {선택알림.gps_lon?.toFixed(5)}
              </span>
            </div>
          )}
          <div className="pt-1 border-t border-[#1E2D45]">
            <span className="text-[#F87171] font-mono font-semibold text-[13px]">
              예상 환수액: ₩{선택알림.예상환수액.toLocaleString()}
            </span>
          </div>
        </div>

        {/* 조치 유형 */}
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">조치 유형</div>
          <div className="grid grid-cols-2 gap-2">
            {ACTION_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setSelectedAction(opt.value)}
                className={`px-3 py-2 rounded-lg border text-[12px] font-medium transition text-left ${
                  selectedAction === opt.value
                    ? "border-[#6366F1] bg-[#1E1B4B]/60 text-[#A5B4FC]"
                    : "border-[#223049] text-[#94A3B8] hover:border-[#314258]"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* 사유 선택 */}
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">처리 사유</div>
          <div className="space-y-1.5">
            {REASON_OPTIONS.map((opt) => (
              <label
                key={opt}
                className={`flex items-center gap-3 px-4 py-2.5 rounded-lg border cursor-pointer transition ${
                  selectedReason === opt
                    ? "border-[#6366F1] bg-[#1E1B4B]/40 text-[#F8FAFC]"
                    : "border-[#223049] text-[#94A3B8] hover:border-[#314258]"
                }`}
              >
                <input
                  type="radio" name="reason" value={opt}
                  checked={selectedReason === opt}
                  onChange={() => setSelectedReason(opt)}
                  className="hidden"
                />
                <span className={`w-3.5 h-3.5 rounded-full border-2 flex-shrink-0 flex items-center justify-center ${
                  selectedReason === opt ? "border-[#818CF8]" : "border-[#4A5568]"
                }`}>
                  {selectedReason === opt && (
                    <span className="w-1.5 h-1.5 rounded-full bg-[#818CF8] block" />
                  )}
                </span>
                <span className="text-[12px]">{opt}</span>
              </label>
            ))}
          </div>
        </div>

        {/* 메모 */}
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-2">추가 메모 (선택)</div>
          <textarea
            placeholder="추가 메모를 입력하세요"
            value={memo}
            onChange={(e) => setMemo(e.target.value)}
            className="w-full h-20 px-4 py-3 bg-[#121C2F] border border-[#223049] rounded-lg
                       text-[#CBD5E1] text-[12px] placeholder-[#4A5568]
                       focus:outline-none focus:border-[#6366F1] resize-none"
          />
        </div>
      </div>

      {/* 하단 버튼 */}
      <div className="px-5 py-4 border-t border-[#223049] space-y-2">
        {error && (
          <p className="text-[#F87171] text-[11px] text-center">{error}</p>
        )}
        <div className="flex gap-2">
          <button
            onClick={닫기}
            className="flex-1 py-2.5 rounded-lg border border-[#223049] text-[#94A3B8]
                       hover:bg-[#121C2F] transition text-[13px] font-medium"
          >
            취소
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitted || submitting}
            className={`flex-1 py-2.5 rounded-lg font-semibold text-[13px] transition flex items-center justify-center gap-2 ${
              submitted
                ? "bg-[#166534] text-[#86EFAC] cursor-default"
                : submitting
                ? "bg-[#1E1B4B] text-[#A5B4FC] cursor-wait"
                : "bg-[#4F46E5] hover:bg-[#6366F1] text-white"
            }`}
          >
            {submitted ? "✅ 조치 완료 등록됨" : submitting ? "전송 중…" : "조치 완료 등록"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-[12px]">
      <span className="text-[#64748B] w-24 flex-shrink-0">{label}:</span>
      <span className="text-[#E2E8F0] font-mono">{value}</span>
    </div>
  );
}
