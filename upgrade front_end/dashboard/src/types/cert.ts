// Evaluator 잠금 — Generator 수정 금지
export type WormLockMode = 'COMPLIANCE' | 'GOVERNANCE' | 'UNKNOWN';

export interface CertData {
  ledger_id:         string;
  seal_event_id:     string;
  chain_hash:        string;     // 64자 hex — 표시 시 전체 노출 필수
  cert_self_hash:    string;     // 64자 hex
  worm_lock_mode:    WormLockMode;
  worm_retain_until: string;     // ISO-8601
  pdf_url?:          string;     // 1시간 TTL B2 서명 URL (verify 엔드포인트 미노출)
  json_url?:         string;
  issued_at:         string;
  issuer_version:    string;
}
