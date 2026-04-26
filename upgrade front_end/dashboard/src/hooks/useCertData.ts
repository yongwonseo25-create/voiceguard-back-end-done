import { useState, useEffect } from 'react';
import type { CertData } from '../types/cert.ts';

const API_BASE = (import.meta as { env?: { VITE_API_URL?: string } }).env?.VITE_API_URL ?? '';

export function useCertData(ledgerId: string | null) {
  const [certData, setCertData] = useState<CertData | null>(null);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState<string | null>(null);

  useEffect(() => {
    if (!ledgerId) {
      setCertData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetch(`${API_BASE}/api/v2/cert/${ledgerId}`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<CertData>;
      })
      .then(data => {
        if (!cancelled) { setCertData(data); setLoading(false); }
      })
      .catch((e: Error) => {
        if (!cancelled) { setError(e.message); setLoading(false); }
      });

    return () => { cancelled = true; };
  }, [ledgerId]);

  return { certData, loading, error };
}

// 공개 검증 엔드포인트 (QR 스캔 → VerifyPage 전용, auth 불필요)
export function useVerifyCert(ledgerId: string | null) {
  const [certData, setCertData] = useState<CertData | null>(null);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState<string | null>(null);

  useEffect(() => {
    if (!ledgerId) return;
    let cancelled = false;
    setLoading(true);

    fetch(`${API_BASE}/api/v2/cert/${ledgerId}/verify`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<CertData>;
      })
      .then(data => {
        if (!cancelled) { setCertData(data); setLoading(false); }
      })
      .catch((e: Error) => {
        if (!cancelled) { setError(e.message); setLoading(false); }
      });

    return () => { cancelled = true; };
  }, [ledgerId]);

  return { certData, loading, error };
}
