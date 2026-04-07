# 긴급 복구: PlanVsActualPanel.tsx 원본 코드 100% 덮어쓰기 지시

너는 디자인을 수정할 권한이 없다. 아래 제공되는 코드를 단 1글자의 클래스명 수정 없이, components/admin/PlanVsActualPanel.tsx 파일에 100% 덮어쓰기(Overwrite) 해라.

```tsx
"use client";
import { useState, useEffect } from "react";

export interface PlanVsActualPanelProps { 
  onSelectRiskItem: (recipientName: string, missingItem: string) => void; 
}

interface RecipientData { 
  name: string; 
  items: { [key: string]: "완료" | "미기록"; }; 
}

interface DashboardData { 
  matchRate: number; 
  estimatedRiskAmount: number; 
  recipients: RecipientData[]; 
}

const COLUMNS = ["식사", "체위변경", "투약", "배설", "개인위생", "활동"];

export default function PlanVsActualPanel({ onSelectRiskItem }: PlanVsActualPanelProps) { 
  const [data, setData] = useState<DashboardData | null>(null); 
  const [loading, setLoading] = useState(true); 
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { 
    const fetchData = async () => { 
      try { 
        const res = await fetch("/api/plan-actual"); 
        if (!res.ok) throw new Error("데이터를 불러오는데 실패했습니다."); 
        const json = await res.json(); 
        setData(json); 
      } catch (err: any) { 
        setError(err.message); 
      } finally { 
        setLoading(false); 
      } 
    };
    fetchData();
  }, []);

  if (loading) { 
    return ( 
      <div className="flex h-full min-h-[400px] items-center justify-center bg-slate-900 text-slate-300 p-6 rounded-xl border border-slate-800"> 
        데이터 로딩 중... 
      </div> 
    ); 
  }

  if (error || !data) { 
    return ( 
      <div className="flex h-full min-h-[400px] items-center justify-center bg-slate-900 text-red-400 p-6 rounded-xl border border-slate-800"> 
        {error || "데이터가 없습니다."} 
      </div> 
    ); 
  }

  return ( 
    <div className="flex flex-col h-full bg-slate-900 rounded-xl border border-slate-800 overflow-hidden shadow-xl"> 
      
      {/* 1. 상단 KPI 영역 */} 
      <div className="flex items-center justify-between p-6 border-b border-slate-800 bg-slate-900/80"> 
        <div> 
          <h2 className="text-sm font-medium text-slate-400 mb-1">전체 증거 일치율</h2> 
          <div className="flex items-end gap-2"> 
            <span className="text-4xl font-bold text-emerald-400">{data.matchRate}%</span> 
          </div> 
        </div> 
        <div className="text-right"> 
          <h2 className="text-sm font-medium text-slate-400 mb-1">예상 환수 리스크 금액</h2> 
          <div className="text-3xl font-bold text-red-500"> 
            {data.estimatedRiskAmount.toLocaleString()}원 
          </div> 
        </div> 
      </div>

      {/* 2. 데이터 그리드 표 영역 (사령관님이 추출하신 완벽한 조각) */}
      <div className="flex-1 overflow-auto p-6">
        <table className="w-full text-sm text-left text-slate-300">
          <thead className="text-xs text-slate-400 uppercase bg-slate-800/50">
            <tr>
              <th className="px-4 py-4 font-medium rounded-tl-md">수급자</th>
              {COLUMNS.map((col) => (
                <th key={col} className="px-4 py-4 font-medium text-center">
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/50">
            {data.recipients.map((recipient, idx) => (
              <tr key={idx} className="hover:bg-slate-800/30 transition-colors">
                <td className="px-4 py-4 font-medium text-slate-200">{recipient.name}</td>
                {COLUMNS.map((col) => {
                  const status = recipient.items[col];
                  const isMissing = status === "미기록";
                  return (
                    <td key={col} className="px-2 py-3 text-center">
                      {isMissing ? (
                        <button
                          onClick={() => onSelectRiskItem(recipient.name, col)}
                          className="w-full py-2 px-2 rounded bg-red-900/40 text-red-400 font-bold hover:bg-red-800/60 transition-colors cursor-pointer border border-red-800/50 shadow-sm"
                        >
                          미기록
                        </button>
                      ) : (
                        <div className="w-full py-2 px-2 text-center text-slate-500 font-medium cursor-not-allowed">
                          완료
                        </div>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

    </div> 
  ); 
}