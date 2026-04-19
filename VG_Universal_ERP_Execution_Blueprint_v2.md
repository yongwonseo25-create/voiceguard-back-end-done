# VG_Universal_ERP_Execution_Blueprint_v2
## Voice Guard 범용 ERP 자동 이관 시스템 — 최상위 어드바이저 설계도

> **발행자**: 최상위 클로드 어드바이저 (Evaluator 역할)
> **수신자**: 실행 에이전트 (Generator 역할)
> **발행일**: 2026-04-19
> **상태**: FINAL — 실행 에이전트는 이 설계도를 단일 진실의 원천(Source of Truth)으로 삼아라.

---

## 0. 어드바이저 사전 경고 (실행 에이전트 필독)

이 설계도는 **결과물 판정 기준**과 **시스템 구조 결단**을 담은 문서다.
세세한 함수 이름, 파일 경로, 변수명은 네(Generator)가 결정할 영역이다.
어드바이저는 "무엇을, 왜, 어떤 기준으로"를 정의한다. "어떻게 코딩하느냐"는 네 몫이다.

**절대 금지 사항:**
- 이 설계도의 아키텍처 결단을 무단 변경하는 행위
- 채점 루브릭을 통과하지 못한 상태에서 "완료"를 선언하는 행위
- ERP 어댑터가 내부 표준 모델(CTO)을 거치지 않고 외부 ERP와 직접 매핑하는 행위

---

## 결단 1: 3계층 아키텍처 — CTO 표준 모델 의무 통과

### 핵심 결단
**외부 ERP와 직접 매핑(Direct Mapping)을 금지한다.** 모든 데이터는 반드시 내부 표준 모델(Canonical Transfer Object, CTO)을 통과한 후에만 어댑터로 진입할 수 있다.

### 이유
ERP가 5개만 되어도 직접 매핑은 필드 규칙·예외·버전 관리가 폭발한다. CTO가 없으면 결국 예외의 무덤이 된다.

### 3계층 구조 정의

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: 경험 계층 (Experience Layer)                  │
│  Notion 승인 버튼 → Webhook → Approval Ingestor         │
│  → 표준 내부 이벤트만 발행. 외부 ERP를 모른다.          │
└────────────────────┬────────────────────────────────────┘
                     │ 표준화된 CTO 페이로드
┌────────────────────▼────────────────────────────────────┐
│  Layer 2: 오케스트레이션 계층 (Process & Orch Layer)    │
│  Integration Orchestrator (상태 머신)                    │
│  Ops DB 상태 관리 + Adapter Registry 라우팅              │
│  → CTO를 보고 "어떤 어댑터로 갈 것인가"만 결정한다.    │
└────────────────────┬────────────────────────────────────┘
                     │ 어댑터별 변환 명령
┌────────────────────▼────────────────────────────────────┐
│  Layer 3: 시스템 계층 (System Layer)                    │
│  Adapter Worker (Cloud Run)                             │
│  Tier 1: API Adapter → REST/SOAP/GraphQL                │
│  Tier 2: File Adapter → CSV/XLSX/SFTP                   │
│  Tier 3: UI/RPA Adapter → Playwright 헤드리스 봇       │
│  Tier 4: Desktop/VNC Fallback (극단 케이스만)           │
└─────────────────────────────────────────────────────────┘
```

### CTO(Canonical Transfer Object) 필수 필드

```json
{
  "idempotency_key": "sha256(tenant_id+facility_id+internal_record_id+record_version+target_system+mapping_version)",
  "tenant_id": "string",
  "facility_id": "string",
  "internal_record_id": "string",
  "record_version": "integer",
  "approved_at": "ISO8601",
  "approved_by": "string",
  "target_system": "angel|carefo|wiseman|...",
  "target_adapter_version": "string",
  "clinical_payload": { /* 6대 의무기록 구조화 JSON */ },
  "legal_hash": "sha256_of_worm_entry",
  "evidence_refs": ["worm_row_id_1", ...]
}
```

**이 객체가 완성되기 전에는 어댑터를 절대 호출하지 마라.**

### 역할 분리 강제

| 시스템 | 역할 | 금지 사항 |
|--------|------|-----------|
| **Notion** | 운영자 관제/승인 UI | 상태 머신 DB로 쓰는 것 금지 |
| **WORM 원장** | 법적 증거 append-only | 운영 상태(QUEUED→RUNNING) 빠른 갱신 금지 |
| **Ops DB (PostgreSQL)** | Mutable 상태 머신 단독 운용 | WORM과 역할 혼용 금지 |

**Ops DB 최소 필수 테이블:**
- `integration_transfers` — 이관 요청 및 상태
- `transfer_attempts` — 재시도 이력
- `adapter_versions` — 어댑터 버전 레지스트리
- `credential_refs` — Secret Manager 키 참조 (평문 금지)
- `reconciliation_jobs` — UNKNOWN 상태 재조사 작업

---

## 결단 2: 워커 스택 — Playwright 엔진 + Robocorp 스캐폴딩

### 핵심 결단
**브라우저 자동화 엔진은 Playwright, 프로덕션 스캐폴딩은 Robocorp로 확정한다.**
Selenium은 신규 도입 금지. Puppeteer는 Chrome-only 보조 역할로만 허용.

### 이유
레거시 ERP는 JS 렌더링 타이밍이 불규칙하다. Playwright의 Built-in Auto-wait가 이 문제를 원천 해결한다. Robocorp는 Cloud Run 리눅스 컨테이너의 브라우저 의존성 지옥(폰트, X11, 공유 객체)을 `conda.yaml` 선언만으로 완전 자동화한다.

### Auto-wait 구현 지침

```python
# 실행 에이전트에게: 이 패턴을 의무화하라
# ❌ 절대 금지: time.sleep() 또는 page.wait_for_timeout()
# ✅ 강제 패턴: Playwright Auto-wait + Role-based Locator

