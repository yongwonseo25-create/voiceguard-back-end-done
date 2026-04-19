## 🔒 프로젝트 인프라 맥락 및 4대 절대 규칙 (신규 에이전트 최우선 독해)

### 규칙 1 (인프라 맥락)
본 프로젝트는 **Voice Guard B2B MVP**다. 백엔드 파이프라인은
**Firebase App Hosting + Cloud Run(voice-guard-api) + Cloud SQL(PostgreSQL WORM 원장) + Secret Manager**
구글 생태계 정식 레일 위에 완공되어 있다. 이 인프라를 건드리지 마라.

### 규칙 2 (마이크 앱 진입점 강제)
Firebase 기본 URL(`https://voice-guard-pilot.web.app/`) 접속 시 **즉각 모바일 마이크 녹음 앱 UI**가 떠야 한다.
원장 관제탑 대시보드는 `/admin` 경로에 묶인다. 단일 프론트엔드(`Directer_Dashboard/`)로 구성한다.

### 규칙 3 (파이프라인 고정)
데이터 흐름: **[마이크 음성 투척 → POST /api/v2/ingest → Whisper → Gemini → PostgreSQL WORM 원장 → 대시보드 표출]**
이 순서를 절대 바꾸지 마라.

### 규칙 4 (키 인식 오류 원천 차단)
`GEMINI_API_KEY`는 Google AI Studio Key와 동일하다. 별도로 제미나이 키를 요구하거나 생성하지 마라.

### 규칙 5-A (이중 매핑 강제) ⚠️ 위반 즉시 중단
담당 보호사 데이터는 반드시 **Relation(`VG_보호사직원_DB`)과 Person(`알림용 담당자`) 속성에 이중으로 매핑**하여 알림 누락을 막는다.
`lookup_staff()` 단일 호출로 `(page_id, user_ids)` 튜플을 받아 두 속성에 동시에 적재한다. 어느 하나라도 빠지면 버그다.

### 규칙 5 (UI 창조 절대 금지 및 기존 폴더 강제 매핑) ⚠️ 위반 즉시 중단
프론트엔드 UI를 띄울 때 **절대 임의로 코드를 새로 작성하거나 창조하지 마라.**
사령관님이 이미 완성해 둔 기존 프론트엔드 소스코드는 **`FRONT END/` 폴더**에 있다.
정확히 찾아내어, 그 안에 있는 코드를 **1바이트도 수정하지 말고** 그대로 빌드/복사/연결해서 배포해라.

---

## ⚖️ 법적 증거 인프라 선언 (최상위 불변 원칙)
> **본 시스템은 '6대 필수기록(식사, 투약, 배설, 체위변경, 위생, 특이사항)'과
> WORM 해시체인 증거 원장을 다루는 법적 증거 인프라다.
> 모든 DB 적재와 API는 이 기준을 통과해야 한다.**

## 0. Absolute Rules (위반 시 즉시 중단)
- **Core 원장(ledger) 테이블 스키마 수정 절대 금지** — 기존 컬럼 삭제/타입 변경/이름 변경 불가
- **Append-only 유지** — UPDATE/DELETE on ledger rows 금지, 보정은 반드시 새 row INSERT
- **트리거 원천 차단** — 모든 원장(Ledger)은 Append-Only이며 UPDATE/DELETE/TRUNCATE는 DB 트리거로 원천 차단한다. care_plan_ledger의 is_superseded/superseded_by 컬럼만 조건부 UPDATE 허용 (계획 무효화 전용)
- **이중 노동 제로** — 같은 로직을 두 곳에 구현하지 않는다. 공통 함수로 추출
- **Self-evaluation 금지** — 내가 작성한 코드를 스스로 "완벽하다"고 판단하지 않는다. 반드시 테스트/검증기 통과로만 확인
- **고차원 목표 중심 실행** — 마이크로 구현 계획 나열 금지. 프로덕트 목표를 잡고 즉시 실행

## 1. Northstar
- 데이터 유실 0% 및 환수 방어
- Source of Truth: 추측 금지, 실제 코드 기반의 결정론적 검증만 인정

## 2. Model Allocation
- Architecture & Planning: Opus 4.6 (Deep reasoning)
- Basic Coding: Sonnet (Standard implementation)
- Evaluator & Formatting: Haiku (Cost/Speed optimization)
- Research & Large Context: Gemini Flash (Opus 85% quality, 1/10 cost)

## 3. GSD Workflow (7-Step Pipeline)
1. Map -> 2. New -> 3. Discuss -> 4. Plan -> 5. Execute -> 6. Verify -> 7. Next
- Generator(Opus/Sonnet)와 Evaluator(Haiku)를 엄격히 분리
- 'Done Contract' 확정 전에는 구현 착수 금지

## 4. Context Hygiene & Efficiency
- One Window = One Task (주제 전환 시 /clear 및 새 세션)
- /compact: 컨텍스트 60% 도달 전 수행 (98.5% 재독 비용 방지)
- Peak Hours: ET 8am-2pm 회피 (세션 한도 관리)
- Window: 첫 메시지 후 5시간 윈도우 관리 준수

## 5. Applied Learning (Gotchas)
- 중복 가드는 공통 밸리데이터에만 집중한다. (7단어)
- 신규 기능과 클린업을 한 커밋에 섞지 않는다. (9단어)
- 평가자는 생성자의 가정을 배제하고 적대적으로 검증한다. (9단어)
- 세션 종료 전 기술 부채 정리 스킬을 실행한다. (9단어)

## 6. Runtime & Commands
- MCP2CLI: 무거운 MCP는 CLI를 통해 필요 시에만 호출
- Build: 프로젝트 표준 빌드 커맨드 사용
- Test: 명시된 테스트 스위트 전체 실행
- Skill: voice-guard-tech-debt (세션 종료 필수)