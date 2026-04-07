"use client";

import { useState, useEffect, useCallback } from "react";

/* ═══════════════════════════════════════════════════════════════════
   타입 정의
   ═══════════════════════════════════════════════════════════════════ */

type AngelStatus =
  | "DETECTED"
  | "REVIEW_REQUIRED"
  | "APPROVED_FOR_EXPORT"
  | "REJECTED"
  | "RECLASSIFIED"
  | "EXPORTED";

interface PendingItem {
  review_event_id: string;
  ledger_id: string;
  angel_status: AngelStatus;
  reviewer_id: string | null;
  decision_note: string | null;
  reclassified_to: string | null;
  review_ts: string;
  facility_id: string;
  beneficiary_id: string;
  shift_id: string;
  care_type: string | null;
  ingested_at: string;
  audio_sha256: string;
  chain_hash: string;
  transcript_text: string;
  worm_object_key: string | null;
  is_flagged: boolean;
}

interface CoverageItem {
  beneficiary_id: string;
  recorded: string[];
  missing_items: string[];
  coverage_rate: number;
  is_complete: boolean;
}

interface DetailData {
  evidence: Record<string, unknown>;
  review_history: Array<{
    id: string;
    status: AngelStatus;
    reviewer_id: string | null;
    decision_note: string | null;
    created_at: string;
  }>;
  current_status: AngelStatus | null;
}

