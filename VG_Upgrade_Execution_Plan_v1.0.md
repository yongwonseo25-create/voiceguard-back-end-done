# VG_Upgrade_Execution_Plan v1.0
> 발행자: 최상위 검수자(Evaluator Agent)  
> 수신자: 실행 에이전트(Generator Agent)  
> 발행일: 2026-04-19 | 상태: 실행 대기(READY TO EXECUTE)  
> 참조: VG_CareOps_System_Architecture_v1.0.md §8~9 보안 스캔 결과

---

## ⚠️ 실행 에이전트 필독 — 절대 제약 (위반 시 즉시 중단)

```
1. 기존 evidence_ledger / care_record_ledger 스키마 수정 절대 금지
2. FRONT END/ 폴더 코드 1바이트도 수정 금지
3. 기존 엔드포인트 URI 변경 금지 (/api/v2/ingest 등)
4. 신규 기능과 클린업을 한 커밋에 섞지 마라
5. 자기 평가(self-evaluation) 금지 — 테스트 통과로만 완료 확인
6. 구현 완료 후 반드시 Evaluator에게 검수 요청 제출
```

---

## UPGRADE-01: Firebase App Check 백엔드 인증 게이트 (H-01 해소)

### 배경 및 목적

**현재 취약점**: `/api/v2/ingest`, `/api/v5/handover/*` 등 모든 엔드포인트가 인증 없이 노출.  
`ALLOWED_ORIGINS` CORS 설정만 존재 — cURL/Postman으로 즉시 우회 가능.  
**목표**: 정상적인 Voice Guard 모바일 앱 클라이언트만 API 호출 가능하도록 차단.

### 구현 스펙

#### 1-A. 환경변수 추가 (`.env` + Secret Manager)

```
FIREBASE_PROJECT_ID=voice-guard-pilot          # Firebase 프로젝트 ID
APP_CHECK_BYPASS_TOKEN=<테스트용 시크릿>         # 개발/테스트 환경 우회 토큰
API_GATEWAY_KEY=<SHA-256 랜덤 32바이트 hex>     # 대체 API Key 방식
```

#### 1-B. `backend/app_check_middleware.py` 신규 파일 (유일한 신규 파일)

```python
"""
Voice Guard — Firebase App Check 검증 미들웨어
모든 /api/* 경로 요청의 X-Firebase-AppCheck 헤더를 검증한다.
검증 실패 시 401 반환. /health 경로는 제외.
"""
import os
import logging
import httpx
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("voice_guard.app_check")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
APP_CHECK_BYPASS_TOKEN = os.getenv("APP_CHECK_BYPASS_TOKEN", "")
APP_CHECK_VERIFY_URL = (
    "https://firebaseappcheck.googleapis.com/v1/projects/"
    f"{FIREBASE_PROJECT_ID}/apps/{{app_id}}:verifyAppCheckToken"
)

SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class AppCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 인증 제외 경로
        if request.url.path in SKIP_PATHS:
            return await call_next(request)

        # /api/* 경로만 검증
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        token = request.headers.get("X-Firebase-AppCheck", "")

        # 개발 환경 우회 (APP_CHECK_BYPASS_TOKEN 일치 시 통과)
        if APP_CHECK_BYPASS_TOKEN and token == APP_CHECK_BYPASS_TOKEN:
            logger.info("[APP-CHECK] 개발 우회 토큰 통과")
            return await call_next(request)

        if not token:
            raise HTTPException(status_code=401, detail="X-Firebase-AppCheck 헤더 누락")

        # Firebase App Check 토큰 원격 검증
        verified = await _verify_token(token)
        if not verified:
            raise HTTPException(status_code=401, detail="App Check 토큰 검증 실패")

        return await call_next(request)


async def _verify_token(token: str) -> bool:
    """Firebase App Check REST API로 토큰 검증."""
    if not FIREBASE_PROJECT_ID:
        logger.warning("[APP-CHECK] FIREBASE_PROJECT_ID 미설정 — 검증 스킵")
        return True  # 미설정 환경(로컬)에서는 통과

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"https://firebaseappcheck.googleapis.com/v1beta/projects/"
                f"{FIREBASE_PROJECT_ID}:verifyAppCheckToken",
                json={"app_check_token": token},
            )
            if resp.status_code == 200:
                data = resp.json()
                # alreadyConsumed=True면 재사용 토큰 → 거부
                if data.get("alreadyConsumed", False):
                    logger.warning("[APP-CHECK] 재사용 토큰 거부")
                    return False
                return True
            logger.warning(f"[APP-CHECK] 검증 실패 HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"[APP-CHECK] 검증 오류: {e}")
        return False  # 외부 API 장애 시 거부 (fail-closed 원칙)
```

