# Voice Guard — 아키텍처 레벨 무결점 수정 설계도
## next_session_handoff.md (v2 — Architect Edition)

> **작성 일시:** 2026-04-14
> **작성 역할:** 최고급 아키텍트 에이전트 (Architect)
> **수신자:** 다음 세션 구현 에이전트 (Generator) — 이 문서만 읽고 즉시 타격 가능하도록 설계됨
> **이전 버전 v1 완전 대체** — v1(적대적 검수자 작성)은 폐기

---

## ── CHAPTER 0. 시스템 컨텍스트 & 불변 원칙 ──────────────────────────

### 프로젝트 정체
Voice Guard = 요양 시설 6대 의무기록(식사·투약·배설·체위변경·위생·특이사항)을
**WORM 해시체인 원장**에 Append-Only로 봉인하는 **법적 증거 인프라**.
모든 수정은 이 목적에 복무해야 한다.

### 불변 원칙 (수정 에이전트 준수 사항)
1. `evidence_ledger`, `care_record_ledger`, `care_plan_ledger` **스키마 변경 절대 금지**
2. 원장 테이블 **UPDATE / DELETE 절대 금지** — 보정은 새 row INSERT
3. 코드 수정 범위: `FRONT END/`, `dashboard/`, `backend/` **3개 폴더만**
4. **신규 기능 추가 금지** — 결함 수정 전용 세션

### 수정 대상 결함 목록 (총 9건 — 필수)
| ID | 등급 | 위치 | 1줄 요약 |
|---|---|---|---|
| C-1 | 🔴 CRITICAL | `FRONT END/src/services/` | DEV_MOCK 하드코딩으로 실서버 통신 전면 차단 |
| C-2 | 🔴 CRITICAL | `FRONT END/src/services/handoverApi.ts` | postHandoverRecord가 잘못된 엔드포인트·파라미터 구조 사용 |
| C-3 | 🔴 CRITICAL | `dashboard/app/api/plan-actual/route.ts` | 하드코딩 더미 데이터 — 실 API 전혀 미호출 |
| C-4 | 🔴 CRITICAL | `dashboard/app/page.tsx` | 법적 증거 화면에 클라이언트 위조 SHA-256 표시 |
| M-1 | 🟡 MEDIUM | `backend/main.py` | SSE keep-alive `asyncio.sleep(0)` → 루프당 즉시 발사 |
| M-2 | 🟡 MEDIUM | `backend/notifier.py` | `datetime.now()` 타임존 없음 → KST 9시간 오차 |
| M-3 | 🟡 MEDIUM | `backend/notifier.py` + `main.py` | fanout `ledger_id=None` → Redis 장애 시 중복 발송 |
| M-4 | 🟡 MEDIUM | `dashboard/lib/` vs `dashboard/app/hooks/` | useVoiceGuardSSE 훅 이원화 — 구버전 잔존 |
| M-5 | 🟡 MEDIUM | `FRONT END/src/services/api.ts` | SSE `onerror`에서 `close()` 호출 → 재연결 영구 차단 |

---

## ── CHAPTER 1. 아키텍처 설계 원칙 ────────────────────────────────────

### 설계 철학: 땜질 금지, 구조 복원
각 결함의 근본 원인은 다음 3가지 구조적 문제에서 파생된다:

```
① 환경 분기 로직의 분산 (C-1)
   → 해결: 환경 감지를 단일 진실원(Single Source of Truth)으로 집중

② API 계약(Contract) 불일치 (C-2, C-3, C-4)
   → 해결: FE와 BE 간 인터페이스를 백엔드 실제 스펙에 맞춰 재정렬

③ 비동기 리소스 생명주기 오관리 (M-1, M-5)
   → 해결: 비동기 루프/이벤트 핸들러의 올바른 패턴 적용
```

---

## ── CHAPTER 2. CRITICAL 수정 설계 ──────────────────────────────────

---

### [C-1] DEV_MOCK 하드코딩 — 환경 감지 단일화 설계

#### 근본 원인
`api.ts`와 `handoverApi.ts` **두 파일이 각자 독립적으로** 환경을 판단한다.
두 곳 모두 `DEV_MOCK = true`로 잠겨 있으며 주석에 복원 방법이 명시되어 있음에도
실행되지 않은 상태다.

