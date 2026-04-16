import { useEffect, useState, ReactNode, MouseEvent } from 'react';
import { AlertTriangle, CheckCircle, Clock, FileWarning, Send, ShieldCheck, ChevronDown, Users, FileKey, AlertOctagon } from 'lucide-react';
import { fetchDirectorKPIs, fetchDecisionQueue, instructDecision, type KPIStats, type DecisionItem } from '../api/api';
import { cn } from '../lib/utils';

export default function DirectorDashboard() {
  const [kpis, setKpis] = useState<KPIStats | null>(null);
  const [queue, setQueue] = useState<DecisionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeDropdown, setActiveDropdown] = useState<string | null>(null);

  // Click outside to close dropdown
  useEffect(() => {
    const handleClickOutside = () => setActiveDropdown(null);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, []);

  useEffect(() => {
    Promise.all([
      fetchDirectorKPIs().catch(() => null),
      fetchDecisionQueue().catch(() => [])
    ]).then(([kpiData, queueData]) => {
      if (kpiData) setKpis(kpiData);
      if (queueData) setQueue(queueData);
      setLoading(false);
    });
  }, []);

  const handleInstruct = async (id: string, actionType: string, e: MouseEvent) => {
    e.stopPropagation();
    try {
      setActiveDropdown(null);
      await instructDecision(id, actionType);
      // Closed-loop visual effect: item disappears
      setQueue(q => q.filter(item => item.id !== id));
    } catch (error) {
      console.error('지시 실패', error);
    }
  };

  const toggleDropdown = (id: string, e: MouseEvent) => {
    e.stopPropagation();
    setActiveDropdown(prev => prev === id ? null : id);
  };

  return (
    <div className="space-y-10 max-w-[1600px] mx-auto">
      {/* KPI Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-8">
        <KpiCard 
          title="미조치 고위험 건수" 
          value={kpis?.redFlags ?? '-'} 
          icon={<AlertTriangle className="w-10 h-10 text-red-400" />}
          isRedFlag={true}
        />
        <KpiCard 
          title="오늘 기록 완결률" 
          value={kpis ? `${kpis.completionRate}%` : '-'} 
          icon={<CheckCircle className="w-10 h-10 text-emerald-400" />}
        />
        <KpiCard 
          title="SLA 초과 조치 대기" 
          value={kpis?.slaExceeded ?? '-'} 
          icon={<Clock className="w-10 h-10 text-red-400" />}
          isRedFlag={true}
        />
        <KpiCard 
          title="인수인계 누락 건수" 
          value={kpis?.missingAck ?? '-'} 
          icon={<FileWarning className="w-10 h-10 text-red-400" />}
          isRedFlag={true}
        />
      </div>

      {/* Decision Queue Table */}
      <div className="bg-slate-800/40 backdrop-blur-md border border-slate-700/50 rounded-2xl overflow-visible shadow-xl shadow-black/50">
        <div className="px-8 py-6 border-b border-slate-700/50 flex items-center justify-between bg-slate-800/50 rounded-t-2xl">
          <h2 className="text-2xl font-extrabold text-white flex items-center gap-3">
            <AlertTriangle className="w-7 h-7 text-amber-500 drop-shadow-[0_0_8px_rgba(245,158,11,0.5)]" />
            우선 조치 리스트
          </h2>
        </div>
        <div className="overflow-x-auto overflow-y-visible pb-10 min-h-[400px]">
          <table className="w-full text-left text-base table-fixed">
            <thead className="bg-slate-800/80 text-white text-lg font-extrabold border-b border-slate-700/50">
              <tr>
                <th className="py-5 w-[5%] text-center">위험</th>
                <th className="px-4 py-5 w-[12%]">기관명</th>
                <th className="px-4 py-5 w-[10%]">책임 관리자</th>
                <th className="px-4 py-5 w-[12%] text-center">문제 유형</th>
                <th className="px-4 py-5 w-[10%] text-center">영향 수급자</th>
                <th className="px-4 py-5 w-[14%] text-center">증거 패키지</th>
                <th className="px-4 py-5 w-[10%] text-center">경과 시간</th>
                <th className="px-4 py-5 w-[12%] text-right">예상 환수액</th>
                <th className="pr-8 pl-4 py-5 w-[15%] text-right">긴급 지시</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700/50">
              {queue.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-12 py-24 text-center">
                    <div className="flex flex-col items-center justify-center space-y-6">
                      <div className="w-28 h-28 rounded-full bg-emerald-500/10 flex items-center justify-center border border-emerald-500/20 shadow-[0_0_40px_rgba(16,185,129,0.2)]">
                        <ShieldCheck className="w-14 h-14 text-emerald-400 drop-shadow-[0_0_15px_rgba(16,185,129,0.5)]" />
                      </div>
                      <div className="text-center space-y-3">
                        <h3 className="text-3xl font-extrabold text-emerald-400 tracking-tight">모든 환수 위험 요소가 완벽히 차단되었습니다.</h3>
                        <p className="text-xl text-slate-300 font-bold">현재 기관은 100% 안전 상태입니다.</p>
                      </div>
                    </div>
                  </td>
                </tr>
              ) : (
                queue.map((item) => (
                  <tr key={item.id} className={cn(
                    "transition-colors duration-200 group",
                    item.riskLevel === 'severe' ? "bg-red-950/40 hover:bg-red-900/50" : "hover:bg-slate-700/30"
                  )}>
                    <td className="py-6 text-center">
                      {item.riskLevel === 'severe' && <AlertOctagon className="w-6 h-6 text-red-500 drop-shadow-[0_0_8px_rgba(239,68,68,0.8)] mx-auto" />}
                      {item.riskLevel === 'high' && <AlertTriangle className="w-6 h-6 text-amber-500 mx-auto" />}
                      {item.riskLevel === 'medium' && <AlertTriangle className="w-6 h-6 text-blue-500 mx-auto" />}
                    </td>
                    <td className="px-4 py-6 text-white font-bold text-lg truncate">{item.facilityName}</td>
                    <td className="px-4 py-6 text-slate-300 font-medium truncate">{item.adminName}</td>
                    <td className="px-4 py-6 text-center">
                      <span className="inline-flex items-center justify-center px-4 py-1.5 rounded-full text-sm font-bold bg-red-500/20 text-red-400 border border-red-500/30 shadow-[0_0_10px_rgba(239,68,68,0.1)] whitespace-nowrap">
                        {item.problemType}
                      </span>
                    </td>
                    <td className="px-4 py-6 text-center">
                      <div className="flex items-center justify-center gap-2 text-slate-200 font-bold text-lg">
                        <Users className="w-5 h-5 text-slate-400" />
                        {item.affectedRecipients}명
                      </div>
                    </td>
                    <td className="px-4 py-6 text-center">
                      {item.evidenceStatus === 'ready' && <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"><FileKey className="w-4 h-4"/> 완벽 방어</span>}
                      {item.evidenceStatus === 'missing' && <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-bold bg-red-500/20 text-red-400 border border-red-500/30"><FileWarning className="w-4 h-4"/> 증거 누락</span>}
                      {item.evidenceStatus === 'partial' && <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30"><FileWarning className="w-4 h-4"/> 일부 누락</span>}
                    </td>
                    <td className="px-4 py-6 text-slate-300 font-medium text-center whitespace-nowrap">{item.elapsedTime}</td>
                    <td className="px-4 py-6 text-amber-400 font-bold text-lg tracking-tight text-right whitespace-nowrap">
                      ₩{item.expectedClawback.toLocaleString()}
                    </td>
                    <td className="pr-8 pl-4 py-6 text-right relative">
                      <button 
                        onClick={(e) => toggleDropdown(item.id, e)}
                        className="inline-flex items-center justify-between gap-2 px-4 py-2.5 bg-gradient-to-r from-red-600 to-red-500 hover:from-red-500 hover:to-red-400 shadow-lg shadow-red-500/30 text-white font-bold rounded-lg transform transition-all hover:-translate-y-0.5 focus:ring-4 focus:ring-red-500/50 outline-none whitespace-nowrap w-full max-w-[150px] ml-auto"
                      >
                        <span className="flex items-center gap-2"><Send className="w-4 h-4" /> 지시하기</span>
                        <ChevronDown className={cn("w-4 h-4 transition-transform", activeDropdown === item.id ? "rotate-180" : "")} />
                      </button>

                      {/* Dropdown Menu */}
                      {activeDropdown === item.id && (
                        <div className="absolute right-8 top-full mt-2 w-56 bg-slate-800 border border-slate-600 rounded-xl shadow-2xl shadow-black/50 z-50 overflow-hidden flex flex-col animate-in fade-in slide-in-from-top-2">
                          <button 
                            onClick={(e) => handleInstruct(item.id, '현장 감사 배정', e)} 
                            className="w-full text-left px-5 py-4 hover:bg-slate-700 text-white font-bold border-b border-slate-700 transition-colors flex items-center gap-3"
                          >
                            <AlertOctagon className="w-5 h-5 text-red-400" />
                            현장 감사 배정
                          </button>
                          <button 
                            onClick={(e) => handleInstruct(item.id, '관리자 즉시 소집', e)} 
                            className="w-full text-left px-5 py-4 hover:bg-slate-700 text-white font-bold transition-colors flex items-center gap-3"
                          >
                            <Users className="w-5 h-5 text-amber-400" />
                            관리자 즉시 소집
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function KpiCard({ title, value, icon, isRedFlag = false }: { title: string, value: string | number, icon: ReactNode, isRedFlag?: boolean }) {
  return (
    <div className={cn(
      "bg-slate-800/40 backdrop-blur-md border rounded-2xl p-8 flex flex-col gap-6 relative overflow-hidden transition-all duration-300 hover:-translate-y-1",
      isRedFlag 
        ? "border-red-500/50 shadow-[0_0_20px_rgba(239,68,68,0.15)]" 
        : "border-slate-700/50 shadow-xl shadow-black/50"
    )}>
      {isRedFlag && (
        <div className="absolute top-0 left-0 w-full h-1.5 bg-gradient-to-r from-red-500 to-red-600 shadow-[0_0_10px_rgba(239,68,68,0.8)]" />
      )}
      <div className="flex items-center justify-between">
        <span className="text-white text-xl font-extrabold">{title}</span>
        <div className={cn(
          "p-3 rounded-xl border",
          isRedFlag ? "bg-red-500/10 border-red-500/20" : "bg-slate-700/50 border-slate-600/50"
        )}>
          {icon}
        </div>
      </div>
      <div className={cn(
        "text-6xl font-black tracking-tight",
        isRedFlag ? "text-red-400 drop-shadow-[0_0_12px_rgba(239,68,68,0.3)]" : "text-white"
      )}>
        {value}
      </div>
    </div>
  );
}
