# VG_Evidence_UI_Blueprint.md
## Voice Guard — 무결성 증거 UI 시각화 마스터 하네스 설계도
### 버전 1.0.0 | 최상위 평가자(Evaluator) 인가 문서 | 2026-04-22

---

## 0. 발간 목적 및 법적 방어 근거

**목적:** 백엔드 `certificate_ledger`에서 발급된 PDF/JSON 증거 검증서 데이터를 현장 원장 및 건보공단
조사관이 5초 안에 100% 납득할 수 있는 시각적 신뢰 보증서 UI로 변환한다.

**방어 논리:**
- 조사관이 들이닥쳤을 때 **녹색 자물쇠 + COMPLIANCE 뱃지 + 스캔 즉시 검증되는 QR 코드**를 제시
- `chain_hash`가 화면에 노출되면 "이 데이터는 사후 조작이 수학적으로 불가능하다"는 심리적·법리적 방어선 즉각 형성
- 조사관이 QR 스캔 → 독립 검증 페이지 → SHA-256 해시 실시간 재계산 → "변조 없음" 판정 즉시 확인

---

## 통제 1: 고수준 아키텍처 (High-Level Architecture)

### 1-A. 데이터 흐름 전체 지도

```
[PostgreSQL]
  certificate_ledger (pdf_key, json_key, cert_hash, seal_event_id)
  evidence_seal_event (chain_hash, worm_retain_until, locked_at)
        │
        ▼
[Cloud Run: voice-guard-api]
  GET /api/v2/cert/{ledger_id}          ← 새 엔드포인트 (backend 구현)
  GET /api/v2/cert/{ledger_id}/verify   ← QR 코드 타깃 (공개 접근)
        │
        ▼
[Firebase App Hosting: voice-guard-pilot.web.app]
  /admin  → Direct_Dashboard (기존 App.tsx 확장)
              ├─ WormSealBadge 컴포넌트  ← 자물쇠 아이콘 + 판정 색상
              ├─ IntegrityQrCode 컴포넌트 ← QR 생성 + 스캔 라우팅
              └─ CertDetailDrawer 컴포넌트 ← 행 클릭 시 슬라이드인 패널
  /verify/:ledger_id  → VerifyPage (신규 라우트)
              └─ 독립 검증 페이지 (QR 스캔 진입)
```

### 1-B. 신규 백엔드 API 계약 (backend/main.py에 추가)

```
GET /api/v2/cert/{ledger_id}
Authorization: Bearer {JWT}   ← 관리자 대시보드용

Response 200:
{
  "ledger_id":       string,   // UUID
  "seal_event_id":   string,   // UUID
  "chain_hash":      string,   // 64자 hex
  "cert_self_hash":  string,   // 64자 hex (JSON cert 무결성)
  "worm_lock_mode":  "COMPLIANCE" | "GOVERNANCE" | "UNKNOWN",
  "worm_retain_until": string, // ISO-8601
  "pdf_url":         string,   // 서명된 B2 다운로드 URL (1시간 TTL)
  "json_url":        string,   // 서명된 B2 다운로드 URL (1시간 TTL)
  "issued_at":       string,   // ISO-8601 (ingested_at 기반 결정론적)
  "issuer_version":  string    // "vg-cert-v1.0.0"
}

GET /api/v2/cert/{ledger_id}/verify
Authorization: 없음 (공개 — QR 코드 타깃)

Response 200: 위와 동일하되 pdf_url, json_url 제외 (해시값만 공개)
```

### 1-C. 프론트엔드 신규 파일 목록 (기존 App.tsx 수정 + 신규 컴포넌트 삽입)

```
FRONT END/Direct_Dashboard/src/
├── App.tsx                       ← 기존 파일 수정 (컴포넌트 임포트 + 열 교체)
├── components/
│   ├── WormSealBadge.tsx         ← 신규: 자물쇠 아이콘 + 판정 뱃지
│   ├── IntegrityQrCode.tsx       ← 신규: QR 코드 생성 + 검증 URL
│   └── CertDetailDrawer.tsx      ← 신규: 슬라이드인 증거 패널
├── pages/
│   └── VerifyPage.tsx            ← 신규: /verify/:ledger_id 독립 검증 페이지
└── hooks/
    └── useCertData.ts            ← 신규: GET /api/v2/cert/{ledger_id} SWR 훅
```

---

## 통제 2: Generator/Evaluator 분리 및 Done Contract

