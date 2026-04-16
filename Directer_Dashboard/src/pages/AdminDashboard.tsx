import { useEffect, useState, ReactNode } from 'react';
import { CheckCircle2, AlertCircle, FileText, Check } from 'lucide-react';
import { 
  fetchPendingReviews, approveAllReviews, 
  fetchActionQueue, resolveAction, 
  fetchHandovers, ackHandover,
  type PendingReviewItem, type ActionItem, type HandoverItem
} from '../api/api';
import { cn } from '../lib/utils';

type Tab = 'pending' | 'actions' | 'handovers';

export default function AdminDashboard() {
  const [activeTab, setActiveTab] = useState<Tab>('pending');
  
  const [pendingReviews, setPendingReviews] = useState<PendingReviewItem[]>([]);
  const [actionQueue, setActionQueue] = useState<ActionItem[]>([]);
  const [handovers, setHandovers] = useState<HandoverItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetchPendingReviews().catch(() => []),
      fetchActionQueue().catch(() => []),
      fetchHandovers().catch(() => [])
    ]).then(([reviews, actions, handoverData]) => {
      setPendingReviews(reviews);
      setActionQueue(actions);
      setHandovers(handoverData);
      setLoading(false);
    });
  }, []);

  const handleApproveAll = async () => {
    try {
      await approveAllReviews();
      setPendingReviews([]);
    } catch (error) {
      console.error('전체 승인 실패', error);
    }
  };

  const handleResolveAction = async (id: string) => {
    try {
      await resolveAction(id);
      setActionQueue(q => q.filter(item => item.id !== id));
    } catch (error) {
      console.error('해결 완료 처리 실패', error);
    }
  };

  const handleAckHandover = async (id: string) => {
    try {
      await ackHandover(id);
      setHandovers(h => h.filter(item => item.id !== id));
    } catch (error) {
      console.error('인수인계 확인 실패', error);
    }
  };

  return (
    <div className="max-w-[1200px] mx-auto flex flex-col h-full gap-8">
      {/* Tabs */}
      <div className="flex space-x-2 bg-slate-800/40 backdrop-blur-md p-2 rounded-2xl border border-slate-700/50 shrink-0 shadow-xl shadow-black/50">
        <TabButton 
          active={activeTab === 'pending'} 
          onClick={() => setActiveTab('pending')}
          icon={<FileText className="w-5 h-5" />}
          label="반영 대기함"
          count={pendingReviews.length}
        />
        <TabButton 
          active={activeTab === 'actions'} 
          onClick={() => setActiveTab('actions')}
          icon={<AlertCircle className="w-5 h-5" />}
          label="통합 긴급 지시"
          count={actionQueue.length}
        />
        <TabButton 
          active={activeTab === 'handovers'} 
          onClick={() => setActiveTab('handovers')}
          icon={<CheckCircle2 className="w-5 h-5" />}
          label="인수인계 승인판"
          count={handovers.length}
        />
      </div>

      {/* Content Area */}
      <div className="flex-1 overflow-hidden flex flex-col bg-slate-800/40 backdrop-blur-md border border-slate-700/50 rounded-2xl shadow-2xl shadow-black/50">
        {activeTab === 'pending' && (
          <div className="flex flex-col h-full">
            <div className="p-6 border-b border-slate-700/50 flex justify-between items-center bg-slate-800/60">
              <h3 className="text-white font-bold text-xl">기존 시스템 전송 전 1차 검수</h3>
              <button 
                onClick={handleApproveAll}
                disabled={pendingReviews.length === 0}
                className="px-6 py-3 bg-gradient-to-r from-emerald-600 to-emerald-500 hover:from-emerald-500 hover:to-emerald-400 disabled:from-slate-600 disabled:to-slate-700 disabled:text-slate-400 disabled:shadow-none disabled:transform-none shadow-lg shadow-emerald-500/30 text-white text-lg font-bold rounded-xl transform transition-all hover:-translate-y-0.5"
              >
                전체 승인
              </button>
            </div>
            <div className="flex-1 overflow-auto p-6 space-y-4">
              {pendingReviews.length === 0 ? (
                <EmptyState message="모든 검수가 완료되었습니다. (오늘 미조치 업무 완료)" />
              ) : (
                pendingReviews.map(item => (
                  <div key={item.id} className="p-6 rounded-xl border border-slate-600/50 bg-slate-800/80 flex justify-between items-start hover:bg-slate-700/50 transition-colors shadow-md">
                    <div>
                      <h4 className="text-white font-bold text-lg">{item.title}</h4>
                      <p className="text-slate-300 text-base mt-2 leading-relaxed">{item.details}</p>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeTab === 'actions' && (
          <div className="flex flex-col h-full">
            <div className="p-6 border-b border-slate-700/50 bg-slate-800/60">
              <h3 className="text-white font-bold text-xl">오늘 당장 해결해야 할 이슈</h3>
            </div>
            <div className="flex-1 overflow-auto p-6 space-y-4">
              {actionQueue.length === 0 ? (
                <EmptyState message="현재 대기 중인 긴급 지시가 없습니다. (오늘 미조치 업무 완료)" />
              ) : (
                actionQueue.map(item => (
                  <div key={item.id} className="p-6 rounded-xl border border-slate-600/50 bg-slate-800/80 flex justify-between items-center hover:bg-slate-700/50 transition-colors shadow-md group">
                    <div className="flex items-center gap-4">
                      <div className={cn(
                        "w-3 h-3 rounded-full shadow-[0_0_8px_currentColor]",
                        item.urgency === 'high' ? "bg-red-500 text-red-500" : item.urgency === 'medium' ? "bg-amber-500 text-amber-500" : "bg-blue-500 text-blue-500"
                      )} />
                      <span className="text-white font-bold text-lg">{item.issue}</span>
                    </div>
                    <button 
                      onClick={() => handleResolveAction(item.id)}
                      className="flex items-center gap-2 px-4 py-2 text-slate-300 font-bold hover:text-emerald-400 hover:bg-emerald-400/20 rounded-lg transition-all duration-200"
                      title="해결 완료"
                    >
                      <Check className="w-6 h-6" />
                      <span>해결 완료</span>
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeTab === 'handovers' && (
          <div className="flex flex-col h-full">
            <div className="p-6 border-b border-slate-700/50 bg-slate-800/60">
              <h3 className="text-white font-bold text-xl">이전 교대조 브리핑 확인</h3>
            </div>
            <div className="flex-1 overflow-auto p-6 space-y-4">
              {handovers.length === 0 ? (
                <EmptyState message="확인할 인수인계 사항이 없습니다. (오늘 미조치 업무 완료)" />
              ) : (
                handovers.map(item => (
                  <div key={item.id} className="p-6 rounded-xl border border-slate-600/50 bg-slate-800/80 flex flex-col gap-5 hover:bg-slate-700/50 transition-colors shadow-md">
                    <div className="flex justify-between items-start">
                      <span className="inline-flex items-center px-4 py-1.5 rounded-full text-sm font-bold bg-blue-500/20 text-blue-300 border border-blue-500/30 shadow-[0_0_10px_rgba(59,130,246,0.15)]">
                        {item.shift}
                      </span>
                      <button 
                        onClick={() => handleAckHandover(item.id)}
                        className="px-6 py-2.5 bg-gradient-to-r from-blue-600 to-blue-500 hover:from-blue-500 hover:to-blue-400 shadow-lg shadow-blue-500/30 text-white text-base font-bold rounded-xl transform transition-all hover:-translate-y-0.5"
                      >
                        확인 (승인)
                      </button>
                    </div>
                    <p className="text-slate-200 text-lg leading-relaxed bg-slate-900/50 p-5 rounded-xl border border-slate-700/50">
                      {item.briefing}
                    </p>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function TabButton({ active, onClick, icon, label, count }: { active: boolean, onClick: () => void, icon: ReactNode, label: string, count: number }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex-1 flex items-center justify-center gap-3 py-4 px-6 rounded-xl text-lg font-bold transition-all duration-300",
        active 
          ? "bg-slate-700/80 text-white shadow-lg shadow-black/20 border border-slate-600/50" 
          : "text-slate-400 hover:text-white hover:bg-slate-700/40 border border-transparent"
      )}
    >
      {icon}
      {label}
      {count > 0 && (
        <span className={cn(
          "ml-2 px-3 py-1 rounded-full text-sm font-extrabold",
          active ? "bg-blue-500/30 text-blue-300 border border-blue-500/30" : "bg-slate-800 text-slate-500 border border-slate-700"
        )}>
          {count}
        </span>
      )}
    </button>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="h-full flex flex-col items-center justify-center text-slate-400 space-y-6">
      <div className="w-24 h-24 rounded-full bg-slate-800/50 flex items-center justify-center border border-slate-700/50 shadow-inner">
        <CheckCircle2 className="w-12 h-12 text-emerald-500/60 drop-shadow-[0_0_10px_rgba(16,185,129,0.3)]" />
      </div>
      <p className="text-xl font-bold text-slate-300">{message}</p>
    </div>
  );
}
