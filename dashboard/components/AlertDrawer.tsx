"use client";
import { useState } from "react";
import { AlertRecord, patchEvidenceResolution } from "../lib/api";
import { X, MapPin, Shield, Clock, AlertTriangle } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { ko } from "date-fns/locale";

interface Props {
  record: AlertRecord | null;
  onClose: () => void;
}

const CAUSE_OPTIONS = [
  "정상 기록 (보호자 증빙 확보)",
  "요양보호사 앱 오류",
  "GPS 신호 불량",
  "현장 확인 중",
  "기타 사유 (메모 필수)",
];

export default function AlertDrawer({ record, onClose }: Props) {
  const [selectedCause, setSelectedCause] = useState("");
  const [memo, setMemo]                   = useState("");
  const [submitted, setSubmitted]         = useState(false);
  const [submitting, setSubmitting]       = useState(false);
  const [error, setError]                 = useState<string | null>(null);

  if (!record) return null;

  const elapsed = record.minutes_elapsed;
  const urgency = elapsed <= 2 ? "critical" : elapsed <= 5 ? "warning" : "info";

  const handleSubmit = async () => {
    if (!selectedCause) return alert("사유를 선택해 주세요.");
    setSubmitting(true);
    setError(null);
    try {
      await patchEvidenceResolution(record.id, { cause: selectedCause, memo });
      setSubmitted(true);
      setTimeout(onClose, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "전송 실패. 다시 시도해 주세요.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      {/* 오버레이 */}
      <div
        className="fixed inset-0 bg-black/60 z-40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* 서랍 패널 */}
      <aside className="fixed right-0 top-0 h-full w-full max-w-lg bg-[#0f1729] border-l border-slate-700 z-50 flex flex-col shadow-2xl animate-slide-in">
        {/* 헤더 */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
          <div className="flex items-center gap-2">
            <AlertTriangle
              className={urgency === "critical" ? "text-red-400" : "text-amber-400"}
              size={20}
            />
            <span className="text-white font-bold text-lg">미기록 건 처리</span>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white transition">
            <X size={22} />
          </button>
        </div>

        {/* 증거 메타데이터 */}
        <div className="px-6 py-5 space-y-3 border-b border-slate-700">
          <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-widest mb-3">
            증거 메타데이터
          </h3>

          <Row label="수급자 ID"    value={record.beneficiary_id} />
          <Row label="요양기관"     value={record.facility_id} />
          <Row label="급여 유형"    value={record.care_type ?? "미지정"} />
          <Row label="근무 교대 ID" value={record.shift_id} />

          <div className="flex items-center gap-2 text-sm">
            <Clock size={14} className="text-slate-400" />
            <span className="text-slate-400">수신 시각:</span>
            <span className="text-white">
              {new Date(record.ingested_at).toLocaleString("ko-KR")}
            </span>
            <span
              className={`ml-1 px-2 py-0.5 rounded-full text-xs font-bold ${
                urgency === "critical"
                  ? "bg-red-900/60 text-red-300"
                  : "bg-amber-900/60 text-amber-300"
              }`}
            >
              {elapsed.toFixed(1)}분 경과
            </span>
          </div>

          {record.gps_lat && (
            <div className="flex items-center gap-2 text-sm">
              <MapPin size={14} className="text-slate-400" />
              <span className="text-slate-400">GPS:</span>
              <span className="text-white font-mono text-xs">
                {record.gps_lat.toFixed(6)}, {record.gps_lon?.toFixed(6)}
              </span>
            </div>
          )}

          <div className="flex items-center gap-2 text-sm">
            <Shield size={14} className="text-slate-400" />
            <span className="text-slate-400">동기화 상태:</span>
            <StatusBadge status={record.sync_status} attempts={record.sync_attempts} />
          </div>
        </div>

        {/* 처리 액션 */}
        <div className="px-6 py-5 flex-1 overflow-y-auto space-y-4">
          <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-widest">
            현장 확인 요청 / 사유 분류
          </h3>

          <div className="space-y-2">
            {CAUSE_OPTIONS.map((opt) => (
              <label
                key={opt}
                className={`flex items-center gap-3 px-4 py-3 rounded-lg border cursor-pointer transition
                  ${selectedCause === opt
                    ? "border-indigo-500 bg-indigo-900/40 text-white"
                    : "border-slate-700 hover:border-slate-500 text-slate-300"
                  }`}
              >
                <input
                  type="radio" name="cause" value={opt}
                  checked={selectedCause === opt}
                  onChange={() => setSelectedCause(opt)}
                  className="hidden"
                />
                <span className={`w-4 h-4 rounded-full border-2 flex-shrink-0 flex items-center justify-center
                  ${selectedCause === opt ? "border-indigo-400" : "border-slate-500"}`}
                >
                  {selectedCause === opt && (
                    <span className="w-2 h-2 rounded-full bg-indigo-400 block" />
                  )}
                </span>
                <span className="text-sm">{opt}</span>
              </label>
            ))}
          </div>

          <textarea
            placeholder="추가 메모 (선택)"
            value={memo}
            onChange={(e) => setMemo(e.target.value)}
            className="w-full h-24 px-4 py-3 bg-slate-800 border border-slate-600 rounded-lg text-slate-200
                       text-sm placeholder-slate-500 focus:outline-none focus:border-indigo-500 resize-none"
          />
        </div>

        {/* 하단 버튼 */}
        <div className="px-6 py-4 border-t border-slate-700 flex flex-col gap-2">
          {error && (
            <p className="text-red-400 text-xs text-center px-2">{error}</p>
          )}
          <div className="flex gap-3">
            <button
              onClick={onClose}
              className="flex-1 py-2.5 rounded-lg border border-slate-600 text-slate-300
                         hover:bg-slate-700 transition text-sm font-medium"
            >
              취소
            </button>
            <button
              onClick={handleSubmit}
              disabled={submitted || submitting}
              className={`flex-1 py-2.5 rounded-lg font-semibold text-sm transition flex items-center justify-center gap-2
                ${submitted
                  ? "bg-green-700 text-green-200 cursor-default"
                  : submitting
                  ? "bg-indigo-800 text-indigo-300 cursor-wait"
                  : "bg-indigo-600 hover:bg-indigo-500 text-white"
                }`}
            >
              {submitted ? (
                <>✅ 알림톡 발송 완료</>
              ) : submitting ? (
                "전송 중…"
              ) : (
                "현장 확인 요청 전송"
              )}
            </button>
          </div>
        </div>
      </aside>

    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-sm">
      <span className="text-slate-400 w-28 flex-shrink-0">{label}:</span>
      <span className="text-white font-mono">{value}</span>
    </div>
  );
}

function StatusBadge({ status, attempts }: { status: string; attempts: number }) {
  const map: Record<string, string> = {
    pending:    "bg-amber-900/50 text-amber-300",
    processing: "bg-blue-900/50 text-blue-300",
    done:       "bg-green-900/50 text-green-300",
    dlq:        "bg-red-900/50 text-red-300",
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${map[status] ?? "bg-slate-700 text-slate-300"}`}>
      {status} {attempts > 0 && `(${attempts}회 시도)`}
    </span>
  );
}