### 2-A. 4단계 순차 게이트 (어느 하나 미통과 시 즉시 REJECT)

```
[Gate 1: 타입 안전성]
  npx tsc --noEmit → 오류 0건 필수
  (WormLockMode 유니온 타입, CertData 인터페이스 모두 strict 선언)

[Gate 2: 린트 강제]
  npx eslint src/ --max-warnings 0 → 경고 포함 0건 필수
  (no-props-missing 커스텀 룰: WormSealBadge에 wormLockMode prop 없으면 빌드 실패)

[Gate 3: 컴포넌트 시각 검증 — 스크린샷 제출 필수]
  3-1. WormSealBadge: COMPLIANCE 입력 → 초록 자물쇠, UNKNOWN → 주황 자물쇠
  3-2. IntegrityQrCode: QR 이미지 렌더링 + 아래 검증 URL 텍스트 표시
  3-3. CertDetailDrawer: 결정 대기열 행 클릭 → 패널 슬라이드인, chain_hash 전체 표시
  3-4. VerifyPage: /verify/test-id 로드 → "검증 완료 ✅" 또는 "검증 실패 ❌" 판정 표시

[Gate 4: QR 라우팅 E2E]
  QR 코드 안에 인코딩된 URL 추출 → 브라우저 접속 → VerifyPage 로드 → 해시값 표시
  (수동 또는 Playwright 스크린샷으로 증명)
```

### 2-B. Done Contract 최종 보고 형식 (Generator 의무)

```
[GATE 1] tsc --noEmit: ✅ 오류 0건
[GATE 2] eslint: ✅ 경고 0건
[GATE 3-1] WormSealBadge COMPLIANCE: ✅ [스크린샷 첨부]
[GATE 3-2] IntegrityQrCode 렌더링: ✅ [스크린샷 첨부]
[GATE 3-3] CertDetailDrawer 슬라이드인: ✅ [스크린샷 첨부]
[GATE 3-4] VerifyPage 판정: ✅ [스크린샷 첨부]
[GATE 4] QR → 브라우저 → VerifyPage: ✅ [증명 캡처]
Evaluator 제출 대기 중.
```

**금지 사항:** Generator가 위 7개 항목 중 1개라도 빠뜨린 채 "완료"를 선언하면 즉시 REJECT.

---

## 통제 3: 무결점 채점 루브릭 (100점 만점 | 합격선 85점)

| 항목 | 배점 | 세부 채점 기준 | 실격 기준 |
|------|------|----------------|-----------|
| **직관성** | 30점 | • COMPLIANCE → 녹색 (10pt)<br>• GOVERNANCE/DEGRADED → 주황 (10pt)<br>• UNKNOWN/ERROR → 빨간 (5pt)<br>• 일반인이 5초 내 판독 가능한 한국어 레이블 (5pt) | 자물쇠 없이 텍스트만 → -15pt |
| **반응형 완벽성** | 25점 | • 1920px 데스크톱 레이아웃 무결 (10pt)<br>• 1280px 노트북 레이아웃 무결 (10pt)<br>• 768px 태블릿 최소 가독성 (5pt) | 1280px에서 overflow 발생 → -20pt |
| **QR 스캔 검증 정확성** | 25점 | • QR 이미지 렌더링 (5pt)<br>• 인코딩 URL = /verify/{ledger_id} (5pt)<br>• VerifyPage 로드 → chain_hash 64자 표시 (10pt)<br>• 한국어 "검증 완료 / 검증 실패" 판정 표시 (5pt) | QR 미생성 → 0pt; URL 오인코딩 → -10pt |
| **구조적 강제** | 20점 | • tsc 오류 0건 (7pt)<br>• eslint 경고 0건 (7pt)<br>• Pre-commit hook: lint+tsc 통과 필수 (6pt) | 타입 any 사용 → -5pt; hook 미설치 → -10pt |

**보너스 (+5점):**
- `motion/react`로 WormSealBadge 씰(seal) 등장 애니메이션 구현 시
- CertDetailDrawer에 PDF 다운로드 버튼 + 실제 B2 URL 연결 시

---

## 통제 4: 구조적 강제 — Hooks & Linters

### 4-A. ESLint 커스텀 룰 (`.eslintrc.cjs` 또는 `eslint.config.js`)

```js
// 필수 추가 룰: WormSealBadge에 wormLockMode prop 없으면 빌드 에러
rules: {
  'react/prop-types': 'error',
  '@typescript-eslint/no-explicit-any': 'error',
  '@typescript-eslint/strict-null-checks': 'error',
}
```

