# Voice Guard 2차 코어 엔진 — 무결점 개발 로드맵 및 아키텍처

> 확정일: 2026-04-08
> 상태: 설계 확정 — 코딩 대기
> 전제: 기존 evidence_ledger 스키마 수정 절대 금지 (CLAUDE.md §0)

---

## 전체 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Voice Guard 2차 코어 엔진                             │
├──────────────┬──────────────┬──────────────────┬────────────────────────────┤
│  Phase 1     │  Phase 2     │  Phase 3         │  Phase 4                   │
│  데이터 수집   │  Access      │  Reconciliation  │  Unified Worker            │
│  파이프라인    │  Ledger      │  Engine          │  + Event Router            │
│              │              │                  │                            │
│ ┌──────────┐ │ ┌──────────┐ │ ┌──────────────┐ │ ┌────────────────────────┐ │
│ │care_plan │ │ │access_   │ │ │canonical_    │ │ │  unified_worker.py     │ │
│ │_schedule │ │ │ledger    │ │ │service_fact  │ │ │  ┌──────────────────┐  │ │
│ └────┬─────┘ │ └────┬─────┘ │ └──────┬───────┘ │ │  │ Event Router    │  │ │
│ ┌────▼─────┐ │ ┌────▼─────┐ │ ┌──────▼───────┐ │ │  │ ┌────────────┐ │  │ │
│ │billing_  │ │ │access_   │ │ │reconciliation│ │ │  │ │ Handler    │ │  │ │
│ │claim     │ │ │token     │ │ │_anomaly      │ │ │  │ │ Registry   │ │  │ │
│ └──────────┘ │ └──────────┘ │ └──────────────┘ │ │  │ └────────────┘ │  │ │
│              │              │ ┌──────────────┐ │ │  └──────────────────┘  │ │
│              │              │ │overlap_matrix│ │ └────────────────────────┘ │
│              │              │ │tolerance_cfg │ │                            │
│              │              │ │rule_engine   │ │                            │
│              │              │ └──────────────┘ │                            │
└──────────────┴──────────────┴──────────────────┴────────────────────────────┘
                                    │
            ┌───────────────────────▼───────────────────────┐
            │           기존 인프라 (변경 없음)                 │
            │  PostgreSQL (evidence_ledger, outbox_events)   │
            │  Redis Streams + Pub/Sub                       │
            │  Backblaze B2 WORM                             │
            └───────────────────────────────────────────────┘
```

---

## Phase 1: 데이터 수집 파이프라인 선행 구축

> **목적:** 3각 검증의 전제 조건인 계획(Plan)과 청구(Billing) 데이터 수집 경로 확보
> **해결:** Blind Spot 4 (계획·청구 데이터 수집 경로 부재)

### 1.1 신규 테이블: `care_plan_schedule`

케어 계획(급여 제공 계획서)을 수집하는 Append-Only 원장.
사업소가 주/월 단위로 작성하는 수급자별 케어 스케줄을 저장한다.

```
care_plan_schedule (INSERT-ONLY, UPDATE/DELETE/TRUNCATE 트리거 차단)
├─ id: UUID PRIMARY KEY
├─ facility_id: VARCHAR(255) NOT NULL
├─ beneficiary_id: VARCHAR(255) NOT NULL
├─ caregiver_id: VARCHAR(255)          -- 계획된 수행자 (대체 수행 비교용)
├─ care_type: VARCHAR(100) NOT NULL    -- 6대 급여유형
├─ plan_date: DATE NOT NULL            -- 계획 일자 (시간 해상도 = 일)
├─ plan_start_time: TIME               -- 계획 시작 시각 (nullable: 야간 등 무시간 계획)
├─ plan_end_time: TIME                 -- 계획 종료 시각
├─ plan_duration_minutes: INTEGER      -- 예정 소요시간 (분)
├─ is_recurring: BOOLEAN DEFAULT FALSE -- 반복 일정 여부
├─ recurrence_rule: VARCHAR(100)       -- e.g., 'WEEKLY:MON,WED,FRI'
├─ plan_source: VARCHAR(50) NOT NULL   -- 'MANUAL' | 'EXCEL_UPLOAD' | 'API_SYNC'
├─ plan_version: INTEGER DEFAULT 1     -- 같은 날짜+수급자 재계획 시 버전 증가
├─ uploaded_by: VARCHAR(100)
├─ created_at: TIMESTAMPTZ DEFAULT NOW()
├─ idempotency_key: CHAR(64) UNIQUE   -- SHA-256(facility||beneficiary||plan_date||care_type||version)
└─ Indexes:
   idx_plan_lookup: (facility_id, beneficiary_id, plan_date)
   idx_plan_date_range: (plan_date, facility_id)
   idx_plan_caregiver: (caregiver_id, plan_date)
```

**설계 근거:**
- `plan_start_time`/`plan_end_time`을 nullable로 설계 → 야간/비정규 돌봄에서 시간 없는 계획 허용 (정규화 결함 3 해결)
- `caregiver_id` 별도 저장 → 대체 수행(Substitution) 비교 근거 확보 (정규화 결함 2 해결)
- `plan_version` → 같은 날짜에 계획 수정 시 새 row INSERT (Append-Only), 최신 버전만 검증 대상

### 1.2 신규 테이블: `billing_claim`

국민건강보험공단 청구 데이터를 수집하는 원장.
월 1회 배치로 엑셀 업로드 또는 API 연동으로 입력.

```
billing_claim (INSERT-ONLY, UPDATE/DELETE/TRUNCATE 트리거 차단)
├─ id: UUID PRIMARY KEY
├─ facility_id: VARCHAR(255) NOT NULL
├─ beneficiary_id: VARCHAR(255) NOT NULL
├─ care_type: VARCHAR(100) NOT NULL
├─ claim_date: DATE NOT NULL           -- 청구 대상 일자 (시간 없음)
├─ claim_month: VARCHAR(7) NOT NULL    -- 'YYYY-MM' (청구 월)
├─ claim_duration_minutes: INTEGER     -- 청구 소요시간
├─ claim_amount_krw: INTEGER           -- 청구 금액 (원)
├─ claim_code: VARCHAR(20)             -- 급여유형코드 (11, 12, 13 등)
├─ claim_source: VARCHAR(50) NOT NULL  -- 'EXCEL_UPLOAD' | 'NHIS_API' | 'MANUAL'
├─ claim_batch_id: UUID                -- 같은 엑셀에서 온 건은 동일 batch_id
├─ uploaded_by: VARCHAR(100)
├─ created_at: TIMESTAMPTZ DEFAULT NOW()
├─ idempotency_key: CHAR(64) UNIQUE   -- SHA-256(facility||beneficiary||claim_date||care_type||claim_month)
└─ Indexes:
   idx_claim_lookup: (facility_id, beneficiary_id, claim_date)
   idx_claim_month: (claim_month, facility_id)
   idx_claim_batch: (claim_batch_id)
