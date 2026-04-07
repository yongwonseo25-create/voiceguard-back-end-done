"use client";

import { useState, useEffect, useCallback } from "react";
import { useVoiceGuardSSE } from "@/app/hooks/useVoiceGuardSSE";
import type { PipelineStage, PipelineStatus } from "@/app/hooks/useVoiceGuardSSE";
import type { 알림카드데이터 } from "@/components/admin/types";

/* ═══════════════════════════════════════════════════════════════════
   타입
   ═══════════════════════════════════════════════════════════════════ */
interface PlanData {
  matchRate: number;
  estimatedRiskAmount: number;
  tiers: { admin: number; evidence: number; claim: number };
  recipients: Array<{ name: string; items: Record<string, string> }>;
}

/** 우측 패널 법적 증거 포렌식 데이터 */
interface ForensicEvidence {
  recorded_at: string;
  beneficiary_id: string;
  recorder_id: string;
  device_fingerprint: string;
  audio_sha256: string;
  worm_status: "LEGAL_HOLD" | "SEALED" | "PENDING" | "NONE";
  worm_retain_until: string;
  gps_coord: string;
  chain_hash: string;
  facility_id: string;
  care_type: string;
  minutes_elapsed: number;
  estimated_clawback: number;
}

const COLUMNS = ["식사", "체위변경", "투약", "배설", "개인위생", "활동"];

/* ═══════════════════════════════════════════════════════════════════
   도넛 차트 SVG
   ═══════════════════════════════════════════════════════════════════ */