#### 1-C. `ingest_api.py` 수정 — 미들웨어 등록 (3줄 추가, 기존 코드 무수정)

```python
# ingest_api.py 상단 import 블록에 추가
from app_check_middleware import AppCheckMiddleware

# app = FastAPI(...) 선언 직후 추가
app.add_middleware(AppCheckMiddleware)
```

#### 1-D. `env_guard.py` 수정 — APP_CHECK 필수 변수 추가

```python
# check_env_vars() 내 "app_check" 그룹 추가
_REQUIRED_VARS = {
    ...기존 그룹 유지...,
    "app_check": ["FIREBASE_PROJECT_ID", "APP_CHECK_BYPASS_TOKEN"],
}
```

### 코딩 제약

- `_verify_token()`에서 외부 API 장애 시 **반드시 `return False`** (fail-closed). `return True` 폴백 절대 금지.
- `SKIP_PATHS`에 `/api/*` 경로 추가 금지 (인증 우회 구멍 생성).
- `APP_CHECK_BYPASS_TOKEN`은 절대 하드코딩 금지 — 환경변수 전용.
- 기존 CORS 미들웨어 순서 변경 금지 (`AppCheckMiddleware`는 CORS 뒤에 등록).

### UPGRADE-01 채점 루브릭 (Evaluator 기준)

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| 미들웨어 등록 정확성 | 20점 | `/api/*` 전체 경로 적용, `/health` 제외 확인 |
| fail-closed 구현 | 25점 | `_verify_token()` 예외 시 `False` 반환, `True` 시 즉시 실격 |
| 재사용 토큰 거부 | 15점 | `alreadyConsumed=True` 거부 로직 존재 |
| 우회 토큰 환경변수 | 15점 | `APP_CHECK_BYPASS_TOKEN` 하드코딩 없음, `.env`에만 존재 |
| 기존 엔드포인트 무수정 | 15점 | `ingest_api.py` 3줄 추가 외 변경 없음 |
| 테스트: 토큰 없이 401 반환 | 10점 | `curl -X POST /api/v2/ingest` → 401 확인 |
| **합격 커트라인** | **85점** | 미달 시 재구현 |

---

## UPGRADE-02: CI/CD 파이프라인 + 자동 보안 스캔

### 배경 및 목적

**현재 상태**: `backend/cloudbuild.yaml` 존재하나 Docker 빌드 + Cloud Run 배포만 포함.  
보안 취약점 스캔, 의존성 CVE 체크, 테스트 자동화 없음.  
**목표**: 매 커밋마다 보안 → 테스트 → 빌드 → 배포 자동화 파이프라인.

### 구현 스펙

#### 2-A. `backend/cloudbuild.yaml` 수정 (기존 파일 확장)