#### 아키텍처 결정: 단일 진실원 모듈 신설
`FRONT END/src/services/` 폴더에 `config.ts`를 **신규 생성**하여
모든 서비스 파일의 환경 판단을 한 곳에서 관리한다.

```
FRONT END/src/services/
  ├── config.ts          ← 신규 생성 (환경 단일 진실원)
  ├── api.ts             ← DEV_MOCK 로직 삭제, config.ts import
  ├── handoverApi.ts     ← DEV_MOCK 로직 삭제, config.ts import
  └── deviceId.ts        ← 변경 없음
```

#### `config.ts` 설계 (신규 생성)

**파일 경로:** `FRONT END/src/services/config.ts`

```typescript
/**
 * Voice Guard — 서비스 환경 설정 단일 진실원
 *
 * 판단 기준:
 *   VITE_API_BASE_URL 이 설정된 경우  → 실서버 모드 (DEV_MOCK = false)
 *   VITE_API_BASE_URL 미설정          → 개발 목업 모드 (DEV_MOCK = true)
 *
 * .env.production 에 VITE_API_BASE_URL 이 이미 설정되어 있으므로
 * 프로덕션 빌드 시 자동으로 실서버 모드 활성화.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';
export const IS_DEV_MOCK: boolean = !API_BASE_URL;
```

#### `api.ts` 수정 지침

**삭제할 줄 (line 8):**
```typescript
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';
```

**삭제할 블록 (line 144-150):**
```typescript
// ── 🔧 DEV MOCK MODE ─────────────────────────────────────────────
// 🔒 Mock 강제 활성화 — ...
const DEV_MOCK = true;
const mockDelay = (ms: number) =>
  new Promise<void>((r) => setTimeout(r, ms));
```

**파일 상단에 추가할 import (기존 import 블록 최상단):**
```typescript
import { API_BASE_URL, IS_DEV_MOCK } from './config';
```

**추가할 전용 선언 (import 바로 아래):**
```typescript
const mockDelay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
```

**전체 파일 내 치환 (replace_all):**
- `DEV_MOCK` → `IS_DEV_MOCK`
- `API_BASE_URL` (기존 선언 삭제 후) → `API_BASE_URL` (config에서 import된 것 사용)

#### `handoverApi.ts` 수정 지침

**삭제할 블록 (line 14-18):**
```typescript
const BASE       = import.meta.env.VITE_API_BASE_URL || '';
// 🔒 Mock 강제 활성화 — ...
// 실서비스 전환 시: const DEV_MOCK = !BASE; 로 복원.
const DEV_MOCK   = true;
const mockDelay  = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
```

**파일 상단에 추가할 import:**
```typescript
import { API_BASE_URL as BASE, IS_DEV_MOCK as DEV_MOCK } from './config';
```

**추가할 전용 선언:**
```typescript
const mockDelay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
```

> **검증:** 수정 완료 후 `tsc --noEmit` 에러 0건 확인.
> `IS_DEV_MOCK` / `DEV_MOCK` 변수명이 기존 조건문에서 그대로 동작하므로
> apiService 객체 내부 조건문은 수정 불필요.

---

### [C-2] postHandoverRecord — 엔드포인트 재정렬 설계

#### 근본 원인 분석
`postHandoverRecord`는 **텍스트 기록 함수**다.
그런데 현재 호출 엔드포인트인 `/api/v2/ingest`는
**오디오 파일(multipart/form-data) 전용** 엔드포인트다.
이는 단순 파라미터 오류가 아니라 **잘못된 엔드포인트 선택**이 근본 원인이다.

#### 아키텍처 결정: 올바른 엔드포인트로 재정렬

백엔드에 텍스트 기반 케어 기록을 위한 전용 엔드포인트가 이미 존재한다:

```
POST /api/v8/care-record
Content-Type: application/json

Body:
  facility_id:    str   ← App.tsx의 LS_FACILITY_ID
  beneficiary_id: str   ← App.tsx에 LS_BENEFICIARY_ID 추가 필요
  caregiver_id:   str   ← App.tsx의 LS_WORKER_ID
  raw_voice_text: str   ← recordedText (음성 전사 텍스트)
  recorded_at:    str?  ← ISO-8601 (선택)

Response:
  accepted:  true
  record_id: UUID
  server_ts: ISO-8601
```