```

**설계 근거:**
- `claim_date`는 DATE (시간 없음) → 청구 데이터의 실제 해상도 반영 (정규화 결함 1 해결)
- `claim_month`로 월 단위 배치 매칭 지원
- `claim_batch_id`로 같은 엑셀 파일에서 온 레코드를 그룹핑 → 롤백/재업로드 추적

### 1.3 API 엔드포인트

```
POST /api/v2/plan/upload          -- 케어 계획 엑셀 업로드 (openpyxl 파싱)
POST /api/v2/plan/entry           -- 단건 계획 수동 입력
GET  /api/v2/plan/schedule        -- 기간별/수급자별 계획 조회
POST /api/v2/billing/upload       -- 청구 데이터 엑셀 업로드
POST /api/v2/billing/entry        -- 단건 청구 수동 입력
GET  /api/v2/billing/claims       -- 기간별/수급자별 청구 조회
```

**엑셀 업로드 플로우:**
```
┌─────────────┐    ┌────────────────┐    ┌────────────────────┐
│ 관리자       │    │ FastAPI        │    │ PostgreSQL         │
│ 엑셀 파일    │───▶│ openpyxl 파싱   │───▶│ Atomic Batch       │
│ (.xlsx)     │    │ + 행별 검증     │    │ INSERT             │
└─────────────┘    │ + SHA-256 idem │    │ (ON CONFLICT SKIP) │
                   └────────────────┘    └────────────────────┘
                          │
                   유효성 검증 실패 행 → 응답에 포함 (거부 사유 명시)
                   성공 행 → care_plan_schedule 또는 billing_claim INSERT
```

**검증 규칙 (엑셀 파싱 시):**
- 필수 컬럼: facility_id, beneficiary_id, care_type, date
- care_type → 6대 급여유형 목록 매칭 (허용 목록 외 거부)
- date → 과거 3개월 이내만 허용 (미래 계획은 허용)
- 중복 → idempotency_key 충돌 시 해당 행 SKIP (ON CONFLICT DO NOTHING)

### 1.4 데이터 수집 타이밍 매트릭스

| 데이터 소스 | 수집 빈도 | 수집 방식 | 시간 해상도 |
|-----------|----------|----------|-----------|
| 계획 (Plan) | 주 1회 or 수시 | 엑셀 업로드 / 수동 입력 | 분 단위 (nullable) |
| 기록 (Record) | 실시간 | 모바일 앱 → POST /ingest | 초 단위 (서버 타임스탬프) |
| 청구 (Billing) | 월 1회 | 엑셀 업로드 | 일 단위 |

---

## Phase 2: Access Ledger + 메타 감사 체계

> **목적:** 내부 조작 탐지용 조회 감사 로그 + I/O 격리 + 감시자의 감시
> **해결:** CQRS Write-on-Read 역전, Blind Spot 1 (메타 감사 부재)

### 2.1 신규 테이블: `access_ledger`

```
access_ledger (INSERT-ONLY, 별도 스키마 'audit' 에 배치)
├─ id: UUID PRIMARY KEY
├─ actor_id: VARCHAR(100) NOT NULL     -- 조회자 ID
├─ actor_role: VARCHAR(50) NOT NULL    -- 'admin' | 'auditor' | 'system'
├─ action: VARCHAR(30) NOT NULL        -- 'REQUESTED' | 'GRANTED' | 'DENIED' | 'DOWNLOADED'
├─ resource_type: VARCHAR(50) NOT NULL -- 'evidence' | 'transcript' | 'audio' | 'export_batch'
├─ resource_id: UUID NOT NULL          -- 대상 리소스 ID (ledger_id 등)
├─ access_scope: VARCHAR(100)          -- 'single' | 'batch' | 'facility_wide'
├─ ip_address: VARCHAR(45)             -- IPv4/IPv6
├─ user_agent: VARCHAR(500)
├─ access_context: JSONB               -- 추가 맥락 (검색 조건, 필터 등)
├─ created_at: TIMESTAMPTZ DEFAULT NOW()
└─ Indexes:
   idx_access_actor: (actor_id, created_at DESC)
   idx_access_resource: (resource_id, created_at DESC)
   idx_access_anomaly: (actor_id, action, created_at)  -- 이상 패턴 탐지용