### 4-B. Pre-commit Hook (`.husky/pre-commit` 또는 lefthook)

```bash
#!/bin/sh
cd "FRONT END/Direct_Dashboard"
npx tsc --noEmit || { echo "❌ TypeScript 오류 — 커밋 차단"; exit 1; }
npx eslint src/ --max-warnings 0 || { echo "❌ ESLint 경고 — 커밋 차단"; exit 1; }
echo "✅ FE 타입+린트 통과"
```

### 4-C. TypeScript 필수 타입 계약 (`src/types/cert.ts`)

```typescript
// Generator는 이 타입을 1바이트도 수정할 수 없다 — Evaluator 전용 잠금
export type WormLockMode = 'COMPLIANCE' | 'GOVERNANCE' | 'UNKNOWN';

export interface CertData {
  ledger_id:        string;
  seal_event_id:    string;
  chain_hash:       string;       // 64자 hex — 표시 시 전체 노출 필수
  cert_self_hash:   string;       // 64자 hex
  worm_lock_mode:   WormLockMode;
  worm_retain_until: string;      // ISO-8601
  pdf_url?:         string;       // 1시간 TTL B2 서명 URL
  json_url?:        string;
  issued_at:        string;
  issuer_version:   string;
}
```

---

## 컴포넌트 상세 설계 명세 (Generator 실행 지침)

### C-1. `WormSealBadge.tsx` — 자물쇠 아이콘 + WORM 판정 뱃지

```
Props:
  wormLockMode: WormLockMode        ← 필수 (없으면 빌드 에러)
  chainHash: string                 ← 필수 (앞 12자 표시)
  retainUntil?: string              ← 선택 (툴팁 표시용)
  size?: 'sm' | 'md' | 'lg'        ← 기본값 'md'

시각 명세:
  COMPLIANCE:
    - 배경: bg-green-50, 테두리: border-green-300
    - 아이콘: lucide-react Lock (색상: text-green-600)
    - 레이블: "WORM 봉인 완료" (한국어), 아래에 chain_hash[:12] mono 텍스트
    - 선택적: motion/react으로 appear 시 scale 0.8 → 1.0, opacity 0 → 1

  GOVERNANCE:
    - 배경: bg-orange-50, 테두리: border-orange-300
    - 아이콘: lucide-react LockOpen (색상: text-orange-500)
    - 레이블: "준수 모드 (주의)" (한국어)

  UNKNOWN:
    - 배경: bg-red-50, 테두리: border-red-200
    - 아이콘: lucide-react ShieldAlert (색상: text-red-500)
    - 레이블: "봉인 상태 미확인"

결정 대기열 "증거 패키지" 열: 기존 wormHash 텍스트 span을 WormSealBadge(size='sm')로 교체
```

### C-2. `IntegrityQrCode.tsx` — QR 코드 + 검증 URL

```
Props:
  ledgerId: string    ← 필수
  size?: number       ← 기본값 128 (px)

구현 방식:
  - npm 패키지: qrcode.react (^3.x) — package.json에 추가
  - 인코딩 URL: https://voice-guard-pilot.web.app/verify/{ledgerId}
  - QR 아래에 URL 텍스트 (truncated: /verify/{ledgerId[:8]}…) 표시
  - QR 클릭 시 새 탭으로 URL 열림

배치:
  - CertDetailDrawer 하단 영역 (QR 크기 160px)
  - 우측 감사 패널 (무결점 증거 패널) 내 selectedData 존재 시 표시 (QR 크기 128px)
```

### C-3. `CertDetailDrawer.tsx` — 슬라이드인 증거 상세 패널