이것이 의미론적으로도 정확하다:
- `/api/v2/ingest` = 오디오 파일 수집 (WORM 봉인용)
- `/api/v8/care-record` = 텍스트 발화 기반 6대 의무기록 수집

#### `handoverApi.ts` 수정 지침

**삭제할 함수 (line 181-201):**
```typescript
export async function postHandoverRecord(params: {
  text: string;
  worker_id: string;
  facility_id: string;
}): Promise<void> { ... }
```

**교체할 함수 (동일 위치에 작성):**
```typescript
/**
 * POST /api/v8/care-record
 * 텍스트 발화 기반 케어 기록 적재.
 * 의미론적으로 올바른 엔드포인트: audio ingest(/v2/ingest)가 아닌
 * 텍스트 케어 기록 전용 API 사용.
 */
export async function postHandoverRecord(params: {
  raw_voice_text: string;
  worker_id:      string;   // caregiver_id로 백엔드 전달
  facility_id:    string;
  beneficiary_id: string;   // 필수 — App.tsx에서 localStorage 조회
}): Promise<void> {
  if (DEV_MOCK) {
    await mockDelay(500);
    return;
  }
  const res = await fetch(`${BASE}/api/v8/care-record`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      facility_id:    params.facility_id,
      beneficiary_id: params.beneficiary_id,
      caregiver_id:   params.worker_id,
      raw_voice_text: params.raw_voice_text,
      recorded_at:    new Date().toISOString(),
    }),
  });
  await throwIfNotOk(res);
}
```

#### `App.tsx` 호출부 수정 지침

`App.tsx` 상단 상수 블록에 추가:
```typescript
const LS_BENEFICIARY_ID = 'vg_beneficiary_id';
```

`handleExecute` 내 `postHandoverRecord` 호출부 (약 line 196-200) 수정:
```typescript
// 기존
await postHandoverRecord({
  text: recordedText,
  worker_id:   localStorage.getItem(LS_WORKER_ID)   ?? 'WORKER_001',
  facility_id: localStorage.getItem(LS_FACILITY_ID) ?? 'FACILITY_001',
});

// 수정 후
await postHandoverRecord({
  raw_voice_text: recordedText,
  worker_id:      localStorage.getItem(LS_WORKER_ID)      ?? 'WORKER_001',
  facility_id:    localStorage.getItem(LS_FACILITY_ID)     ?? 'FACILITY_001',
  beneficiary_id: localStorage.getItem(LS_BENEFICIARY_ID)  ?? 'UNKNOWN_BID',
});
```

---

### [C-3] plan-actual/route.ts — BFF 프록시 패턴 설계

#### 근본 원인
Next.js Route Handler가 **백엔드를 호출하지 않고** 하드코딩 더미를 반환한다.
`dashboard/lib/api.ts`에 `fetchPlanVsActual()` 함수가 완비되어 있으나 연결되지 않았다.

#### 아키텍처 결정: Route Handler = BFF(Backend For Frontend) 프록시 레이어

Route Handler의 책임을 명확히 정의한다:
- **클라이언트 → Route Handler:** Next.js 내부 API 호출
- **Route Handler → 백엔드:** 서버 사이드 fetch (CORS 우회, 인증 헤더 주입 가능)
- **백엔드 장애 시:** 503 명시적 반환 (더미 데이터 노출 절대 금지)

#### `dashboard/app/api/plan-actual/route.ts` 전체 교체

**현재 파일 전체 내용 삭제 후 아래로 교체:**

```typescript
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
```

> **검증:** `dashboard/components/admin/plan-actual-panel.tsx` 또는
> `PlanVsActualPanel.tsx`에서 이 Route Handler를 호출하는 코드가 있다면
> 응답 구조가 `{ plans: PlanRecord[] }` 형식인지 확인.
> 백엔드 `/api/v2/plan` 응답이 `{ plans: [...] }` 형식이므로 변환 없이 투명 전달.

---

### [C-4] 포렌식 패널 위조 해시 — 실 WORM 원장 연동 설계

#### 근본 원인
`dashboard/app/page.tsx`의 `alertToForensic()` 함수가
`e3b0c44298fc1c149afbf4c8996fb924...` 형태의 **클라이언트 생성 가짜 SHA-256**을
법적 증거 화면에 표시한다.