# 예시 (개념 코드 — 실제 구현은 Generator가 담당)
page.get_by_role("button", name="저장").click()        # actionability 자동 검증
page.get_by_label("수급자 ID").fill(resident_id)       # DOM 준비 후 자동 진입
page.get_by_text("저장 완료").wait_for()               # 성공 신호 감지
```

**금지 패턴:**
- `page.locator("//div[3]/table/tr[2]/td[1]")` — XPath 고정 셀렉터 금지
- `page.wait_for_timeout(3000)` — 하드코딩 슬립 금지
- `page.locator(".btn-submit")` — CSS 클래스 덕지덕지 금지

**허용 패턴:**
- `get_by_role`, `get_by_label`, `get_by_text`, `get_by_test_id`

### Robocorp 컨테이너 설정 강제

```yaml
# conda.yaml — 이 구조를 반드시 사용하라
channels:
  - conda-forge
dependencies:
  - python=3.11
  - pip:
    - robotframework
    - robotframework-playwright
    - playwright  # 브라우저 바이너리 자동 설치
    # Cloud Run 환경 의존성 선언으로 "로컬만 되고 서버에서 안 됨" 버그 0화
```

### Cloud Run 배포 분리

| 워크로드 | 배포 방식 | 용도 |
|---------|-----------|------|
| 건별 실시간 이관 | Cloud Run Service (Cloud Tasks 트리거) | 승인 후 즉시 실행 |
| 대량 재처리/백필 | Cloud Run Jobs | 야간 배치, UNKNOWN 재조사 |
| 극단 Desktop-only ERP | Desktop/VNC Fallback (격리된 별도 서비스) | 예외 케이스 격리 |

**Playwright Trace Viewer를 반드시 활성화하라.** 실패 분석 시 DOM 스냅샷·네트워크·콘솔 로그를 사후 포렌식으로 제공한다. 헬스케어 B2B에서 "왜 이관 실패했는지" 설명 가능성은 필수다.

---

## 결단 3: Saga 패턴 — 분산 트랜잭션 무결성

### 핵심 결단
**롤백(Rollback) 개념은 버려라. "검증 가능한 상태 머신 + 보상 트랜잭션"으로 대체한다.**
초기 1~3개 ERP: **Cloud Tasks + Ops DB**로 시작.
어댑터 10개 초과 또는 예외 플로우 폭증 시: **Temporal.io** 도입으로 진화.

### 이유
외부 ERP UI 자동화에서 진짜 롤백은 불가능하다. 저장 버튼이 눌렸는지, 타임아웃 직전에 성공했는지 확신이 없다. 상태 머신 + Idempotency + Read-after-write가 정답이다.

### 의무 상태 머신

```
정상 경로:
APPROVED → QUEUED → DISPATCHED → AUTHENTICATED → WRITING → SUBMITTED → VERIFYING → COMMITTED

