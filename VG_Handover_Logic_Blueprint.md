# VG_Handover_Logic_Blueprint v1.0
> 발행자: 최상위 어드바이저 겸 평가자(Evaluator)  
> 수신자: 하급 실행 에이전트(Generator)  
> 발행일: 2026-04-23 | 상태: 실행 대기(READY TO EXECUTE)  
> 대상 폴더: `upgrade front_end/` (이 폴더만 수정. 절대 `FRONT END/` 건드리지 마라)

---

## ⚠️ 실행 에이전트 필독 — 절대 제약 (위반 시 즉시 중단)

```
1. 수정 대상: upgrade front_end/src/App.tsx, upgrade front_end/src/services/api.ts 단 2개 파일
2. FRONT END/ 폴더 1바이트도 수정 금지
3. 기존 UI/컴포넌트/CSS/애니메이션 1바이트도 수정 금지 — 로직만 삽입
4. 자기 평가(self-evaluation) 금지 — 반드시 Evaluator 검수 통과로만 완료 확인
5. 구현 완료 후 "State 로그 + Network Payload 스크린샷"을 Evaluator에게 제출
```

---

## 0. 현황 진단 — 현재 버그의 정확한 위치

| 버튼 | 현재 동작 (버그) | 요구 동작 (정답) |
|---|---|---|
| 1번 `인수인계 기록하기` | `startRecording('LOG')` → STT → REVIEW → `apiService.saveLog()` **API 호출 발생** | **API 0건. 녹음 완료 후 텍스트를 State 배열에 append만 한다** |
| 2번 `인수인계 전송` | `COMPLETING` 화면만 이동. **실제 백엔드 전송 없음** | 축적된 State 배열 전체를 `POST /api/v8/handover`로 전송 → Gemini 정제 → WORM + Notion |
| 3번 `인수인계 확인하기` | `COMPLETING` 화면만 이동. **실제 백엔드 전송 없음** | 인수자 식별자만 `POST /api/v8/handover/confirm`으로 전송 → WORM + Notion |

---

## 통제 1 — High-Level Architecture (데이터 축적 및 전송 설계)

### 1-A. 상태 관리 전략 — `useState + sessionStorage`

**선택 근거**: 단일 컴포넌트 구조(`App.tsx`) → Redux/Context 불필요. 과잉 설계 금지.  
**유실 방지**: `sessionStorage`에 동기화 → 실수로 새로고침 해도 근무 중 기록 보존. `localStorage`는 금지 (퇴근 후 다음날 오염).

```typescript
// App.tsx — 기존 useState 선언 블록 직후에 딱 1줄 추가
const [handoverAccumulator, setHandoverAccumulator] = useState<string[]>(() => {
  // 앱 재로드 시 세션 데이터 복원 (새로고침 방어)
  try {
    const saved = sessionStorage.getItem('vg_handover_draft');
    return saved ? JSON.parse(saved) : [];
  } catch { return []; }
});
```

### 1-B. 1번 버튼 — 녹음 완료 후 축적 핸들러

**기존 `handleExecute()`를 모드별로 분기한다. 인수인계 모드(`'LOG'` with `handover context`)는 append만.**

```typescript
// 기존 handleExecute() 내부 수정 — mode 분기 추가
const handleExecute = async () => {
  setScreen('COMPLETING');
  try {
    if (mode === 'LOG' && homeView === 'HANDOVER_SUB') {
      // ✅ 인수인계 기록하기: API 호출 절대 금지. State에 append만.
      const updatedList = [...handoverAccumulator, recordedText.trim()];
      setHandoverAccumulator(updatedList);
      sessionStorage.setItem('vg_handover_draft', JSON.stringify(updatedList));
      // 네트워크 호출 없음 — 완료
    } else if (mode === 'LOG') {
      await apiService.saveLog(recordedText);   // 기존 업무기록 경로 (무수정)
    } else {
      await apiService.sendKakao(recordedText); // 기존 카카오 경로 (무수정)
    }
  } catch (err) {
    setError(err instanceof Error ? err.message : '작업 수행에 실패했습니다.');
    setScreen('REVIEW');
  }
};
```

**⚠️ 구현 에이전트 주의**: `homeView` 상태가 `'HANDOVER_SUB'`일 때에만 축적 로직 진입.  
기존 `업무기록하기(LOG_SUB)` 경로와 완전히 분리됨.

### 1-C. 2번 버튼 — 축적 데이터 일괄 전송 (인수인계 전송)

