/**
 * Voice Guard — dashboard/lib/api.ts
 * 백엔드 API 클라이언트 + 타입 정의
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ══════════════════════════════════════════════════════════════════
// 타입 정의
// ══════════════════════════════════════════════════════════════════

export interface AlertRecord {
  id: string;
  facility_id: string;
  beneficiary_id: string;
  shift_id: string;
  care_type: string | null;
  ingested_at: string;
  gps_lat: number | null;
  gps_lon: number | null;
  is_flagged: boolean;
  sync_status: string;
  sync_attempts: number;
  minutes_elapsed: number;
}

export interface AuditRecord {
  id: string;
  facility_id: string;
  beneficiary_id: string;
  shift_id: string;
  care_type: string | null;
  recorded_at: string;
  ingested_at: string;
  audio_sha256: string;
  chain_hash: string;
  worm_bucket: string;
  worm_object_key: string;
  worm_retain_until: string;
  has_audio: boolean;
  is_sealed: boolean;
  is_flagged: boolean;
  outbox_status: string;
}

export interface CareItem {
  label: string;
  match: "full" | "partial" | "missing";
}

export interface PlanRecord {
  beneficiary_id: string;
  beneficiary_name: string;
  care_items: CareItem[];
}

export interface ResolutionPayload {
  cause: string;
  memo: string;
}

export interface DirectivePayload {
  beneficiary_id: string;
  action: "field_check" | "freeze" | "escalate" | "memo_only";
  reason: string;
  memo?: string;
  commanded_by?: string;
}

export interface DirectiveResult {
  issued: boolean;
  command_id: string;
  outbox_id: string;
  beneficiary_id: string;
  action: string;
  commanded_at: string;
}

export interface NotificationLog {
  id: string;
  ledger_id: string | null;
  trigger_type: "NT-1" | "NT-2" | "NT-3";
  recipient_phone: string;
  template_code: string;
  status: "sent" | "failed";
  error_msg: string | null;
  sent_at: string;
}

// ══════════════════════════════════════════════════════════════════
// API 함수
// ══════════════════════════════════════════════════════════════════

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`[${res.status}] ${path}: ${text}`);
  }
  return res.json() as Promise<T>;
}

/** GET /api/v2/alerts — N분 이내 미처리 건 */
export async function fetchAlerts(minutes = 5): Promise<AlertRecord[]> {
  const data = await apiFetch<{ alerts: AlertRecord[] }>(
    `/api/v2/alerts?minutes=${minutes}`
  );
  return data.alerts;
}

/** GET /api/v2/audit — 현지조사 방어 뷰 */
export async function fetchAuditRecords(
  facilityId?: string
): Promise<AuditRecord[]> {
  const qs = facilityId ? `?facility_id=${encodeURIComponent(facilityId)}` : "";
  const data = await apiFetch<{ records: AuditRecord[] }>(
    `/api/v2/audit${qs}`
  );
  return data.records;
}

/** GET /api/v2/plan — 급여 계획 대조 매트릭스 */
export async function fetchPlanVsActual(): Promise<PlanRecord[]> {
  const data = await apiFetch<{ plans: PlanRecord[] }>("/api/v2/plan");
  return data.plans;
}

/** PATCH /api/v2/evidence/:id — 미기록 건 처리 사유 기록 */
export async function patchEvidenceResolution(
  ledgerId: string,
  payload: ResolutionPayload
): Promise<void> {
  await apiFetch<void>(`/api/v2/evidence/${encodeURIComponent(ledgerId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** POST /api/v2/directive — 원장 하향식 지시 발행 (원자 트랜잭션) */
export async function postDirective(
  payload: DirectivePayload
): Promise<DirectiveResult> {
  return apiFetch<DirectiveResult>("/api/v2/directive", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** GET /api/v2/directive — 지시 원장 이력 조회 */
export async function fetchDirectives(
  beneficiaryId?: string
): Promise<DirectiveResult[]> {
  const qs = beneficiaryId
    ? `?beneficiary_id=${encodeURIComponent(beneficiaryId)}`
    : "";
  const data = await apiFetch<{ directives: DirectiveResult[] }>(
    `/api/v2/directive${qs}`
  );
  return data.directives;
}