interface BatchHistoryItem {
  id: string;
  facility_id: string;
  status: string;
  item_count: number;
  zip_sha256: string;
  exported_by: string;
  created_at: string;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001";

const STATUS_LABELS: Record<AngelStatus, string> = {
  DETECTED: "감지됨",
  REVIEW_REQUIRED: "검수 대기",
  APPROVED_FOR_EXPORT: "승인 완료",
  REJECTED: "반영 제외",
  RECLASSIFIED: "분류 정정",
  EXPORTED: "내보내기 완료",
};

const STATUS_COLORS: Record<AngelStatus, string> = {
  DETECTED: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  REVIEW_REQUIRED: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  APPROVED_FOR_EXPORT: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  REJECTED: "bg-red-500/20 text-red-400 border-red-500/30",
  RECLASSIFIED: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  EXPORTED: "bg-gray-500/20 text-gray-400 border-gray-500/30",
};

const CARE_6 = ["식사 보조", "배변 보조", "체위 변경", "구강 위생", "목욕 보조", "이동 보조"];

/* ═══════════════════════════════════════════════════════════════════
   메인 컴포넌트: 반영 대기함 (Pending Vault)

   레이아웃:
   ┌─────────────────────┬──────────────────────────┐
   │  좌측 사이드바        │  우측 상세 패널            │
   │  ┌─────────────┐    │  ┌──────────────────────┐ │
   │  │ 6대항목 누락  │    │  │ 증거 원본 정보        │ │
   │  │ 경고 위젯    │    │  │ (해시/WORM/원음)      │ │
   │  └─────────────┘    │  ├──────────────────────┤ │
   │  ┌─────────────┐    │  │ 판정 이력 타임라인     │ │
   │  │ 대기 건 목록  │    │  ├──────────────────────┤ │
   │  │ (필터 탭)    │    │  │ 판정 버튼              │ │
   │  │              │    │  │ [승인][보류][제외][정정]│ │
   │  └─────────────┘    │  └──────────────────────┘ │
   └─────────────────────┴──────────────────────────┘
   ═══════════════════════════════════════════════════════════════════ */

export default function PendingVaultPanel() {
  const [items, setItems] = useState<PendingItem[]>([]);
  const [coverage, setCoverage] = useState<CoverageItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DetailData | null>(null);
  const [statusFilter, setStatusFilter] = useState<AngelStatus | "ALL">("ALL");
  const [loading, setLoading] = useState(false);

  // ── 대기 건 목록 로드 ──────────────────────────────────────────
  const fetchPending = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (statusFilter !== "ALL") params.set("status_filter", statusFilter);
      const res = await fetch(`${API_BASE}/api/v2/angel/pending?${params}`);
      const data = await res.json();
      setItems(data.items || []);
    } catch (e) {
      console.error("[PendingVault] 목록 로드 실패:", e);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  // ── 6대항목 커버리지 로드 ──────────────────────────────────────
  const fetchCoverage = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/coverage`);
      const data = await res.json();
      setCoverage(data.beneficiaries || []);
    } catch (e) {
      console.error("[PendingVault] 커버리지 로드 실패:", e);
    }
  }, []);

  // ── 상세 로드 ──────────────────────────────────────────────────
  const fetchDetail = useCallback(async (ledgerId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/detail/${ledgerId}`);
      const data = await res.json();
      setDetail(data);
    } catch (e) {
      console.error("[PendingVault] 상세 로드 실패:", e);
    }
  }, []);

  useEffect(() => { fetchPending(); }, [fetchPending]);
  useEffect(() => { fetchCoverage(); }, [fetchCoverage]);
  useEffect(() => {
    if (selectedId) fetchDetail(selectedId);
    else setDetail(null);
  }, [selectedId, fetchDetail]);

  // ── 판정 제출 ──────────────────────────────────────────────────
  const submitReview = async (
    decision: AngelStatus,
    note: string = "",
    reclassifiedTo?: string,
  ) => {
    if (!selectedId) return;
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ledger_id: selectedId,
          decision,
          reviewer_id: "admin",  // TODO: 실제 인증 연동
          note,
          reclassified_to: reclassifiedTo || null,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(`판정 실패: ${err.detail}`);
        return;
      }
      // 새로고침
      await fetchPending();
      await fetchDetail(selectedId);
    } catch (e) {
      console.error("[PendingVault] 판정 실패:", e);
    }
  };

  // ── Export ZIP 다운로드 ─────────────────────────────────────────
  const [exporting, setExporting] = useState(false);
  const [lastBatchId, setLastBatchId] = useState<string | null>(null);

  const handleExportZip = async () => {
    setExporting(true);
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/export/zip`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          facility_id: items[0]?.facility_id || "default",
          exported_by: "admin",
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(`Export 실패: ${err.detail}`);
        return;
      }
      const batchId = res.headers.get("X-Batch-Id") || "";
      const zipSha = res.headers.get("X-Zip-SHA256") || "";
      setLastBatchId(batchId);

      // 파일 다운로드 트리거
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `voiceguard_export_${batchId.slice(0, 8)}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      // 목록 새로고침
      await fetchPending();
      alert(`Export 완료!\nBatch: ${batchId.slice(0, 8)}\nZIP SHA-256: ${zipSha.slice(0, 16)}...`);
    } catch (e) {
      console.error("[PendingVault] Export 실패:", e);
      alert("Export 중 오류가 발생했습니다.");
    } finally {
      setExporting(false);
    }
  };

  // ── RPA 시작 ───────────────────────────────────────────────────
  const [rpaStarting, setRpaStarting] = useState(false);

  const handleRpaStart = async () => {
    if (!lastBatchId) {
      alert("먼저 Export ZIP을 생성하세요.");
      return;
    }
    setRpaStarting(true);
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/rpa/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_id: lastBatchId,
          bot_id: "playwright-bot-1",
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(`RPA 시작 실패: ${err.detail}`);
        return;
      }
      alert(`RPA 시작됨! Batch: ${lastBatchId.slice(0, 8)}\n엔젤시스템 자동 반영 진행 중...`);
    } catch (e) {
      console.error("[PendingVault] RPA 시작 실패:", e);
    } finally {
      setRpaStarting(false);
    }
  };

  // ── 배치 이력 ─────────────────────────────────────────────────
  const [showBatchHistory, setShowBatchHistory] = useState(false);
  const [batchHistory, setBatchHistory] = useState<BatchHistoryItem[]>([]);

  const fetchBatchHistory = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v2/angel/export/batches?limit=20`);
      const data = await res.json();
      setBatchHistory(data.batches || []);
      setShowBatchHistory(true);
    } catch (e) {
      console.error("[PendingVault] 배치 이력 로드 실패:", e);
    }
  };

  // ── 누락 경고 카운트 ───────────────────────────────────────────
  const totalMissing = coverage.reduce((s, c) => s + c.missing_items.length, 0);
  const approvedCount = items.filter((i) => i.angel_status === "APPROVED_FOR_EXPORT").length;

  return (
    <div className="flex h-full gap-4 p-4 bg-gray-950 text-gray-100">

      {/* ══ 좌측 사이드바 ══════════════════════════════════════════ */}
      <div className="w-[380px] flex-shrink-0 flex flex-col gap-4">

        {/* ── 6대항목 누락 경고 위젯 ─────────────────────────────── */}
        <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-bold text-red-400">
              6대 필수항목 누락 감시
            </h3>
            <span className="text-xs px-2 py-0.5 rounded-full bg-red-500/20 text-red-300">
              {totalMissing}건 누락
            </span>
          </div>
          <div className="space-y-1.5 max-h-[160px] overflow-y-auto">
            {coverage
              .filter((c) => !c.is_complete)
              .map((c) => (
                <div
                  key={c.beneficiary_id}
                  className="flex items-center justify-between text-xs py-1 px-2 rounded bg-gray-900/50"
                >
                  <span className="text-gray-300 truncate max-w-[120px]">
                    {c.beneficiary_id}
                  </span>
                  <div className="flex gap-1 flex-wrap justify-end">
                    {c.missing_items.map((item) => (
                      <span
                        key={item}
                        className="px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 text-[10px]"
                      >
                        {item}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            {coverage.every((c) => c.is_complete) && (
              <p className="text-xs text-emerald-400 text-center py-2">
                전체 수급자 6대항목 충족
              </p>
            )}
          </div>
        </div>

        {/* ── 상태 필터 탭 ───────────────────────────────────────── */}
        <div className="flex gap-1 flex-wrap">
          {(["ALL", "DETECTED", "REVIEW_REQUIRED", "APPROVED_FOR_EXPORT", "REJECTED"] as const).map(
            (s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`text-[11px] px-2 py-1 rounded border transition-colors ${
                  statusFilter === s
                    ? "bg-blue-600 border-blue-500 text-white"
                    : "bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500"
                }`}
              >
                {s === "ALL" ? "전체" : STATUS_LABELS[s]}
              </button>
            ),
          )}
        </div>

        {/* ── 대기 건 리스트 ──────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto space-y-1.5">
          {loading && <p className="text-xs text-gray-500 text-center py-4">로딩 중...</p>}
          {!loading && items.length === 0 && (
            <p className="text-xs text-gray-500 text-center py-4">대기 건 없음</p>
          )}
          {items.map((item) => (
            <button
              key={item.ledger_id}
              onClick={() => setSelectedId(item.ledger_id)}
              className={`w-full text-left p-3 rounded-lg border transition-colors ${
                selectedId === item.ledger_id
                  ? "border-blue-500 bg-blue-500/10"
                  : "border-gray-800 bg-gray-900 hover:border-gray-600"
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-gray-400 font-mono">
                  {item.ledger_id.slice(0, 8)}...
                </span>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded border ${
                    STATUS_COLORS[item.angel_status]
                  }`}
                >
                  {STATUS_LABELS[item.angel_status]}
                </span>
              </div>
              <div className="text-xs text-gray-300">
                {item.beneficiary_id} &middot; {item.care_type || "미분류"}
              </div>
              <div className="text-[10px] text-gray-500 mt-0.5">
                {new Date(item.ingested_at).toLocaleString("ko-KR")}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* ══ 우측 상세 패널 ══════════════════════════════════════════ */}
      <div className="flex-1 flex flex-col gap-4 min-w-0">
        {!detail ? (
          <div className="flex-1 flex items-center justify-center text-gray-600 text-sm">
            좌측에서 건을 선택하세요
          </div>
        ) : (
          <>
            {/* ── 증거 원본 카드 ──────────────────────────────────── */}
            <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
              <h3 className="text-sm font-bold text-gray-200 mb-3">
                증거 원본 정보
              </h3>
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
                <InfoRow label="수급자" value={String(detail.evidence.beneficiary_id)} />
                <InfoRow label="급여유형" value={String(detail.evidence.care_type || "미분류")} />
                <InfoRow label="기관" value={String(detail.evidence.facility_id)} />
                <InfoRow label="교대" value={String(detail.evidence.shift_id)} />
                <InfoRow
                  label="기록시각"
                  value={new Date(String(detail.evidence.recorded_at)).toLocaleString("ko-KR")}
                />
                <InfoRow
                  label="오디오 크기"
                  value={`${detail.evidence.audio_size_kb}KB`}
                />
              </div>

              {/* 해시 증빙 배지 */}
              <div className="mt-3 flex flex-wrap gap-2">
                <HashBadge
                  label="Audio SHA-256"
                  hash={String(detail.evidence.audio_sha256)}
                />
                <HashBadge
                  label="Chain Hash"
                  hash={String(detail.evidence.chain_hash)}
                />
                {detail.evidence.worm_object_key && (
                  <span className="text-[10px] px-2 py-1 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    WORM: {String(detail.evidence.worm_object_key)}
                  </span>
                )}
              </div>

              {/* 원음 재생 placeholder */}
              <div className="mt-3 p-2 rounded bg-gray-800 border border-gray-700">
                <div className="flex items-center gap-2">
                  <button className="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors">
                    ▶ 원음 재생 (10초)
                  </button>
                  <span className="text-[10px] text-gray-500">
                    {String(detail.evidence.worm_object_key || "로컬 오디오")}
                  </span>
                </div>
              </div>

              {/* 음성 텍스트 */}
              {detail.evidence.transcript_text && (
                <div className="mt-2 p-2 rounded bg-gray-800 text-xs text-gray-300 max-h-[80px] overflow-y-auto">
                  {String(detail.evidence.transcript_text)}
                </div>
              )}
            </div>

            {/* ── 판정 이력 타임라인 ─────────────────────────────── */}
            <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 flex-1 overflow-y-auto">
              <h3 className="text-sm font-bold text-gray-200 mb-3">
                판정 이력
              </h3>
              <div className="space-y-2">
                {detail.review_history.map((evt, i) => (
                  <div key={evt.id} className="flex items-start gap-3">
                    <div className="flex flex-col items-center">
                      <div
                        className={`w-2.5 h-2.5 rounded-full ${
                          i === detail.review_history.length - 1
                            ? "bg-blue-500"
                            : "bg-gray-600"
                        }`}
                      />
                      {i < detail.review_history.length - 1 && (
                        <div className="w-px h-6 bg-gray-700" />
                      )}
                    </div>
                    <div className="text-xs">
                      <span
                        className={`px-1.5 py-0.5 rounded border ${
                          STATUS_COLORS[evt.status]
                        }`}
                      >
                        {STATUS_LABELS[evt.status]}
                      </span>
                      <span className="text-gray-500 ml-2">
                        {new Date(evt.created_at).toLocaleString("ko-KR")}
                      </span>
                      {evt.reviewer_id && (
                        <span className="text-gray-400 ml-2">by {evt.reviewer_id}</span>
                      )}
                      {evt.decision_note && (
                        <p className="text-gray-400 mt-0.5">{evt.decision_note}</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* ── 판정 액션 버튼 바 ──────────────────────────────── */}
            <div className="rounded-lg border border-gray-800 bg-gray-900 p-3">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => submitReview("APPROVED_FOR_EXPORT", "관리자 승인")}
                  disabled={detail.current_status === "EXPORTED"}
                  className="px-4 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold transition-colors disabled:opacity-30"
                >
                  승인
                </button>
                <button
                  onClick={() => submitReview("REVIEW_REQUIRED", "보류 — 추가 확인 필요")}
                  disabled={detail.current_status === "EXPORTED"}
                  className="px-4 py-2 rounded bg-amber-600 hover:bg-amber-500 text-white text-xs font-bold transition-colors disabled:opacity-30"
                >
                  보류
                </button>
                <button
                  onClick={() => submitReview("REJECTED", "반영 제외")}
                  disabled={detail.current_status === "EXPORTED"}
                  className="px-4 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-xs font-bold transition-colors disabled:opacity-30"
                >
                  반영 제외
                </button>
                <button
                  onClick={() => {
                    const newType = prompt("새 급여유형을 입력하세요 (예: 식사 보조)");
                    if (newType) submitReview("RECLASSIFIED", `분류 정정 → ${newType}`, newType);
                  }}
                  disabled={detail.current_status === "EXPORTED"}
                  className="px-4 py-2 rounded bg-purple-600 hover:bg-purple-500 text-white text-xs font-bold transition-colors disabled:opacity-30"
                >
                  분류 정정
                </button>

                {/* 현재 상태 표시 */}
                {detail.current_status && (
                  <span
                    className={`ml-auto text-[11px] px-2 py-1 rounded border ${
                      STATUS_COLORS[detail.current_status]
                    }`}
                  >
                    현재: {STATUS_LABELS[detail.current_status]}
                  </span>
                )}
              </div>
            </div>
          </>
        )}

        {/* ══ 하단 Export 바 ═══════════════════════════════════════ */}
        <div className="rounded-lg border border-cyan-500/30 bg-cyan-500/5 p-3 mt-auto">
          <div className="flex items-center justify-between">
            <div className="text-xs">
              <span className="text-cyan-400 font-bold">
                증빙 팩 포함 엔젤 Export
              </span>
              <span className="text-gray-500 ml-2">
                승인 완료 {approvedCount}건
              </span>
              {lastBatchId && (
                <span className="text-gray-600 ml-2">
                  마지막 배치: {lastBatchId.slice(0, 8)}
                </span>
              )}
            </div>
            <button
              onClick={handleExportZip}
              disabled={exporting || approvedCount === 0}
              className={`px-5 py-2 rounded text-xs font-bold transition-colors ${
                approvedCount > 0
                  ? "bg-cyan-600 hover:bg-cyan-500 text-white"
                  : "bg-gray-700 text-gray-500 cursor-not-allowed"
              } disabled:opacity-50`}
            >
              {exporting
                ? "Export 중..."
                : `Export ZIP (${approvedCount}건)`}
            </button>
          </div>
          <div className="flex items-center gap-2 mt-2">
            <button
              onClick={handleRpaStart}
              disabled={rpaStarting || !lastBatchId}
              className={`px-4 py-2 rounded text-xs font-bold transition-colors ${
                lastBatchId
                  ? "bg-orange-600 hover:bg-orange-500 text-white"
                  : "bg-gray-700 text-gray-500 cursor-not-allowed"
              } disabled:opacity-50`}
            >
              {rpaStarting
                ? "RPA 실행 중..."
                : "엔젤 시스템 자동 반영 (RPA 시작)"}
            </button>
            <button
              onClick={fetchBatchHistory}
              className="px-3 py-2 rounded text-xs border border-gray-600 text-gray-400 hover:border-gray-400 hover:text-gray-200 transition-colors"
            >
              배치 이력
            </button>
          </div>
          <p className="text-[10px] text-gray-600 mt-1">
            angel_import.csv + proof_manifest.csv + export_receipt.json
          </p>
        </div>

        {/* ══ 배치 이력 모달 ═══════════════════════════════════════ */}
        {showBatchHistory && (
          <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
            <div className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-3xl max-h-[70vh] overflow-hidden flex flex-col">
              <div className="flex items-center justify-between p-4 border-b border-gray-800">
                <h3 className="text-sm font-bold text-gray-200">
                  Export 배치 & RPA 반영 이력
                </h3>
                <button
                  onClick={() => setShowBatchHistory(false)}
                  className="text-gray-500 hover:text-gray-300 text-lg"
                >
                  x
                </button>
              </div>
              <div className="flex-1 overflow-y-auto p-4">
                {batchHistory.length === 0 ? (
                  <p className="text-xs text-gray-500 text-center py-8">이력 없음</p>
                ) : (
                  <div className="space-y-2">
                    {batchHistory.map((b) => (
                      <div
                        key={b.id}
                        className="p-3 rounded-lg border border-gray-800 bg-gray-950"
                      >
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-xs font-mono text-gray-400">
                            {b.id.slice(0, 8)}...
                          </span>
                          <BatchStatusBadge status={b.status} />
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-[11px] text-gray-400">
                          <span>기관: {b.facility_id}</span>
                          <span>건수: {b.item_count}</span>
                          <span>
                            {new Date(b.created_at).toLocaleString("ko-KR")}
                          </span>
                        </div>
                        <div className="text-[10px] text-gray-600 mt-1 truncate">
                          ZIP: {b.zip_sha256?.slice(0, 24)}...
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   서브 컴포넌트
   ═══════════════════════════════════════════════════════════════════ */

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200 ml-2">{value}</span>
    </div>
  );
}

const BATCH_STATUS_STYLES: Record<string, string> = {
  CREATED: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  DOWNLOADED: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  RPA_IN_PROGRESS: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  APPLIED_CONFIRMED: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  APPLY_FAILED: "bg-red-500/20 text-red-400 border-red-500/30",
};

function BatchStatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded border ${
        BATCH_STATUS_STYLES[status] || "bg-gray-500/20 text-gray-400 border-gray-500/30"
      }`}
    >
      {status}
    </span>
  );
}

function HashBadge({ label, hash }: { label: string; hash: string }) {
  const isPending = hash.length === 64 && hash.startsWith("pending") === false;
  return (
    <span
      className={`text-[10px] px-2 py-1 rounded border ${
        isPending
          ? "bg-cyan-500/10 text-cyan-400 border-cyan-500/20"
          : "bg-gray-700/50 text-gray-500 border-gray-600"
      }`}
      title={hash}
    >
      {label}: {hash.slice(0, 12)}...
    </span>
  );
}