```

**스키마 분리 전략:**
```sql
CREATE SCHEMA IF NOT EXISTS audit;
-- access_ledger는 audit 스키마에 생성
-- 별도 테이블스페이스 지정 가능 (DBA 판단)
-- 기존 public 스키마의 I/O와 물리적 분리
```

### 2.2 2-Phase 토큰 발급 (Write-on-Read 해결)

핵심 문제: 조회 API에서 access_ledger INSERT가 동기적으로 발생하면 읽기 성능 저하.

**해결 구조: Synchronous-INSERT + Asynchronous-Commit**

```
┌──────────────┐    ┌─────────────────────────────────────────────┐
│ 관리자        │    │ FastAPI Middleware: AccessAuditMiddleware   │
│ GET /audit   │───▶│                                             │
│              │    │  1. access_ledger INSERT (REQUESTED)        │
│              │    │     → synchronous_commit = local (off)      │
│              │    │     → WAL flush 대기 없음 (μs 단위)          │
│              │    │                                             │
│              │    │  2. access_token UUID 생성 (메모리)          │
│              │    │                                             │
│              │    │  3. 실제 쿼리 실행 + 결과 반환               │
│              │    │                                             │
│              │    │  4. access_ledger INSERT (GRANTED)          │
│              │    │     → synchronous_commit = local (off)      │
│              │    │                                             │
│              │    │  5. 응답 + access_token 헤더 포함            │
│              │    └─────────────────────────────────────────────┘
└──────────────┘

PostgreSQL 설정 (access_ledger 전용 세션):
  SET LOCAL synchronous_commit = 'local';
  -- WAL은 로컬 디스크에 쓰지만, 복제 대기 없음
  -- 크래시 시 최대 ~수백ms 분량 유실 가능 (허용 범위)
  -- 일반 evidence_ledger는 기본 'on' 유지 (무손실)
```

**왜 완전 비동기가 아닌 `local`인가:**
- `off`: 크래시 시 데이터 유실 → 감사 로그로서 부적격
- `local`: 로컬 WAL 보장, 복제만 비동기 → 감사 무결성 유지 + 성능 확보
- `on`: 복제까지 대기 → 불필요한 지연

### 2.3 다운로드 토큰 격리

증거 원본(오디오, 트랜스크립트) 다운로드 시 별도 토큰 발급.

```
신규 테이블: access_token (TTL 관리용, 일반 테이블 — Append-Only 불필요)
├─ token: UUID PRIMARY KEY
├─ access_ledger_id: UUID FK → access_ledger(id)
├─ resource_id: UUID NOT NULL
├─ resource_type: VARCHAR(50) NOT NULL
├─ expires_at: TIMESTAMPTZ NOT NULL    -- 발급 후 5분
├─ consumed_at: TIMESTAMPTZ            -- 사용 시 기록
├─ is_consumed: BOOLEAN DEFAULT FALSE
```

```
다운로드 플로우:
  1. GET /api/v2/evidence/{id}/download-token
     → access_ledger INSERT (REQUESTED)
     → access_token INSERT (TTL 5분)
     → 토큰 UUID 반환

  2. GET /api/v2/evidence/download/{token}
     → access_token 유효성 검증 (만료? 사용됨?)
     → access_ledger INSERT (DOWNLOADED)
     → access_token.is_consumed = TRUE
     → 실제 파일 스트리밍
```

### 2.4 메타 감사: pgaudit 통합 (Blind Spot 1 해결)

**문제:** 악의적 관리자가 psql로 DB 직접 접속하여 access_ledger를 SELECT하면
어플리케이션 레이어 감사가 우회된다.

**해결:**

```sql
-- PostgreSQL 서버 설정 (postgresql.conf)
shared_preload_libraries = 'pgaudit'

-- access_ledger 테이블에 대한 모든 SELECT를 DB 레벨에서 기록
ALTER TABLE audit.access_ledger ENABLE ROW LEVEL SECURITY;

-- pgaudit 로그 설정
-- audit 스키마의 모든 테이블에 대한 READ를 기록
ALTER ROLE voice_guard_reader SET pgaudit.log = 'read';
ALTER ROLE voice_guard_ingestor SET pgaudit.log = 'read';

-- pgaudit 로그 → PostgreSQL log → 외부 전송
-- 방법 1: CloudWatch Logs (AWS RDS)
-- 방법 2: pgaudit.log_destination = 'syslog' → Fluentd → SIEM
```

**감시 계층 구조:**
```
Layer 1: 어플리케이션 감사 → access_ledger (FastAPI 미들웨어)
Layer 2: DB 감사            → pgaudit 로그 (PostgreSQL 엔진)
Layer 3: 인프라 감사         → CloudWatch / SIEM (AWS 레벨)

각 레이어는 하위 레이어를 감시하며, 최상위(Layer 3)는
어플리케이션 코드로 조작 불가능한 인프라 영역에 존재한다.
```

### 2.5 이상 조회 탐지 쿼리 (배치)

```sql
-- 야간 대량 조회 탐지 (매일 06:00 실행)
SELECT actor_id, COUNT(*) AS access_count,
       MIN(created_at) AS first_access,
       MAX(created_at) AS last_access
FROM audit.access_ledger
WHERE created_at >= CURRENT_DATE - INTERVAL '1 day'
  AND EXTRACT(HOUR FROM created_at AT TIME ZONE 'Asia/Seoul')
      NOT BETWEEN 8 AND 18  -- 업무 외 시간
  AND action = 'GRANTED'
GROUP BY actor_id
HAVING COUNT(*) > 20  -- 임계값: 업무 외 20건 초과
ORDER BY access_count DESC;

-- 단일 수급자 집중 조회 탐지
SELECT actor_id, resource_id, COUNT(*) AS repeat_count
FROM audit.access_ledger
WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
  AND action = 'GRANTED'