```yaml
# backend/cloudbuild.yaml — 기존 steps 앞에 삽입
steps:

# ── Step 0: 의존성 설치 ──────────────────────────────────────────
- name: 'python:3.11-slim'
  id: 'install-deps'
  entrypoint: pip
  args: ['install', '-r', 'requirements-api.txt', 'bandit', 'safety', 'pytest', 'pytest-asyncio']

# ── Step 1: Bandit 보안 스캔 (HIGH 심각도 발견 시 빌드 중단) ─────
- name: 'python:3.11-slim'
  id: 'security-scan'
  waitFor: ['install-deps']
  entrypoint: bandit
  args:
    - '-r'
    - '.'
    - '-ll'           # LOW 이상 리포트
    - '--severity-level'
    - 'high'          # HIGH 발견 시 exit code 1 → 빌드 중단
    - '--exclude'
    - './.venv,./node_modules,./evaluator_audit'

# ── Step 2: Safety 의존성 CVE 체크 ──────────────────────────────
- name: 'python:3.11-slim'
  id: 'cve-check'
  waitFor: ['install-deps']
  entrypoint: safety
  args: ['check', '--full-report']

# ── Step 3: 전체 테스트 스위트 실행 ─────────────────────────────
- name: 'python:3.11-slim'
  id: 'test-suite'
  waitFor: ['security-scan', 'cve-check']
  entrypoint: pytest
  args:
    - 'backend/test_care_record_e2e.py'
    - 'backend/test_ai_pipeline.py'
    - '-x'            # 첫 번째 실패 시 즉시 중단
    - '-v'
  env:
    - 'DATABASE_URL=$$DATABASE_URL_TEST'
    - 'GEMINI_API_KEY=$$GEMINI_API_KEY'
  secretEnv: ['DATABASE_URL_TEST', 'GEMINI_API_KEY']

# ── Step 4: Docker 빌드 (기존 유지) ─────────────────────────────
- name: 'gcr.io/cloud-builders/docker'
  id: 'docker-build'
  waitFor: ['test-suite']
  args: ['build', '-t', 'gcr.io/$PROJECT_ID/voice-guard-api:$COMMIT_SHA', '.']

# ── Step 5: Cloud Run 배포 (기존 유지) ──────────────────────────
- name: 'gcr.io/cloud-builders/gcloud'
  id: 'deploy'
  waitFor: ['docker-build']
  args:
    - 'run'
    - 'deploy'
    - 'voice-guard-api'
    - '--image=gcr.io/$PROJECT_ID/voice-guard-api:$COMMIT_SHA'
    - '--region=asia-northeast3'
    - '--platform=managed'

availableSecrets:
  secretManager:
    - versionName: projects/$PROJECT_ID/secrets/DATABASE_URL_TEST/versions/latest
      env: 'DATABASE_URL_TEST'
    - versionName: projects/$PROJECT_ID/secrets/GEMINI_API_KEY/versions/latest
      env: 'GEMINI_API_KEY'
```

#### 2-B. `backend/Dockerfile` 수정 — 보안 강화 (최소 변경)

```dockerfile
# 기존 FROM 라인 아래에 추가 (USER 권한 강화)
RUN addgroup --system vguard && adduser --system --group vguard
USER vguard
```

#### 2-C. `.github/workflows/pr_check.yaml` 신규 (GitHub Actions 병행)

```yaml
name: PR Security Gate
on:
  pull_request:
    branches: [main]

jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - run: pip install bandit safety
      - run: bandit -r backend/ -ll --severity-level high
      - run: safety check
```

### 코딩 제약

- Step 순서 변경 금지: `security-scan → cve-check → test-suite → docker-build → deploy`
- Bandit `--severity-level high` 제거 금지 (LOW만 스캔하도록 약화 금지)
- `waitFor` 의존성 체인 반드시 유지 (병렬 실행으로 보안 단계 우회 불가)
- 테스트 DB는 운영 DB와 **절대 분리** — `DATABASE_URL_TEST` 별도 Secret 사용

### UPGRADE-02 채점 루브릭

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| Bandit HIGH 차단 | 25점 | bandit HIGH 발견 시 exit 1, 빌드 중단 확인 |
| Safety CVE 체크 | 20점 | safety check 스텝 존재 + 실패 시 빌드 중단 |
| 테스트→빌드 순서 보장 | 20점 | waitFor 체인 test-suite → docker-build 확인 |
| 운영/테스트 DB 분리 | 20점 | DATABASE_URL_TEST Secret 별도 사용 |
| Dockerfile USER 강화 | 15점 | root가 아닌 vguard 유저로 실행 확인 |
| **합격 커트라인** | **80점** | 미달 시 재구현 |

---

## UPGRADE-03: DLQ 자동 재처리 워커 + 관리자 알림

### 배경 및 목적

**현재 상태**: `dead_letter_queue` 테이블에 이관 후 자동 재처리 없음.  
Notion API 장기 장애 시 레코드 소실 가능성. 관리자 인지 불가.  
**목표**: DLQ 항목 주기적 재시도 + 알림팝으로 관리자 즉시 인지.

### 구현 스펙

#### 3-A. `backend/dlq_recovery_worker.py` 신규 파일

