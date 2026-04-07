import './globals.css';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'VoiceGuard Admin Dashboard',
  description: '미기록 실시간 대응 상황판',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // suppressHydrationWarning을 추가하여 브라우저 확장 프로그램 등에 의한 Hydration 에러 방지
    <html lang="ko" suppressHydrationWarning>
      <body className="bg-slate-900 text-slate-100 antialiased" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