GROUP BY actor_id, resource_id
HAVING COUNT(*) > 10  -- 같은 건을 10회 이상 조회
ORDER BY repeat_count DESC;
```

---

## Phase 3: 요양 도메인 특화 3각 검증 엔진

> **목적:** 계획-기록-청구 3각 매칭으로 누락/중복/과잉 청구 이상 징후 탐지
> **해결:** 정규화 결함 5건 전체, Blind Spot 2 (타이밍), Blind Spot 3 (격리 수준)

### 3.1 Canonical Service Fact (정규화 모델)

3종 데이터를 통일하되, **각 소스의 시간 해상도 차이를 명시적으로 보존.**

```
canonical_service_fact (VIEW, 물리 테이블 아님 — 검증 시점에 동적 생성)
├─ fact_id: UUID                       -- 원본 레코드 ID
├─ source_type: ENUM                   -- 'PLAN' | 'RECORD' | 'BILLING'
├─ facility_id: VARCHAR(255)
├─ beneficiary_id: VARCHAR(255)
├─ caregiver_id: VARCHAR(255)          -- PLAN/RECORD만 존재, BILLING은 NULL
├─ care_type: VARCHAR(100)
├─ fact_date: DATE                     -- 모든 소스 공통 (일 단위)
├─ fact_start_time: TIME               -- PLAN/RECORD만 (nullable)
├─ fact_end_time: TIME                 -- PLAN/RECORD만 (nullable)
├─ fact_duration_minutes: INTEGER      -- 모든 소스 (RECORD는 계산)
├─ time_resolution: ENUM               -- 'MINUTE' | 'DAY_ONLY'
│                                      -- ↑ 핵심: 해상도 차이 명시적 보존
├─ amount_krw: INTEGER                 -- BILLING만 존재
├─ source_created_at: TIMESTAMPTZ      -- 원본 생성 시각
```

**시간 해상도 명시가 해결하는 것 (정규화 결함 1):**
- PLAN vs RECORD: `time_resolution = 'MINUTE'` → 분 단위 겹침/오차 계산 가능
- PLAN vs BILLING: `time_resolution` 혼합 → **일 단위로만 매칭** (자동 해상도 하향)
- RECORD vs BILLING: `time_resolution` 혼합 → **일 단위로만 매칭**

```
매칭 해상도 자동 결정 규칙:
  IF both.time_resolution == 'MINUTE' → 분 단위 매칭
  ELSE → 일 단위 매칭 (fact_date 기준)
```

### 3.2 Overlap Matrix (급여유형 간 합법적 겹침 규칙)

> 정규화 결함 4 해결

```
overlap_permission_matrix (설정 테이블 — 관리자 수정 가능)
├─ care_type_a: VARCHAR(100)
├─ care_type_b: VARCHAR(100)
├─ overlap_allowed: BOOLEAN
├─ max_overlap_minutes: INTEGER        -- 허용 겹침 상한
├─ note: TEXT                          -- 허용/거부 사유
├─ PRIMARY KEY (care_type_a, care_type_b)
```

**초기 데이터 (요양 현장 기준):**

```
 care_type_a  │ care_type_b  │ overlap │ max_min │ 사유
──────────────┼──────────────┼─────────┼─────────┼──────────────────
 이동 보조     │ 목욕 보조    │  TRUE   │   30    │ 욕실 이동 포함
 이동 보조     │ 식사 보조    │  TRUE   │   15    │ 식당 이동 포함
 이동 보조     │ 배변 보조    │  TRUE   │   15    │ 화장실 이동 포함
 이동 보조     │ 체위 변경    │  TRUE   │   10    │ 자세 변경 시 이동
 이동 보조     │ 구강 위생    │  TRUE   │   10    │ 세면대 이동 포함
 식사 보조     │ 구강 위생    │  TRUE   │   15    │ 식후 구강 케어
 식사 보조     │ 식사 보조    │  FALSE  │    0    │ 동일 유형 중복 불가
 목욕 보조     │ 목욕 보조    │  FALSE  │    0    │ 동일 유형 중복 불가
 배변 보조     │ 체위 변경    │  TRUE   │   10    │ 배변 후 체위 변경
 (그 외 동일 유형 조합)       │  FALSE  │    0    │ 중복 청구 방지
```

**매칭 엔진의 겹침 판정 로직:**
```python
# 의사코드 (pseudo-code)
def is_overlap_anomaly(fact_a, fact_b):
    """두 기록의 시간이 겹칠 때, 이상인지 정상인지 판정"""
    if fact_a.time_resolution != 'MINUTE' or fact_b.time_resolution != 'MINUTE':
        return None  # 시간 해상도 부족 → 겹침 판정 불가 → SKIP

    overlap_minutes = calculate_overlap(fact_a, fact_b)
    if overlap_minutes <= 0:
        return False  # 겹치지 않음

    rule = overlap_matrix.get(fact_a.care_type, fact_b.care_type)
    if rule is None:
        return True   # 규칙 미정의 = 보수적으로 이상 판정

    if not rule.overlap_allowed:
        return True   # 겹침 자체가 금지된 조합

    if overlap_minutes > rule.max_overlap_minutes:
        return True   # 허용 범위 초과

    return False      # 합법적 겹침
```

### 3.3 비율 기반 차등 Tolerance (급여유형별)

> 정규화 결함 5 해결

```
tolerance_config (설정 테이블)
├─ care_type: VARCHAR(100) PRIMARY KEY
├─ absolute_tolerance_min: INTEGER     -- 절대 허용 오차 (분)
├─ relative_tolerance_pct: DECIMAL     -- 상대 허용 오차 (%)
├─ effective_tolerance: GENERATED AS   -- MAX(absolute, plan_duration * relative)
├─ min_duration_minutes: INTEGER       -- 최소 인정 시간
├─ max_duration_minutes: INTEGER       -- 최대 인정 시간
├─ updated_at: TIMESTAMPTZ
├─ updated_by: VARCHAR(100)
```

**초기 설정값:**

```
 care_type   │ abs_tol │ rel_tol │ min_dur │ max_dur │ 적용 예시