실제 `audio_sha256`과 `chain_hash`는 백엔드 `/api/v2/audit` 엔드포인트에 이미 존재한다.
(`backend/main.py:460-496` — `audio_sha256`, `chain_hash`, `worm_retain_until` 반환 완비)

#### 아키텍처 결정: 비동기 Lazy-Load 패턴

포렌식 패널은 클릭 시 해시를 즉석 생성하는 대신,
**백엔드에서 실 해시를 비동기 조회**하는 구조로 전환한다.

```
사용자 클릭 alert 카드
       │
       ▼
  [로딩 상태] "WORM 해시 조회 중..."
       │
       ▼ fetch /api/v2/audit
  [완료 상태] 실제 audio_sha256 + chain_hash 렌더링
       │
       ▼ (fetch 실패 시)
  [폴백 상태] "PENDING — WORM 봉인 처리 중" (위조값 절대 금지)
```

#### `dashboard/lib/api.ts` 수정 지침 — 단건 조회 함수 추가

파일 끝에 아래 함수를 추가한다:

```typescript
/**
 * GET /api/v2/audit — 단건 WORM 해시 조회
 * ledger_id로 해당 원장 레코드의 실제 audio_sha256, chain_hash 조회.
 * 존재하지 않거나 아직 봉인 미완료이면 null 반환.
 */
export async function fetchAuditByLedgerId(
  ledgerId: string
): Promise<AuditRecord | null> {
  try {
    // 백엔드 /api/v2/audit은 전체 목록 반환 — facility_id 없이 호출 후 클라이언트 필터
    const data = await apiFetch<{ records: AuditRecord[] }>("/api/v2/audit");
    return data.records.find((r) => r.id === ledgerId) ?? null;
  } catch {
    return null;
  }
}
```

#### `dashboard/app/page.tsx` 수정 지침

**Step 1 — import 추가:**
```typescript
import { fetchAuditByLedgerId } from "@/lib/api";
```

**Step 2 — 포렌식 상태를 비동기 로딩 가능한 구조로 전환:**

현재 `selectedAlert` 상태 근처에 아래 상태 추가:
```typescript
const [forensicData, setForensicData]     = useState<ForensicEvidence | null>(null);
const [forensicLoading, setForensicLoading] = useState(false);
```

**Step 3 — `alertToForensic` 동기 함수 삭제 후 비동기 핸들러로 교체:**

삭제 대상: 현재 `alertToForensic(a: 알림카드데이터): ForensicEvidence` 함수 전체.

신규 작성 (동일 위치):
```typescript
/**
 * 알림 카드 클릭 시 백엔드에서 실제 WORM 해시를 조회하여 ForensicEvidence 구성.
 * 조회 실패 시 PENDING 상태로 폴백 — 위조 해시 생성 절대 금지.
 */
async function loadForensicEvidence(a: 알림카드데이터): Promise<void> {
  setForensicLoading(true);
  setForensicData(null);

  const auditRecord = await fetchAuditByLedgerId(a.id);
  const fp = `DV-${a.facility_id.slice(0, 6)}-${a.id.slice(-4)}-AOS12`;

  setForensicData({
    recorded_at:        a.ingested_at,
    beneficiary_id:     a.beneficiary_id,
    recorder_id:        a.shift_id,
    device_fingerprint: fp,
    audio_sha256:       auditRecord?.audio_sha256   ?? "PENDING — WORM 봉인 처리 중",
    chain_hash:         auditRecord?.chain_hash     ?? "PENDING — WORM 봉인 처리 중",
    worm_status:        auditRecord?.is_sealed      ? "LEGAL_HOLD" : "PENDING",
    worm_retain_until:  auditRecord?.worm_retain_until ?? "—",
    gps_coord:          a.gps_lat != null ? `${a.gps_lat}, ${a.gps_lon}` : "미수집",
    facility_id:        a.facility_id,
    care_type:          a.care_type ?? "미분류",
    minutes_elapsed:    a.minutes_elapsed,
    estimated_clawback: a.예상환수액,
  });
  setForensicLoading(false);
}
```

