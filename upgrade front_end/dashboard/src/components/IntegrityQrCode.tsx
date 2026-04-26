import React from 'react';
import { QRCodeSVG } from 'qrcode.react';

const VERIFY_BASE = 'https://voice-guard-pilot.web.app/admin/verify';

interface IntegrityQrCodeProps {
  ledgerId: string;
  size?:    number;
}

export default function IntegrityQrCode({ ledgerId, size = 128 }: IntegrityQrCodeProps) {
  // QR 타깃: /api/v2/cert/{id}/verify 공개 엔드포인트 (pdf_url, json_url 미포함)
  const verifyUrl = `${VERIFY_BASE}/${ledgerId}`;
  const shortId   = ledgerId.slice(0, 8);

  return (
    <div className="flex flex-col items-center gap-2">
      <a
        href={verifyUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="rounded-xl overflow-hidden border border-gray-200 shadow-sm hover:shadow-md transition-shadow p-2 bg-white"
        title="스캔 시 즉시 무결성 검증 페이지로 이동"
      >
        <QRCodeSVG
          value={verifyUrl}
          size={size}
          bgColor="#ffffff"
          fgColor="#111111"
          level="M"
          includeMargin={false}
        />
      </a>
      <span className="text-[10px] font-mono text-gray-400 tracking-tight">
        /verify/{shortId}…
      </span>
    </div>
  );
}