```python
"""
Voice Guard — DLQ 자동 재처리 워커
dead_letter_queue 항목을 주기적으로 재시도하거나 관리자 알림을 발행한다.

실행: python dlq_recovery_worker.py (별도 Cloud Run Job으로 배포)
스케줄: Cloud Scheduler → 매 30분마다 트리거
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from notifier import send_alimtalk, ADMIN_PHONE

load_dotenv()
logger = logging.getLogger("voice_guard.dlq_recovery")

DATABASE_URL  = os.getenv("DATABASE_URL")
MAX_DLQ_RETRY = int(os.getenv("DLQ_MAX_RETRY", "3"))   # DLQ 재시도 한도

engine = create_engine(DATABASE_URL, pool_size=3, max_overflow=5,
    pool_pre_ping=True) if DATABASE_URL else None


async def run_recovery():
    """DLQ 전체 재처리 루프 — 1회 실행 후 종료 (Cloud Scheduler 호출 방식)."""
    if not engine:
        logger.error("[DLQ-RECOVERY] DB 미연결 — 종료")
        return

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, ledger_id, outbox_id, failure_reason,
                   original_payload, retry_count, detected_at
            FROM dead_letter_queue
            WHERE retry_count < :max_retry
              AND (alerted_at IS NULL
                   OR alerted_at < NOW() - INTERVAL '30 minutes')
            ORDER BY detected_at ASC
            LIMIT 50
        """), {"max_retry": MAX_DLQ_RETRY}).fetchall()

    logger.info(f"[DLQ-RECOVERY] 재처리 대상: {len(rows)}건")

    for row in rows:
        await _process_dlq_item(dict(row._mapping))


async def _process_dlq_item(item: dict):
    """DLQ 단건 재처리 — 성공 시 resolved 표시, 실패 시 알림."""
    dlq_id    = item["id"]
    ledger_id = item["ledger_id"]
    retry_count = item.get("retry_count", 0)

    logger.info(f"[DLQ-RECOVERY] 재처리 시도 dlq_id={dlq_id} retry={retry_count}")

    # 재처리 시도 (notion_pipeline 재실행)
    try:
        # outbox 상태를 pending으로 복원 → 기존 워커가 재처리
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE outbox_events
                SET status='pending', attempts=0, error_message=NULL
                WHERE id = :outbox_id
            """), {"outbox_id": item["outbox_id"]})

            conn.execute(text("""
                UPDATE dead_letter_queue
                SET retry_count = retry_count + 1,
                    alerted_at  = NOW()
                WHERE id = :id
            """), {"id": dlq_id})

        logger.info(f"[DLQ-RECOVERY] outbox 복원 완료 ledger_id={ledger_id}")

        # 관리자 알림 (재처리 시도 사실 통보)
        await send_alimtalk(
            ADMIN_PHONE,
            f"[VoiceGuard DLQ] 재처리 시도 #{retry_count + 1}\n"
            f"ledger_id: {ledger_id[:12]}...\n"
            f"원인: {item['failure_reason'][:80]}"
        )

    except Exception as e:
        logger.error(f"[DLQ-RECOVERY] 재처리 실패: {e}")

        # 재시도 한도 초과 → 최종 실패 알림
        if retry_count + 1 >= MAX_DLQ_RETRY:
            await send_alimtalk(
                ADMIN_PHONE,
                f"[VoiceGuard DLQ 최종실패] 수동 개입 필요\n"
                f"ledger_id: {ledger_id[:12]}...\n"
                f"원인: {item['failure_reason'][:80]}"
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    asyncio.run(run_recovery())
```

#### 3-B. Cloud Scheduler 설정 (gcloud 명령, 실행 에이전트는 명령만 출력)

```bash
gcloud scheduler jobs create http dlq-recovery-job \
  --schedule="*/30 * * * *" \
  --uri="https://voice-guard-api-<hash>-an.a.run.app/internal/dlq-recovery" \
  --message-body="{}" \
  --oidc-service-account-email="voice-guard-sa@voice-guard-pilot.iam.gserviceaccount.com" \
  --location=asia-northeast3
```

#### 3-C. `ingest_api.py` — `/internal/dlq-recovery` 엔드포인트 추가