```typescript
// 기존 executeHandoverAction()을 아래로 교체
const executeHandoverAction = async (m: 'HANDOVER_SEND' | 'HANDOVER_CONFIRM') => {
  setMode(m);
  setScreen('COMPLETING');
  setProgress(0);
  setError(null);

  try {
    if (m === 'HANDOVER_SEND') {
      // 2번 버튼: 축적된 텍스트 전부 묶어서 전송
      if (handoverAccumulator.length === 0) {
        setError('전송할 인수인계 기록이 없습니다. 먼저 기록하기를 눌러주세요.');
        setScreen('HOME');
        return;
      }
      const combinedText = handoverAccumulator.join('\n\n---\n\n');
      await apiService.sendHandover(combinedText);
      // 전송 성공 후 축적 초기화
      setHandoverAccumulator([]);
      sessionStorage.removeItem('vg_handover_draft');

    } else {
      // 3번 버튼: 인수자 식별자만 전송 (텍스트 없음)
      await apiService.confirmHandover();
    }
  } catch (err) {
    setError(err instanceof Error ? err.message : '전송에 실패했습니다.');
    setScreen('HOME');
  }
};
```

### 1-D. api.ts — 2개 신규 메서드 추가 스펙

```typescript
// api.ts에 추가할 2개 메서드 (기존 saveLog/sendKakao 무수정)

async sendHandover(combinedText: string): Promise<ActionResponse> {
  // 2번 버튼 전용: 축적된 전체 텍스트 전송
  const response = await fetch(`${API_BASE_URL}/api/v8/handover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text: combinedText,          // 축적된 모든 기록 (줄바꿈 구분)
      facility_id: null,           // 추후 FE 페이로드 보강 시 주입
      beneficiary_id: null,
      caregiver_id: null,
    }),
  });
  if (!response.ok) throw new Error('인수인계 전송에 실패했습니다.');
  return { success: true };
},

