import type { 알림카드데이터 } from "./types";

// 정적 타임스탬프 사용 — Date.now()는 서버/클라이언트 평가 시점 차이로 Hydration 에러 유발
export const 알림카드목록: 알림카드데이터[] = [
  {
    id: "alert-001",
    beneficiary_id: "BEN-001",
    facility_id: "FAC-서울요양원",
    shift_id: "SHIFT-20260405-AM",
    care_type: "식사 보조",
    ingested_at: "2026-04-05T14:26:00.000Z",
    minutes_elapsed: 3.0,
    예상환수액: 125000,
    gps_lat: 37.5665,
    gps_lon: 126.978,
  },
  {
    id: "alert-002",
    beneficiary_id: "BEN-002",
    facility_id: "FAC-서울요양원",
    shift_id: "SHIFT-20260405-AM",
    care_type: "배변 보조",
    ingested_at: "2026-04-05T14:27:30.000Z",
    minutes_elapsed: 1.5,
    예상환수액: 98000,
  },
  {
    id: "alert-003",
    beneficiary_id: "BEN-003",
    facility_id: "FAC-강남노인센터",
    shift_id: "SHIFT-20260405-PM",
    care_type: "체위 변경",
    ingested_at: "2026-04-05T14:24:12.000Z",
    minutes_elapsed: 4.8,
    예상환수액: 87500,
  },
];
