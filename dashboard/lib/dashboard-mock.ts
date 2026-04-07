import type { DashboardEvidence } from "./dashboard-types";

export const gaugeData = [
  { label: "미기록률", value: 23, color: "#ef4444" },
  { label: "봉인 완료", value: 77, color: "#22c55e" },
];

export const matrixRows = [
  {
    beneficiary_id: "BEN-001",
    beneficiary_name: "김○○",
    care_items: [
      { label: "식사 보조", match: "full"    as const },
      { label: "배변 보조", match: "missing" as const },
      { label: "체위 변경", match: "full"    as const },
      { label: "구강 위생", match: "missing" as const },
      { label: "목욕 보조", match: "full"    as const },
      { label: "이동 보조", match: "missing" as const },
    ],
  },
  {
    beneficiary_id: "BEN-002",
    beneficiary_name: "이○○",
    care_items: [
      { label: "식사 보조", match: "full"    as const },
      { label: "배변 보조", match: "full"    as const },
      { label: "체위 변경", match: "missing" as const },
      { label: "구강 위생", match: "full"    as const },
      { label: "목욕 보조", match: "missing" as const },
      { label: "이동 보조", match: "full"    as const },
    ],
  },
];

// 정적 타임스탬프 — Date.now() 제거 (서버/클라이언트 불일치 Hydration 에러 방지)
export const evidenceRows: DashboardEvidence[] = [
  {
    id: "ev-001",
    beneficiary_id: "BEN-001",
    facility_id: "FAC-서울요양원",
    care_type: "식사 보조",
    recorded_at: "2026-04-05T14:19:00.000Z",
    chain_hash: "pending",
    is_sealed: false,
    is_flagged: false,
  },
  {
    id: "ev-002",
    beneficiary_id: "BEN-002",
    facility_id: "FAC-서울요양원",
    care_type: "배변 보조",
    recorded_at: "2026-04-05T14:04:00.000Z",
    chain_hash: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    is_sealed: true,
    is_flagged: false,
  },
];