```python
@app.post("/internal/dlq-recovery", tags=["내부"], include_in_schema=False)
async def trigger_dlq_recovery(request: Request):
    """Cloud Scheduler 전용 — OIDC 인증 필수. Swagger UI 미노출."""
    # OIDC 토큰 검증은 Cloud Run 인프라가 처리
    from dlq_recovery_worker import run_recovery
    asyncio.create_task(run_recovery())
    return {"accepted": True, "message": "DLQ 재처리 시작"}
```

### 코딩 제약

- `UPDATE outbox_events SET status='pending'` — **outbox만 복원**. `evidence_ledger`/`care_record_ledger` 절대 수정 금지.
- `MAX_DLQ_RETRY` 하드코딩 금지 — 환경변수 전용.
- `/internal/dlq-recovery`는 `include_in_schema=False` 반드시 유지 (외부 Swagger 노출 금지).
- `send_alimtalk()` 실패 시 전체 처리 중단 금지 — 알림 실패는 로그만 기록 후 계속.

### UPGRADE-03 채점 루브릭

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| outbox 복원 로직 | 25점 | `status='pending'` 복원, ledger 무수정 확인 |
| 재시도 한도 환경변수 | 15점 | `DLQ_MAX_RETRY` 하드코딩 없음 |
| 최종 실패 알림 | 20점 | retry_count >= MAX 시 alimtalk 발행 확인 |
| 내부 엔드포인트 은닉 | 15점 | `include_in_schema=False`, Swagger 미노출 |
| 알림 실패 무중단 | 15점 | send_alimtalk 예외 시 try/except로 계속 진행 |
| Cloud Scheduler 연동 | 10점 | gcloud 명령 정확성 (30분 간격, OIDC) |
| **합격 커트라인** | **80점** | 미달 시 재구현 |

---

## UPGRADE-04: Redis 공유 캐시 (In-Memory → Cluster Cache)

### 배경 및 목적

**현재 상태**: `notion_pipeline.py`의 `_page_id_cache`가 프로세스 로컬 딕셔너리.  
Cloud Run 인스턴스 2개 이상 시 캐시 미스 → Notion API 중복 호출 → Rate Limit 위험.  
**목표**: Redis TTL 캐시로 교체 → 모든 인스턴스가 캐시 공유.

### 구현 스펙

#### 4-A. `backend/notion_pipeline.py` 수정 — 캐시 레이어 교체

```python
# 기존 코드 (삭제 대상)
# _page_id_cache: dict[str, tuple[str, float]] = {}
# _CACHE_TTL_SEC  = 3600.0

# 신규 코드 (교체)
import redis.asyncio as aioredis

_CACHE_TTL_SEC = 3600
_REDIS_CACHE_PREFIX = "vg:notion_cache:"

async def _cache_get(key: str, redis_client: aioredis.Redis) -> Optional[str]:
    """Redis TTL 캐시 조회 — MISS 시 None 반환."""
    try:
        val = await redis_client.get(f"{_REDIS_CACHE_PREFIX}{key}")
        return val if val else None
    except Exception as e:
        logger.warning(f"[CACHE] Redis 조회 실패 (캐시 MISS로 처리): {e}")
        return None  # Redis 장애 시 API 호출로 폴백

async def _cache_set(key: str, page_id: str, redis_client: aioredis.Redis) -> None:
    """Redis TTL 캐시 저장 — 실패는 경고만 (파이프라인 블로킹 금지)."""
    try:
        await redis_client.setex(f"{_REDIS_CACHE_PREFIX}{key}", _CACHE_TTL_SEC, page_id)
    except Exception as e:
        logger.warning(f"[CACHE] Redis 저장 실패 (무시): {e}")
```

#### 4-B. `lookup_page_id()` 시그니처 수정

```python
# 기존: async def lookup_page_id(client, db_id, prop_name, prop_val)
# 수정: redis_client 파라미터 추가

async def lookup_page_id(
    client:       httpx.AsyncClient,
    db_id:        str,
    prop_name:    str,
    prop_val:     str,
    redis_client: Optional[aioredis.Redis] = None,   # ← 신규 파라미터
) -> Optional[str]:
    cache_key = f"{db_id}::{prop_name}::{prop_val}"

    # Redis 캐시 우선 조회
    if redis_client:
        cached = await _cache_get(cache_key, redis_client)
        if cached:
            logger.debug(f"[CACHE] HIT — val={prop_val!r}")
            return cached

    # 캐시 MISS → Notion API 호출 (기존 로직 유지)
    ...기존 API 호출 로직 그대로...

    # 결과 캐시 저장
    if page_id and redis_client:
        await _cache_set(cache_key, page_id, redis_client)

    return page_id or None
```

