import React from 'react';
import { X, ShieldCheck, FileText, FileJson } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { useCertData } from '../hooks/useCertData.ts';
import WormSealBadge from './WormSealBadge.tsx';
import IntegrityQrCode from './IntegrityQrCode.tsx';

interface CertDetailDrawerProps {
  ledgerId: string | null;
  isOpen:   boolean;
  onClose:  () => void;
}

function toKST(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ko-KR', {
      timeZone: 'Asia/Seoul',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }) + ' KST';
  } catch { return iso; }
}

function HashBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-black text-gray-400 uppercase tracking-widest">{label}</span>
      <div className="bg-gray-50 rounded-xl px-3 py-2 border border-gray-100">
        <span className="font-mono text-[9px] text-gray-600 break-all leading-relaxed">{value}</span>
      </div>
    </div>
  );
}

function SkeletonBlock({ h = 'h-4' }: { h?: string }) {
  return <div className={`${h} bg-gray-200 rounded-xl animate-pulse w-full`} />;
}

export default function CertDetailDrawer({ ledgerId, isOpen, onClose }: CertDetailDrawerProps) {
  const { certData, loading, error } = useCertData(isOpen ? ledgerId : null);

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* 배경 오버레이 */}
          <motion.div
            key="overlay"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 bg-black/20 z-40"
            onClick={onClose}
          />

          {/* 드로어 패널 */}
          <motion.div
            key="drawer"
            initial={{ x: 480 }}
            animate={{ x: 0 }}
            exit={{ x: 480 }}
            transition={{ type: 'spring', stiffness: 320, damping: 32 }}
            className="fixed right-0 top-0 h-screen w-[480px] bg-white shadow-2xl z-50 flex flex-col"
          >
            {/* 헤더 */}
            <div className="flex items-center justify-between px-6 py-5 border-b border-gray-100 shrink-0">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 bg-indigo-600 rounded-xl flex items-center justify-center">
                  <ShieldCheck className="w-5 h-5 text-white" strokeWidth={2.5} />
                </div>
                <div>
                  <h2 className="text-[17px] font-black text-[#111111] leading-none">법적 증거 검증서</h2>
                  <p className="text-[11px] text-gray-400 font-bold mt-0.5">국민건강보험공단 감사 방어용</p>
                </div>
              </div>
              <button
                onClick={onClose}
                className="w-8 h-8 rounded-full flex items-center justify-center bg-gray-100 hover:bg-gray-200 transition-colors"
              >
                <X className="w-4 h-4 text-gray-600" />
              </button>
            </div>

            {/* 본문 스크롤 영역 */}
            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">

              {/* 에러 */}
              {error && (
                <div className="bg-orange-50 border border-orange-200 rounded-2xl px-4 py-3">
                  <p className="text-[13px] font-bold text-orange-700">
                    증거 데이터를 불러오지 못했습니다. 네트워크를 확인하세요.
                  </p>
                </div>
              )}

              {/* 섹션 1: WORM 봉인 상태 */}
              <section className="space-y-3">
                <h3 className="text-[12px] font-black text-gray-400 uppercase tracking-widest">
                  WORM 봉인 상태
                </h3>
                {loading || !certData ? (
                  <div className="space-y-2">
                    <SkeletonBlock h="h-10" />
                    <SkeletonBlock h="h-4" />
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">
                    <WormSealBadge
                      wormLockMode={certData.worm_lock_mode}
                      chainHash={certData.chain_hash}
                      size="lg"
                    />
                    <div className="text-[12px] text-gray-500 font-semibold">
                      보존 기한: <span className="text-gray-700 font-bold">{toKST(certData.worm_retain_until)}</span>
                    </div>
                  </div>
                )}
              </section>

              {/* 섹션 2: 해시체인 무결성 */}
              <section className="space-y-3">
                <h3 className="text-[12px] font-black text-gray-400 uppercase tracking-widest">
                  해시체인 무결성
                </h3>
                {loading || !certData ? (
                  <div className="space-y-3">
                    <SkeletonBlock h="h-12" />
                    <SkeletonBlock h="h-12" />
                    <SkeletonBlock h="h-4" />
                  </div>
                ) : (
                  <div className="space-y-3">
                    <HashBox label="Chain Hash (HMAC-SHA256)" value={certData.chain_hash} />
                    <HashBox label="Cert Self Hash (SHA256)" value={certData.cert_self_hash} />
                    <div className="grid grid-cols-2 gap-3 text-[12px]">
                      <div>
                        <span className="text-gray-400 font-bold">발급 시각</span>
                        <p className="text-gray-700 font-black text-[11px]">{toKST(certData.issued_at)}</p>
                      </div>
                      <div>
                        <span className="text-gray-400 font-bold">발급자</span>
                        <p className="text-gray-700 font-black text-[11px] font-mono">{certData.issuer_version}</p>
                      </div>
                    </div>
                  </div>
                )}
              </section>

              {/* 섹션 3: QR 코드 + 다운로드 */}
              <section className="space-y-3">
                <h3 className="text-[12px] font-black text-gray-400 uppercase tracking-widest">
                  즉시 검증 QR 코드
                </h3>
                {ledgerId ? (
                  <div className="flex flex-col items-center gap-4">
                    <IntegrityQrCode ledgerId={ledgerId} size={160} />
                    <p className="text-[11px] text-gray-400 font-semibold text-center">
                      조사관이 QR 스캔 시 즉시 무결성 검증 페이지로 이동합니다
                    </p>
                  </div>
                ) : null}

                {/* 다운로드 버튼 */}
                {certData && (
                  <div className="flex gap-3">
                    {certData.pdf_url && (
                      <a
                        href={certData.pdf_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex-1 flex items-center justify-center gap-2 bg-indigo-600 text-white rounded-xl py-3 text-[13px] font-black hover:bg-indigo-700 transition-colors"
                      >
                        <FileText className="w-4 h-4" />
                        PDF 검증서
                      </a>
                    )}
                    {certData.json_url && (
                      <a
                        href={certData.json_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex-1 flex items-center justify-center gap-2 bg-gray-900 text-white rounded-xl py-3 text-[13px] font-black hover:bg-gray-700 transition-colors"
                      >
                        <FileJson className="w-4 h-4" />
                        JSON 검증서
                      </a>
                    )}
                    {!certData.pdf_url && !certData.json_url && (
                      <div className="w-full bg-gray-50 rounded-xl py-3 text-center">
                        <span className="text-[12px] text-gray-400 font-bold">다운로드 URL 준비 중...</span>
                      </div>
                    )}
                  </div>
                )}
              </section>
            </div>

            {/* 푸터 */}
            <div className="shrink-0 px-6 py-4 border-t border-gray-100 bg-gray-50">
              <p className="text-[10px] text-gray-400 font-semibold leading-relaxed text-center">
                이 검증서는 WORM(Write Once Read Many) 불변 저장소에 봉인되었습니다.
                <br />데이터 조작은 형사 처벌 대상입니다.
              </p>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