**Step 4 — 알림 카드 클릭 이벤트 핸들러에서 기존 `alertToForensic(a)` 호출부를 찾아 교체:**
```typescript
// 기존 (동기 직접 생성)
setSelectedAlert(a);
setForensicEvidence(alertToForensic(a));

// 수정 후 (비동기 WORM 조회)
setSelectedAlert(a);
loadForensicEvidence(a);   // 비동기 — 로딩 중 forensicLoading=true
```

**Step 5 — 포렌식 패널 렌더링부에서 로딩 상태 표시 추가:**
```tsx
{forensicLoading && (
  <div className="text-slate-400 text-xs animate-pulse">
    WORM 해시 조회 중...
  </div>
)}
{forensicData && !forensicLoading && (
  // 기존 포렌식 패널 렌더링 (forensicData.audio_sha256, forensicData.chain_hash 사용)
)}
```

---

## ── CHAPTER 3. MEDIUM 수정 설계 ────────────────────────────────────

---

### [M-1] SSE keep-alive 루프 — 올바른 비동기 패턴 설계

#### 근본 원인
`asyncio.sleep(0)`은 이벤트 루프에 CPU를 잠시 양보할 뿐, **대기 시간이 0**이다.
`get_message(timeout=1.0)`이 미수신 시 반환하는 즉시 keep-alive를 발사하므로
결과적으로 **초당 1회 keep-alive 폭주** 발생.

#### 아키텍처 결정: 명시적 카운터 기반 간격 제어

SSE 루프 내에 `idle_ticks` 카운터를 도입하여
**30회 폴링(≈30초) 후 1회 keep-alive** 전송 패턴으로 전환.

#### `backend/main.py` 수정 지침

수정 위치: `event_generator()` 내부 while 루프 (약 line 388-410)

**현재 코드:**
```python
while True:
    if await request.is_disconnected():
        break
    message = await pubsub.get_message(
        ignore_subscribe_messages=True,
        timeout=1.0,
    )
    if message and message.get("type") == "message":
        try:
            payload = json.loads(message["data"])
            event   = payload.get("event", "update")
            data    = payload.get("data", {})
            yield sse_format(event, data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[SSE] 메시지 파싱 실패: {e}")
    else:
        # 30초마다 keep-alive (프락시 타임아웃 방지)
        yield ": keep-alive\n\n"
        await asyncio.sleep(0)
```

**교체 코드:**
```python
idle_ticks = 0                           # keep-alive 간격 카운터

while True:
    if await request.is_disconnected():
        break

    message = await pubsub.get_message(
        ignore_subscribe_messages=True,
        timeout=1.0,                     # 최대 1초 블로킹 대기
    )

    if message and message.get("type") == "message":
        idle_ticks = 0                   # 메시지 수신 시 카운터 리셋
        try:
            payload = json.loads(message["data"])
            event   = payload.get("event", "update")
            data    = payload.get("data", {})
            yield sse_format(event, data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[SSE] 메시지 파싱 실패: {e}")
    else:
        idle_ticks += 1
        if idle_ticks >= 30:             # 30 × 1초 = 30초 간격
            yield ": keep-alive\n\n"
            idle_ticks = 0
        # asyncio.sleep 불필요 — get_message(timeout=1.0)이 이미 대기 수행
```

> **효과:** 기존 초당 1회 → 30초당 1회로 keep-alive 빈도 30배 감소.
> 클라이언트 100명 × 초당 1건 = 100건/초 → 100명 × 30초당 1건 ≈ 3.3건/초.

---

### [M-2] 교대조 타임존 결함 — KST 단일 진실원 설계

#### 근본 원인
`datetime.now()`는 서버 OS 로컬 타임존을 사용한다.
클라우드 서버 기본값은 UTC이므로 한국(KST = UTC+9) 기준 9시간 오차 발생.
DAY/EVENING/NIGHT 경계가 최대 9시간 밀림.

#### 아키텍처 결정: Python stdlib `zoneinfo` 사용 (추가 의존성 0)

`pytz`는 외부 패키지이지만, `zoneinfo`는 **Python 3.9+ 표준 라이브러리**다.
`requirements.txt` 수정 불필요.

#### `backend/notifier.py` 수정 지침

**import 블록 수정 (line 15 근처 `from datetime import ...` 줄 찾아 교체):**
```python
# 기존
from datetime import datetime, timezone

# 수정 후
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
```

