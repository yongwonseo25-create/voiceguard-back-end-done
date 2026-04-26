import React from 'react';
import { Lock, LockOpen, ShieldAlert } from 'lucide-react';
import { motion } from 'motion/react';
import type { WormLockMode } from '../types/cert.ts';

interface WormSealBadgeProps {
  wormLockMode: WormLockMode;
  chainHash:    string;
  retainUntil?: string;
  size?:        'sm' | 'md' | 'lg';
}

const LABEL: Record<WormLockMode, string> = {
  COMPLIANCE: 'WORM 봉인 완료',
  GOVERNANCE: '준수 모드 (주의)',
  UNKNOWN:    '봉인 상태 미확인',
};

const STYLE: Record<WormLockMode, { bg: string; border: string; text: string; icon: string }> = {
  COMPLIANCE: {
    bg:     'bg-green-50',
    border: 'border-green-300',
    text:   'text-green-700',
    icon:   'text-green-600',
  },
  GOVERNANCE: {
    bg:     'bg-orange-50',
    border: 'border-orange-300',
    text:   'text-orange-700',
    icon:   'text-orange-500',
  },
  UNKNOWN: {
    bg:     'bg-red-50',
    border: 'border-red-200',
    text:   'text-red-600',
    icon:   'text-red-500',
  },
};

const ICON_SIZE: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'w-3.5 h-3.5',
  md: 'w-4 h-4',
  lg: 'w-5 h-5',
};

const TEXT_SIZE: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'text-[11px]',
  md: 'text-[13px]',
  lg: 'text-[15px]',
};

export default function WormSealBadge({
  wormLockMode,
  chainHash,
  retainUntil,
  size = 'md',
}: WormSealBadgeProps) {
  const s  = STYLE[wormLockMode];
  const ic = ICON_SIZE[size];
  const tx = TEXT_SIZE[size];

  const IconComponent =
    wormLockMode === 'COMPLIANCE' ? Lock :
    wormLockMode === 'GOVERNANCE' ? LockOpen :
    ShieldAlert;

  const hash12 = (chainHash ?? '').slice(0, 12);

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.85 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: 'spring', stiffness: 320, damping: 22 }}
      className={`inline-flex flex-col items-center gap-0.5 px-2.5 py-1.5 rounded-xl border ${s.bg} ${s.border}`}
      title={retainUntil ? `보존 기한: ${retainUntil}` : undefined}
    >
      <div className={`flex items-center gap-1 ${s.text}`}>
        <IconComponent className={ic} strokeWidth={2.5} />
        <span className={`${tx} font-black tracking-tight`}>
          {LABEL[wormLockMode]}
        </span>
      </div>
      {hash12 && (
        <span className={`${tx === 'text-[11px]' ? 'text-[9px]' : 'text-[10px]'} font-mono text-gray-400 tracking-tight`}>
          {hash12}…
        </span>
      )}
    </motion.div>
  );
}
