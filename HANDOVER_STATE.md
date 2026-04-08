# Voice Guard — Phase 1 Handover State
**Date:** 2026-04-08  
**Status:** COMPLETE — Ready for Phase 2

---

## What was built today

### DB (schema_v9_phase1.sql — applied to prod)
| Table | Columns | Key Constraint | Trigger |
|-------|---------|----------------|---------|
| `care_plan_ledger` | 14 | `plan_hash` CHAR(64) UNIQUE | Conditional UPDATE (is_superseded/superseded_by only), DELETE/TRUNCATE blocked |
| `billing_ledger` | 13 | `billing_hash` CHAR(64) UNIQUE | UPDATE/DELETE/TRUNCATE fully blocked |

### API (mounted in ingest_api.py)
| Endpoint | File | Notes |
|----------|------|-------|
| `POST /api/v2/care-plan/upload` | `care_plan_api.py` | Excel/CSV, Partial Insert |
| `POST /api/v2/care-plan/entry` | `care_plan_api.py` | Single manual entry |
| `GET  /api/v2/care-plan` | `care_plan_api.py` | Filter by facility/beneficiary/date |
| `POST /api/v2/billing/upload` | `billing_api.py` | NHIS CSV/Excel, Partial Insert |
| `GET  /api/v2/billing` | `billing_api.py` | Filter by facility/beneficiary/month |

### Verification
- `test_phase1_ledgers.py` — 9/9 PASS (adversarial: INSERT, duplicate block, UPDATE block, conditional UPDATE allow, DELETE block, API import)

---

## Phase 2 Entry Conditions

### What Phase 2 must build
**3각 검증 엔진 (Triangulation Core)**  
이제 3개 원장(evidence_ledger + care_plan_ledger + billing_ledger)이 모두 존재한다.  
Phase 2 목표: 이 3개를 조인하여 **불일치(gap)를 자동 탐지**하는 엔진 구축.

### Candidate tasks for Phase 2
1. `GET /api/v2/triangulate` — beneficiary_id + billing_month 기준으로 Plan vs Record vs Billing 3각 비교
2. Gap 탐지 룰 정의:
   - Record 없는 Plan → "미실행 계획" 알림
   - Plan 없는 Record → "무계획 실행" 알림  
   - Billing > Record duration → "과청구" 알림
3. Dashboard Panel: `PlanVsActualPanel.tsx` 연동 (이미 파일 존재, API 연결 필요)

### Files to review at Phase 2 start
- `PlanVsActualPanel.tsx` (프로젝트 루트) — 대시보드 컴포넌트, 현재 API 미연결
- `ingest_api.py` — 메인 앱 (라우터 마운트 포인트)
- `backend/main.py` — 별도 백엔드 확인 필요
- `care_plan_api.py`, `billing_api.py` — Phase 1 산출물

### Open issues / Tech debt
- `requirements.txt`: pandas 버전 pin (`>=2.2.0`) → 정확한 버전으로 고정 권장
- `care_plan_api.py` upload: `planned_start/planned_end` 컬럼 없을 때 `row.get()` KeyError 미처리 (현재 try/except로 row 단위 스킵되어 무해하나 로그 개선 여지)
- billing `claim_status` 검증: CSV 입력값이 CHECK 제약 외 값일 경우 row 에러 처리됨 (허용 값 목록을 API 레벨에서도 사전 검증하면 더 명확한 에러 메시지 제공 가능)

---

## Phase 2 Blueprint: Access Ledger + Meta Audit
**내일 즉시 코딩 시작 가능 — 설계 확정**

### 목표
운영자/시스템이 원장에 "누가/언제/무엇을 읽었는가"를 감사하는 3계층 감사 인프라.  
환수 분쟁 시 "공단이 어느 데이터를 열람했는가"를 증명 가능하게 만든다.

---

### Layer 1: I/O 격리 (Read/Write 역할 분리)

**원칙:** 원장 Write는 `voice_guard_ingestor` 전용. Read는 `voice_guard_reader` 전용.  
두 역할을 코드 레벨에서 강제 분리 → 단일 연결이 양쪽을 동시에 소유하지 못하게 막는다.

```sql
-- 신규 Read 전용 역할
CREATE ROLE voice_guard_reader NOLOGIN;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO voice_guard_reader;
-- ingestor에서 SELECT 권한 박탈 (write-only 강제)
REVOKE SELECT ON public.care_plan_ledger FROM voice_guard_ingestor;
REVOKE SELECT ON public.billing_ledger FROM voice_guard_ingestor;
-- 신규 DB 유저에 역할 바인딩
CREATE USER vg_reader_app PASSWORD '...' IN ROLE voice_guard_reader;
```

