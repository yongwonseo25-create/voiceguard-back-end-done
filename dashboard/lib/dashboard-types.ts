export interface DashboardEvidence {
  id: string;
  beneficiary_id: string;
  facility_id: string;
  care_type: string | null;
  recorded_at: string;
  chain_hash: string;
  is_sealed: boolean;
  is_flagged: boolean;
}
