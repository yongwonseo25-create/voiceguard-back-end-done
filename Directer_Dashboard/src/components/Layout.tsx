import { Link, Outlet, useLocation } from 'react-router-dom';
import { Shield, LayoutDashboard, Inbox, Mic } from 'lucide-react';
import { cn } from '../lib/utils';

export default function Layout() {
  const location = useLocation();

  return (
    <div className="min-h-screen bg-slate-950 text-slate-50 flex font-sans">
      {/* Sidebar */}
      <aside className="w-72 bg-slate-900/80 backdrop-blur-xl border-r border-slate-700/50 flex flex-col shadow-2xl z-10">
        <div className="p-8 flex items-center gap-4 text-white">
          <Shield className="w-10 h-10 text-blue-500 drop-shadow-[0_0_10px_rgba(59,130,246,0.5)]" />
          <span className="text-2xl font-bold tracking-tight">보이스 가드</span>
        </div>

        <nav className="flex-1 px-6 space-y-3 mt-6">
          <Link
            to="/"
            className="flex items-center gap-4 px-5 py-4 rounded-xl transition-all duration-200 hover:bg-slate-800/50 text-slate-300 hover:text-white"
          >
            <Mic className="w-6 h-6" />
            <span className="font-bold text-lg">마이크 앱</span>
          </Link>

          <Link
            to="/admin"
            className={cn(
              "flex items-center gap-4 px-5 py-4 rounded-xl transition-all duration-200",
              location.pathname === '/admin'
                ? "bg-blue-600/20 text-blue-400 border border-blue-500/30 shadow-[0_0_15px_rgba(59,130,246,0.15)]"
                : "hover:bg-slate-800/50 text-slate-300 hover:text-white"
            )}
          >
            <LayoutDashboard className="w-6 h-6" />
            <span className="font-bold text-lg">원장 관제탑</span>
          </Link>

          <Link
            to="/admin/ops"
            className={cn(
              "flex items-center gap-4 px-5 py-4 rounded-xl transition-all duration-200",
              location.pathname === '/admin/ops'
                ? "bg-blue-600/20 text-blue-400 border border-blue-500/30 shadow-[0_0_15px_rgba(59,130,246,0.15)]"
                : "hover:bg-slate-800/50 text-slate-300 hover:text-white"
            )}
          >
            <Inbox className="w-6 h-6" />
            <span className="font-bold text-lg">현장 관리 보드</span>
          </Link>
        </nav>

        <div className="p-6 border-t border-slate-700/50 text-base text-slate-400 font-medium">
          부당청구 방어 시스템 v1.0.0
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col h-screen overflow-hidden relative">
        {/* Background Glow Effects */}
        <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] rounded-full bg-blue-900/20 blur-[120px] pointer-events-none" />
        <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] rounded-full bg-emerald-900/10 blur-[120px] pointer-events-none" />

        <header className="h-20 border-b border-slate-700/50 bg-slate-900/40 backdrop-blur-md flex items-center justify-center px-10 z-10 shadow-sm">
          <h1 className="text-2xl font-bold text-white tracking-tight text-center">
            {location.pathname === '/admin/ops' ? '오늘 미조치 업무' : '환수 방어 및 리스크 컨트롤 타워'}
          </h1>
        </header>
        <div className="flex-1 overflow-auto p-10 z-10">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
