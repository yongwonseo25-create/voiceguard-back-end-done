import { NextResponse } from "next/server";

/**
 * GET /api/plan-actual
 * BFF 프록시: 백엔드 /api/v2/plan 을 서버 사이드에서 호출하여 반환.
 *
 * 설계 원칙:
 *   - cache: "no-store" — 법적 의무기록은 캐시 절대 금지
 *   - 백엔드 장애 시 503 반환 — 더미 데이터 노출 완전 차단
 *   - facility_id 쿼리 파라미터 투명 전달
 */

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const facilityId = searchParams.get("facility_id");
  const qs = facilityId ? `?facility_id=${encodeURIComponent(facilityId)}` : "";

  try {
    const res = await fetch(`${BACKEND_URL}/api/v2/plan${qs}`, {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      return NextResponse.json(
        { error: `Backend error ${res.status}`, detail: text },
        { status: res.status }
      );
    }

    const data = await res.json();
    return NextResponse.json(data);

  } catch (err) {
    return NextResponse.json(
      { error: "Backend unreachable", detail: String(err) },
      { status: 503 }
    );
  }
}
