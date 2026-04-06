"use client";

import { useState, useEffect } from "react";
import PlanVsActualPanel from "@/components/admin/PlanVsActualPanel";
import { useVoiceGuardSSE } from "@/lib/useVoiceGuardSSE";

export default function DashboardPage() {
  // Hydration 에러 방지를 위한 상태
  const [mounted, setMounted] = useState(false);
  const [currentTime, setCurrentTime] = useState("");
  const [instructions, setInstructions] = useState<string[]>([]);

  // 실시간 SSE 구독 (new_evidence 이벤트 수신)
  const { alerts: sseAlerts, connected: sseConnected } = useVoiceGuardSSE();

  useEffect(() => {
    // 1. 클라이언트 마운트 완료 처리
    setMounted(true);

    // 2. 실시간 시계 로직 (Hydration 안전)
    const updateTime = () => setCurrentTime(new Date().toLocaleTimeString('ko-KR'));
    updateTime();
    const timer = setInterval(updateTime, 1000);

    // 3. 하향식 지시 API Fetch 로직
    const fetchInstructions = async () => {
      try {
        // 실제 백엔드 연동 시 아래 URL을 '/api/instructions' 등으로 변경
        // const response = await fetch('/api/instructions');
        // const data = await response.json();

        // 임시 Mock 데이터 (API 응답 시뮬레이션)
        const mockData = [
          "[긴급] 5분 이내 치명 건 우선 처리 요망",
          "[공지] 금일 18시 시스템 점검 예정"
        ];
        setInstructions(mockData);
      } catch (error) {
        console.error("하향식 지시사항을 불러오는데 실패했습니다.", error);
      }
    };

    fetchInstructions();

    return () => clearInterval(timer);
  }, []);

  // 서버 사이드 렌더링 시에는 아무것도 그리지 않아 Hydration 불일치 원천 차단
  if (!mounted) return null;

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 p-6 font-sans">
      {/* Header */}
      <header className="flex justify-between items-center mb-6 border-b border-slate-700 pb-4">
        <div>
          <h1 className="text-2xl font-bold text-white">VoiceGuard Admin Dashboard</h1>
          <p className="text-sm text-slate-400 mt-1">미기록 실시간 대응 상황판</p>
        </div>
        <div className="text-right">
          <div className="text-lg font-mono font-bold text-emerald-400">{currentTime}</div>
          <div className="text-xs text-slate-400 mt-1">서버 시간 기준</div>
        </div>
      </header>

      {/* 하향식 지시사항 패널 (API 연동 결과) */}
      <div className="mb-6 bg-slate-800 border border-slate-700 rounded-lg p-4 shadow-md">
        <h2 className="text-sm font-bold text-slate-300 mb-2 uppercase tracking-wider">
          하향식 지시사항
        </h2>
        <ul className="space-y-1.5">
          {instructions.map((inst, idx) => (
            <li key={idx} className="text-[15px] text-rose-400 font-bold flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span>
              {inst}
            </li>
          ))}
        </ul>
      </div>

      {/* 3단 대시보드 메인 화면 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* Column 1: 미기록 실시간 알림 패널 — SSE 실데이터 */}
        <section className="bg-slate-800 border border-slate-700 rounded-lg p-5 flex flex-col shadow-lg">
          <h2 className="text-xl font-bold text-white mb-1 text-center">미기록 실시간 알림</h2>
          <p className={`text-[11px] text-center mb-3 font-mono ${sseConnected ? "text-emerald-400" : "text-slate-500"}`}>
            {sseConnected ? "● SSE 연결됨" : "○ SSE 연결 대기 중..."}
          </p>
          <div className="flex-1 space-y-3 overflow-y-auto">
            {sseAlerts.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-slate-400 text-sm">
                실시간 미기록 건 대기 중...
              </div>
            ) : (
              sseAlerts.map((item) => {
                const isCritical = item.minutes_elapsed <= 2;
                return (
                  <div
                    key={item.id}
                    className={`bg-slate-900 border rounded-md p-4 hover:border-slate-500 transition-colors cursor-pointer ${
                      isCritical ? "border-rose-800" : "border-amber-800"
                    }`}
                  >
                    <div className="flex justify-between items-start mb-2">
                      <span className="text-[17px] font-bold text-white">{item.beneficiary_id}</span>
                      <span className={`text-[13px] font-extrabold px-2.5 py-1 rounded border ${
                        isCritical
                          ? "bg-rose-900 text-rose-300 border-rose-800"
                          : "bg-amber-900 text-amber-300 border-amber-800"
                      }`}>
                        {item.minutes_elapsed.toFixed(1)}분 경과
                      </span>
                    </div>
                    <p className="text-[14px] text-slate-300 font-medium">
                      {item.care_type ?? "미지정"} 미기록 · {item.facility_id}
                    </p>
                    <p className="text-[11px] text-rose-400 font-mono mt-1">
                      예상 환수 ₩{item.예상환수액.toLocaleString()}
                    </p>
                  </div>
                );
              })
            )}
          </div>
        </section>

        {/* Column 2: 급여계획 비교 패널 (PlanVsActualPanel) */}
        <section className="bg-slate-800 border border-slate-700 rounded-lg p-5 flex flex-col shadow-lg">
          <h2 className="text-xl font-bold text-white mb-4 text-center">급여계획 비교 패널</h2>
          <div className="flex-1">
            <PlanVsActualPanel
              onSelectRiskItem={(recipientName, missingItem) => {
                console.log(`[리스크 선택] 수급자: ${recipientName}, 미기록 항목: ${missingItem}`);
              }}
            />
          </div>
        </section>

        {/* Column 3: 현지조사 증빙 및 액션 패널 */}
        <section className="bg-slate-800 border border-slate-700 rounded-lg p-5 flex flex-col shadow-lg">
          <h2 className="text-xl font-bold text-white mb-4 text-center">현지조사 증빙 및 액션 패널</h2>
          <div className="flex-1 flex flex-col gap-4">

            {/* 즉시 조치 버튼 영역 */}
            <div className="bg-slate-900 border border-slate-700 rounded-md p-4">
              <div className="text-[16px] font-bold text-white mb-3 text-center">즉시 조치 버튼</div>
              <div className="space-y-2.5">
                <button className="w-full bg-slate-700 hover:bg-slate-600 border border-slate-500 text-white font-bold py-2.5 px-4 rounded transition-colors text-[15px]">
                  현장 확인 요청 넛지 발송
                </button>
                <button className="w-full bg-slate-700 hover:bg-slate-600 border border-slate-500 text-white font-bold py-2.5 px-4 rounded transition-colors text-[15px]">
                  관리자 대체 입력 승인
                </button>
              </div>
            </div>

            {/* 조치 기록 메모 영역 */}
            <div className="bg-slate-900 border border-slate-700 rounded-md p-4 flex-1 flex flex-col">
              <div className="text-[16px] font-bold text-white mb-2 text-center">조치 기록 메모</div>
              <textarea
                className="w-full flex-1 min-h-[120px] bg-slate-800 border border-slate-600 rounded-md p-3 text-white text-[16px] leading-relaxed placeholder-slate-400 resize-none overflow-y-auto focus:outline-none focus:border-emerald-500 transition-colors"
                placeholder="현장 확인 결과, 누락 원인 등을 명확하게 남깁니다."
              ></textarea>
            </div>

            {/* 다운로드 버튼 */}
            <button className="w-full bg-emerald-500 hover:bg-emerald-600 text-white font-bold py-3.5 px-4 rounded-md shadow-md transition-colors text-center text-[16px] flex items-center justify-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              현지조사 방어 증거 패키지 다운로드 (PDF/ZIP)
            </button>
          </div>
        </section>

      </div>
    </div>
  );
}