실패 계열 (별도 분기):
RETRYABLE_FAILED → (재시도) → SUBMITTED
UNKNOWN_OUTCOME → (재조사 후 판단) → COMMITTED 또는 재전송
TERMINAL_FAILED → MANUAL_REVIEW_REQUIRED
```

**COMMITTED는 외부 시스템에 실제 존재함이 Read-after-write로 확인된 후에만 찍는다.**

### Idempotency Key 생성 규칙 (강제)

```python
# 실행 에이전트는 이 규칙을 그대로 구현하라
import hashlib

def generate_idempotency_key(cto: dict) -> str:
    raw = "|".join([
        cto["tenant_id"],
        cto["facility_id"],
        cto["internal_record_id"],
        str(cto["record_version"]),
        cto["target_system"],
        cto["target_adapter_version"]
    ])
    return hashlib.sha256(raw.encode()).hexdigest()
```

이 키는:
1. WORM 원장에 봉인
2. Ops DB의 `integration_transfers` 테이블에 UNIQUE CONSTRAINT 적용
3. API ERP면 헤더 또는 request body에 포함
4. UI ERP면 가능하면 메모/비고 필드에 기록

### UNKNOWN 처리 규칙 (절대 준수)

```
타임아웃 발생 시:
1. 상태 → UNKNOWN_OUTCOME (재전송 즉시 금지)
2. 외부 ERP 탐색/조회 기반 reconciliation 수행
3. 흔적 있음 → 상태 → COMMITTED
4. 흔적 없음 → 그때만 재전송 허용
```

### 보상 트랜잭션 발동 조건 및 절차

| 오류 유형 | 분류 | 처리 방식 |
|-----------|------|-----------|
| 타임아웃, 5xx, 네트워크 단절 | RETRYABLE | Exponential Backoff (min 1s, max 60s, jitter 포함) + Circuit Breaker |
| 자격증명 불일치, 계정 잠금 | TERMINAL | 재시도 중단 → 보상 트랜잭션 즉시 발동 |
| selector 파손, 필수 필드 누락 | TERMINAL | 재시도 중단 → 보상 트랜잭션 즉시 발동 |

**보상 트랜잭션 절차 (Backward Recovery):**
1. Ops DB: 해당 transfer 상태 → `TRANSFER_FAILED`
2. WORM 원장: `MIGRATION_FAILED` 이벤트 append (불변 증거)
3. Notion PATCH: 해당 페이지 상태 → '이관 실패 (담당자 점검 필요)' + 실패 타임스탬프
4. 관리자 알림 발송

### Queue 전략 분리

| 역할 | 도구 | 이유 |
|------|------|------|
| 실제 어댑터 실행 트리거 | Cloud Tasks | 명시적 대상 호출, retry/backoff, rate limit, dedupe |
| 이벤트 fan-out, 순서 보장 | Pub/Sub (ordering key: `tenant_id:entity_key`) | 동일 수급자 기록의 순서 보장 |
| 반복 실패 메시지 격리 | Pub/Sub Dead Letter Topic | 메인 플로우 차단 없이 오프라인 분석 |

**ERP별 Cloud Tasks Queue 분리를 강제한다** (예: `queue-angel`, `queue-carefo`). 느린 ERP는 1~3 rps, 동시 1~2로 제한한다.

---

## 결단 4: 제로 트러스트 보안 — 봉투 암호화

### 핵심 결단
**고객사 ERP 계정(ID/PW)을 앱 DB에 평문으로 저장하는 것을 절대 금지한다.**
GCP Secret Manager + Cloud KMS 봉투 암호화(Envelope Encryption)를 사용하고,
런타임 시 인메모리(In-Memory)로 주입 후 컨테이너 소멸과 동시에 즉각 파기한다.

### 이유
B2B SaaS에서 수백 개 요양원의 평문 자격증명이 DB에 저장되면, 단 한 번의 침해로 전체 고객 계정이 유출된다. 봉투 암호화는 DB 해킹 시에도 복호화 키가 없어 무용지물만 남긴다.

### 봉투 암호화 저장 파이프라인 (Storage Phase)

```
고객 ERP 계정 등록 시:
1. 백엔드 → Cloud KMS 호출 → DEK(Data Encryption Key) 동적 생성
2. DEK로 평문 ID/PW 암호화 → 암호문(Ciphertext)만 Ops DB에 저장
3. DEK 자체 → KEK(Key Encryption Key, Cloud KMS 마스터 키)로 재암호화
4. 암호화된 DEK → Secret Manager 저장
5. Ops DB에는 암호문 + Secret Manager 키 참조값만 존재. 평문은 어디에도 없다.