**`resolve_shift_code_auto` 함수 수정 (line 57-69):**
```python
def resolve_shift_code_auto(now: Optional[datetime] = None) -> str:
    """
    사령관 원칙 #2: shiftCode는 100% AUTO. 서버 시계 단일 진실원.
    타임존: KST(Asia/Seoul) 고정 — 서버 OS 로컬 설정에 무관.
        DAY     06:00 ≤ h < 14:00  (KST)
        EVENING 14:00 ≤ h < 22:00  (KST)
        NIGHT   22:00 ≤ h < 06:00  (KST)
    """
    # UTC 기준 현재 시각을 KST로 변환 — datetime.now() 로컬 타임존 의존 제거
    utc_now = now if now else datetime.now(timezone.utc)
    kst_now = utc_now.astimezone(_KST)
    h = kst_now.hour
    if 6 <= h < 14:
        return "DAY"
    if 14 <= h < 22:
        return "EVENING"
    return "NIGHT"
```

> **검증:** `python -c "from zoneinfo import ZoneInfo; ZoneInfo('Asia/Seoul')"` — 에러 없음 확인.
> Python 3.9+ 환경이면 추가 설치 불필요.

---

### [M-3] fanout 중복 방지 무력화 — 멱등성 복구 설계

#### 근본 원인
`fanout_alimtalk()` → `send_alimtalk(ledger_id=None)` 호출 시
`_is_already_sent()`가 `ledger_id is None`을 감지하고 **즉시 False 반환** (체크 스킵).
Redis SETNX가 유일한 방어선이지만 Redis 장애/재시작 시 완전히 무효화됨.

#### 아키텍처 결정: 복합 키(idempotency_key + phone) 기반 DB 레벨 중복 차단

Redis가 없어도 `notification_log` DB 테이블 레벨에서 중복을 막는 구조.
`ledger_id` 컬럼을 fan-out 전용 합성 키로 재활용 (스키마 변경 없음, TEXT 비교).

#### `backend/notifier.py` 수정 지침

**`_is_already_sent` 함수 수정 (line 174-185):**
```python
def _is_already_sent(conn, ledger_id: Optional[str], trigger_type: str) -> bool:
    """중복 발송 방지.
    ledger_id가 UUID(36자, 하이픈 4개) → ::uuid 캐스트 사용.
    ledger_id가 합성 키(fan-out용) → TEXT 비교.
    ledger_id가 None → 체크 스킵 (기존 동작 유지).
    """
    if not ledger_id:
        return False

    # UUID 여부 판별 — fan-out 합성 키는 UUID 형식이 아님
    is_uuid = (
        len(ledger_id) == 36
        and ledger_id.count("-") == 4
        and all(c in "0123456789abcdefABCDEF-" for c in ledger_id)
    )

    if is_uuid:
        sql = """
            SELECT 1 FROM notification_log
            WHERE ledger_id = :lid::uuid
              AND trigger_type = :tt
              AND status = 'sent'
            LIMIT 1
        """
    else:
        sql = """
            SELECT 1 FROM notification_log
            WHERE ledger_id::text = :lid
              AND trigger_type = :tt
              AND status = 'sent'
            LIMIT 1
        """

    row = conn.execute(text(sql), {"lid": ledger_id, "tt": trigger_type}).fetchone()
    return row is not None
```

**`fanout_alimtalk` 함수 시그니처 수정 (line 301-307):**
```python
def fanout_alimtalk(
    engine,
    phones: list,
    template_code: str,
    variables: dict,
    trigger_type: str,
    idempotency_key: Optional[str] = None,   # ← 신규 파라미터 추가
):
```

**`fanout_alimtalk` 내부 `send_alimtalk` 호출부 수정 (for 루프 내):**
```python
    for phone in phones:
        # idempotency_key + phone 합성 → phone별 고유 dedup 키 생성
        # Redis 장애 시에도 notification_log DB 레벨에서 중복 차단 보장
        dedup_key = f"{idempotency_key}:{phone.replace('-', '')}" if idempotency_key else None

        ok = send_alimtalk(
            engine=engine,
            phone=phone,
            template_code=template_code,
            variables=variables,
            trigger_type=trigger_type,
            ledger_id=dedup_key,         # None 대신 합성 키 전달
        )
```

#### `backend/main.py` 수정 지침 — 호출부 2곳