─────────────┼─────────┼─────────┼─────────┼─────────┼────────────────
 식사 보조    │  10분   │  20%    │  20분   │  90분   │ 60분 계획 → 48~72분 허용
 배변 보조    │  10분   │  30%    │  10분   │  60분   │ 30분 계획 → 21~39분 허용
 체위 변경    │   5분   │  25%    │   5분   │  30분   │ 15분 계획 → 11~19분 허용
 구강 위생    │   5분   │  25%    │   5분   │  30분   │ 15분 계획 → 11~19분 허용
 목욕 보조    │  15분   │  15%    │  30분   │ 180분   │ 120분 계획 → 102~138분 허용
 이동 보조    │   5분   │  30%    │   5분   │  60분   │ 20분 계획 → 14~26분 허용
```

**Tolerance 계산 공식:**
```
effective_tolerance = MAX(absolute_tolerance_min, plan_duration * relative_tolerance_pct)
허용 범위 = [plan_duration - effective_tolerance, plan_duration + effective_tolerance]
허용 범위 CLAMP to [min_duration_minutes, max_duration_minutes]
```

### 3.4 맥락 기반 룰 엔진 (대체 수행 + 무계획 돌봄)

> 정규화 결함 2, 3 해결

**룰 체계: 3단계 판정**

```
┌──────────────────────────────────────────────────────────┐
│                    Rule Engine Pipeline                   │
│                                                          │
│  Input: Canonical Service Fact 쌍 (Plan↔Record 등)        │
│                                                          │
│  ┌────────────────────────────────────────────┐          │
│  │ Stage 1: Exact Match                       │          │
│  │ (수급자 + 일자 + 급여유형 + 수행자 일치)      │          │
│  │ → MATCH → 정상                              │          │
│  │ → FAIL  → Stage 2                          │          │
│  └────────────────────────────────────────────┘          │
│                                                          │
│  ┌────────────────────────────────────────────┐          │
│  │ Stage 2: Substitution Match                │          │
│  │ (수급자 + 일자 + 급여유형 일치, 수행자 다름) │          │
│  │ → IF 동일 시설 내 수행자 → SUBSTITUTION      │          │
│  │   (대체 수행 — 정상, 주석 부가)              │          │
│  │ → IF 타 시설 수행자    → ANOMALY             │          │
│  │   (교차 청구 의심)                           │          │
│  │ → FAIL (수행자 모두 불일치) → Stage 3        │          │
│  └────────────────────────────────────────────┘          │
│                                                          │
│  ┌────────────────────────────────────────────┐          │
│  │ Stage 3: Contextual Match                  │          │
│  │ (계획 없는 기록, 또는 기록 없는 계획)         │          │
│  │                                            │          │
│  │ Case A: 기록 있음, 계획 없음                 │          │
│  │   → IF 야간(22:00~06:00) → UNPLANNED_NIGHT │          │
│  │     (야간 긴급 돌봄 — INFO, Anomaly 아님)    │          │
│  │   → IF 공휴일           → UNPLANNED_HOLIDAY │          │
│  │     (공휴일 돌봄 — INFO, Anomaly 아님)       │          │
│  │   → IF 주간 정규 시간   → UNPLANNED_REGULAR │          │
│  │     (무계획 주간 돌봄 — WARNING, 확인 필요)   │          │
│  │                                            │          │
│  │ Case B: 계획 있음, 기록 없음                 │          │
│  │   → MISSING_RECORD                         │          │
│  │     (미수행 — CRITICAL, 환수 위험)           │          │
│  │                                            │          │
│  │ Case C: 청구 있음, 기록+계획 없음            │          │
│  │   → PHANTOM_CLAIM                          │          │
│  │     (허위 청구 의심 — CRITICAL)              │          │
│  └────────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────────┘
```

### 3.5 Reconciliation 결과 저장

```
reconciliation_run (검증 실행 메타 — Append-Only)
├─ id: UUID PRIMARY KEY
├─ facility_id: VARCHAR(255) NOT NULL
├─ run_type: VARCHAR(20) NOT NULL      -- 'PLAN_VS_RECORD' | 'FULL_TRIANGULAR'
├─ run_scope_start: DATE NOT NULL
├─ run_scope_end: DATE NOT NULL
├─ snapshot_isolation: VARCHAR(30)     -- 'REPEATABLE_READ' (강제)
├─ total_facts_checked: INTEGER
├─ anomalies_found: INTEGER
├─ run_duration_ms: INTEGER
├─ triggered_by: VARCHAR(100)          -- 'SCHEDULER' | 'MANUAL' | actor_id
├─ created_at: TIMESTAMPTZ DEFAULT NOW()

reconciliation_anomaly (이상 징후 원장 — Append-Only)
├─ id: UUID PRIMARY KEY
├─ run_id: UUID FK → reconciliation_run(id)
├─ anomaly_type: VARCHAR(50) NOT NULL
│   -- 'MISSING_RECORD' | 'PHANTOM_CLAIM' | 'TIME_MISMATCH' |
│   -- 'DURATION_EXCEEDED' | 'OVERLAP_VIOLATION' | 'SUBSTITUTION' |
│   -- 'UNPLANNED_REGULAR'
├─ severity: VARCHAR(10) NOT NULL      -- 'INFO' | 'WARNING' | 'CRITICAL'
├─ facility_id: VARCHAR(255)
├─ beneficiary_id: VARCHAR(255)
├─ care_type: VARCHAR(100)
├─ fact_date: DATE
├─ plan_fact_id: UUID                  -- 관련 계획 레코드 (nullable)
├─ record_fact_id: UUID                -- 관련 기록 레코드 (nullable)
├─ billing_fact_id: UUID               -- 관련 청구 레코드 (nullable)
├─ detail: JSONB NOT NULL              -- 구체적 수치 (오차 분, 겹침 분 등)
├─ is_resolved: BOOLEAN DEFAULT FALSE
├─ resolved_at: TIMESTAMPTZ
├─ resolved_by: VARCHAR(100)
├─ resolution_note: TEXT
├─ created_at: TIMESTAMPTZ DEFAULT NOW()
└─ Indexes:
   idx_anomaly_severity: (severity, is_resolved, created_at DESC)
   idx_anomaly_beneficiary: (beneficiary_id, fact_date)
   idx_anomaly_run: (run_id)