결과:
- DB 해킹 → 암호문만 획득. 복호화 키 없음. 무용지물.
- Secret Manager에는 KEK로 암호화된 DEK만. 마스터 키는 KMS가 별도 보호.
```

### 런타임 주입 파이프라인 (Injection Phase)

```
어댑터 워커 실행 시:
1. Cloud Run 컨테이너 → OIDC Workload Identity 임시 토큰 획득 (영구 키 파일 금지)
2. 워커 → Ops DB에서 암호화된 자격증명 읽기
3. 워커 → Secret Manager/KMS에 복호화 요청 → 평문 ID/PW 반환
4. 평문 → 하드디스크 기록 없이 메모리(Python 변수)에만 로드
5. Playwright 엔진에 런타임 환경변수로 주입
6. 브라우저 작업 완료 → 컨테이너 소멸 → 메모리 완전 휘발 파기
```

### Secret Manager 키 명명 규칙 (강제)

```
{env}/tenant-{tenant_id}/{erp_system}/login
예:
prod/tenant-001/angel/login
prod/tenant-001/carefo/login
prod/tenant-001/wiseman/login
prod/tenant-001/angel/cert
```

Secret 값은 JSON 구조 사용:
```json
{
  "username": "...",
  "password": "...",
  "login_url": "...",
  "mfa_mode": "none|totp|human-stepup",
  "account_scope": "write_records_only"
}
```

### IAM 최소권한 원칙 강제

| 서비스 계정 | 접근 권한 |
|------------|-----------|
| `adapter-angel-sa` | 엔젤 관련 secret만 `secretAccessor` |
| `adapter-carefo-sa` | 케어포 관련 secret만 `secretAccessor` |
| `replay-job-sa` | 재처리 제한 권한 |
| 개발자 인간 계정 | prod secret 직접 read **금지** |

**Data Access Audit Log를 반드시 활성화하라.** `AccessSecretVersion` 호출 전수 기록 의무.

### 로그 마스킹 강제
Robocorp가 수집하는 DLQ 아티팩트, 스크린샷, 콘솔 로그에 자격증명 필드가 노출되지 않도록 **RPA 프레임워크 레벨에서 Vault 마스킹을 강제 적용**한다.

---

## 결단 5: Generator/Evaluator 역할 분리 — 4대 루브릭 및 Done Contract

### 핵심 결단
**어드바이저(나)는 결과물이 완벽한가를 판단한다. Generator는 결과물을 만든다.**
어드바이저는 마이크로 코딩 지시를 내리지 않는다. Generator는 자기 코드를 스스로 "완벽하다"고 선언하지 않는다.

### 4대 채점 루브릭

모든 스프린트 결과물은 다음 4개 차원에서 **외부 검증자(Evaluator) 또는 테스트 스위트**가 판정한다. Generator의 자체 선언은 점수에 반영되지 않는다.

---

#### Rubric 1: 기능성 (Functionality)
**판정 기준: 데이터가 실제로 외부 ERP에 정확히 들어갔는가?**

| 점검 항목 | 합격 기준 |
|-----------|-----------|
| CTO 필수 필드 완전성 | 12개 필드 모두 존재 및 타입 정확 |
| Idempotency | 동일 CTO 2회 전송 시 ERP에 중복 데이터 0건 |
| Read-after-write | 전송 완료 후 ERP 조회 스크래핑으로 존재 확인 |
| 3계층 어댑터 라우팅 | API/File/UI 중 올바른 Tier 선택 |
| COMMITTED 상태 정확성 | 외부 확인 전 COMMITTED 찍는 코드 0건 |

---

#### Rubric 2: 보안성 (Security)
**판정 기준: 자격증명이 평문으로 단 1바이트도 노출되지 않는가?**

| 점검 항목 | 합격 기준 |
|-----------|-----------|
| 봉투 암호화 적용 | Ops DB에 평문 자격증명 컬럼 0개 |
| 인메모리 주입 확인 | 로컬 파일, 환경변수 하드코딩, 로그에 자격증명 0건 |
| IAM 최소권한 | 각 어댑터 SA가 타 tenant secret 접근 불가 검증 |
| Workload Identity | 영구 키 파일(*.json) 사용 코드 0건 |
| 로그 마스킹 | 스크린샷·콘솔 로그에 password 필드 노출 0건 |

---

#### Rubric 3: 무결성 (Integrity)
**판정 기준: WORM·Ops DB·Notion의 상태가 항상 일치하는가?**

| 점검 항목 | 합격 기준 |
|-----------|-----------|
| WORM append 체인 | APPROVED → MIGRATION_CONFIRMED 또는 MIGRATION_FAILED 이벤트 연속 존재 |
| Ops DB 상태 전이 | 상태 역행(COMMITTED → QUEUED 등) 코드 0건 |
| Notion PATCH 동기화 | 성공/실패 모두 Notion 상태 업데이트 코드 존재 |
| UNKNOWN 처리 | 타임아웃 즉시 재전송 코드 0건 (reconcile 선행 강제) |
| 보상 트랜잭션 | TERMINAL_FAIL 시 WORM + Notion 롤백 코드 존재 |

---

#### Rubric 4: 운영성 (Operability)
**판정 기준: 장애 발생 시 원인을 30분 이내에 파악할 수 있는가?**

| 점검 항목 | 합격 기준 |
|-----------|-----------|
| Playwright Trace Viewer | 실패 케이스에서 trace 파일 자동 수집 코드 존재 |
| Structured Logging | 모든 상태 전이에 `tenant_id`, `idempotency_key`, `timestamp` 포함 |
| DLQ 운영 | TERMINAL_FAIL 메시지 Dead Letter Topic 분리 확인 |
| Selector Canary | 매일 ERP UI 변경 탐지 스크립트 존재 (Tier 3 어댑터 대상) |
| 어댑터 버전 추적 | 어떤 `selector_profile_version`으로 어떤 기록이 들어갔는지 Ops DB에서 추적 가능 |

---

### Done Contract (스프린트 완료 조건)

**Generator는 다음 조건이 모두 충족되어야만 스프린트 완료를 선언할 수 있다:**

```
[ ] Rubric 1 기능성: 5개 점검 항목 전체 합격
[ ] Rubric 2 보안성: 5개 점검 항목 전체 합격
[ ] Rubric 3 무결성: 5개 점검 항목 전체 합격
[ ] Rubric 4 운영성: 5개 점검 항목 전체 합격
[ ] 단위 테스트: 각 어댑터 Tier별 정상/실패/UNKNOWN 시나리오 커버
[ ] 통합 테스트: CTO 생성 → 이관 → WORM append → Notion PATCH E2E 통과
[ ] 보안 스캔: 평문 자격증명 grep 결과 0건 확인
```

**위 체크리스트 중 단 1개라도 미통과 시 — 완료 선언 금지.**

---

## 부록: 안티패턴 블랙리스트

Generator는 다음 코드 패턴을 작성하는 즉시 중단하고 설계도를 재독하라.

| 안티패턴 | 이유 |
|----------|------|
| `erp_password = db.get_plaintext_password()` | 봉투 암호화 위반 |
| `time.sleep(3)` | Auto-wait 원칙 위반 |
| `page.locator("//table/tr[2]/td[1]")` | XPath 고정 셀렉터, UI 변경 시 즉사 |
| `status = "COMMITTED"` (검증 전) | 외부 확인 전 COMMITTED 금지 |
| `notion_db.update(state="QUEUED")` (Notion을 상태 DB로 사용) | Notion 역할 위반 |
| `worm.update(transfer_status="RUNNING")` (WORM에 mutable 상태 저장) | WORM append-only 위반 |
| `if timeout: resend_immediately()` | UNKNOWN 재조사 없이 즉시 재전송 금지 |
| ERP 어댑터 내부에서 직접 `clinical_payload` 필드 접근 | CTO 표준 모델 우회 금지 |

---

## 부록: 어댑터 메타데이터 계약 (Adapter Registry 등록 의무)

각 ERP 어댑터는 코드보다 이 메타데이터 계약이 먼저 완성되어야 한다:

```yaml
adapter_id: "angel_v1"
adapter_type: "ui"  # api | file | ui
erp_system: "angel"
supported_entities:
  - care_note
  - medication
  - meal_record
mapping_version: "1.0.0"
selector_profile_version: "2026-04-19"
login_strategy: "id_pw_form"
rate_limit_profile:
  max_rps: 2
  max_concurrent: 1
verification_strategy: "read_after_write_scrape"
evidence_policy: "screenshot_on_success_and_failure"
recoverability_rules:
  retryable: ["timeout", "5xx", "network_error"]
  terminal: ["auth_failure", "selector_broken", "missing_required_field"]
```

이 메타데이터가 없는 어댑터는 Adapter Registry에 등록 불가 — 실행 불가.

---

*이 설계도는 VoiceGuard Universal ERP Integration System의 최상위 아키텍처 결단을 담는다. Generator는 이 문서의 결단을 구현하고, Evaluator는 Done Contract의 충족 여부만을 판정한다.*