**`v7_notify_emergency` (약 line 1005) `fanout_alimtalk` 호출:**
```python
sent, failed = fanout_alimtalk(
    engine=engine,
    phones=targets,
    template_code=ALIMTALK_TPL_EMERGENCY,
    variables=variables,
    trigger_type="V7-EMERGENCY",
    idempotency_key=idempotency_key,     # ← 추가: Header에서 수신한 값
)
```

**`v7_notify_shift_group` (약 line 1059) `fanout_alimtalk` 호출:**
```python
sent, failed = fanout_alimtalk(
    engine=engine,
    phones=targets,
    template_code=ALIMTALK_TPL_SHIFT_GROUP,
    variables=variables,
    trigger_type="V7-SHIFT",
    idempotency_key=idempotency_key,     # ← 추가
)
```

---

### [M-4] useVoiceGuardSSE 이원화 — 단일 정규 훅으로 통합

#### 근본 원인
```
dashboard/lib/useVoiceGuardSSE.ts       ← 구버전 (중복 방지 없음, pipeline 없음)
dashboard/app/hooks/useVoiceGuardSSE.ts ← 신버전 (seenIdsRef, PipelineStatus 포함)
```
두 파일이 공존하여 `CLAUDE.md §0 "이중 노동 제로"` 위반.

#### 아키텍처 결정: lib/ 버전을 re-export stub으로 교체

삭제 대신 re-export로 교체하는 이유:
- 기존 import 경로를 사용하는 컴포넌트가 있을 경우 빌드 에러 방지
- 단순 파일 삭제는 숨겨진 import 경로가 있을 경우 빌드 실패 위험

#### `dashboard/lib/useVoiceGuardSSE.ts` 전체 내용 교체

```typescript
/**
 * @deprecated
 * 이 파일은 app/hooks/useVoiceGuardSSE.ts 로 통합되었습니다.
 * 신규 코드는 반드시 아래 경로를 직접 import하십시오:
 *   import { useVoiceGuardSSE } from "@/app/hooks/useVoiceGuardSSE";
 *
 * 이 re-export는 기존 import 경로 호환성 유지용입니다.
 */
export { useVoiceGuardSSE } from "@/app/hooks/useVoiceGuardSSE";
export type {
  PipelineStage,
  PipelineStatus,
} from "@/app/hooks/useVoiceGuardSSE";
```

> **검증:** `grep -r "lib/useVoiceGuardSSE" dashboard/` 로 사용처 확인.
> 발견된 파일은 import 경로를 `@/app/hooks/useVoiceGuardSSE`로 직접 변경.
> `dashboard/app/hooks/useVoiceGuardSSE.ts` — 수정 불필요, 그대로 유지.

---

### [M-5] SSE onerror close() — 브라우저 재연결 복구 설계

#### 근본 원인
`EventSource`는 오류 발생 시 **브라우저가 자동 재연결**하는 내장 메커니즘을 가진다.
그런데 `onerror` 핸들러에서 `eventSource.close()`를 명시 호출하면
`readyState = CLOSED(2)`로 전환되어 **브라우저 자동 재연결이 영구 차단**됨.

#### `FRONT END/src/services/api.ts` 수정 지침

수정 위치: `connectDashboardSSE` 함수 내 `onerror` 핸들러 (약 line 293-299)

**현재 코드:**
```typescript
eventSource.onerror = (error) => {
  console.error('SSE Connection Error:', error);
  if (onError) onError(error);
  eventSource.close();   // ← 이 줄 삭제
};
```

**교체 코드:**
```typescript
eventSource.onerror = (error) => {
  console.error('SSE Connection Error:', error);
  if (onError) onError(error);
  // close() 호출 금지:
  //   EventSource는 readyState=CONNECTING(0) 또는 OPEN(1) 상태에서
  //   오류 발생 시 브라우저가 자동 재연결을 시도한다.
  //   수동 close()는 readyState=CLOSED(2)로 전환 → 재연결 영구 차단.
  //   명시적 연결 종료가 필요하면 호출자에서 반환된 EventSource 인스턴스에
  //   직접 .close()를 호출할 것.
};
```

---

## ── CHAPTER 4. 수정 실행 순서 (의존성 기반) ──────────────────────────