```
트리거: 결정 대기열 행 클릭 (기존 selectedRowId 상태 활용)

레이아웃: 우측에서 슬라이드인 (width: 480px, height: 100vh)
  - motion/react: x: 480 → 0 (spring animation)
  - 배경: bg-white, shadow-2xl, z-50

패널 구성 (위에서 아래로):
  [헤더]
    ShieldCheck 아이콘 + "법적 증거 검증서" 제목
    X 버튼 (닫기)

  [섹션 1: WORM 봉인 상태]
    WormSealBadge(size='lg', wormLockMode=certData.worm_lock_mode, chainHash=certData.chain_hash)
    보존 기한: certData.worm_retain_until (KST 변환 표시)

  [섹션 2: 해시체인 무결성]
    chain_hash: 전체 64자 (글씨 크기 9px, font-mono, 줄바꿈 허용)
    cert_self_hash: 전체 64자 (동일 형식)
    발급 시각: issued_at (KST)
    발급자: issuer_version

  [섹션 3: QR 코드 + 다운로드]
    IntegrityQrCode(ledgerId, size=160)
    [PDF 다운로드 버튼] → certData.pdf_url (신규 탭)
    [JSON 다운로드 버튼] → certData.json_url (신규 탭)

  [푸터]
    "이 검증서는 WORM(Write Once Read Many) 불변 저장소에 봉인되었습니다."
    소형 회색 텍스트

로딩 상태:
  certData가 null (API 호출 중) → 스켈레톤 UI (각 섹션 회색 막대)
에러 상태:
  API 실패 → "증거 데이터를 불러오지 못했습니다. 네트워크를 확인하세요." (주황 경고)
```

### C-4. `VerifyPage.tsx` — 독립 검증 페이지 (`/verify/:ledger_id`)

```
라우터 설정: react-router-dom 사용 (package.json에 추가)
  main.tsx에 BrowserRouter + Routes 추가:
    / → App (기존 대시보드)
    /verify/:ledger_id → VerifyPage

VerifyPage 레이아웃 (풀스크린, bg-white):
  [최상단: 검증 결과 헤더 — 가장 크게]
    API 호출: GET /api/v2/cert/{ledger_id}/verify (공개 엔드포인트)

    성공 + COMPLIANCE:
      🔒 녹색 원형 배경 + ShieldCheck (80px)
      "WORM 봉인 완료 — 데이터 무결성 확인됨" (32px, font-black)
      "이 기록은 조작이 불가능하도록 COMPLIANCE 모드로 봉인되었습니다." (회색 부제)

    성공 + UNKNOWN/오류:
      🔓 주황 원형 배경 + ShieldAlert (80px)
      "봉인 상태 경고 — 관리자에게 문의하세요" (주황)

    API 실패 / ledger_id 없음:
      ❌ 빨간 원형 배경 + AlertCircle (80px)
      "검증 실패 — 유효하지 않은 검증 코드입니다"

  [중단: 해시체인 상세]
    chain_hash: 전체 64자 (font-mono, 배경 gray-50, padding)
    cert_self_hash: 전체 64자 (동일)
    보존 기한: worm_retain_until (KST)
    발급 시각: issued_at (KST)

  [하단: 검증 방법 (조사관용 설명)]
    "① WORM 저장소에서 위 경로의 음성 파일을 수령합니다."
    "② SHA-256(음성파일) = chain_hash 앞 부분과 일치하는지 확인합니다."
    "③ B2 head_object → ObjectLockMode = COMPLIANCE 확인합니다."
    "④ 모든 값 일치 시 무결성 100% 보장됩니다."

  [최하단: 브랜딩]
    Voice Guard 로고 + "국민건강보험공단 현지조사 대응 무결성 검증 시스템"
```

---

## App.tsx 수정 명세 (기존 파일 최소 수술)

### 수정 1: 결정 대기열 "증거 패키지" 열 교체

```tsx
// 수정 전 (기존 코드):
<td className="px-4 py-8 whitespace-nowrap text-center border-0">
  <span className="inline-flex items-center px-3 py-1 bg-gray-100 text-gray-500 rounded-full font-mono text-xs tracking-tight">
    {row.wormHash}
  </span>
</td>

// 수정 후 (WormSealBadge 교체):
<td className="px-4 py-8 whitespace-nowrap text-center border-0">
  <WormSealBadge
    wormLockMode={row.wormLockMode ?? 'UNKNOWN'}
    chainHash={row.wormHash}
    size="sm"
  />
</td>
```

### 수정 2: DecisionItem 타입에 wormLockMode 추가

```tsx
type DecisionItem = {
  ...existing fields...
  wormHash:      string;
  wormLockMode:  WormLockMode;  // 신규
  certIssued:    boolean;       // 신규: certificate_ledger에 존재 여부
};
```

### 수정 3: API fetchInitial()에서 cert 상태 매핑

```tsx
items: Array.isArray(queue)
  ? queue.map((item: any) => ({
      ...existing mapping...
      wormHash:     item.wormHashShort ?? 'N/A',
      wormLockMode: item.wormLockMode  ?? 'UNKNOWN',  // 신규
      certIssued:   item.certIssued    ?? false,       // 신규
    }))
  : [],
```

