import React from 'react';
import { useParams } from 'react-router-dom';
import { ShieldCheck, ShieldAlert, AlertCircle, Lock } from 'lucide-react';
import { motion } from 'motion/react';
import { useVerifyCert } from '../hooks/useCertData.ts';
import type { WormLockMode } from '../types/cert.ts';

function toKST(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ko-KR', {
      timeZone: 'Asia/Seoul',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }) + ' KST';
  } catch { return iso; }
}

function HashRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-black text-gray-400 uppercase tracking-widest">{label}</span>
      <div className="bg-gray-50 rounded-2xl px-4 py-3 border border-gray-100">
        <span className="font-mono text-[10px] text-gray-600 break-all leading-relaxed">{value}</span>
      </div>
    </div>
  );
}

type VerifyStatus = 'loading' | 'compliance' | 'warning' | 'error';

function getStatus(loading: boolean, error: string | null, wormLockMode?: WormLockMode): VerifyStatus {
  if (loading) return 'loading';
  if (error)   return 'error';
  if (wormLockMode === 'COMPLIANCE') return 'compliance';
  return 'warning';
}

const STATUS_CONFIG = {
  compliance: {
    bg:       'bg-green-50',
    iconBg:   'bg-green-100',
    icon:     <ShieldCheck className="w-20 h-20 text-green-600" strokeWidth={1.5} />,
    lockIcon: <Lock className="w-5 h-5 text-green-600" strokeWidth={2.5} />,
    title:    'WORM 봉인 완료 — 데이터 무결성 확인됨',
    subtitle: '이 기록은 조작이 불가능하도록 COMPLIANCE 모드로 봉인되었습니다.',
    titleColor: 'text-green-700',
  },
  warning: {
    bg:       'bg-orange-50',
    iconBg:   'bg-orange-100',
    icon:     <ShieldAlert className="w-20 h-20 text-orange-500" strokeWidth={1.5} />,
    lockIcon: <ShieldAlert className="w-5 h-5 text-orange-500" strokeWidth={2.5} />,
    title:    '봉인 상태 경고 — 관리자에게 문의하세요',
    subtitle: 'COMPLIANCE 봉인이 확인되지 않았습니다. 시스템 관리자에게 즉시 문의하세요.',
    titleColor: 'text-orange-600',
  },
  error: {
    bg:       'bg-red-50',
    iconBg:   'bg-red-100',
    icon:     <AlertCircle className="w-20 h-20 text-red-500" strokeWidth={1.5} />,
    lockIcon: <AlertCircle className="w-5 h-5 text-red-500" strokeWidth={2.5} />,
    title:    '검증 실패 — 유효하지 않은 검증 코드입니다',
    subtitle: '해당 증거 ID가 존재하지 않거나 접근이 차단되었습니다.',
    titleColor: 'text-red-600',
  },
  loading: {
    bg:       'bg-gray-50',
    iconBg:   'bg-gray-100',
    icon:     <ShieldCheck className="w-20 h-20 text-gray-300 animate-pulse" strokeWidth={1.5} />,
    lockIcon: null,
    title:    '검증 중...',
    subtitle: '무결성 데이터를 확인하고 있습니다.',
    titleColor: 'text-gray-500',
  },
};

export default function VerifyPage() {
  const { ledger_id } = useParams<{ ledger_id: string }>();
  const { certData, loading, error } = useVerifyCert(ledger_id ?? null);

  const status = getStatus(loading, error, certData?.worm_lock_mode);
  const cfg    = STATUS_CONFIG[status];

  return (
    <div className="min-h-screen bg-white font-sans antialiased text-[#111111]">

      {/* 판정 헤더 */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className={`w-full ${cfg.bg} py-12 px-6 flex flex-col items-center gap-6`}
      >
        <div className={`w-32 h-32 ${cfg.iconBg} rounded-full flex items-center justify-center shadow-inner`}>
          {cfg.icon}
        </div>
        <div className="text-center max-w-lg">
          <h1 className={`text-[26px] font-black ${cfg.titleColor} leading-tight mb-2`}>
            {cfg.title}
          </h1>
          <p className="text-[15px] text-gray-500 font-semibold leading-snug">
            {cfg.subtitle}
          </p>
        </div>
      </motion.div>

      {/* 해시체인 상세 */}
      <div className="max-w-2xl mx-auto px-6 py-8 space-y-6">

        {loading && (
          <div className="space-y-4">
            {[1, 2, 3].map(i => (
              <div key={i} className="h-16 bg-gray-100 rounded-2xl animate-pulse" />
            ))}
          </div>
        )}

        {certData && !loading && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.4 }}
            className="space-y-4"
          >
            <HashRow label="Chain Hash (HMAC-SHA256)" value={certData.chain_hash} />
            <HashRow label="Cert Self Hash (SHA256)"  value={certData.cert_self_hash} />

            <div className="grid grid-cols-2 gap-4">
              <div className="bg-gray-50 rounded-2xl px-4 py-3 border border-gray-100">
                <span className="text-[10px] font-black text-gray-400 uppercase tracking-widest block mb-1">보존 기한</span>
                <span className="text-[12px] font-bold text-gray-700">{toKST(certData.worm_retain_until)}</span>
              </div>
              <div className="bg-gray-50 rounded-2xl px-4 py-3 border border-gray-100">
                <span className="text-[10px] font-black text-gray-400 uppercase tracking-widest block mb-1">발급 시각</span>
                <span className="text-[12px] font-bold text-gray-700">{toKST(certData.issued_at)}</span>
              </div>
            </div>
          </motion.div>
        )}

        {/* 검증 방법 (조사관용) */}
        <div className="bg-gray-50 rounded-2xl p-6 border border-gray-100 space-y-3">
          <h2 className="text-[13px] font-black text-gray-500 uppercase tracking-widest">검증 방법 (조사관용)</h2>
          {[
            'WORM 저장소에서 위 경로의 음성 파일을 수령합니다.',
            'SHA-256(음성파일) = chain_hash 앞 부분과 일치하는지 확인합니다.',
            'B2 head_object → ObjectLockMode = COMPLIANCE 확인합니다.',
            '모든 값 일치 시 무결성 100% 보장됩니다.',
          ].map((step, i) => (
            <div key={i} className="flex gap-3">
              <span className="text-[11px] font-black text-indigo-500 shrink-0 w-4">{i + 1}.</span>
              <p className="text-[12px] text-gray-600 font-semibold leading-snug">{step}</p>
            </div>
          ))}
        </div>

        {/* 브랜딩 푸터 */}
        <div className="text-center pt-4 pb-8 border-t border-gray-100">
          <div className="inline-flex items-center gap-2 mb-2">
            <ShieldCheck className="w-5 h-5 text-indigo-600" strokeWidth={2.5} />
            <span className="text-[14px] font-black text-indigo-700">Voice Guard</span>
          </div>
          <p className="text-[11px] text-gray-400 font-semibold">
            국민건강보험공단 현지조사 대응 무결성 검증 시스템
          </p>
          {certData && (
            <p className="text-[10px] font-mono text-gray-300 mt-1">{certData.issuer_version}</p>
          )}
        </div>
      </div>
    </div>
  );
}