**API 레벨:** GET 엔드포인트는 `READER_DATABASE_URL` 환경변수로 별도 engine 사용.

---

### Layer 2: synchronous_commit = off (Write 성능 보호)

**문제:** Append-Only 원장의 대량 INSERT 시 WAL flush 대기로 레이턴시 급증.  
**해결:** 세션 레벨에서만 `synchronous_commit = off` 적용. 데이터 유실 위험 < 성능 이득.

```python
# ingest 엔드포인트 전용 연결에서만 적용
with engine.begin() as conn:
    conn.execute(text("SET LOCAL synchronous_commit = off"))
    conn.execute(INSERT_SQL, params)
    # commit 시 WAL flush 대기 없이 즉시 반환
    # crash 시 최대 ~200ms치 데이터 유실 가능 → 허용 범위 (B2가 원본)
```

**주의:** billing_ledger / care_plan_ledger만 적용. evidence_ledger는 기존 설정 유지.

---

### Layer 3: pgaudit 3계층 감사

**목표:** DB 레벨에서 DDL/DML/SELECT 전부 감사 로그 → 외부 조작 불가능한 증거 생성.

#### 3-1. pgaudit 설치 확인 및 활성화
```sql
-- 확인
SELECT * FROM pg_available_extensions WHERE name = 'pgaudit';
CREATE EXTENSION IF NOT EXISTS pgaudit;

-- postgresql.conf (또는 ALTER SYSTEM)
ALTER SYSTEM SET pgaudit.log = 'write, ddl, role';
ALTER SYSTEM SET pgaudit.log_relation = 'on';
SELECT pg_reload_conf();
```

#### 3-2. access_ledger 테이블 (Read 감사 원장)
```sql
CREATE TABLE IF NOT EXISTS public.access_ledger (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    accessed_by   VARCHAR(50) NOT NULL,   -- DB 유저 또는 API 역할
    access_type   VARCHAR(20) NOT NULL    -- 'READ', 'EXPORT', 'VERIFY'
                  CHECK (access_type IN ('READ','EXPORT','VERIFY','ADMIN')),
    target_table  VARCHAR(50) NOT NULL,
    target_filter JSONB,                  -- 조회에 사용된 필터 파라미터
    row_count     INTEGER,                -- 반환된 row 수
    requester_ip  INET,
    accessed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- UPDATE/DELETE/TRUNCATE 완전 차단 (care_plan과 동일한 트리거 패턴)
```

#### 3-3. API 레벨 자동 기록
```python
# GET 엔드포인트 공통 데코레이터 또는 미들웨어에서 호출
def log_access(target_table, filter_params, row_count, requester_ip, access_type="READ"):
    with reader_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.access_ledger
                (id, accessed_by, access_type, target_table, target_filter, row_count, requester_ip)
            VALUES (:id, :by, :type, :table, :filter, :count, :ip)
        """), {
            "id": str(uuid4()), "by": "api_reader",
            "type": access_type, "table": target_table,
            "filter": json.dumps(filter_params), "count": row_count, "ip": requester_ip,
        })
```

#### 3-4. pgaudit 로그 → S3/B2 자동 적재 (선택)
pgaudit 로그를 `pgaudit_shipper` 워커(배치)가 주기적으로 수집 → B2 WORM 봉인.  
이것으로 "DB 로그 자체가 삭제되었다"는 공격 시나리오도 차단.

---

### Phase 2 실행 순서 (내일 새 창 열면 즉시)

1. `HANDOVER_STATE.md` 읽기 → 맥락 복구
2. `schema_v10_phase2.sql` 생성:
   - `voice_guard_reader` 역할 + `vg_reader_app` 유저
   - `access_ledger` 테이블 + UPDATE/DELETE 트리거
   - pgaudit 활성화 SQL
3. `access_ledger_api.py` 생성:
   - `READER_DATABASE_URL` 별도 engine
   - GET 엔드포인트에 `log_access()` 공통 함수 주입
   - `synchronous_commit = off` 세션 적용
4. `test_phase2_audit.py` — 적대적 검증
5. git commit & push

---

## Invariants (절대 건드리지 말 것)
- `care_plan_ledger`, `billing_ledger` 스키마 컬럼 수정 금지
- 기존 트리거 함수명 변경 금지 (`fn_care_plan_conditional_update`, `fn_block_billing_update` 등)
- `evidence_ledger` 및 이전 스키마(v2~v8) 테이블 일절 수정 금지