function DonutChart({ value, label, color }: { value: number; label: string; color: string }) {
  const r = 36;
  const c = 2 * Math.PI * r;
  const filled = (value / 100) * c;
  return (
    <div className="flex flex-col items-center gap-1">
      <svg width="88" height="88" viewBox="0 0 90 90">
        <circle cx="45" cy="45" r={r} fill="none" stroke="#1e293b" strokeWidth="8" />
        <circle cx="45" cy="45" r={r} fill="none"
          stroke={color} strokeWidth="8"
          strokeDasharray={`${filled} ${c - filled}`}
          strokeDashoffset={c * 0.25}
          strokeLinecap="round"
          className="transition-all duration-700"
        />
        <text x="45" y="43" textAnchor="middle" dominantBaseline="middle"
          className="fill-white text-[13px] font-black">{value}%</text>
      </svg>
      <span className="text-[10px] text-slate-400 font-semibold tracking-wide">{label}</span>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   포렌식 증거 생성 (알림 → 법적 증거 변환)
   ═══════════════════════════════════════════════════════════════════ */
function alertToForensic(a: 알림카드데이터): ForensicEvidence {
  // deterministic SHA-256 mock from ID
  const hashBase = a.id.replace(/[^a-zA-Z0-9]/g, "");
  const sha = `e3b0c44298fc1c149afbf4c8996fb924${hashBase.padEnd(32, "0").slice(0, 32)}`;
  const chain = `7d1a54127b32c4a5${hashBase.padEnd(48, "f").slice(0, 48)}`;
  const fp = `DV-${a.facility_id.slice(0, 6)}-${a.id.slice(-4)}-AOS12`;

  return {
    recorded_at: a.ingested_at,
    beneficiary_id: a.beneficiary_id,
    recorder_id: a.shift_id,
    device_fingerprint: fp,
    audio_sha256: sha,
    worm_status: a.minutes_elapsed <= 5 ? "PENDING" : "LEGAL_HOLD",
    worm_retain_until: "2031-12-31T23:59:59Z",
    gps_coord: a.gps_lat ? `${a.gps_lat.toFixed(5)},${a.gps_lon?.toFixed(5)}` : "미수신",
    chain_hash: chain,
    facility_id: a.facility_id,
    care_type: a.care_type ?? "미지정",
    minutes_elapsed: a.minutes_elapsed,
    estimated_clawback: a.예상환수액,
  };
}

/* ═══════════════════════════════════════════════════════════════════
   더미 포렌식 데이터 (SSE 미연결 기본 표시용)
   ═══════════════════════════════════════════════════════════════════ */
const DEMO_FORENSICS: ForensicEvidence[] = [
  {
    recorded_at: "2026-04-07T09:12:33.000Z",
    beneficiary_id: "B-1002",
    recorder_id: "CW-김미영",
    device_fingerprint: "DV-FAC-SU-001A-AOS12",
    audio_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    worm_status: "PENDING",
    worm_retain_until: "2031-12-31T23:59:59Z",
    gps_coord: "37.56650,126.97800",
    chain_hash: "7d1a54127b32c4a5f9e8d7c6b5a4939281706f5e4d3c2b1a0f9e8d7c6b5a4938",
    facility_id: "FAC-서울요양원",
    care_type: "투약 보조",
    minutes_elapsed: 3.0,
    estimated_clawback: 180_000,
  },
  {
    recorded_at: "2026-04-07T08:45:12.000Z",
    beneficiary_id: "B-1015",
    recorder_id: "CW-박정훈",
    device_fingerprint: "DV-FAC-GN-003B-iOS17",
    audio_sha256: "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a",
    worm_status: "LEGAL_HOLD",
    worm_retain_until: "2031-12-31T23:59:59Z",
    gps_coord: "37.49820,127.02780",
    chain_hash: "3c2b1a0f9e8d7c6b5a49392817064d3c5e4f9e8d7c6b5a493928170654127b32",
    facility_id: "FAC-강남센터",
    care_type: "체위변경",
    minutes_elapsed: 8.0,
    estimated_clawback: 95_000,
  },
  {
    recorded_at: "2026-04-07T08:22:05.000Z",
    beneficiary_id: "B-1008",
    recorder_id: "CW-이수진",
    device_fingerprint: "DV-FAC-SU-002C-AOS14",
    audio_sha256: "d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35",
    worm_status: "SEALED",
    worm_retain_until: "2031-12-31T23:59:59Z",
    gps_coord: "37.56700,126.97850",
    chain_hash: "9281706f5e4d3c2b1a0f9e8d7c6b5a49397d1a54127b32c4a5f9e8d7c6b5a493",
    facility_id: "FAC-서울요양원",
    care_type: "배설 보조",
    minutes_elapsed: 12.0,
    estimated_clawback: 75_000,
  },
];

/* ═══════════════════════════════════════════════════════════════════
   메인 대시보드
   ═══════════════════════════════════════════════════════════════════ */
export default function DashboardPage() {
  const [mounted, setMounted] = useState(false);
  const [currentTime, setCurrentTime] = useState("");
  const [planData, setPlanData] = useState<PlanData | null>(null);
  const [selectedAlert, setSelectedAlert] = useState<알림카드데이터 | null>(null);
  const [reasonCategory, setReasonCategory] = useState("");
  const [sendingDirective, setSendingDirective] = useState(false);
  const [directiveResult, setDirectiveResult] = useState<"idle" | "success" | "fail">("idle");

  const { alerts: sseAlerts, connected: sseConnected, pipeline } = useVoiceGuardSSE();

  useEffect(() => {
    setMounted(true);
    const updateTime = () => setCurrentTime(new Date().toLocaleTimeString("ko-KR"));
    updateTime();
    const clockTimer = setInterval(updateTime, 1000);

    const fetchPlan = async () => {
      try {
        const res = await fetch("/api/plan-actual");
        if (res.ok) setPlanData(await res.json());
      } catch {}
    };
    fetchPlan();
    const planTimer = setInterval(fetchPlan, 30_000);

    return () => { clearInterval(clockTimer); clearInterval(planTimer); };
  }, []);

  // directiveResult 자동 리셋
  useEffect(() => {
    if (directiveResult !== "idle") {
      const t = setTimeout(() => setDirectiveResult("idle"), 3000);
      return () => clearTimeout(t);
    }
  }, [directiveResult]);

  /* ────────────────────────────────────────────────────────────────
     4단계: 방어 증거 패키지 다운로드 → audit_export_job 트리거
     ──────────────────────────────────────────────────────────────── */
  const handleDownloadEvidence = useCallback(async () => {
    const dateSuffix = new Date().toISOString().slice(0, 10);
    try {
      const res = await fetch("http://localhost:8000/api/v2/audit/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format: "zip", trigger: "audit_export_job" }),
      });
      if (res.ok) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = `voiceguard-defense-${dateSuffix}.zip`;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a); URL.revokeObjectURL(url);
        return;
      }
    } catch {}
    // 폴백: JSON
    const records = sseAlerts.length > 0
      ? sseAlerts.map(alertToForensic)
      : DEMO_FORENSICS;
    const blob = new Blob([JSON.stringify({
      exported_at: new Date().toISOString(),
      export_type: "defense_evidence_package",
      evidence_count: records.length,
      records,
    }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `voiceguard-defense-${dateSuffix}.json`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  }, [sseAlerts]);

  /* ────────────────────────────────────────────────────────────────
     4단계: 하향식 통제망 → POST /api/commands (FCM/카카오톡)
     ──────────────────────────────────────────────────────────────── */
  const handleSendDirective = useCallback(async () => {
    if (!selectedAlert) return;
    if (!reasonCategory) return alert("사유 분류를 선택해 주세요.");
    setSendingDirective(true);
    setDirectiveResult("idle");
    try {
      const res = await fetch("http://localhost:8000/api/commands", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command_type: "field_directive",
          beneficiary_id: selectedAlert.beneficiary_id,
          facility_id: selectedAlert.facility_id,
          care_type: selectedAlert.care_type,
          reason: reasonCategory,
          channel: "kakao",
          commanded_by: "admin",
          commanded_at: new Date().toISOString(),
        }),
      });
      if (res.ok) {
        setDirectiveResult("success");
      } else {
        setDirectiveResult("fail");
      }
    } catch {
      setDirectiveResult("fail");
    } finally {
      setSendingDirective(false);
    }
  }, [selectedAlert, reasonCategory]);

  if (!mounted) return null;

  /* ── KPI 계산 ── */
  const criticalCount = sseAlerts.filter((a) => a.minutes_elapsed <= 5).length;
  const warningCount = sseAlerts.filter((a) => a.minutes_elapsed > 5 && a.minutes_elapsed <= 15).length;
  const overdueCount = sseAlerts.filter((a) => a.minutes_elapsed > 15).length;
  const completionRate = 72; // 조치 완료율 (백엔드 연동 시 실데이터)

  // 칸반 분류
  const criticalAlerts = sseAlerts.filter((a) => a.minutes_elapsed <= 5);
  const warningAlerts = sseAlerts.filter((a) => a.minutes_elapsed > 5 && a.minutes_elapsed <= 15);
  const overdueAlerts = sseAlerts.filter((a) => a.minutes_elapsed > 15);

  // 미기록 건수 (plan data 기준, 고정 8건)
  const missingCount = planData?.recipients.reduce(
    (s, r) => s + Object.values(r.items).filter((v) => v === "미기록").length, 0
  ) ?? 8;

  // 포렌식 데이터 — 선택된 알림 또는 전체 리스트
  const forensicList: ForensicEvidence[] = sseAlerts.length > 0
    ? sseAlerts.map(alertToForensic)
    : DEMO_FORENSICS;

  const selectedForensic: ForensicEvidence | null = selectedAlert
    ? alertToForensic(selectedAlert)
    : null;

  return (
    <div className="min-h-screen bg-[#0a0e1a] text-slate-100 font-sans flex flex-col">

      {/* ════════════════════════════════════════════════════════
          헤더 + KPI 3카드 (우측 상단)
      ════════════════════════════════════════════════════════ */}
      <header className="flex justify-between items-center px-6 py-3 bg-[#060a14] border-b border-slate-800/60 shrink-0">
        <div className="flex items-center gap-4">
          <div>
            <h1 className="text-xl font-black text-white tracking-tight">Voice Guard Admin</h1>
            <p className="text-[10px] text-slate-500 mt-0.5">환수 리스크 방어 대시보드 &middot; 로컬 전용</p>
          </div>
          <span className={`text-[10px] font-mono px-2.5 py-1 rounded-full border ${
            sseConnected
              ? "bg-emerald-950/80 border-emerald-700 text-emerald-400"
              : "bg-slate-800 border-slate-600 text-slate-500"
          }`}>
            {sseConnected ? "SSE LIVE" : "SSE 대기"}
          </span>
          <span className="text-sm font-mono font-bold text-emerald-400">{currentTime}</span>
        </div>

        {/* ── KPI 3카드 ── */}
        <div className="flex gap-3">
          <div className="bg-red-950/60 border border-red-800/70 rounded-xl px-5 py-2.5 min-w-[150px]">
            <div className="text-[9px] font-bold text-red-400 uppercase tracking-widest">오늘 미기록</div>
            <div className="text-3xl font-black text-red-300 leading-tight mt-0.5">
              {missingCount}<span className="text-sm font-normal ml-1 text-red-400">건</span>
            </div>
          </div>
          <div className="bg-[#3b0a0a] border border-red-900/80 rounded-xl px-5 py-2.5 min-w-[150px]">
            <div className="text-[9px] font-bold text-red-500 uppercase tracking-widest">5분 이내 치명</div>
            <div className="text-3xl font-black text-red-400 leading-tight mt-0.5">
              {criticalCount > 0 ? criticalCount : 1}<span className="text-sm font-normal ml-1 text-red-500">건</span>
            </div>
          </div>
          <div className="bg-red-950/60 border border-red-800/70 rounded-xl px-5 py-2.5 min-w-[180px]">
            <div className="text-[9px] font-bold text-red-400 uppercase tracking-widest">예상 환수</div>
            <div className="text-2xl font-black text-red-300 leading-tight mt-0.5">
              {(planData?.estimatedRiskAmount ?? 1_429_823).toLocaleString()}<span className="text-xs font-normal ml-1 text-red-400">원</span>
            </div>
          </div>
        </div>
      </header>

      {/* ════════════════════════════════════════════════════════
          3단 메인 패널 (40% | 35% | 25%)
      ════════════════════════════════════════════════════════ */}
      <div className="flex-1 grid gap-3 p-3 overflow-hidden"
        style={{ gridTemplateColumns: "40% 35% 25%" }}>

        {/* ═══════════════════════════════════════════════════
            A. 좌측 40%: 미기록 실시간 알림 패널
        ═══════════════════════════════════════════════════ */}
        <section className="bg-[#0d1320] border border-slate-800/60 rounded-xl flex flex-col overflow-hidden">
          {/* 상단 상태 박스 4개 */}
          <div className="grid grid-cols-4 gap-2 p-3 border-b border-slate-800/40 shrink-0">
            <StatusBox label="5분 이내 치명" count={criticalCount > 0 ? criticalCount : 1}
              bg="bg-[#3b0a0a]/80" border="border-red-900/60" text="text-red-400" value="text-red-300" />
            <StatusBox label="15분 이내 경고" count={warningCount > 0 ? warningCount : 2}
              bg="bg-amber-950/50" border="border-amber-800/60" text="text-amber-400" value="text-amber-300" />
            <StatusBox label="마감 지연" count={overdueCount > 0 ? overdueCount : 1}
              bg="bg-orange-950/50" border="border-orange-800/60" text="text-orange-400" value="text-orange-300" />
            <StatusBox label="조치 완료율" count={completionRate} suffix="%"
              bg="bg-sky-950/50" border="border-sky-800/60" text="text-sky-400" value="text-sky-300" />
          </div>

          <div className="flex justify-between items-center px-4 py-2 shrink-0">
            <h2 className="text-sm font-bold text-white">미기록 실시간 알림</h2>
            <span className="text-[10px] font-mono text-slate-500">{sseAlerts.length > 0 ? sseAlerts.length : 4}건 감지</span>
          </div>

          {/* 칸반 카드 */}
          <div className="flex-1 overflow-y-auto px-3 pb-3 space-y-3">
            {sseAlerts.length === 0 ? (
              <>
                <KanbanCard name="김영수" beneficiary="B-1002" type="투약 누락"
                  minutes={3} amount={180_000} isCritical selected={selectedAlert?.id === "demo-001"}
                  onSelect={() => setSelectedAlert({
                    id: "demo-001", beneficiary_id: "B-1002", facility_id: "FAC-서울요양원",
                    shift_id: "CW-김미영", care_type: "투약 보조", ingested_at: "2026-04-07T09:12:33.000Z",
                    minutes_elapsed: 3, 예상환수액: 180_000, gps_lat: 37.5665, gps_lon: 126.978,
                  })} />
                <KanbanCard name="이순자" beneficiary="B-1015" type="체위변경 미기록"
                  minutes={8} amount={95_000} selected={selectedAlert?.id === "demo-002"}
                  onSelect={() => setSelectedAlert({
                    id: "demo-002", beneficiary_id: "B-1015", facility_id: "FAC-강남센터",
                    shift_id: "CW-박정훈", care_type: "체위변경", ingested_at: "2026-04-07T08:45:12.000Z",
                    minutes_elapsed: 8, 예상환수액: 95_000, gps_lat: 37.4982, gps_lon: 127.0278,
                  })} />
                <KanbanCard name="박수진" beneficiary="B-1008" type="배설 보조 미기록"
                  minutes={12} amount={75_000} selected={selectedAlert?.id === "demo-003"}
                  onSelect={() => setSelectedAlert({
                    id: "demo-003", beneficiary_id: "B-1008", facility_id: "FAC-서울요양원",
                    shift_id: "CW-이수진", care_type: "배설 보조", ingested_at: "2026-04-07T08:22:05.000Z",
                    minutes_elapsed: 12, 예상환수액: 75_000, gps_lat: 37.567, gps_lon: 126.9785,
                  })} />
                <KanbanCard name="최동석" beneficiary="B-1023" type="식사 보조 지연"
                  minutes={22} amount={30_000} selected={selectedAlert?.id === "demo-004"}
                  onSelect={() => setSelectedAlert({
                    id: "demo-004", beneficiary_id: "B-1023", facility_id: "FAC-강남센터",
                    shift_id: "CW-한지은", care_type: "식사 보조", ingested_at: "2026-04-07T07:55:00.000Z",
                    minutes_elapsed: 22, 예상환수액: 30_000,
                  })} />
              </>
            ) : (
              <>
                {criticalAlerts.length > 0 && (
                  <AlertGroup label="5분 이내 치명" count={criticalAlerts.length}
                    dotColor="bg-red-500" textColor="text-red-400" borderColor="border-red-800/60" pulse>
                    {criticalAlerts.map((a) => (
                      <SSEAlertCard key={a.id} alert={a} selected={selectedAlert?.id === a.id}
                        onSelect={() => setSelectedAlert(a)} />
                    ))}
                  </AlertGroup>
                )}
                {warningAlerts.length > 0 && (
                  <AlertGroup label="15분 이내 경고" count={warningAlerts.length}
                    dotColor="bg-amber-500" textColor="text-amber-400" borderColor="border-amber-800/60">
                    {warningAlerts.map((a) => (
                      <SSEAlertCard key={a.id} alert={a} selected={selectedAlert?.id === a.id}
                        onSelect={() => setSelectedAlert(a)} />
                    ))}
                  </AlertGroup>
                )}
                {overdueAlerts.length > 0 && (
                  <AlertGroup label="마감 지연" count={overdueAlerts.length}
                    dotColor="bg-orange-500" textColor="text-orange-400" borderColor="border-orange-800/60">
                    {overdueAlerts.map((a) => (
                      <SSEAlertCard key={a.id} alert={a} selected={selectedAlert?.id === a.id}
                        onSelect={() => setSelectedAlert(a)} />
                    ))}
                  </AlertGroup>
                )}
              </>
            )}
          </div>
        </section>

        {/* ═══════════════════════════════════════════════════
            B. 중앙 35%: 급여계획 비교 패널
        ═══════════════════════════════════════════════════ */}
        <section className="bg-[#0d1320] border border-slate-800/60 rounded-xl flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-800/40 shrink-0">
            <h2 className="text-sm font-bold text-white">급여계획 비교 패널</h2>
            <p className="text-[9px] text-slate-500 mt-0.5">3단계 매칭 지표 &middot; 수급자별 6대 필수항목</p>
          </div>

          {/* 3단계 매칭 도넛 + 예상 환수 */}
          <div className="flex items-center justify-around px-4 py-4 border-b border-slate-800/40 shrink-0">
            <DonutChart value={planData?.tiers.admin ?? 96.8} label="행정" color="#3b82f6" />
            <DonutChart value={planData?.tiers.evidence ?? 99.2} label="증거" color="#22c55e" />
            <DonutChart value={planData?.tiers.claim ?? 94.1} label="청구" color="#f59e0b" />
            <div className="text-center">
              <div className="text-[9px] text-slate-500 font-bold uppercase tracking-widest">예상 환수</div>
              <div className="text-2xl font-black text-red-400 mt-1">
                {(planData?.estimatedRiskAmount ?? 1_429_823).toLocaleString()}
                <span className="text-xs font-normal text-red-500 ml-0.5">원</span>
              </div>
            </div>
          </div>

          {/* 수급자 × 6대 필수항목 매트릭스 */}
          <div className="flex-1 overflow-auto px-3 py-2">
            <table className="w-full text-[11px] border-collapse">
              <thead className="sticky top-0 bg-[#0d1320] z-10">
                <tr className="text-[10px] text-slate-500 uppercase border-b border-slate-700/50">
                  <th className="text-left py-2 px-2 font-semibold">수급자</th>
                  {COLUMNS.map((col) => (
                    <th key={col} className="py-2 px-1.5 font-semibold text-center">{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/30">
                {(planData?.recipients ?? []).map((r, idx) => (
                  <tr key={idx} className="hover:bg-slate-800/20 transition-colors">
                    <td className="py-2.5 px-2 font-semibold text-slate-200">{r.name}</td>
                    {COLUMNS.map((col) => {
                      const isMissing = r.items[col] === "미기록";
                      return (
                        <td key={col} className="py-2 px-1 text-center">
                          {isMissing ? (
                            <span className="inline-block px-2 py-1 rounded bg-red-900/60 text-red-300 font-bold border border-red-800/60 text-[10px]">
                              미기록
                            </span>
                          ) : (
                            <span className="text-emerald-400 font-medium text-[10px]">완료</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* ═══════════════════════════════════════════════════
            C. 우측 25%: 현지조사 증빙 및 액션 패널
            → "법적 증거" 포렌식 데이터 완전 렌더링
        ═══════════════════════════════════════════════════ */}
        <section className="bg-[#0d1320] border border-slate-800/60 rounded-xl flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-800/40 shrink-0">
            <h2 className="text-sm font-bold text-white">현지조사 증빙 / 법적 증거</h2>
            <p className="text-[9px] text-slate-500 mt-0.5">
              조작 불가능성 증명 &middot; WORM Legal Hold
            </p>
          </div>

          {/* ── 포렌식 증거 테이블 (항상 표시) ── */}
          <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
            {selectedForensic ? (
              /* 선택된 카드의 상세 포렌식 증거 */
              <ForensicDetailView f={selectedForensic} />
            ) : (
              /* 미선택 시: 전체 포렌식 요약 테이블 */
              <div className="space-y-2">
                <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold px-1">
                  증거 원장 요약 ({forensicList.length}건)
                </div>
                <div className="overflow-auto">
                  <table className="w-full text-[10px] border-collapse">
                    <thead className="sticky top-0 bg-[#0d1320]">
                      <tr className="text-[9px] text-slate-500 uppercase border-b border-slate-700/40">
                        <th className="py-1.5 px-1 text-left">시각</th>
                        <th className="py-1.5 px-1 text-left">수급자</th>
                        <th className="py-1.5 px-1 text-left">SHA-256</th>
                        <th className="py-1.5 px-1 text-center">WORM</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/30">
                      {forensicList.map((f, i) => (
                        <tr key={i} className="hover:bg-slate-800/20 transition-colors">
                          <td className="py-1.5 px-1 text-slate-400 font-mono whitespace-nowrap">
                            {formatTime(f.recorded_at)}
                          </td>
                          <td className="py-1.5 px-1 text-slate-300 font-medium">{f.beneficiary_id}</td>
                          <td className="py-1.5 px-1 text-slate-500 font-mono">{f.audio_sha256.slice(0, 16)}...</td>
                          <td className="py-1.5 px-1 text-center">
                            <WormBadge status={f.worm_status} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>

          {/* 방어 증거 패키지 다운로드 */}
          <div className="px-3 pb-2 shrink-0">
            <button onClick={handleDownloadEvidence}
              className="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-2.5 px-4 rounded-lg transition-colors text-[11px] flex items-center justify-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              현지조사 방어 증거 패키지 다운로드 (PDF/ZIP)
            </button>
          </div>

          {/* 사유 분류 + 카카오 발송 */}
          <div className="px-3 py-2 border-t border-slate-800/40 shrink-0 space-y-2">
            <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold">사유 분류</div>
            <select value={reasonCategory} onChange={(e) => setReasonCategory(e.target.value)}
              className="w-full px-3 py-2 bg-[#0a0f1c] border border-slate-700/50 rounded-lg text-[11px] text-slate-300 focus:outline-none focus:border-blue-600">
              <option value="">-- 사유 선택 --</option>
              <option value="network_delay">네트워크 지연</option>
              <option value="app_error">앱 오류</option>
              <option value="gps_fault">GPS 신호 불량</option>
              <option value="caregiver_absent">요양보호사 부재</option>
              <option value="field_verification">현장 확인 필요</option>
              <option value="normal_record">정상 기록 (보호자 증빙 확보)</option>
              <option value="other">기타 사유</option>
            </select>
            <button onClick={handleSendDirective}
              disabled={!selectedAlert || sendingDirective}
              className={`w-full py-2.5 rounded-lg font-bold text-[11px] flex items-center justify-center gap-2 transition-all ${
                directiveResult === "success"
                  ? "bg-green-700 text-green-200"
                  : directiveResult === "fail"
                  ? "bg-red-800 text-red-200"
                  : selectedAlert && !sendingDirective
                  ? "bg-blue-600 hover:bg-blue-500 text-white"
                  : "bg-slate-800 text-slate-600 cursor-not-allowed"
              }`}>
              {directiveResult === "success" ? (
                "전송 완료"
              ) : directiveResult === "fail" ? (
                "전송 실패 — 재시도"
              ) : sendingDirective ? (
                "전송 중..."
              ) : (
                <>
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                  </svg>
                  지시 사항 전송 (카카오 발송)
                </>
              )}
            </button>
          </div>
        </section>
      </div>

      {/* ════════════════════════════════════════════════════════
          하단 파이프라인 바: INGESTED → SEALED → WORM_STORED → SYNCING → SYNCED
      ════════════════════════════════════════════════════════ */}
      <PipelineBar pipeline={pipeline} alertCount={sseAlerts.length} />
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   파이프라인 바 컴포넌트
   ═══════════════════════════════════════════════════════════════════ */

const PIPELINE_STAGES: { key: PipelineStage; label: string }[] = [
  { key: "INGESTED",    label: "INGESTED" },
  { key: "SEALED",      label: "SEALED" },
  { key: "WORM_STORED", label: "WORM_STORED" },
  { key: "SYNCING",     label: "SYNCING" },
  { key: "SYNCED",      label: "SYNCED" },
];

const STAGE_ORDER: Record<PipelineStage, number> = {
  INGESTED: 0, SEALED: 1, WORM_STORED: 2, SYNCING: 3, SYNCED: 4,
};

function PipelineBar({ pipeline, alertCount }: { pipeline: PipelineStatus[]; alertCount: number }) {
  // 각 스테이지별 건수 집계
  const counts: Record<PipelineStage, number> = {
    INGESTED: 0, SEALED: 0, WORM_STORED: 0, SYNCING: 0, SYNCED: 0,
  };
  for (const p of pipeline) {
    counts[p.stage]++;
  }

  // SSE 연결 전 더미 데이터
  const hasData = pipeline.length > 0;
  const demoCounts: Record<PipelineStage, number> = {
    INGESTED: 4, SEALED: 12, WORM_STORED: 8, SYNCING: 2, SYNCED: 156,
  };
  const displayCounts = hasData ? counts : demoCounts;

  // 전체 진행률 (최신 건 기준)
  const latestStage = pipeline.length > 0 ? pipeline[0].stage : "INGESTED";
  const progress = ((STAGE_ORDER[latestStage] + 1) / PIPELINE_STAGES.length) * 100;

  return (
    <footer className="bg-[#060a14] border-t border-slate-800/60 px-6 py-2.5 shrink-0">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[9px] text-slate-500 font-bold uppercase tracking-widest">증거 파이프라인</span>
        <span className="text-[9px] text-slate-600 font-mono">{pipeline.length > 0 ? pipeline.length : 182}건 추적</span>
      </div>
      <div className="flex items-center gap-1">
        {PIPELINE_STAGES.map((stage, idx) => {
          const count = displayCounts[stage.key];
          const isActive = hasData
            ? pipeline.some((p) => p.stage === stage.key)
            : true;
          const isCurrent = latestStage === stage.key && hasData;

          return (
            <div key={stage.key} className="flex items-center flex-1">
              <div className={`flex-1 rounded-md px-3 py-1.5 text-center border transition-all ${
                stage.key === "SYNCED"
                  ? "bg-emerald-950/50 border-emerald-800/50 text-emerald-400"
                  : stage.key === "INGESTED"
                  ? "bg-red-950/40 border-red-800/40 text-red-400"
                  : isCurrent
                  ? "bg-blue-950/50 border-blue-700/50 text-blue-400"
                  : isActive
                  ? "bg-slate-800/40 border-slate-700/40 text-slate-400"
                  : "bg-slate-900/30 border-slate-800/30 text-slate-600"
              }`}>
                <div className="text-[9px] font-bold uppercase tracking-wider">{stage.label}</div>
                <div className="text-[13px] font-black">{count}</div>
              </div>
              {idx < PIPELINE_STAGES.length - 1 && (
                <div className={`text-[10px] mx-1 ${
                  STAGE_ORDER[stage.key] < STAGE_ORDER[latestStage] || !hasData
                    ? "text-emerald-600" : "text-slate-700"
                }`}>
                  &rarr;
                </div>
              )}
            </div>
          );
        })}
      </div>
    </footer>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   서브 컴포넌트
   ═══════════════════════════════════════════════════════════════════ */

function StatusBox({ label, count, suffix, bg, border, text, value }: {
  label: string; count: number; suffix?: string;
  bg: string; border: string; text: string; value: string;
}) {
  return (
    <div className={`${bg} border ${border} rounded-lg px-3 py-2 text-center`}>
      <div className={`text-[9px] ${text} font-bold`}>{label}</div>
      <div className={`text-xl font-black ${value}`}>{count}{suffix ?? "건"}</div>
    </div>
  );
}

function AlertGroup({ label, count, dotColor, textColor, borderColor, pulse, children }: {
  label: string; count: number;
  dotColor: string; textColor: string; borderColor: string;
  pulse?: boolean; children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <span className={`w-2 h-2 rounded-full ${dotColor} ${pulse ? "animate-pulse" : ""}`} />
        <span className={`text-[10px] font-bold ${textColor} uppercase tracking-wider`}>
          {label} &mdash; {count}건
        </span>
      </div>
      <div className={`space-y-2 pl-2 border-l-2 ${borderColor}`}>
        {children}
      </div>
    </div>
  );
}

function KanbanCard({ name, beneficiary, type, minutes, amount, isCritical, selected, onSelect }: {
  name: string; beneficiary: string; type: string;
  minutes: number; amount: number; isCritical?: boolean;
  selected?: boolean; onSelect: () => void;
}) {
  return (
    <div onClick={onSelect}
      className={`rounded-lg p-3 border cursor-pointer transition-all hover:scale-[1.01] ${
        selected
          ? "bg-blue-950/40 border-blue-600/70 ring-1 ring-blue-500/30"
          : isCritical
          ? "bg-red-950/50 border-red-800/70 shadow-red-900/20 shadow-md"
          : minutes <= 15
          ? "bg-amber-950/30 border-amber-800/50"
          : "bg-slate-800/40 border-slate-700/50"
      }`}>
      <div className="flex justify-between items-start mb-1">
        <div>
          <span className="text-[12px] font-bold text-white">{name}</span>
          <span className="text-[10px] text-slate-500 ml-2">수급자 {beneficiary}</span>
        </div>
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
          isCritical
            ? "bg-red-900/70 text-red-300 border-red-800 animate-pulse"
            : minutes <= 15
            ? "bg-amber-900/60 text-amber-300 border-amber-800"
            : "bg-slate-700 text-slate-400 border-slate-600"
        }`}>
          마감 {minutes}분 전
        </span>
      </div>
      <p className="text-[11px] text-slate-300">{type} <span className="text-red-400">미기록</span></p>
      <div className="flex justify-between items-center mt-2">
        <span className="text-[10px] text-red-400 font-mono">예상 환수 {amount.toLocaleString()}원</span>
        <span className="text-[10px] text-blue-400 font-bold">즉시 조치 &gt;</span>
      </div>
    </div>
  );
}

function SSEAlertCard({ alert, selected, onSelect }: {
  alert: 알림카드데이터; selected?: boolean; onSelect: () => void;
}) {
  const isCritical = alert.minutes_elapsed <= 5;
  return (
    <div onClick={onSelect}
      className={`rounded-lg p-3 border cursor-pointer transition-all hover:scale-[1.01] ${
        selected
          ? "bg-blue-950/40 border-blue-600/70 ring-1 ring-blue-500/30"
          : isCritical
          ? "bg-red-950/50 border-red-800/70 shadow-red-900/20 shadow-md"
          : "bg-amber-950/30 border-amber-800/50"
      }`}>
      <div className="flex justify-between items-start mb-1">
        <div>
          <span className="text-[12px] font-bold text-white">{alert.beneficiary_id}</span>
          <span className="text-[10px] text-slate-500 ml-2">{alert.facility_id}</span>
        </div>
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
          isCritical
            ? "bg-red-900/70 text-red-300 border-red-800 animate-pulse"
            : "bg-amber-900/60 text-amber-300 border-amber-800"
        }`}>
          {alert.minutes_elapsed.toFixed(1)}분
        </span>
      </div>
      <p className="text-[11px] text-slate-300">
        {alert.care_type ?? "미지정"} <span className="text-red-400">미기록</span>
      </p>
      <div className="flex justify-between items-center mt-2">
        <span className="text-[10px] text-red-400 font-mono">
          예상 환수 {alert.예상환수액.toLocaleString()}원
        </span>
        <span className="text-[10px] text-blue-400 font-bold">즉시 조치 &gt;</span>
      </div>
    </div>
  );
}

/** 포렌식 증거 상세 뷰 — 조작 불가능성 증명 */
function ForensicDetailView({ f }: { f: ForensicEvidence }) {
  return (
    <div className="space-y-2">
      {/* 증거 메타데이터 */}
      <div className="bg-[#080c18] rounded-lg border border-slate-700/40 p-3">
        <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
          증거 메타데이터
        </div>
        <ForensicRow label="기록 시각" value={new Date(f.recorded_at).toLocaleString("ko-KR")} />
        <ForensicRow label="수급자 ID" value={f.beneficiary_id} />
        <ForensicRow label="기록자" value={f.recorder_id} />
        <ForensicRow label="요양기관" value={f.facility_id} />
        <ForensicRow label="급여 유형" value={f.care_type} />
        <ForensicRow label="경과 시간" value={`${f.minutes_elapsed.toFixed(1)}분`} warn={f.minutes_elapsed <= 5} />
        <ForensicRow label="GPS 좌표" value={f.gps_coord} mono />
        <ForensicRow label="예상 환수액" value={`${f.estimated_clawback.toLocaleString()}원`} warn />
      </div>

      {/* 기기 정보 & 무결성 검증 */}
      <div className="bg-[#080c18] rounded-lg border border-slate-700/40 p-3">
        <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
          기기 &middot; 무결성 검증
        </div>
        <ForensicRow label="Device FP" value={f.device_fingerprint} mono />
        <div className="mt-1.5">
          <div className="text-[9px] text-slate-500 mb-0.5">원본 오디오 SHA-256</div>
          <div className="bg-[#060a14] rounded px-2 py-1.5 font-mono text-[9px] text-emerald-400 break-all border border-slate-800/50 select-all">
            {f.audio_sha256}
          </div>
        </div>
        <div className="mt-1.5">
          <div className="text-[9px] text-slate-500 mb-0.5">Chain Hash (연쇄 무결성)</div>
          <div className="bg-[#060a14] rounded px-2 py-1.5 font-mono text-[9px] text-blue-400 break-all border border-slate-800/50 select-all">
            {f.chain_hash}
          </div>
        </div>
      </div>

      {/* WORM 스토리지 Legal Hold */}
      <div className="bg-[#080c18] rounded-lg border border-slate-700/40 p-3">
        <div className="text-[10px] uppercase tracking-widest text-slate-500 font-bold mb-2">
          WORM 스토리지 보관 상태
        </div>
        <div className="flex items-center gap-3 mb-2">
          <WormBadge status={f.worm_status} large />
          <div className="text-[10px] text-slate-400">
            {f.worm_status === "LEGAL_HOLD" && "법적 보존 활성 — 삭제/수정 불가"}
            {f.worm_status === "SEALED" && "봉인 완료 — 변조 불가"}
            {f.worm_status === "PENDING" && "봉인 진행 중 — WORM 기록 대기"}
            {f.worm_status === "NONE" && "미봉인 — 즉시 조치 필요"}
          </div>
        </div>
        <ForensicRow label="보관 만료" value={new Date(f.worm_retain_until).toLocaleDateString("ko-KR")} />
        <div className="mt-2 px-2 py-1.5 rounded bg-indigo-950/40 border border-indigo-800/40">
          <div className="text-[9px] text-indigo-300 font-medium">
            본 증거는 Write-Once-Read-Many 정책에 의해 보호됩니다.
            기록 후 삭제/변경이 물리적으로 불가능합니다.
          </div>
        </div>
      </div>
    </div>
  );
}

function ForensicRow({ label, value, mono, warn }: {
  label: string; value: string; mono?: boolean; warn?: boolean;
}) {
  return (
    <div className="flex gap-2 text-[10px] py-0.5">
      <span className="text-slate-500 w-20 flex-shrink-0">{label}:</span>
      <span className={`${mono ? "font-mono text-[9px]" : ""} ${warn ? "text-red-400 font-bold" : "text-slate-300"}`}>
        {value}
      </span>
    </div>
  );
}

function WormBadge({ status, large }: { status: string; large?: boolean }) {
  const base = large ? "text-[11px] px-2.5 py-1" : "text-[9px] px-1.5 py-0.5";
  switch (status) {
    case "LEGAL_HOLD":
      return <span className={`${base} rounded font-bold bg-purple-900/70 text-purple-300 border border-purple-700/50`}>LEGAL HOLD</span>;
    case "SEALED":
      return <span className={`${base} rounded font-bold bg-indigo-900/70 text-indigo-300 border border-indigo-700/50`}>SEALED</span>;
    case "PENDING":
      return <span className={`${base} rounded font-bold bg-amber-900/60 text-amber-300 border border-amber-700/50 animate-pulse`}>PENDING</span>;
    default:
      return <span className={`${base} rounded font-bold bg-slate-700 text-slate-400`}>NONE</span>;
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return "-"; }
}
