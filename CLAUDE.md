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