#### 4-C. `run_pipeline()` — Redis 클라이언트 주입

```python
async def run_pipeline(
    gemini_care_json: dict,
    facility_id:      str,
    beneficiary_id:   str,
    caregiver_id:     str,
    care_record_id:   str,
    raw_voice_text:   str = "",
    recorded_at:      Optional[str] = None,
    redis_url:        Optional[str] = None,   # ← 신규 파라미터
) -> dict:
    ...
    redis_client = None
    if redis_url:
        try:
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            logger.warning(f"[PIPELINE] Redis 연결 실패 (캐시 없이 진행): {e}")

    async with httpx.AsyncClient() as client:
        # lookup_page_id에 redis_client 전달
        ...

    if redis_client:
        await redis_client.aclose()
```

#### 4-D. `worker.py` — `run_pipeline()` 호출 시 `redis_url` 전달

```python
# worker.py의 run_pipeline() 호출부에 추가
result = await run_pipeline(
    ...기존 파라미터...,
    redis_url=REDIS_URL,   # ← 1줄 추가
)
```

### 코딩 제약

- Redis 장애 시 `_cache_get()` → `None` 반환 (Notion API 호출로 폴백). **예외 전파 절대 금지**.
- `_REDIS_CACHE_PREFIX = "vg:notion_cache:"` 반드시 사용 (다른 Redis 키와 충돌 방지).
- 기존 `_page_id_cache` 딕셔너리 완전 삭제 — 혼용 금지.
- `lookup_staff()` 함수도 동일하게 redis_client 파라미터 추가 적용.

### UPGRADE-04 채점 루브릭

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| In-Memory dict 완전 제거 | 20점 | `_page_id_cache` 딕셔너리 코드베이스에 없음 |
| Redis 장애 시 폴백 | 25점 | `_cache_get()` 예외 → None 반환, 파이프라인 계속 |
| 키 프리픽스 사용 | 15점 | `vg:notion_cache:` 프리픽스 모든 키에 적용 |
| `lookup_staff()` 동일 적용 | 20점 | staff 조회도 Redis 캐시 사용 |
| 클라이언트 aclose() | 10점 | `run_pipeline()` 종료 시 반드시 aclose() 호출 |
| 캐시 HIT 로그 | 10점 | DEBUG 레벨 캐시 HIT 로그 존재 |
| **합격 커트라인** | **80점** | 미달 시 재구현 |

---

## 통합 실행 순서 (Generator 필독)

```
[STEP 1] UPGRADE-01 (App Check 미들웨어) 구현 → 로컬 테스트 → Evaluator 검수 요청
[STEP 2] UPGRADE-04 (Redis 캐시) 구현 → 기존 테스트 재실행 → Evaluator 검수 요청
[STEP 3] UPGRADE-03 (DLQ 워커) 구현 → Evaluator 검수 요청
[STEP 4] UPGRADE-02 (CI/CD) 구현 → cloudbuild.yaml 수정 → 전체 파이프라인 실행 확인
```

**단계 건너뜀 금지** — 이전 UPGRADE 검수 합격 전 다음 단계 착수 금지.

---

## 전체 합격 기준 (Done Contract)

```
✅ UPGRADE-01: curl -X POST /api/v2/ingest 헤더 없이 → 401 반환
✅ UPGRADE-02: cloudbuild.yaml 커밋 시 Bandit HIGH 발견 → 빌드 중단 확인
✅ UPGRADE-03: DLQ 항목 존재 시 30분 내 alimtalk 수신 확인
✅ UPGRADE-04: Cloud Run 2인스턴스 기동 후 Notion API 호출 70% 이상 감소 로그 확인
✅ 기존 E2E 테스트 (test_care_record_e2e.py) 8/8 통과
✅ evidence_ledger / care_record_ledger 스키마 무변경 확인
```

**모든 항목 통과 시에만 "구현 완료"로 선언 가능.**

---

*설계도 종료 — Evaluator Agent 서명: 2026-04-19*  
*이 문서는 실행 에이전트(Generator)의 구현 바이블이다. 해석의 여지 없이 따른다.*
