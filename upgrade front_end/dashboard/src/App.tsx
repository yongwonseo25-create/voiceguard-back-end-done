import React, { useState, useEffect } from 'react';
import WormSealBadge from './components/WormSealBadge.tsx';
import IntegrityQrCode from './components/IntegrityQrCode.tsx';
import CertDetailDrawer from './components/CertDetailDrawer.tsx';
import type { WormLockMode } from './types/cert.ts';
import {
  ShieldCheck,
  Activity,
  Clock,
  Download,
  FileAudio,
  Server,
  ChevronDown,
  ArrowLeft,
  CalendarDays,
  FileText,
  Database,
  FolderArchive,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';

const API_BASE = (import.meta as { env?: { VITE_API_URL?: string } }).env?.VITE_API_URL ?? '';

function formatClawback(amount: number): string {
  return '₩' + amount.toLocaleString('ko-KR');
}

type DecisionItem = {
  id:                     string;
  unitName:               string;
  responsibleManager:     string;
  issueType:              string;
  elapsedTime:            string;
  estimatedReclaimAmount: string;
  wormHash:               string;
  wormLockMode:           WormLockMode;
  certIssued:             boolean;
};

type VoiceGuardState = {
  alerts: { severeOpenCount: number | null };
  records: { todayCompletionRate: string | null };
  tasks: { slaOverdueCount: number | null };
  handovers: { unacknowledgedCount: number | null };
  decisionQueue: { items: DecisionItem[] };
  system: { wormStatus: string | null; lastSyncTime: string | null };
};

export default function App() {
  const [VoiceGuard, setVoiceGuard] = useState<VoiceGuardState | null>(null);
  const [selectedRowId,  setSelectedRowId]  = useState<string | null>(null);
  const [viewMode,       setViewMode]       = useState<'dashboard' | 'export'>('dashboard');
  const [isDrawerOpen,   setIsDrawerOpen]   = useState(false);
  
  // Export Page states
  const [exportPeriod, setExportPeriod] = useState<'date' | 'month'>('month');
  const [exportType, setExportType] = useState<'data' | 'audio' | 'both'>('both');
  const [isCalendarOpen, setIsCalendarOpen] = useState(false);
  const [selectedDate, setSelectedDate] = useState(new Date(2026, 3, 16));
  const [selectedMonth, setSelectedMonth] = useState(new Date(2026, 3, 1));
  const [calendarViewDate, setCalendarViewDate] = useState(new Date(2026, 3, 1));

  // ── Initial Fetch ──────────────────────────────────────────────
  useEffect(() => {
    let isMounted = true;
    async function fetchInitial() {
      try {
        const [kpiRes, queueRes, wormRes] = await Promise.all([
          fetch(`${API_BASE}/api/v8/director/kpi`),
          fetch(`${API_BASE}/api/v8/director/decision-queue`),
          fetch(`${API_BASE}/api/v8/dashboard/worm-records?page=1&page_size=1`),
        ]);
        if (!isMounted) return;
        const kpi   = kpiRes.ok   ? await kpiRes.json()  : null;
        const queue = queueRes.ok ? await queueRes.json() : [];
        const worm  = wormRes.ok  ? await wormRes.json()  : null;
        const lastSync = worm?.records?.[0]?.ingested_at
          ? new Date(worm.records[0].ingested_at).toLocaleString('ko-KR', {
              year: 'numeric', month: '2-digit', day: '2-digit',
              hour: '2-digit', minute: '2-digit',
            })
          : '동기화 대기 중';
        setVoiceGuard({
          alerts:        { severeOpenCount: kpi?.redFlags ?? null },
          records:       { todayCompletionRate: kpi ? String(kpi.completionRate) : null },
          tasks:         { slaOverdueCount: kpi?.slaExceeded ?? null },
          handovers:     { unacknowledgedCount: kpi?.missingAck ?? null },
          decisionQueue: {
            items: Array.isArray(queue)
              ? queue.map((item: {
                  id?: string; facilityName?: string; adminName?: string;
                  problemType?: string; elapsedTime?: string;
                  expectedClawback?: number; wormHashShort?: string;
                  wormLockMode?: WormLockMode; certIssued?: boolean;
                }) => ({
                  id:                     item.id ?? '',
                  unitName:               item.facilityName ?? '',
                  responsibleManager:     item.adminName ?? '',
                  issueType:              item.problemType ?? '',
                  elapsedTime:            item.elapsedTime ?? '',
                  estimatedReclaimAmount: formatClawback(item.expectedClawback ?? 0),
                  wormHash:               item.wormHashShort ?? 'N/A',
                  wormLockMode:           item.wormLockMode ?? 'UNKNOWN',
                  certIssued:             item.certIssued ?? false,
                }))
              : [],
          },
          system: {
            wormStatus:   kpi ? '정상 가동' : '점검 중',
            lastSyncTime: lastSync,
          },
        });
      } catch {
        // VoiceGuard null 유지 → 스켈레톤 방어망 작동
      }
    }
    fetchInitial();
    return () => { isMounted = false; };
  }, []);

  // ── SSE 실시간 동기화 ───────────────────────────────────────────
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/sse/stream`);
    es.addEventListener('new_evidence', () => {
      fetch(`${API_BASE}/api/v8/director/kpi`)
        .then(r => r.ok ? r.json() : null)
        .then(kpi => {
          if (!kpi) return;
          setVoiceGuard(prev => prev ? {
            ...prev,
            alerts:    { severeOpenCount: kpi.redFlags },
            tasks:     { slaOverdueCount: kpi.slaExceeded },
            handovers: { unacknowledgedCount: kpi.missingAck },
            system:    { ...prev.system, lastSyncTime: new Date().toLocaleString('ko-KR') },
          } : prev);
        });
    });
    es.addEventListener('evidence_sealed', (e: MessageEvent) => {
      const data = JSON.parse(e.data);
      setVoiceGuard(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          decisionQueue: {
            items: prev.decisionQueue.items.map(item =>
              item.id === data.ledger_id
                ? { ...item, wormHash: (data.chain_hash ?? '').slice(0, 12) }
                : item
            ),
          },
          system: {
            ...prev.system,
            wormStatus:   '정상 가동',
            lastSyncTime: new Date().toLocaleString('ko-KR'),
          },
        };
      });
    });
    es.addEventListener('error', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        if (data.message?.includes('Redis')) es.close();
      } catch { /* connection-level error, not a data event */ }
    });
    es.onerror = () => { /* EventSource 내장 재연결 위임 */ };
    return () => es.close();
  }, []);

  const selectedData = VoiceGuard?.decisionQueue?.items?.find((r) => r.id === selectedRowId) || null;

  const renderCalendarDays = () => {
    const year = calendarViewDate.getFullYear();
    const month = calendarViewDate.getMonth();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const firstDay = new Date(year, month, 1).getDay();
    const days = Array(firstDay).fill(null).concat(Array.from({length: daysInMonth}, (_, i) => i + 1));

    return (
      <div className="grid grid-cols-7 gap-2 mt-4">
        {['일', '월', '화', '수', '목', '금', '토'].map(d => (
          <div key={d} className="text-center text-[13px] font-black text-gray-400 py-2">{d}</div>
        ))}
        {days.map((d, i) => {
          if (!d) return <div key={`empty-${i}`} />;
          const isSelected = selectedDate.getFullYear() === year && selectedDate.getMonth() === month && selectedDate.getDate() === d;
          return (
            <button 
              key={d}
              onClick={() => {
                setSelectedDate(new Date(year, month, d));
                setIsCalendarOpen(false);
              }}
              className={`h-14 w-full rounded-xl text-[16px] font-bold transition-all focus:outline-none focus:ring-2 focus:ring-gray-900 focus:ring-offset-1
                ${isSelected ? 'bg-[#111111] text-white shadow-md scale-105' : 'text-[#111111] hover:bg-gray-100 hover:text-indigo-600'}`}
            >
              {d}
            </button>
          )
        })}
      </div>
    );
  };

  const renderCalendarMonths = () => {
    const year = calendarViewDate.getFullYear();
    return (
      <div className="grid grid-cols-3 gap-4 mt-4">
        {Array.from({length: 12}, (_, i) => i).map(m => {
          const isSelected = selectedMonth.getFullYear() === year && selectedMonth.getMonth() === m;
          return (
            <button
              key={m}
              onClick={() => {
                 setSelectedMonth(new Date(year, m, 1));
                 setIsCalendarOpen(false);
              }}
              className={`py-6 rounded-xl text-[17px] font-black transition-all focus:outline-none focus:ring-2 focus:ring-gray-900 focus:ring-offset-1
                ${isSelected ? 'bg-[#111111] text-white shadow-lg scale-105' : 'text-[#111111] bg-gray-50 hover:bg-gray-100 hover:text-indigo-600'}`}
            >
              {m + 1}월
            </button>
          )
        })}
      </div>
    );
  };

  if (viewMode === 'export') {
    return (
      <div className="h-screen w-full flex flex-col bg-[#FAFAFA] font-sans antialiased overflow-hidden">
        {/* Export Page Header */}
        <header className="h-20 shrink-0 flex items-center px-8 bg-white border-b border-gray-200">
          <button 
            onClick={() => setViewMode('dashboard')}
            className="flex items-center space-x-2.5 text-indigo-600 hover:text-indigo-800 transition-colors group px-3 py-2 -ml-3 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2"
          >
            <ArrowLeft className="w-[26px] h-[26px] group-hover:-translate-x-1.5 transition-transform" strokeWidth={2.5} />
            <span className="font-black text-[20px] tracking-tight">대시보드 복귀</span>
          </button>
          <div className="mx-auto flex items-center space-x-3 pr-40">
            <div className="w-8 h-8 bg-gray-900 rounded-lg flex items-center justify-center shadow-md">
              <ShieldCheck className="w-4 h-4 text-white" strokeWidth={2.5} />
            </div>
            <h1 className="text-[20px] font-black tracking-tight text-[#111111]">공단 소명용 증거 추출 센터</h1>
          </div>
        </header>

        {/* Content Area */}
        <div className="flex-1 overflow-hidden px-8 py-8 flex justify-center">
          <div className="w-full max-w-4xl flex flex-col justify-between space-y-6 h-full pb-6">
            
            {/* Section 1: Period Selection */}
            <section className="bg-white rounded-[2rem] p-8 shadow-[0_10px_40px_rgb(0,0,0,0.04)] border border-gray-100 flex-1 flex flex-col justify-center">
              <h2 className="text-[20px] font-black text-[#111111] tracking-tight mb-6 flex items-center">
                <CalendarDays className="w-6 h-6 mr-3 text-indigo-600" />
                1. 추출 기간 선택
              </h2>
              
              <div className="flex bg-gray-100 p-1.5 rounded-2xl mb-6">
                <button 
                  onClick={() => setExportPeriod('date')}
                  className={`flex-1 py-3.5 text-[17px] rounded-xl transition-all duration-200 ${exportPeriod === 'date' ? 'bg-[#111111] text-white font-black shadow-md scale-[1.02]' : 'text-gray-500 font-bold hover:text-[#111111] hover:bg-gray-200/50'}`}
                >
                  특정 일자 (단일)
                </button>
                <button 
                  onClick={() => setExportPeriod('month')}
                  className={`flex-1 py-3.5 text-[17px] rounded-xl transition-all duration-200 ${exportPeriod === 'month' ? 'bg-[#111111] text-white font-black shadow-md scale-[1.02]' : 'text-gray-500 font-bold hover:text-[#111111] hover:bg-gray-200/50'}`}
                >
                  특정 월 (전체)
                </button>
              </div>

              {/* Interactive Calendar UI */}
              <div className="relative flex justify-center mt-3 mb-6 w-full">
                <div className="relative flex flex-col items-center">
                  <button 
                    onClick={() => {
                      setCalendarViewDate(exportPeriod === 'date' ? new Date(selectedDate) : new Date(selectedMonth));
                      setIsCalendarOpen(true);
                    }}
                    className="inline-flex items-center space-x-2 bg-gray-100 hover:bg-gray-200 transition-colors cursor-pointer px-6 py-3.5 rounded-full border border-gray-200 shadow-sm focus:outline-none shrink-0"
                  >
                    <span className="text-[18px] font-black text-[#111111] tracking-tight">
                      {exportPeriod === 'date' 
                        ? `${selectedDate.getFullYear()}년 ${selectedDate.getMonth() + 1}월 ${selectedDate.getDate()}일` 
                        : `${selectedMonth.getFullYear()}년 ${selectedMonth.getMonth() + 1}월 전체`}
                    </span>
                    <ChevronDown className="w-5 h-5 text-[#111111] ml-1.5" />
                  </button>

                  {/* Popover Element */}
                  {isCalendarOpen && (
                    <>
                      <div 
                        className="fixed inset-0 z-40" 
                        onClick={() => setIsCalendarOpen(false)}
                      />
                      <div className="absolute top-[calc(100%+16px)] left-1/2 -translate-x-1/2 w-[440px] bg-white rounded-3xl shadow-[0_30px_60px_-15px_rgba(0,0,0,0.3)] border border-gray-200 z-50 p-6 flex flex-col transform opacity-100 scale-100 transition-all origin-top">
                      
                      {/* Calendar Header */}
                      <div className="flex items-center justify-between mb-2">
                        <button 
                          onClick={() => {
                            const step = exportPeriod === 'date' ? new Date(calendarViewDate.getFullYear(), calendarViewDate.getMonth() - 1, 1) : new Date(calendarViewDate.getFullYear() - 1, calendarViewDate.getMonth(), 1);
                            setCalendarViewDate(step);
                          }}
                          className="w-10 h-10 rounded-full flex items-center justify-center bg-gray-50 hover:bg-gray-100 active:scale-95 transition-all text-[#111111]"
                        >
                          <ChevronLeft className="w-5 h-5" />
                        </button>
                        <h3 className="text-[17px] font-black text-[#111111]">
                          {exportPeriod === 'date' 
                            ? `${calendarViewDate.getFullYear()}년 ${calendarViewDate.getMonth() + 1}월`
                            : `${calendarViewDate.getFullYear()}년`
                          }
                        </h3>
                        <button 
                          onClick={() => {
                            const step = exportPeriod === 'date' ? new Date(calendarViewDate.getFullYear(), calendarViewDate.getMonth() + 1, 1) : new Date(calendarViewDate.getFullYear() + 1, calendarViewDate.getMonth(), 1);
                            setCalendarViewDate(step);
                          }}
                          className="w-10 h-10 rounded-full flex items-center justify-center bg-gray-50 hover:bg-gray-100 active:scale-95 transition-all text-[#111111]"
                        >
                          <ChevronRight className="w-5 h-5" />
                        </button>
                      </div>

                      {/* Calendar Body */}
                      {exportPeriod === 'date' ? renderCalendarDays() : renderCalendarMonths()}
                    </div>
                  </>
                )}
                </div>
              </div>
            </section>

            {/* Section 2: Data Type Selection */}
            <section className="bg-white rounded-[2rem] p-8 shadow-[0_10px_40px_rgb(0,0,0,0.04)] border border-gray-100 flex-1 flex flex-col justify-center">
              <h2 className="text-[20px] font-black text-[#111111] tracking-tight mb-6 flex items-center">
                <Database className="w-6 h-6 mr-3 text-indigo-600" />
                2. 추출 데이터 포맷 선택
              </h2>
              
              <div className="grid grid-cols-3 gap-5">
                {/* Type 1: Data Only */}
                <div 
                  onClick={() => setExportType('data')}
                  className={`cursor-pointer rounded-2xl p-6 border-2 transition-all duration-200 flex flex-col ${exportType === 'data' ? 'border-[#111111] bg-gray-50' : 'border-gray-100 hover:border-gray-300 bg-white'}`}
                >
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center mb-4 ${exportType === 'data' ? 'bg-[#111111] text-white' : 'bg-gray-100 text-gray-500'}`}>
                    <FileText className="w-6 h-6" />
                  </div>
                  <h3 className="text-[17px] font-black text-[#111111] mb-2">데이터만 선택</h3>
                  <p className="text-[14px] font-semibold text-gray-500 flex-1 leading-snug">STT 기록 및 조작 방지 증명서</p>
                  <span className="inline-block mt-3 px-3 py-1.5 bg-green-100 text-green-700 text-[11px] font-black rounded-md self-start uppercase tracking-tight">초고속 추출</span>
                </div>

                {/* Type 2: Audio Only */}
                <div 
                  onClick={() => setExportType('audio')}
                  className={`cursor-pointer rounded-2xl p-6 border-2 transition-all duration-200 flex flex-col ${exportType === 'audio' ? 'border-[#111111] bg-gray-50' : 'border-gray-100 hover:border-gray-300 bg-white'}`}
                >
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center mb-4 ${exportType === 'audio' ? 'bg-[#111111] text-white' : 'bg-gray-100 text-gray-500'}`}>
                    <FileAudio className="w-6 h-6" />
                  </div>
                  <h3 className="text-[17px] font-black text-[#111111] mb-2">오디오만 선택</h3>
                  <p className="text-[14px] font-semibold text-gray-500 flex-1 leading-snug">원본 녹음 파일만 추출 (.WAV)</p>
                </div>

                {/* Type 3: Both */}
                <div 
                  onClick={() => setExportType('both')}
                  className={`cursor-pointer rounded-2xl p-6 border-2 transition-all duration-200 flex flex-col relative overflow-hidden ${exportType === 'both' ? 'border-indigo-600 bg-indigo-50/30' : 'border-gray-100 hover:border-gray-300 bg-white'}`}
                >
                  {exportType === 'both' && (
                    <div className="absolute top-0 right-0 bg-indigo-600 text-white text-[11px] font-black px-3 py-1 rounded-bl-xl shadow-sm">
                      권장
                    </div>
                  )}
                  <div className={`w-12 h-12 rounded-full flex items-center justify-center mb-4 ${exportType === 'both' ? 'bg-indigo-600 text-white shadow-md' : 'bg-gray-100 text-gray-500'}`}>
                    <FolderArchive className="w-6 h-6" />
                  </div>
                  <h3 className="text-[17px] font-black text-[#111111] mb-2">전체 패키지</h3>
                  <p className="text-[14px] font-semibold text-gray-500 flex-1 leading-snug">입체적 소명을 위한 데이터+음성 통합본</p>
                </div>
              </div>
            </section>

            {/* Section 3: Action Area (Submit) */}
            <section className="text-center bg-white rounded-[2rem] p-8 shadow-[0_10px_40px_rgb(0,0,0,0.06)] flex flex-col items-center justify-center shrink-0 border border-gray-100">
              <div className="inline-flex items-center justify-center bg-indigo-50 px-8 py-3.5 rounded-xl mb-5 border border-indigo-100 shadow-sm">
                <p className="text-[16.5px] font-black tracking-tight text-indigo-700 whitespace-nowrap">
                  선택하신 기간과 포맷에 맞춰 위조가 불가능하게 봉인된 WORM 원본 데이터를 추출합니다.
                </p>
              </div>
              <button className="w-full max-w-2xl h-16 rounded-2xl bg-indigo-600 text-white text-[18px] font-black flex items-center justify-center space-x-3 shadow-[0_10px_25px_rgba(79,70,229,0.35),inset_0_1px_1px_rgba(255,255,255,0.25)] hover:bg-indigo-700 hover:-translate-y-0.5 active:translate-y-0 transition-all focus:outline-none focus:ring-4 focus:ring-indigo-500/30">
                <Download className="w-6 h-6" strokeWidth={2.5} />
                <span>선택된 원본 자료 추출 및 다운로드 (.ZIP)</span>
              </button>
            </section>
            
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-full flex flex-col bg-[#FAFAFA] font-sans antialiased text-[#111111] overflow-hidden">
      
      {/* 1. TOP 영역: Executive Header */}
      <header className="h-24 shrink-0 flex items-center justify-center px-8 bg-[#FAFAFA] relative">
        <div className="flex items-center space-x-3.5 bg-white px-6 py-2.5 rounded-full shadow-sm border border-gray-200">
          <div className="w-[42px] h-[42px] bg-gray-900 rounded-[14px] flex items-center justify-center shadow-lg">
            <ShieldCheck className="w-[22px] h-[22px] text-white" strokeWidth={2.5} />
          </div>
          <div className="flex items-center">
            <h1 className="text-[24px] font-black tracking-tight text-[#111111]">케어옵스 지휘 관제실</h1>
          </div>
        </div>
        
        {/* Right side activity icon */}
        <div className="absolute right-8 flex items-center space-x-4">
          <div className="w-10 h-10 bg-white rounded-full flex items-center justify-center shadow-sm border border-gray-100">
            <Activity className="w-5 h-5 text-gray-400" />
          </div>
        </div>
      </header>

      {/* Main Content Area */}
          <div className="flex-1 flex overflow-hidden">
        
        {/* Left Side: KPIs & Decision Queue */}
        <div className="flex-1 flex flex-col px-8 pb-5 overflow-hidden">
          
          {/* KPI Widgets */}
          <div className="grid grid-cols-4 gap-8 shrink-0 mb-10">
            {/* KPI 1 */}
            <div className="bg-white rounded-3xl p-8 shadow-[0_12px_40px_rgb(0,0,0,0.06)] flex flex-col items-center justify-center min-h-[160px]">
              {VoiceGuard ? (
                <div className="text-6xl font-black tracking-tight text-[#111111]">{VoiceGuard.alerts?.severeOpenCount}<span className="text-[26px] font-black text-gray-400 ml-2">건</span></div>
              ) : (
                <div className="w-24 h-16 bg-gray-200 rounded-xl mb-1"></div>
              )}
              <div className="flex items-center space-x-2 mt-4">
                <div className="w-2.5 h-2.5 rounded-full bg-[#EF4444]"></div>
                <span className="text-[15px] font-bold text-gray-500 tracking-tight">미조치 고위험 건수</span>
              </div>
            </div>
            {/* KPI 2 */}
            <div className="bg-white rounded-3xl p-8 shadow-[0_12px_40px_rgb(0,0,0,0.06)] flex flex-col items-center justify-center min-h-[160px]">
              {VoiceGuard ? (
                <div className="text-6xl font-black tracking-tight text-[#111111]">{VoiceGuard.records?.todayCompletionRate}<span className="text-[26px] font-black text-gray-400 ml-2">%</span></div>
              ) : (
                <div className="w-32 h-16 bg-gray-200 rounded-xl mb-1"></div>
              )}
              <div className="flex items-center space-x-2 mt-4">
                <div className="w-2.5 h-2.5 rounded-full bg-[#F59E0B]"></div>
                <span className="text-[15px] font-bold text-gray-500 tracking-tight">오늘 기록 완결률</span>
              </div>
            </div>
            {/* KPI 3 */}
            <div className="bg-white rounded-3xl p-8 shadow-[0_12px_40px_rgb(0,0,0,0.06)] flex flex-col items-center justify-center min-h-[160px]">
              {VoiceGuard ? (
                <div className="text-6xl font-black tracking-tight text-[#111111]">{VoiceGuard.tasks?.slaOverdueCount}<span className="text-[26px] font-black text-gray-400 ml-2">건</span></div>
              ) : (
                <div className="w-20 h-16 bg-gray-200 rounded-xl mb-1"></div>
              )}
              <div className="flex items-center space-x-2 mt-4">
                <div className="w-2.5 h-2.5 rounded-full bg-[#FF5A00]"></div>
                <span className="text-[15px] font-bold text-gray-500 tracking-tight">SLA 초과 조치 대기</span>
              </div>
            </div>
            {/* KPI 4 */}
            <div className="bg-white rounded-3xl p-8 shadow-[0_12px_40px_rgb(0,0,0,0.06)] flex flex-col items-center justify-center min-h-[160px]">
              {VoiceGuard ? (
                <div className="text-6xl font-black tracking-tight text-[#111111]">{VoiceGuard.handovers?.unacknowledgedCount}<span className="text-[26px] font-black text-gray-400 ml-2">건</span></div>
              ) : (
                <div className="w-24 h-16 bg-gray-200 rounded-xl mb-1"></div>
              )}
              <div className="flex items-center space-x-2 mt-4">
                <div className="w-2.5 h-2.5 rounded-full bg-[#3B82F6]"></div>
                <span className="text-[15px] font-bold text-gray-500 tracking-tight">인수인계 미완료</span>
              </div>
            </div>
          </div>

          {/* 2. MIDDLE 영역: 2-Column layout (Decision Queue & Audit Panel) */}
          <div className="flex-1 flex space-x-6 overflow-hidden w-full">
            
            {/* Left: Decision Queue */}
            <div className="flex-1 flex flex-col bg-white rounded-3xl shadow-[0_10px_40px_rgb(0,0,0,0.06)] overflow-hidden min-w-0">
              <div className="px-8 py-7 border-b border-gray-100 flex items-center justify-center bg-white shrink-0">
                <div className="flex flex-col items-center justify-center space-y-1.5 bg-gray-50 px-8 py-3.5 rounded-2xl border border-gray-200 shadow-sm">
                  <h2 className="text-[25px] font-black tracking-tight text-[#111111] leading-none">긴급 의사결정 대기열</h2>
                  <span className="text-[13.8px] text-gray-500 font-bold leading-none tracking-wide uppercase">우선 조치 리스트</span>
                </div>
              </div>
              <div className="flex-1 overflow-auto">
              <table className="w-full text-center border-collapse table-auto">
                <thead className="sticky top-0 bg-white/95 backdrop-blur z-10">
                  <tr>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">발생 구역</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">책임 관리자</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">문제 유형</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">경과 시간</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">예상 환수 노출액</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">증거 패키지</th>
                    <th className="px-4 py-4 text-[18px] font-black text-[#111111] tracking-tight whitespace-nowrap border-b border-gray-100 text-center bg-transparent">현장 지시</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {VoiceGuard && Array.isArray(VoiceGuard?.decisionQueue?.items) ? (
                    VoiceGuard.decisionQueue.items.map((row) => (
                      <tr 
                        key={row.id} 
                        onClick={() => { setSelectedRowId(row.id); setIsDrawerOpen(true); }}
                        className={`cursor-pointer transition-all duration-200 border-l-[6px] ${selectedRowId === row.id ? 'bg-indigo-50/80 border-indigo-500 shadow-sm' : 'bg-white border-transparent hover:bg-gray-50'}`}
                      >
                        <td className="px-4 py-8 whitespace-nowrap text-base font-semibold text-[#111111] text-center border-0">{row.unitName}</td>
                        <td className="px-4 py-8 whitespace-nowrap text-[15px] font-medium text-gray-600 text-center border-0">{row.responsibleManager}</td>
                        <td className="px-4 py-8 whitespace-nowrap text-center border-0">
                          <span className="inline-flex items-center px-4 py-1.5 rounded-full text-[14px] font-bold bg-red-50 text-red-600 ring-1 ring-inset ring-red-100">
                            {row.issueType}
                          </span>
                        </td>
                        <td className="px-4 py-8 whitespace-nowrap text-[15px] font-bold text-[#111111] border-0">
                          <div className="flex items-center justify-center"><Clock className="w-4 h-4 mr-1.5 text-[#FF5A00]" />{row.elapsedTime}</div>
                        </td>
                        <td className="px-4 py-8 whitespace-nowrap text-[16px] font-bold text-[#111111] text-center border-0">{row.estimatedReclaimAmount}</td>
                        <td className="px-4 py-8 whitespace-nowrap text-center border-0">
                          <WormSealBadge
                            wormLockMode={row.wormLockMode}
                            chainHash={row.wormHash}
                            size="sm"
                          />
                        </td>
                        <td className="px-4 py-8 whitespace-nowrap text-center border-0">
                          <div className="flex items-center justify-center space-x-2">
                            <button 
                              className="bg-white border border-gray-300 text-gray-700 hover:bg-gray-100 hover:border-gray-400 hover:shadow-md active:bg-gray-200 active:shadow-inner active:scale-95 px-3 py-2.5 rounded-lg text-xs font-bold transition-all flex items-center"
                              onClick={(e) => e.stopPropagation()}
                            >
                              📢 관리자 소집
                            </button>
                            <button 
                              className="bg-[#111111] text-white hover:bg-gray-800 hover:shadow-lg hover:-translate-y-0.5 active:bg-black active:shadow-inner active:scale-95 px-3 py-2.5 rounded-lg text-xs font-bold transition-all flex items-center"
                              onClick={(e) => e.stopPropagation()}
                            >
                              🚨 현장 배정
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  ) : (
                    Array(5).fill(0).map((_, i) => (
                      <tr key={`skeleton-${i}`} className="bg-white border-l-[6px] border-transparent">
                        <td className="px-4 py-8"><div className="w-24 h-5 bg-gray-200 rounded mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-20 h-5 bg-gray-200 rounded mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-28 h-7 bg-gray-200 rounded-full mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-20 h-5 bg-gray-200 rounded mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-24 h-5 bg-gray-200 rounded mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-32 h-6 bg-gray-200 rounded-full mx-auto"></div></td>
                        <td className="px-4 py-8"><div className="w-40 h-8 bg-gray-200 rounded mx-auto"></div></td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* Sub Footer: System Status */}
            <div className="px-8 py-3 bg-gray-50 border-t border-gray-100 flex items-center justify-between shrink-0">
              <div className="flex items-center space-x-6">
                <div className="flex items-center space-x-2">
                  <Server className="w-4 h-4 text-gray-500" />
                  <span className="text-[13px] font-bold text-gray-500">WORM 서버: </span>
                  {VoiceGuard ? (
                    <span className="text-[13px] font-black text-indigo-600">{VoiceGuard.system?.wormStatus}</span>
                  ) : (
                    <div className="w-16 h-4 bg-gray-200 rounded"></div>
                  )}
                </div>
                <div className="flex items-center space-x-2">
                  <Clock className="w-4 h-4 text-gray-500" />
                  <span className="text-[13px] font-bold text-gray-500">최종 동기화: </span>
                  {VoiceGuard ? (
                    <span className="text-[13px] font-black text-gray-700">{VoiceGuard.system?.lastSyncTime}</span>
                  ) : (
                    <div className="w-24 h-4 bg-gray-200 rounded"></div>
                  )}
                </div>
              </div>
            </div>

          </div>

          {/* Right: 우측 추가 패널: Audit Defense Panel */}
          <div className="flex-shrink-0 w-[420px] bg-white rounded-3xl shadow-[0_10px_40px_rgb(0,0,0,0.06)] overflow-hidden flex flex-col">
            <div className="p-8 h-full flex flex-col w-full justify-between relative">
              
              {/* 1. Header is ALWAYS shown */}
              <div className="flex flex-col items-center justify-center shrink-0 mb-6 pt-4">
                <div className="inline-flex items-center space-x-3 bg-white px-5 py-2.5 rounded-full shadow-sm border border-gray-200 mb-3">
                  <div className="w-[38px] h-[38px] bg-indigo-600 rounded-xl flex items-center justify-center shadow-sm">
                    <ShieldCheck className="w-5 h-5 text-white" strokeWidth={2.5} />
                  </div>
                  <h3 className="text-[22px] font-black tracking-tight text-[#111111] leading-none pr-2">무결점 증거 패널</h3>
                </div>
                <span className="text-[14px] text-gray-500 font-bold leading-none tracking-wide text-center">국민건강보험공단 감사 방어용</span>
              </div>

              {/* 2. Middle Content changes conditionally */}
              <div className="flex-1 flex flex-col items-center justify-center space-y-12 mb-8 mt-4">
                {selectedData ? (
                  <>
                    <div className="text-center w-full">
                      <p className="text-[17.5px] font-bold text-gray-400 tracking-wider mb-3">발생 구역 및 책임자</p>
                      <p className="text-[25px] font-black text-[#111111] leading-snug tracking-tight flex flex-col items-center space-y-1">
                        <span>{selectedData.unitName}</span>
                        <span className="text-[21px] font-bold text-gray-500">· {selectedData.responsibleManager}</span>
                      </p>
                    </div>

                    <div className="text-center w-full">
                      <p className="text-[17.5px] font-bold text-gray-400 tracking-wider mb-3">문제 유형</p>
                      <div className="inline-flex items-center px-6 py-2.5 bg-red-50 rounded-[20px] ring-1 ring-inset ring-red-100">
                        <span className="text-[25px] font-black text-red-600 leading-snug tracking-tight">{selectedData.issueType}</span>
                      </div>
                    </div>

                    <div className="w-full flex flex-col items-center gap-1 pt-2">
                      <p className="text-[12px] font-bold text-gray-400 mb-1">즉시 검증 QR</p>
                      <IntegrityQrCode ledgerId={selectedData.id} size={128} />
                    </div>
                  </>
                ) : !VoiceGuard ? (
                  <>
                    <div className="text-center w-full flex flex-col items-center space-y-3">
                      <div className="w-[140px] h-[18px] bg-gray-200 rounded-full"></div>
                      <div className="w-[200px] h-[30px] bg-gray-200 rounded-full"></div>
                      <div className="w-[160px] h-[22px] bg-gray-200 rounded-full"></div>
                    </div>
                    <div className="text-center w-full flex flex-col items-center space-y-3 mt-8">
                      <div className="w-[100px] h-[18px] bg-gray-200 rounded-full"></div>
                      <div className="w-[180px] h-[56px] bg-gray-200 rounded-[20px]"></div>
                    </div>
                  </>
                ) : (
                  <div className="flex flex-col items-center justify-center text-center">
                    <div className="w-20 h-20 bg-gray-50 rounded-full flex items-center justify-center shadow-inner border border-gray-100 mb-6">
                      <ShieldCheck className="w-10 h-10 text-gray-200" strokeWidth={2} />
                    </div>
                    <p className="text-[15px] font-bold text-gray-400">좌측 대기열에서 통제할 구역을 선택해주세요.</p>
                  </div>
                )}
              </div>

              {/* 3. Bottom Action is ALWAYS shown and functional */}
              <div className="shrink-0 pt-6 mt-auto border-t border-gray-100 relative">
                <button 
                  onClick={() => setViewMode('export')}
                  className={`w-full h-[72px] rounded-2xl font-black text-[20px] flex items-center justify-center space-x-3 transition-all focus:outline-none focus:ring-4 
                    ${selectedData || !VoiceGuard /* active lookup styles if data or skeleton */ 
                      ? 'bg-indigo-50 text-indigo-700 border-2 border-indigo-100 shadow-sm hover:bg-indigo-100 hover:border-indigo-200 hover:shadow-md hover:-translate-y-0.5 active:scale-[0.98] focus:ring-indigo-500/20' 
                      : 'bg-white text-indigo-600 border-2 border-indigo-100 shadow-sm hover:bg-indigo-50 hover:-translate-y-0.5 active:scale-[0.98] focus:ring-indigo-500/20'}`}
                >
                  <FolderArchive className="w-[28px] h-[28px]" strokeWidth={2.5} />
                  <span className="tracking-tight whitespace-nowrap">공단 소명용 원본 기록 추출</span>
                </button>
              </div>

            </div>
          </div>

      </div>
    </div>
  </div>

      {/* CertDetailDrawer: 행 클릭 시 슬라이드인 증거 상세 패널 (fixed overlay) */}
      <CertDetailDrawer
        ledgerId={selectedRowId}
        isOpen={isDrawerOpen}
        onClose={() => setIsDrawerOpen(false)}
      />
</div>
  );
}