```
Phase 1 — 단독 수정 (의존성 없음, 즉시 실행)
  ├── [M-1] main.py SSE keep-alive 카운터 (1줄 로직 추가)
  ├── [M-2] notifier.py 타임존 (import + 함수 1개 수정)
  └── [M-5] api.ts onerror close() 제거 (1줄 삭제)

Phase 2 — 단일 파일 수정 (의존성 낮음)
  ├── [C-1] config.ts 신규 생성 → api.ts + handoverApi.ts 수정
  ├── [C-3] plan-actual/route.ts 전체 교체
  └── [M-4] lib/useVoiceGuardSSE.ts re-export stub으로 교체

Phase 3 — 다중 파일 연계 수정
  ├── [C-2] handoverApi.ts 함수 교체 → App.tsx 호출부 수정
  └── [M-3] notifier.py 3곳 + main.py 2곳 동시 수정

Phase 4 — 가장 복잡한 수정 (마지막 실행)
  └── [C-4] lib/api.ts 함수 추가 → page.tsx 비동기 패턴 전환
```

---

## ── CHAPTER 5. 수정 완료 검증 체크리스트 ──────────────────────────────

```
□ [C-1] FRONT END/src/services/config.ts 파일 존재 확인
□ [C-1] api.ts, handoverApi.ts 에서 DEV_MOCK/IS_DEV_MOCK 하드코딩 0건 확인
□ [C-1] VITE_API_BASE_URL 설정 시 IS_DEV_MOCK=false 로직 추적 확인
□ [C-2] postHandoverRecord가 /api/v8/care-record (JSON) 호출 확인
□ [C-2] 전송 body에 beneficiary_id, caregiver_id 포함 확인
□ [C-2] App.tsx에 LS_BENEFICIARY_ID 상수 추가 및 호출부 수정 확인
□ [C-3] plan-actual/route.ts 하드코딩 더미 데이터 0줄 확인
□ [C-3] plan-actual/route.ts가 BACKEND_URL/api/v2/plan 호출 확인
□ [C-4] page.tsx에서 alertToForensic() 동기 함수 완전 제거 확인
□ [C-4] page.tsx에서 가짜 sha/chain 문자열(e3b0c44...) 0건 확인
□ [C-4] fetchAuditByLedgerId 비동기 호출로 대체 확인
□ [M-1] main.py SSE 루프에 idle_ticks 카운터 로직 존재 확인
□ [M-1] asyncio.sleep(0) 코드 0건 확인
□ [M-2] notifier.py에 zoneinfo import 확인
□ [M-2] resolve_shift_code_auto 내 datetime.now() (timezone 없음) 0건 확인
□ [M-2] KST astimezone 변환 로직 존재 확인
□ [M-3] fanout_alimtalk 시그니처에 idempotency_key 파라미터 확인
□ [M-3] fanout 내 dedup_key 합성 로직 존재 확인
□ [M-3] _is_already_sent UUID/TEXT 분기 로직 존재 확인
□ [M-3] main.py fanout_alimtalk 호출 2곳에 idempotency_key 전달 확인
□ [M-4] lib/useVoiceGuardSSE.ts 가 re-export stub으로 교체 확인
□ [M-5] api.ts onerror에서 eventSource.close() 0건 확인
□ [전체] TypeScript: tsc --noEmit 에러 0건
□ [전체] Python: python -m py_compile backend/main.py backend/notifier.py 에러 0건
```

---

## ── CHAPTER 6. 절대 건드리지 말 것 ──────────────────────────────────

```
backend/
  ├── evidence_ledger, care_record_ledger, care_plan_ledger  스키마 (DB)
  ├── worker.py — build_chain() HMAC 해시체인 로직
  ├── env_guard.py — 환경변수 검증 로직 전체
  ├── angel_bridge.py / angel_export.py / angel_rpa.py — 엔젤 브리지 전체
  └── gemini_processor.py — Gemini 프롬프트 및 스키마

FRONT END/
  └── src/services/deviceId.ts — 재설치 시 신규 ID는 의도된 동작

dashboard/
  └── app/hooks/useVoiceGuardSSE.ts — 신버전 훅 (수정 불필요)
```

---

*이 설계도를 기반으로 다음 세션 에이전트는 Phase 1부터 순서대로 즉각 실행하라.*
*설계 의도 불명 시 이 문서의 "아키텍처 결정" 항목을 반드시 재독하라.*