```

### 3.6 트랜잭션 격리 수준 강제 (Blind Spot 3 해결)

```python
# Reconciliation 쿼리 실행 시 격리 수준 강제
def run_reconciliation(facility_id: str, start_date: date, end_date: date):
    """
    REPEATABLE READ 격리 수준에서 스냅샷 일관성 보장.
    쿼리 실행 중 새로운 INSERT가 발생해도 스냅샷 시점의 데이터만 참조.
    """
    with engine.begin() as conn:
        # 세션 격리 수준 강제
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

        # 스냅샷 시점 기록
        snapshot_ts = conn.execute(text("SELECT NOW()")).scalar()

        # 1. Plan 데이터 수집 (최신 버전만)
        plans = conn.execute(text("""
            SELECT DISTINCT ON (facility_id, beneficiary_id, plan_date, care_type)
                   *
            FROM care_plan_schedule
            WHERE facility_id = :fid
              AND plan_date BETWEEN :start AND :end
            ORDER BY facility_id, beneficiary_id, plan_date, care_type,
                     plan_version DESC
        """), {...}).fetchall()

        # 2. Record 데이터 수집
        records = conn.execute(text("""
            SELECT * FROM evidence_ledger
            WHERE facility_id = :fid
              AND recorded_at::date BETWEEN :start AND :end
        """), {...}).fetchall()

        # 3. Billing 데이터 수집 (해당 월)
        claims = conn.execute(text("""
            SELECT * FROM billing_claim
            WHERE facility_id = :fid
              AND claim_date BETWEEN :start AND :end
        """), {...}).fetchall()

        # 4. Canonical Fact 변환 + 룰 엔진 실행
        # (모두 같은 트랜잭션 내 — 스냅샷 일관)

        # 5. 결과 INSERT (같은 트랜잭션)
        # reconciliation_run + reconciliation_anomaly INSERT
```

### 3.7 Reconciliation 실행 타이밍 (Blind Spot 2 해결)

```
┌─────────────────────────────────────────────────────────────┐
│ 검증 타이밍 매트릭스 (2각/3각 분리)                           │
├────────────────────┬──────────────┬──────────────────────────┤
│ 검증 유형           │ 실행 주기     │ 데이터 소스              │
├────────────────────┼──────────────┼──────────────────────────┤
│ Plan vs Record     │ 매일 23:00   │ care_plan + evidence     │
│ (2각 준실시간)      │ (전일 데이터)  │ (분 단위 매칭)           │
├────────────────────┼──────────────┼──────────────────────────┤
│ Full Triangular    │ 월 1회       │ plan + evidence + billing│
│ (3각 월말 배치)     │ (매월 5일)    │ (일 단위 매칭)           │
├────────────────────┼──────────────┼──────────────────────────┤
│ Manual Trigger     │ 수시         │ 관리자가 범위 지정        │
│ (수동 실행)         │              │                          │
└────────────────────┴──────────────┴──────────────────────────┘

API:
  POST /api/v2/reconciliation/run     -- 수동 실행 (facility_id, start, end)
  GET  /api/v2/reconciliation/runs    -- 실행 이력 조회
  GET  /api/v2/reconciliation/anomalies -- 이상 징후 조회 (severity 필터)
  PATCH /api/v2/reconciliation/anomalies/{id}/resolve -- 이상 징후 해소
```

---

## Phase 4: 통합 워커 + 이벤트 라우터

> **목적:** Outbox 증식 방지, 단일 진입점으로 모든 비동기 작업 관리
> **해결:** Blind Spot 5 (운영 복잡도)

### 4.1 현재 문제: 워커 파편화

```
현재 상태 (각각 독립 프로세스):
  redis_worker.py   → voice:ingest 스트림 소비
  worker.py         → outbox_events 폴링 (레거시)
  notion_sync.py    → notion_sync_outbox 폴링

Phase 2-3 추가 시 예상:
  access_worker.py  → access 이상 탐지 배치
  recon_worker.py   → reconciliation 배치

= 5개 이상의 독립 워커 = 모니터링 지옥
```

### 4.2 해결: Unified Worker + Event Router

```
┌──────────────────────────────────────────────────────────────┐
│              unified_worker.py (단일 프로세스)                 │
│                                                              │
│  ┌────────────────────────────────────────────────────┐      │
│  │              Event Router (라우팅 테이블)            │      │
│  │                                                    │      │
│  │  event_type          →  Handler Function           │      │
│  │  ─────────────────────────────────────────         │      │
│  │  'ingest'            →  handle_ingest()            │      │
│  │  'notion_sync'       →  handle_notion_sync()       │      │
│  │  'cmd:*'             →  handle_command()           │      │
│  │  'recon:daily'       →  handle_daily_recon()       │      │
│  │  'recon:monthly'     →  handle_monthly_recon()     │      │
│  │  'access:anomaly'    →  handle_access_anomaly()    │      │
│  └────────────────┬───────────────────────────────────┘      │
│                   │                                          │
│  ┌────────────────▼───────────────────────────────────┐      │
│  │           Unified Outbox Table (신규)               │      │
│  │                                                    │      │
│  │  모든 비동기 작업을 단일 테이블에서 관리              │      │
│  │  event_type 컬럼으로 라우팅                         │      │
│  │  기존 outbox_events 구조 유지 + event_type 추가     │      │
│  └────────────────────────────────────────────────────┘      │
│                                                              │
│  ┌────────────────────────────────────────────────────┐      │
│  │           Health Monitor (내장)                     │      │
│  │                                                    │      │
│  │  - 핸들러별 처리 건수, 평균 처리 시간, 에러율        │      │
│  │  - Redis PUBLISH → SSE로 대시보드 실시간 표시       │      │
│  │  - DLQ 건수 임계치 초과 시 NT-2 알림톡 발송         │      │
│  └────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 Unified Outbox 테이블

