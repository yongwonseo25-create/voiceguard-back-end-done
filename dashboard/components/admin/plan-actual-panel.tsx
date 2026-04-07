"use client";

interface GaugeItem {
  label: string;
  value: number;
  color: string;
}

interface MatrixRow {
  beneficiary_id: string;
  beneficiary_name: string;
  care_items: { label: string; match: "full" | "partial" | "missing" }[];
}

interface Props {
  gaugeData: GaugeItem[];
  matrixRows: MatrixRow[];
  totalRisk: string;
}

const MATCH_COLOR: Record<string, string> = {
  full:    "bg-[#052E16] text-[#4ADE80] border border-[#166534]",
  partial: "bg-[#422006] text-[#FB923C] border border-[#9A3412]",
  missing: "bg-[#450A0A] text-[#F87171] border border-[#991B1B]",
};
const MATCH_LABEL: Record<string, string> = {
  full: "✅", partial: "⚠", missing: "❌",
};

export function PlanActualPanel({ gaugeData, matrixRows, totalRisk }: Props) {
  return (
    <div className="flex flex-col h-full gap-4 overflow-y-auto">
      {/* 게이지 */}
      <div className="bg-[#0E1728] rounded-xl border border-[#223049] p-4">
        <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-3">
          오늘 기록 현황
        </div>
        <div className="space-y-3">
          {gaugeData.map((g) => (
            <div key={g.label}>
              <div className="flex justify-between text-[12px] mb-1">
                <span className="text-[#94A3B8]">{g.label}</span>
                <span style={{ color: g.color }} className="font-mono font-bold">{g.value}%</span>
              </div>
              <div className="h-2 bg-[#1E2D45] rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${g.value}%`, background: g.color }}
                />
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 pt-3 border-t border-[#1E2D45]">
          <span className="text-[11px] text-[#64748B]">누적 예상 환수 위험</span>
          <div className="text-[#F87171] font-mono font-bold text-[16px] mt-0.5">{totalRisk}</div>
        </div>
      </div>

      {/* 매트릭스 */}
      <div className="bg-[#0E1728] rounded-xl border border-[#223049] p-4 flex-1">
        <div className="text-[10px] uppercase tracking-[0.18em] text-[#8FA1B9] mb-3">
          수급자별 6대 케어 매트릭스
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] border-collapse">
            <thead>
              <tr className="text-[#64748B]">
                <th className="text-left pb-2 pr-3 font-medium">수급자</th>
                {matrixRows[0]?.care_items.map((c) => (
                  <th key={c.label} className="pb-2 px-1 font-medium text-center whitespace-nowrap">{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrixRows.map((row) => (
                <tr key={row.beneficiary_id} className="border-t border-[#1E2D45]">
                  <td className="py-2 pr-3">
                    <div className="font-semibold text-[#E2E8F0]">{row.beneficiary_name}</div>
                    <div className="text-[#4A5568] font-mono">{row.beneficiary_id}</div>
                  </td>
                  {row.care_items.map((item) => (
                    <td key={item.label} className="py-2 px-1 text-center">
                      <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${MATCH_COLOR[item.match]}`}>
                        {MATCH_LABEL[item.match]}
                      </span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