### 수정 4: 우측 감사 패널에 QR 코드 추가

```tsx
// selectedData 존재 시 무결점 증거 패널 하단에 추가:
{selectedData && (
  <div className="mt-4">
    <IntegrityQrCode ledgerId={selectedData.id} size={128} />
  </div>
)}
```

### 수정 5: CertDetailDrawer 연결

```tsx
// 결정 대기열 행 클릭 이벤트에서 기존 setSelectedRowId 유지 + Drawer 열기
const [isDrawerOpen, setIsDrawerOpen] = useState(false);

// 행 클릭 시:
onClick={() => {
  setSelectedRowId(row.id);
  setIsDrawerOpen(true);
}}

// JSX 최하단:
<CertDetailDrawer
  ledgerId={selectedRowId}
  isOpen={isDrawerOpen}
  onClose={() => setIsDrawerOpen(false)}
/>
```

---

## 신규 백엔드 엔드포인트 명세 (backend/main.py 추가)

```python
@app.get("/api/v2/cert/{ledger_id}")
async def get_cert_data(ledger_id: str, authorization: str = Header(...)):
    """
    certificate_ledger + evidence_seal_event JOIN으로 증거 데이터 반환.
    pdf_url, json_url: B2 generate_presigned_url (3600초 TTL)
    """
    ...

@app.get("/api/v2/cert/{ledger_id}/verify")
async def verify_cert(ledger_id: str):
    """
    공개 엔드포인트 — QR 코드 타깃.
    Authorization 없음. pdf_url, json_url 제외하고 반환.
    rate_limit: IP당 10 req/min
    """
    ...
```

---

## 의존성 추가 목록 (package.json)

```json
{
  "dependencies": {
    "qrcode.react": "^3.1.0",
    "react-router-dom": "^6.22.0"
  },
  "devDependencies": {
    "eslint": "^8.57.0",
    "@typescript-eslint/eslint-plugin": "^7.0.0",
    "@typescript-eslint/parser": "^7.0.0",
    "eslint-plugin-react": "^7.33.0",
    "eslint-plugin-react-hooks": "^4.6.0"
  }
}
```

---

## 인수인계 및 기술 부채 기록

1. **QR 코드 오프라인 환경 대응 미구현**: 네트워크 없는 조사 환경에서는 QR 미표시 대신
   "chain_hash를 직접 입력하여 검증" 안내 텍스트로 fallback 필요 (Phase 11 대상)

2. **react-router-dom 미설치 상태**: main.tsx에 BrowserRouter 추가 전 `npm install react-router-dom`
   필수. 미설치 시 VerifyPage 라우팅 불가.

3. **B2 서명 URL TTL**: 1시간 TTL — 대시보드 장기 열람 시 URL 만료 가능.
   향후 `/api/v2/cert/{ledger_id}/refresh-url` 엔드포인트 추가 권장 (Phase 12 대상)

4. **useCertData 훅**: SWR 또는 TanStack Query 중 선택 필요.
   현재 스택에 둘 다 미설치 — 단순 useState + useEffect 구현으로 시작 허용 (Blueprint 내 선택 자유)

---

## 최종 검수 체크리스트 (Evaluator 전용)

```
□ WormSealBadge: COMPLIANCE → 녹색 자물쇠 렌더링 확인
□ WormSealBadge: UNKNOWN   → 주황 알림 아이콘 렌더링 확인
□ IntegrityQrCode: QR 이미지 렌더링 확인
□ IntegrityQrCode: 인코딩 URL = voice-guard-pilot.web.app/verify/{id} 확인
□ CertDetailDrawer: 행 클릭 → 슬라이드인 동작 확인
□ CertDetailDrawer: chain_hash 64자 전체 표시 확인
□ VerifyPage: /verify/test-id 접속 → 판정 UI 표시 확인
□ VerifyPage: COMPLIANCE → 녹색 헤더 확인
□ tsc --noEmit → 오류 0건
□ eslint src/ --max-warnings 0 → 경고 0건
□ Pre-commit hook 설치 확인 (git commit 시 자동 실행)
□ 1280px 뷰포트에서 레이아웃 overflow 없음 확인
```

---

*이 설계도는 최상위 평가자(Evaluator)가 인가하였으며, 하급 Generator는 이 문서의 명세를 1바이트도
임의 해석 없이 실행해야 한다. Done Contract 7개 항목 전수 미통과 시 즉시 REJECT.*