기존 `outbox_events`, `notion_sync_outbox`, `command_outbox`를 단일화하지는 않는다.
(기존 테이블 스키마 변경은 위험하고, 트리거 의존성이 있다.)

대신, **새로운 이벤트(Reconciliation, Access Anomaly 등)만** `unified_outbox`에 적재:

```
unified_outbox (신규 — 기존 outbox들과 병존)
├─ id: UUID PRIMARY KEY
├─ event_type: VARCHAR(50) NOT NULL    -- 라우팅 키
├─ source_table: VARCHAR(50)           -- 원본 테이블 (nullable)
├─ source_id: UUID                     -- 원본 레코드 ID
├─ status: VARCHAR(20) DEFAULT 'pending'
│   CHECK (status IN ('pending','processing','done','dlq'))
├─ attempts: INTEGER DEFAULT 0
├─ max_attempts: INTEGER DEFAULT 5
├─ payload: JSONB NOT NULL
├─ priority: INTEGER DEFAULT 0        -- 높을수록 우선 처리
├─ created_at: TIMESTAMPTZ DEFAULT NOW()
├─ processed_at: TIMESTAMPTZ
├─ next_retry_at: TIMESTAMPTZ
├─ error_message: TEXT
├─ worker_id: VARCHAR(100)             -- 처리 중인 워커 식별
└─ Indexes:
   idx_unified_pending: (status, priority DESC, created_at)
     WHERE status IN ('pending', 'processing')
   idx_unified_type: (event_type, status)
```

### 4.4 통합 워커 소비 루프

```
unified_worker.py 메인 루프:

  LOOP:
    ┌─ [1] Redis XREADGROUP (기존 voice:ingest 스트림) ──────────┐
    │  → handle_ingest() 실행                                     │
    │  → 기존 outbox_events 상태 관리 (호환성 유지)                │
    └─────────────────────────────────────────────────────────────┘

    ┌─ [2] 기존 Outbox 폴링 (하위 호환) ─────────────────────────┐
    │  SELECT FROM outbox_events WHERE status='pending' LIMIT 10  │
    │  SELECT FROM notion_sync_outbox WHERE status='pending'      │
    │  SELECT FROM command_outbox WHERE status='pending'           │
    │  → 각각 해당 핸들러로 라우팅                                  │
    └─────────────────────────────────────────────────────────────┘

    ┌─ [3] Unified Outbox 폴링 (신규 이벤트) ────────────────────┐
    │  SELECT FROM unified_outbox                                  │
    │  WHERE status='pending'                                      │
    │  ORDER BY priority DESC, created_at                          │
    │  LIMIT 10                                                    │
    │  FOR UPDATE SKIP LOCKED  ← 다중 워커 인스턴스 안전           │
    │  → event_type으로 핸들러 라우팅                               │
    └─────────────────────────────────────────────────────────────┘

    ┌─ [4] XAUTOCLAIM (좀비 메시지 복구) ────────────────────────┐
    │  30초 이상 미승인 메시지 자동 재수령                          │
    └─────────────────────────────────────────────────────────────┘

    ┌─ [5] Health Metrics PUBLISH ───────────────────────────────┐
    │  매 60초: 핸들러별 처리 통계 → Redis PUBLISH → SSE           │
    └─────────────────────────────────────────────────────────────┘
```

### 4.5 핸들러 레지스트리 패턴

```python
# 의사코드 (pseudo-code)
HANDLER_REGISTRY = {
    "ingest":          handle_ingest,        # B2 + Whisper + Hash
    "notion_sync":     handle_notion_sync,   # Notion API
    "cmd:field_check": handle_command,       # 알림톡 발송
    "cmd:freeze":      handle_command,
    "cmd:escalate":    handle_command,
    "recon:daily":     handle_daily_recon,   # Plan vs Record
    "recon:monthly":   handle_monthly_recon, # Full 3각 검증
    "access:anomaly":  handle_access_check,  # 이상 조회 탐지
}

async def route_event(event_type: str, payload: dict):
    handler = HANDLER_REGISTRY.get(event_type)
    if handler is None:
        # 와일드카드 매칭 (cmd:* 등)
        prefix = event_type.split(":")[0] + ":*"
        handler = HANDLER_REGISTRY.get(prefix)
    if handler is None:
        logger.error(f"Unknown event_type: {event_type}")
        return False
    return await handler(payload)
```

### 4.6 워커 헬스 대시보드 SSE 이벤트

```json
{
  "event": "worker_health",
  "data": {
    "worker_id": "unified-worker-12345",
    "uptime_seconds": 86400,
    "handlers": {
      "ingest":      {"processed": 1523, "failed": 3, "avg_ms": 4200},
      "notion_sync": {"processed": 1520, "failed": 0, "avg_ms": 890},
      "cmd:*":       {"processed": 45,   "failed": 1, "avg_ms": 1200},
      "recon:daily": {"processed": 1,    "failed": 0, "avg_ms": 15000}
    },
    "queues": {
      "outbox_events":     {"pending": 2, "dlq": 3},
      "notion_sync_outbox":{"pending": 0, "dlq": 0},
      "command_outbox":    {"pending": 1, "dlq": 0},
      "unified_outbox":    {"pending": 0, "dlq": 0}
    },
    "redis_stream_lag": 0,
    "last_heartbeat": "2026-04-08T15:30:00Z"
  }
}
```

---

## 개발 순서 및 의존성

