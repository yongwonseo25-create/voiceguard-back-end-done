# Voice Guard — 일일 작업 인수인계서

> **작성일**: 2026-04-10 (세션 종료 시점)
> **커밋**: `bbb022f` (백엔드 부모 repo) / `dd6b17f` (FRONT END repo)

---

## ✅ Done — 오늘 완수한 작업

### Phase C — 프론트엔드 하드코딩 3대 결함 수정

| 타격 | 파일 | 변경 내용 |
|------|------|-----------|
| 타격 1 | `api.ts`, `handoverApi.ts` | `DEV_MOCK = true` 강제 하드코딩 → 500 에러 원천 차단 |
| 타격 2 | `App.tsx` | 업무기록/카카오 박스 `pt-10→pt-3`, `gap-3→gap-2` → 하단 오버플로우 32px 제거 |
| 타격 3 | `App.tsx` | 수신확인 완료 배너 `useEffect + setTimeout(2000)` 자동 소멸 구현 |

**카카오톡 2-Way 타겟팅 (이전 세션 완성분, 오늘 커밋 반영)**
- `KAKAO_EMERGENCY`: 🚨 원장님 전달 → `/api/v7/notify/emergency` — 20초 골든타임 TTL
- `KAKAO_SHIFT`: 👥 관리자+동료 전달 → `/api/v7/notify/shift-group` — 60초 골든타임 TTL
- `shiftCode: 'AUTO'` 하드코딩 (백엔드 시계 기반 자동 산정, FE 개입 금지)
- 데드라인 초과 → `NotifyDeadlineError` → UI 적색 경고 + 전화 유도 렌더링

**Phase 7~8 백엔드 일괄 반영**
- `schema_v15`: 6대 의무기록(식사/투약/배설/체위변경/위생/특이사항) 원장 스키마
- `schema_v16`: Append-Only 이벤트 원장 (UPDATE/DELETE DB 트리거 차단)
- `gemini_processor.py`: Gemini Flash 연동 처리기 신규 추가
- `env_guard.py`: 환경변수 보안 가드 신규 추가
- E2E 테스트 `test_care_record_e2e.py` 신규 추가

---

## 🔧 State — 현재 시스템 상태

```
백엔드(Gemini/Notion): 물리적 단절 — DEV_MOCK 강제 활성화 상태
프론트엔드 빌드: Vite 개발 서버 (npm run dev)
VITE_API_BASE_URL: 미설정 (env 파일 없음, .gitignore 처리)
```

**Mock 응답 경로**:
- `triggerHandover()` → 1초 딜레이 → `PENDING` 상태 더미 report
- `pollReport()` → 300ms 딜레이 → `DONE` 상태 더미 report
- `ackHandover()` → 800ms 딜레이 → 성공 ACK
- `transcribeAudio()` → 1500ms 딜레이 → 더미 음성인식 텍스트

**복원 방법** (실서비스 전환 시):
```typescript
// api.ts 및 handoverApi.ts 에서
const DEV_MOCK = true;          // ← 이 줄을
const DEV_MOCK = !API_BASE_URL; // ← 이렇게 복원 후 .env.local에 URL 설정
```

---

## 🎯 Next Steps — 내일 즉시 타격할 목표

### 우선순위 1 — 더미 연동 해제 및 실제 API 복구
```
조건: 백엔드 FastAPI 서버 재가동 후
1. .env.local 파일 생성: VITE_API_BASE_URL=http://localhost:8000
2. api.ts / handoverApi.ts: DEV_MOCK 복원 (위 복원 방법 참고)
3. npm run dev 재시작 → 실 API 연결 확인
```

### 우선순위 2 — 인수인계 플로우 E2E 검증
```
실 API 복구 후 반드시 확인:
- [퇴근 보호사] 인수인계하기 버튼 → PENDING → DONE 폴링 확인
- [출근 보호사] 인수인계 확인 → 수신확인 완료 → 2초 후 자동 소멸
- 카카오 긴급/교대조 알림 실제 발송 확인 (골든타임 20초/60초)
```

### 우선순위 3 — 시연 영상 추출 준비
```
- 모바일 화면 캡처 또는 브라우저 녹화 도구 준비
- DEV_MOCK 상태에서 1차 시연 영상 촬영 (백엔드 없이도 가능)
- 촬영 순서: 홈 → 업무기록 → 카카오긴급 → 인수인계 전달 → 수신확인
```

### 우선순위 4 — schema_v16 DB 적용
```
schema_v16_append_only_events.sql을 실제 DB에 마이그레이션
(schema_v15는 이전 세션에 적용 완료, v16은 미적용 상태)
```

---

## 📁 주요 파일 위치

| 파일 | 경로 | 역할 |
|------|------|------|
| `App.tsx` | `FRONT END/src/App.tsx` | 메인 UI 로직 전체 |
| `api.ts` | `FRONT END/src/services/api.ts` | 카카오/음성 API Mock |
| `handoverApi.ts` | `FRONT END/src/services/handoverApi.ts` | 인수인계 전용 API Mock |
| `main.py` | `backend/main.py` | FastAPI 라우터 전체 |
| `notifier.py` | `backend/notifier.py` | 카카오 알림톡 팬아웃 |
| `schema_v16` | `schema_v16_append_only_events.sql` | ⚠️ DB 미적용 상태 |