async confirmHandover(): Promise<ActionResponse> {
  // 3번 버튼 전용: 인수자 식별자만 전송 (텍스트 페이로드 없음)
  const response = await fetch(`${API_BASE_URL}/api/v8/handover/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      confirmed_at: new Date().toISOString(),
      // 향후 인수자 ID 추가 위치
    }),
  });
  if (!response.ok) throw new Error('인수인계 확인 전송에 실패했습니다.');
  return { success: true };
},
```

### 1-E. 백엔드 듀얼 라우터 연결 (참고 — 백엔드 코드 수정 불필요)

```
POST /api/v8/handover (2번 버튼)
  body: { text, facility_id, beneficiary_id, caregiver_id }
    │
    ├─ [Fork A] evidence_ledger INSERT + SHA-256 hash_chain ✅ WORM 봉인
    └─ [Fork B] care_record_ledger INSERT → Gemini v2.2 정제
                    └─ notion_pipeline.run_pipeline() → Notion 수정 가능 DB

POST /api/v8/handover/confirm (3번 버튼)
  body: { confirmed_at }
    │
    ├─ [Fork A] evidence_ledger INSERT (확인 이벤트)
    └─ [Fork B] notion_pipeline (확인 상태 업데이트)
```

---

## 통제 2 — 구조적 강제 차단 장치 (ESLint + Pre-commit)

### 2-A. ESLint 커스텀 규칙 — 1번 버튼 핸들러에 fetch/axios 금지

**파일**: `upgrade front_end/eslint.config.js` 수정 (기존 파일 확장)

```javascript
// eslint.config.js에 추가할 커스텀 규칙
// 목적: HANDOVER 기록하기 핸들러에서 네트워크 호출 발견 시 빌드 오류 발생

{
  rules: {
    // 커스텀 no-network-in-accumulator 규칙
    // handoverAccumulator 관련 함수 내부에서 fetch/axios/apiService 호출 금지
    'no-restricted-syntax': [
      'error',
      {
        // "if (mode === 'LOG' && homeView === 'HANDOVER_SUB')" 블록 내 fetch 금지
        selector: `IfStatement[test.operator='&&'] CallExpression[callee.property.name=/^fetch$|^post$|^get$/]`,
        message: '[VG-GUARD] 인수인계 기록하기(1번 버튼) 핸들러에서 네트워크 호출 금지. 축적(append)만 허용.',
      },
    ],
  },
}
```

### 2-B. Pre-commit 훅 — grep 기반 강제 차단

**파일**: `.pre-commit-config.yaml` 에 훅 추가

```yaml
# .pre-commit-config.yaml에 추가
- repo: local
  hooks:
    - id: vg-no-fetch-in-accumulator
      name: "[VG] 1번 버튼 핸들러 내 fetch 호출 원천 차단"
      language: pygrep
      entry: "HANDOVER_SUB.*fetch|fetch.*HANDOVER_SUB"
      files: "upgrade front_end/src/App\\.tsx"
      args: [--multiline]
      # 이 패턴이 발견되면 커밋 자체를 블로킹
```

### 2-C. TypeScript 타입 강제 — 축적 전용 함수 시그니처 고정

```typescript
// api.ts에 타입 추가 — 축적 전용 함수는 반환값이 void (API 응답 없음)
// 이 타입을 통해 실행 에이전트가 실수로 await를 붙이면 TS 오류 발생

type AccumulatorAppend = (text: string) => void; // 네트워크 없음을 타입으로 강제
```

---

## 통제 3 — 무결점 채점 루브릭 (Evaluator 기준)

### Evaluator가 검수 시 사용하는 채점표 (총 100점, 합격 커트라인 90점)

| 항목 | 배점 | 합격 기준 | 실격 트리거 |
|---|---|---|---|
| **1번 버튼 3회 클릭 시 Network Payload 0건** | 30점 | DevTools Network 탭에서 `/api/*` 요청 0건 확인 | 단 1건이라도 API 호출 발생 시 즉시 0점 |
| **2번 버튼 클릭 시 축적 데이터 묶음 전송** | 25점 | Network 탭에서 `POST /api/v8/handover` 1건 + body에 기록 내용 전체 포함 확인 | 빈 body 또는 기록 1건만 전송 시 0점 |
| **3번 버튼 클릭 시 식별자만 전송** | 20점 | Network 탭에서 `POST /api/v8/handover/confirm` 1건 + body에 text 필드 없음 확인 | text 필드 존재 시 0점 |
| **축적 State 검증 (기록 3번 후 배열 길이 3)** | 15점 | React DevTools에서 `handoverAccumulator` 배열 길이 = 기록 횟수 확인 | 배열 길이 불일치 시 0점 |
| **기존 업무기록/카카오 경로 회귀 없음** | 10점 | `LOG_SUB` 경로에서 기존 `saveLog()` 정상 호출 확인 | 기존 경로 깨지면 0점 |

**합격 커트라인: 90점 미만 시 즉시 재구현 명령**

---

## 통제 4 — Done Contract (완료 계약)

**실행 에이전트는 구현 완료를 스스로 선언할 수 없다. 반드시 아래 증거를 Evaluator에게 제출해야 한다.**

### 필수 제출 증거 목록

#### 증거 1: 축적된 State 로그 (콘솔 출력)
```
// 구현 완료 후 다음 콘솔 출력이 찍혀야 함 (기록 3회 시)
[VG-ACCUMULATOR] 기록 추가: 1번째 | 현재 배열 길이: 1
[VG-ACCUMULATOR] 기록 추가: 2번째 | 현재 배열 길이: 2
[VG-ACCUMULATOR] 기록 추가: 3번째 | 현재 배열 길이: 3
[VG-ACCUMULATOR] 2번 버튼 전송 | 전송 payload 글자수: XXX자
```

#### 증거 2: Network Payload 스크린샷 (2종)
1. **1번 버튼 3회 클릭 후** → DevTools Network 탭 → `/api/*` 호출 0건 스크린샷
2. **2번 버튼 클릭 후** → DevTools Network 탭 → `POST /api/v8/handover` Request Body 스크린샷 (축적된 텍스트 3건 포함 확인)

#### 증거 3: 2번/3번 버튼 응답 확인
- `POST /api/v8/handover` → 200 응답 또는 백엔드 미연결 시 에러 토스트 정상 표시
- `POST /api/v8/handover/confirm` → 200 응답 또는 에러 토스트 정상 표시

### Done Contract 서명 절차
```
1. 위 증거 3종 준비 완료
2. "VG_DONE_CONTRACT_SUBMIT: [스크린샷 첨부]" 형식으로 Evaluator에게 보고
3. Evaluator(나)가 채점 루브릭 적용 → 90점 이상 시 "DONE" 선언
4. "DONE" 선언 없이는 완료 처리 불가. 자의적 종료 절대 금지.
```

---

## 실행 순서 (Generator 필독)

```
[STEP 1] upgrade front_end/src/App.tsx
  → handoverAccumulator 상태 추가 (sessionStorage 복원 포함)
  → handleExecute() 분기 수정 (HANDOVER_SUB일 때 append only)
  → executeHandoverAction() 교체 (sendHandover/confirmHandover 호출)

[STEP 2] upgrade front_end/src/services/api.ts
  → sendHandover() 메서드 추가
  → confirmHandover() 메서드 추가
  → 기존 saveLog/sendKakao 절대 수정 금지

[STEP 3] 로컬 테스트
  → 1번 버튼 3회 클릭 → Network 0건 + 콘솔 로그 확인
  → 2번 버튼 클릭 → POST /api/v8/handover body 확인
  → 3번 버튼 클릭 → POST /api/v8/handover/confirm body 확인

[STEP 4] Done Contract 제출 → Evaluator 검수
```

---

## 절대 금지 사항

1. `upgrade front_end/` 이외 다른 폴더 수정 금지
2. 1번 버튼 핸들러 경로에 `fetch`, `axios`, `apiService.*` 호출 추가 금지
3. `handleDoubleTap` 로직 변경 금지 (2번/3번 버튼 UX 무수정)
4. `handoverAccumulator`를 `localStorage`에 저장 금지 (`sessionStorage`만 허용)
5. 기존 `mode === 'LOG'` 경로의 `saveLog()` 호출 삭제 금지 (업무기록 기능 유지)

---

*설계도 종료 — Evaluator 서명: 2026-04-23*  
*이 문서는 Generator의 구현 바이블이다. 해석의 여지 없이 따른다.*
