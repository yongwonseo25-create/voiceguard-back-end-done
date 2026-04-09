# Voice Guard — Phase 4 Handover State
**Date:** 2026-04-09  
**Status:** COMPLETE — Ready for Phase 5

---

## Phase 1 산출물 (변경 없음 — 불변 원칙 유지)

| Table | 트리거 |
|-------|--------|
| `care_plan_ledger` | 조건부 UPDATE (is_superseded/superseded_by만 허용) + DELETE 차단 |
| `billing_ledger`   | UPDATE/DELETE/TRUNCATE 완전 차단 |

API: `POST /api/v2/care-plan/upload`, `POST /api/v2/billing/upload` 등 (변경 없음)

---

## Phase 3 산출물 (오늘 구축)

### DB (schema_v10_phase3.sql)

| 객체 | 유형 | 역할 |
|------|------|------|
| `canonical_day_fact`    | MATERIALIZED VIEW | 3종 Day-Level 매칭 뷰 (has_plan/record/billing 비트) |
| `canonical_time_fact`   | MATERIALIZED VIEW | Plan vs Record 시간 매칭 (야간/대타 탐지 포함) |
| `overlap_rule`          | TABLE | 6x6 급여유형 겹침 허용 매트릭스 (11행 초기화) |
| `tolerance_ratio`       | TABLE | 6개 급여유형 비율 기반 허용 오차 |
| `reconciliation_result` | TABLE (Append-Only) | 검증 결과 원장 + UPDATE/DELETE 트리거 차단 |

#### MATERIALIZED VIEW 갱신 명령
```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_day_fact;
REFRESH MATERIALIZED VIEW CONCURRENTLY public.canonical_time_fact;
```
> 검증 실행 전 또는 `POST /api/v3/reconcile/refresh` 호출로 갱신.

### API (reconciliation_api.py — ingest_api.py에 마운트됨)

| Endpoint | 설명 |
|----------|------|
| `POST /api/v3/reconcile`          | 3각 검증 실행 (dry_run 지원) |
| `GET  /api/v3/reconcile/results`  | 결과 조회 (날짜/기관/상태 필터) |
| `GET  /api/v3/reconcile/summary`  | 날짜별 요약 통계 |
| `POST /api/v3/reconcile/refresh`  | Materialized View 수동 갱신 |

### Rule Engine (reconciliation_engine.py)

| Rule | 함수 | 설명 |
|------|------|------|
| Rule 1 | `_classify_triangulation()` | 3각 비트 매칭 — PHANTOM_BILLING 등 8조합 |
| Rule 2 | `_classify_unplanned()` | 야간(22~06)/긴급 → 정상, 주간 무계획 → ANOMALY |
| Rule 3 | `_check_substitution()` | 대타 담당자 + 오차 범위 내 → SUBSTITUTION |
| Rule 4a | `_check_tolerance()` | 비율 기반 오차 (tolerance_ratio 테이블 참조) |
| Rule 4b | `_check_overlap_in_batch()` | 급여유형 겹침 탐지 (overlap_rule 테이블 참조) |

**핵심 설계:**
- `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` 스냅샷 위에서 전체 실행
- `CAST(:p AS jsonb)` — SQLAlchemy text() JSONB 파라미터 충돌 방지 (Gotcha 적용)
- reconciliation_result: Append-Only — 재검증 시 새 run_at으로 새 row INSERT

### Verification
- `test_phase3_reconciliation.py` — T-01~T-08 (단위 + mock DB 통합)

---

## anomaly_code 체계

| Code | Status | 설명 |
|------|--------|------|
| `PHANTOM_BILLING`       | ANOMALY    | 기록 없이 청구 (최고 위험) |
| `UNPLANNED_BILLING`     | ANOMALY    | 무계획 실행+청구 |
| `OVER_BILLING`          | ANOMALY    | 청구 시간 > 계획+허용 오차 |
| `OVER_BILLING_ZERO_PLAN`| ANOMALY    | 계획 0분인데 청구 |
| `ILLEGAL_OVERLAP`       | ANOMALY    | 동시 청구 불허 급여유형 조합 |
| `PLANNED_NOT_EXECUTED`  | PARTIAL    | 계획만 있고 미실행·미청구 |
| `UNBILLED_CARE`         | PARTIAL    | 케어 실행했으나 미청구 |
| `UNDER_BILLING`         | PARTIAL    | 청구 시간 < 계획-허용 오차 |
| `UNPLANNED_NIGHT`       | UNPLANNED  | 야간 무계획 돌봄 (정상) |
| `UNPLANNED_EMERGENCY`   | UNPLANNED  | 긴급 무계획 돌봄 (정상) |
| `UNPLANNED_DAYTIME`     | ANOMALY    | 주간 무계획 돌봄 (이상) |
| `SUBSTITUTION`          | MATCH      | 대타 담당자 (정상 처리) |

---

## Phase 4 산출물 (오늘 구축)

### DB (schema_v11_phase4.sql)

| 객체 | 유형 | 역할 |
|------|------|------|
| `unified_outbox`          | TABLE (Append-Only) | 단일 이벤트 원장 (Event Sourcing, UPDATE/DELETE 차단) |
| `v_unified_outbox_current`| VIEW | event_id별 최신 상태 프로젝션 |
| `worker_throughput_log`   | TABLE (Append-Only) | 핸들러별 처리량 기록 |

### Worker (event_router_worker.py)

| 핸들러 | event_type | 처리 내용 |
|--------|------------|---------|
| IngestHandler  | ingest    | B2 WORM + Whisper + 해시체인 |
| NotionHandler  | notion    | Notion 동기화 |
| ReconHandler   | reconcile | Phase 3 검증 엔진 실행 |
| AlertHandler   | alert     | 카카오 알림톡 (NT-1/NT-2) |

- 단일 Redis Stream: `voice:events`
- 상태 전이: INSERT(보상 트랜잭션) — UPDATE 0
- `logging.WARNING` 기준 (성공 조용히, 실패만 시끄럽게)

### API (worker_health_api.py)

| Endpoint | 반환 |
|----------|------|
| `GET /api/v2/worker/health` | Lag, DLQ 건수, 핸들러 처리량, Redis Stream 정보 |

### Verification
- `test_phase4_router.py` — T-01~T-06 (Append-Only + 라우팅 + DLQ 경로)

---

## Phase 5 진입 조건

### Phase 5 후보 작업
1. **대시보드 연동**: `GET /api/v3/reconcile/results` + `GET /api/v2/worker/health` → 프론트 패널
2. **배치 스케줄러**: pg_cron 또는 cron으로 ReconHandler 자동 트리거 (새벽 3시)
3. **canonical_day_fact REFRESH**: 검증 실행 전 자동 REFRESH 로직 추가

### Open Issues / Tech Debt
- `overlap_rule`: 실제 NHIS 급여유형 코드로 교체 필요 (현재 영문 ENUM)
- `tolerance_ratio`: NHIS 공식 기준서 확인 후 조정 필요
- Phase 2 Access Ledger (보류됨): MVP 완료 후 필요 시 재개
- 기존 `redis_worker.py` / `backend/worker.py`: legacy — `event_router_worker.py`로 점진 이관

---

## Invariants (절대 건드리지 말 것)
- `care_plan_ledger`, `billing_ledger`, `evidence_ledger` 스키마 수정 금지
- 기존 트리거 함수명 변경 금지 (`fn_care_plan_conditional_update`, `fn_block_billing_update` 등)
- `reconciliation_result` UPDATE/DELETE 금지 — 트리거로 차단됨
