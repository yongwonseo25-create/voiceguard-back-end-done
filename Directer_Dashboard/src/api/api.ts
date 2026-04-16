/// <reference types="vite/client" />

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

// Types
export interface KPIStats {
  redFlags: number;
  completionRate: number;
  slaExceeded: number;
  missingAck: number;
}

export interface DecisionItem {
  id: string;
  facilityName: string;
  adminName: string;
  problemType: string;
  elapsedTime: string;
  expectedClawback: number;
  affectedRecipients: number;
  evidenceStatus: 'ready' | 'missing' | 'partial';
  riskLevel: 'severe' | 'high' | 'medium';
}

export interface PendingReviewItem {
  id: string;
  title: string;
  details: string;
}

export interface ActionItem {
  id: string;
  issue: string;
  urgency: 'high' | 'medium' | 'low';
}

export interface HandoverItem {
  id: string;
  shift: string;
  briefing: string;
}

// API Skeletons
export const fetchDirectorKPIs = async (): Promise<KPIStats> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/director/kpi`);
  if (!response.ok) throw new Error('Failed to fetch KPIs');
  return response.json();
};

export const fetchDecisionQueue = async (): Promise<DecisionItem[]> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/director/decision-queue`);
  if (!response.ok) throw new Error('Failed to fetch decision queue');
  return response.json();
};

export const instructDecision = async (id: string, actionType: string): Promise<void> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/director/decision/${id}/instruct`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actionType })
  });
  if (!response.ok) throw new Error('Failed to instruct decision');
};

export const fetchPendingReviews = async (): Promise<PendingReviewItem[]> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/pending-reviews`);
  if (!response.ok) throw new Error('Failed to fetch pending reviews');
  return response.json();
};

export const approveAllReviews = async (): Promise<void> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/pending-reviews/approve-all`, { method: 'POST' });
  if (!response.ok) throw new Error('Failed to approve all reviews');
};

export const fetchActionQueue = async (): Promise<ActionItem[]> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/action-queue`);
  if (!response.ok) throw new Error('Failed to fetch action queue');
  return response.json();
};

export const resolveAction = async (id: string): Promise<void> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/action-queue/${id}/resolve`, { method: 'POST' });
  if (!response.ok) throw new Error('Failed to resolve action');
};

export const fetchHandovers = async (): Promise<HandoverItem[]> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/handovers`);
  if (!response.ok) throw new Error('Failed to fetch handovers');
  return response.json();
};

export const ackHandover = async (id: string): Promise<void> => {
  const response = await fetch(`${API_BASE_URL}/api/v8/admin/handovers/${id}/ack`, { method: 'POST' });
  if (!response.ok) throw new Error('Failed to ack handover');
};