```
Phase 1 (Week 1-2): 데이터 수집 파이프라인
  ├─ care_plan_schedule 테이블 + 트리거
  ├─ billing_claim 테이블 + 트리거
  ├─ POST /api/v2/plan/upload (openpyxl)
  ├─ POST /api/v2/plan/entry
  ├─ POST /api/v2/billing/upload
  ├─ POST /api/v2/billing/entry
  ├─ GET  /api/v2/plan/schedule
  └─ GET  /api/v2/billing/claims
      │
      ▼
Phase 2 (Week 3-4): Access Ledger
  ├─ audit 스키마 생성
  ├─ access_ledger 테이블 + 트리거
  ├─ access_token 테이블
  ├─ AccessAuditMiddleware (synchronous_commit=local)
  ├─ GET  /api/v2/evidence/{id}/download-token
  ├─ GET  /api/v2/evidence/download/{token}
  ├─ pgaudit 설정 (DBA 협조 필요)
  └─ 이상 조회 탐지 쿼리 (배치)
      │
      ▼
Phase 3 (Week 5-7): Reconciliation Engine
  ├─ overlap_permission_matrix 테이블 + 초기 데이터
  ├─ tolerance_config 테이블 + 초기 데이터
  ├─ reconciliation_run 테이블 + 트리거
  ├─ reconciliation_anomaly 테이블 + 트리거
  ├─ Canonical Service Fact 변환 로직
  ├─ 3-Stage Rule Engine (Exact → Substitution → Contextual)
  ├─ REPEATABLE READ 격리 강제
  ├─ POST /api/v2/reconciliation/run
  ├─ GET  /api/v2/reconciliation/anomalies
  └─ PATCH /api/v2/reconciliation/anomalies/{id}/resolve
      │
      ▼
Phase 4 (Week 8): Unified Worker
  ├─ unified_outbox 테이블
  ├─ unified_worker.py (Event Router + Handler Registry)
  ├─ 기존 워커 코드를 핸들러 함수로 리팩터링
  ├─ Health Monitor + SSE 이벤트
  ├─ 기존 redis_worker.py / worker.py 흡수 (단일 프로세스화)
  └─ 대시보드 워커 헬스 패널 추가
```

---

## 수정 대상 기존 파일 목록

| 파일 | 수정 내용 | Phase |
|------|----------|-------|
| `backend/main.py` | 신규 라우터 마운트, AccessAuditMiddleware 추가 | 1, 2 |
| `init_db.py` | 신규 테이블 DDL + 트리거 추가 | 1, 2, 3, 4 |
| `redis_worker.py` | 핸들러 함수 추출 → unified_worker에서 import | 4 |
| `backend/worker.py` | 핸들러 함수 추출 → unified_worker에서 import | 4 |
| `backend/notion_sync.py` | 핸들러 함수 추출 → unified_worker에서 import | 4 |
| `dashboard/lib/api.ts` | 신규 API 클라이언트 함수 추가 | 1, 3 |
| `dashboard/app/hooks/useVoiceGuardSSE.ts` | worker_health 이벤트 구독 추가 | 4 |

## 신규 파일 목록

| 파일 | 역할 | Phase |
|------|------|-------|
| `schema_v9_plan_billing.sql` | Phase 1 DDL | 1 |
| `schema_v10_access_ledger.sql` | Phase 2 DDL | 2 |
| `schema_v11_reconciliation.sql` | Phase 3 DDL | 3 |
| `schema_v12_unified_outbox.sql` | Phase 4 DDL | 4 |
| `backend/plan_billing.py` | Plan/Billing API 라우터 | 1 |
| `backend/access_audit.py` | Access Ledger 미들웨어 + API | 2 |
| `backend/reconciliation.py` | 3각 검증 엔진 (Rule Engine 포함) | 3 |
| `backend/unified_worker.py` | 통합 워커 + Event Router | 4 |

---

## 검증 계획 (각 Phase 완료 시)

### Phase 1 검증
- [ ] 엑셀 업로드 → care_plan_schedule INSERT 확인 (중복 시 SKIP)
- [ ] 엑셀 업로드 → billing_claim INSERT 확인
- [ ] 유효하지 않은 care_type 거부 확인
- [ ] UPDATE/DELETE 트리거 차단 확인
- [ ] 이전 Phase 기능 회귀 없음 확인

### Phase 2 검증
- [ ] GET /audit 호출 시 access_ledger에 REQUESTED+GRANTED 2건 INSERT 확인
- [ ] 다운로드 토큰 발급 → 5분 후 만료 확인
- [ ] synchronous_commit=local 설정 확인 (access_ledger 세션만)
- [ ] evidence_ledger는 기본 synchronous_commit=on 유지 확인
- [ ] pgaudit 로그에 SELECT 기록 확인

### Phase 3 검증
- [ ] Plan vs Record: 정상 매칭 → 이상 없음 확인
- [ ] Plan vs Record: 대체 수행 → SUBSTITUTION (INFO) 판정 확인
- [ ] Plan vs Record: 야간 무계획 → UNPLANNED_NIGHT (INFO) 판정 확인
- [ ] Plan vs Record: 주간 무계획 → UNPLANNED_REGULAR (WARNING) 판정 확인
- [ ] Plan vs Record: 미수행 → MISSING_RECORD (CRITICAL) 판정 확인
- [ ] Overlap Matrix: 이동+목욕 겹침 허용, 식사+식사 겹침 거부 확인
- [ ] Tolerance: 식사 60분 계획, 72분 기록 → 허용 범위 내 확인
- [ ] Tolerance: 식사 60분 계획, 95분 기록 → 범위 초과 ANOMALY 확인
- [ ] REPEATABLE READ: 검증 중 INSERT 발생해도 결과 일관성 확인

### Phase 4 검증
- [ ] unified_worker 단일 프로세스로 모든 이벤트 처리 확인
- [ ] 기존 ingest → outbox → done → notion_sync 파이프라인 정상 확인
- [ ] worker_health SSE 이벤트 수신 확인
- [ ] 핸들러 에러 시 retry + DLQ 이관 확인
- [ ] FOR UPDATE SKIP LOCKED: 다중 워커 인스턴스 간 중복 처리 없음 확인
