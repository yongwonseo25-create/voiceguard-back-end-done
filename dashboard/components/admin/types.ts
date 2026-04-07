export interface 알림카드데이터 {
  id: string;
  beneficiary_id: string;
  facility_id: string;
  shift_id: string;
  care_type: string | null;
  ingested_at: string;
  minutes_elapsed: number;
  예상환수액: number;
  gps_lat?: number | null;
  gps_lon?: number | null;
}
