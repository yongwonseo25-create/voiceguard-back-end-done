# VG_Evidence_Certificate_Blueprint.md
## Voice Guard 증거 검증서 자동 발급 시스템 — 최상위 어드바이저 마스터 설계도

**발간자:** 최상위 어드바이저(Evaluator) — Claude Sonnet 4.6  
**발간일:** 2026-04-22  
**목적:** 국민건강보험공단 현지조사 시 WORM 원장 무결성을 즉시 입증할 PDF + JSON 검증서 자동 발급  
**하급 에이전트 사용 제한:** 본 설계도의 모든 통제 규약을 위반한 코드는 평가자가 REJECT한다.

---

## 1. The "Why" — 법적 골든타임 수호 아키텍처

### 1.1 방어 시나리오
국민건강보험공단 조사관이 현장에 도착했을 때:
- **T+0분:** 조사관이 특정 수급자/날짜의 기록 원본 제출 요구
- **T+5분 이내:** 시스템이 PDF 검증서 자동 출력 → 조사관에게 제시
- **T+10분 이내:** JSON 검증서로 기계 검증 수행 → 조작 불가 입증

### 1.2 기술적 필요성
현재 상태 (Phase 8 완료 기준):
- `evidence_seal_event` 테이블에 `chain_hash` + `audio_sha256` + `transcript_sha256` 저장됨
- WORM B2 ObjectLockMode=COMPLIANCE 검증됨
- **누락:** 인간이 읽을 수 있는 시각적 증명서 발급 로직 없음

---

## 2. 통제 1 (System-Level Architecture) — 병렬 이중 발급 설계

### 2.1 트리거 지점 (절대 변경 불가)

```
[worker.py: process() 함수]
  evidence_seal_event INSERT 성공
       ↓ (같은 DB 트랜잭션 안에서)
  certificate_issue() 호출
  ├── [Thread A] PDF 렌더링 → B2 업로드 → certificate_ledger INSERT
  └── [Thread B] JSON 생성 → 스키마 검증 → certificate_ledger INSERT
       ↓ (A, B 둘 다 성공해야만)
  트랜잭션 COMMIT
       ↓ (실패 시)
  전체 ROLLBACK → outbox_events 상태 'pending' 복원
```

### 2.2 신규 테이블: `certificate_ledger` (Append-Only, WORM 봉인)

```sql
-- schema_v16_certificate_ledger.sql (Generator가 작성해야 할 파일)
CREATE TABLE certificate_ledger (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id        UUID        NOT NULL REFERENCES evidence_ledger(id),
    seal_event_id    UUID        NOT NULL REFERENCES evidence_seal_event(id),
    cert_type        VARCHAR(4)  NOT NULL CHECK (cert_type IN ('PDF', 'JSON')),
    cert_hash        CHAR(64)    NOT NULL,         -- SHA-256 of certificate content
    storage_key      TEXT        NOT NULL,         -- B2 object key
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issuer_version   VARCHAR(20) NOT NULL,         -- 'vg-cert-v1.0.0'
    worm_retain_until TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_cert_per_seal UNIQUE (seal_event_id, cert_type)
);

-- Append-Only 봉인 트리거 (UPDATE/DELETE 원천 차단)
CREATE RULE no_update_certificate_ledger AS
    ON UPDATE TO certificate_ledger DO INSTEAD NOTHING;
CREATE RULE no_delete_certificate_ledger AS
    ON DELETE TO certificate_ledger DO INSTEAD NOTHING;
```

### 2.3 PDF 검증서 필수 포함 요소 (인간 가독성)

```
┌─────────────────────────────────────────────────────┐
│  [Voice Guard 법적 증거 검증서]           [QR Code]  │
│  발급일시: 2026-04-22 14:30:15 KST                   │
│  발급번호: cert-{uuid8}                              │
├─────────────────────────────────────────────────────┤
│  수급자 ID:    {beneficiary_id}                      │
│  시설 ID:      {facility_id}                         │
│  서비스 유형:  {care_type}                           │
│  기록 시각:    {ingested_at} (UTC+9)                 │
├─────────────────────────────────────────────────────┤
│  WORM 저장소 상태                                    │
│  ● 잠금 모드: COMPLIANCE ✅                          │
│  ● 보존 기한: {worm_retain_until}                    │
│  ● 객체 키:   {worm_object_key}                      │
├─────────────────────────────────────────────────────┤
│  무결성 해시체인                                     │
│  음성 SHA-256:     {audio_sha256[:16]}...            │
│  전사 SHA-256:     {transcript_sha256[:16]}...       │
│  체인 해시(HMAC):  {chain_hash[:16]}...              │
├─────────────────────────────────────────────────────┤
│  AI 전사 내용 (요약)                                 │
│  {transcript_text[:500]}...                          │
├─────────────────────────────────────────────────────┤
│  검증 방법: JSON 검증서의 chain_hash를               │
│  HMAC-SHA256(SECRET_KEY, SHA256(payload))로 재계산   │
│  일치 시 원본 보장 / 불일치 시 조작 의심             │
│                                                      │
│  [법원 제출용] 본 문서는 WORM 불변 저장소에         │
│  봉인된 원본 음성 데이터의 법적 증거 검증서입니다.  │
└─────────────────────────────────────────────────────┘
```

### 2.4 JSON 검증서 스키마 (기계 검증용)

```json
{
  "$schema": "https://voiceguard.kr/schemas/cert/v1.0.0",
  "cert_id": "uuid-v4",
  "cert_type": "EVIDENCE_CERTIFICATE",
  "issuer": "VoiceGuard-WORM-System",
  "issuer_version": "vg-cert-v1.0.0",
  "issued_at": "2026-04-22T05:30:15.000Z",
  "subject": {
    "ledger_id": "uuid",
    "seal_event_id": "uuid",
    "facility_id": "string",
    "beneficiary_id": "string",
    "care_type": "string",
    "ingested_at": "ISO8601"
  },
  "worm": {
    "bucket": "voice-guard-korea",
    "object_key": "evidence/2026/04/22/{ledger_id}.wav",
    "lock_mode": "COMPLIANCE",
    "retain_until": "2031-04-22T05:30:15.000Z"
  },
  "integrity": {
    "audio_sha256": "hex64",
    "transcript_sha256": "hex64",
    "chain_hash": "hex64",
    "chain_algorithm": "HMAC-SHA256",
    "chain_input_fields": [
      "ledger_id", "facility_id", "beneficiary_id",
      "shift_id", "server_ts", "audio_sha256",
      "transcript_sha256", "b2_key"
    ]
  },
  "verification_instructions": {
    "step1": "Retrieve audio from worm.object_key",
    "step2": "SHA256(audio_bytes) must equal integrity.audio_sha256",
    "step3": "HMAC-SHA256(SECRET_KEY, SHA256(sorted_json_payload)) must equal integrity.chain_hash",
    "step4": "B2 head_object ObjectLockMode must equal 'COMPLIANCE'"
  },
  "cert_self_hash": "SHA256(this document excluding this field)"
}
```

### 2.5 병렬 렌더링 전략 (`asyncio.gather`)

```python
# worker.py 내 evidence_seal_event INSERT 트랜잭션 안에서:
# ThreadPoolExecutor를 사용한 병렬 렌더링
pdf_task = loop.run_in_executor(_cert_pool, render_pdf, seal_data)
json_task = loop.run_in_executor(_cert_pool, render_json, seal_data)

pdf_bytes, json_bytes = await asyncio.gather(pdf_task, json_task)
# 둘 중 하나라도 예외 발생 → gather가 예외 전파 → 트랜잭션 ROLLBACK
```

---

## 3. 통제 2 (Generator/Evaluator 분리 및 Done Contract)

### 3.1 구현 순서 — Generator 실행 단계

Generator(하급 실행 에이전트)는 반드시 아래 순서대로 구현하고,
각 단계 완료 후 Evaluator(본 설계도)의 Done Contract를 통과해야 다음 단계로 진행한다.

```
Step 1: schema_v16_certificate_ledger.sql 작성 및 적용
         → Done Contract: psql로 \d certificate_ledger 출력 제출
Step 2: backend/cert_renderer.py 작성 (PDF + JSON 렌더링 모듈)
         → Done Contract: pytest backend/test_cert_renderer.py 전체 통과
Step 3: backend/worker.py 수정 (certificate 발급 트리거 삽입)
         → Done Contract: E2E 테스트 통과 + 실제 PDF 파일 생성 확인
Step 4: .pre-commit-config.yaml 훅 등록
         → Done Contract: 의도적으로 잘못된 JSON 커밋 시도 → 훅이 차단 확인
```

### 3.2 조기 종료 차단 규약

Generator는 다음 조건 중 하나라도 미충족 시 "완료" 선언 불가:
- [ ] `certificate_ledger` 테이블에 실제 row가 INSERT됨
- [ ] PDF 파일이 B2 스토리지에 업로드됨 (key 패턴: `certs/pdf/{ledger_id}.pdf`)
- [ ] JSON 파일이 B2 스토리지에 업로드됨 (key 패턴: `certs/json/{ledger_id}.json`)
- [ ] PDF 생성 실패 시 `evidence_seal_event`도 커밋되지 않음 (롤백 확인)
- [ ] `backend/test_cert_e2e.py` 전체 PASS (Evaluator가 직접 실행)

---

## 4. 통제 3 (채점 기준 — Evaluator 4대 지표)

Evaluator는 Generator가 제출한 코드를 아래 기준으로 채점한다.
**85점 미만 → REJECT, 재구현 명령.**

### 4.1 기능성 (Functionality) — 30점

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| PDF 렌더링 성공률 | 10 | 100건 중 100건 성공 (0% 실패) |
| JSON 스키마 완전성 | 10 | jsonschema Draft-7 검증 통과 |
| 원자성 보장 | 10 | PDF 실패 시 seal_event 롤백 확인 (의도적 실패 주입 테스트) |

### 4.2 가독성 (Human Readability) — 25점

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| PDF 레이아웃 명확성 | 10 | 비개발자(원장)가 30초 이내 핵심 3가지 파악 가능 |
| 해시값 표시 방식 | 8 | 앞 16자리 표시 + 전체 해시는 접힌 섹션 또는 QR |
| 한국어 표기 일관성 | 7 | 모든 레이블 한국어, ISO8601 날짜는 KST 변환 |

### 4.3 위변조 방지 (Tamper Evidence) — 30점

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| cert_self_hash 구현 | 10 | JSON 문서 자체의 SHA256 포함 (self 제외) |
| WORM 상태 실시간 검증 | 10 | B2 head_object로 ObjectLockMode 재확인 후 PDF 발급 |
| 체인 검증 지침 삽입 | 10 | PDF에 조사관이 직접 검증하는 절차 명문화 |

### 4.4 보안성 (Security) — 15점

| 항목 | 배점 | 합격 기준 |
|------|------|-----------|
| SECRET_KEY 노출 차단 | 8 | PDF/JSON 어디에도 SECRET_KEY 미포함 |
| bandit 스캔 | 4 | High 0건, Medium 0건 |
| 인증서 저장 경로 | 3 | B2 버킷 내 `certs/` 접두사, WORM COMPLIANCE 동일 적용 |

---

## 5. 통제 4 (구조적 강제 — Hooks & 트랜잭션 원자성)

### 5.1 DB 트랜잭션 원자성 강제 (코드 레벨)

```python
# backend/worker.py — process() 함수 수정 목표 구조
# Generator는 반드시 아래 구조를 따라야 한다.

async def _issue_certificates(seal_data: dict, b2_client, engine) -> tuple[str, str]:
    """
    PDF와 JSON 검증서를 병렬 생성 후 B2 업로드.
    예외 발생 시 caller의 engine.begin() 트랜잭션 전체 ROLLBACK됨.
    """
    loop = asyncio.get_event_loop()
    pdf_bytes, json_bytes = await asyncio.gather(
        loop.run_in_executor(_cert_pool, render_pdf_certificate, seal_data),
        loop.run_in_executor(_cert_pool, render_json_certificate, seal_data),
        # 어느 하나라도 실패 → 전체 예외 전파 → ROLLBACK
    )
    # B2 업로드 (WORM COMPLIANCE 동일 적용)
    pdf_key  = f"certs/pdf/{seal_data['ledger_id']}.pdf"
    json_key = f"certs/json/{seal_data['ledger_id']}.json"
    retain   = datetime.now(timezone.utc) + timedelta(days=365 * WORM_YEARS)
    
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key=pdf_key, Body=pdf_bytes,
        ContentType="application/pdf",
        ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain)
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key=json_key, Body=json_bytes,
        ContentType="application/json",
        ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain)
    
    return pdf_key, json_key

# worker.py process() 내부 — evidence_seal_event INSERT와 동일 트랜잭션
with engine.begin() as conn:
    # 1. evidence_seal_event INSERT (기존 코드)
    conn.execute(text("INSERT INTO evidence_seal_event ..."), {...})
    
    # 2. 인증서 발급 (트랜잭션 밖에서 B2 업로드 먼저, 실패 시 except에서 캐치)
    # 참고: B2 업로드는 트랜잭션 밖이지만 실패 시 전체 except로 빠져 ROLLBACK
    pdf_key, json_key = await _issue_certificates(seal_data, b2, engine)
    
    # 3. certificate_ledger INSERT (트랜잭션 안)
    conn.execute(text("""
        INSERT INTO certificate_ledger
            (id, ledger_id, seal_event_id, cert_type, cert_hash,
             storage_key, issuer_version, worm_retain_until)
        VALUES
            (gen_random_uuid(), :lid, :seid, 'PDF', :phash, :pkey, 'vg-cert-v1.0.0', :ret),
            (gen_random_uuid(), :lid, :seid, 'JSON', :jhash, :jkey, 'vg-cert-v1.0.0', :ret)
    """), {
        "lid": ledger_id, "seid": seal_event_id,
        "phash": hashlib.sha256(pdf_bytes).hexdigest(),
        "jkey": json_key, "jhash": hashlib.sha256(json_bytes).hexdigest(),
        "pkey": pdf_key, "ret": retain
    })
    # conn.__exit__() 시 COMMIT. 예외 시 ROLLBACK.
```

### 5.2 Pre-commit 훅 강제 (JSON 스키마 검증)

```yaml
# .pre-commit-config.yaml (신규 추가)
repos:
  - repo: local
    hooks:
      - id: validate-cert-json-schema
        name: VG Certificate JSON Schema Validator
        entry: python backend/validate_cert_schema.py
        language: python
        files: ^backend/cert_renderer\.py$
        types: [python]
        
      - id: bandit-cert-module
        name: Bandit Security Scan (cert_renderer)
        entry: bandit -r backend/cert_renderer.py -c backend/.bandit
        language: system
        files: ^backend/cert_renderer\.py$
```

```python
# backend/validate_cert_schema.py (프리커밋 훅 진입점)
"""
pre-commit 훅: cert_renderer.py가 생성하는 샘플 JSON이
공식 스키마를 통과하는지 검증. 실패 시 exit(1)로 커밋 차단.
"""
import json, sys, jsonschema
from cert_renderer import render_json_certificate, SAMPLE_SEAL_DATA, CERT_JSON_SCHEMA

sample_json_bytes = render_json_certificate(SAMPLE_SEAL_DATA)
cert_doc = json.loads(sample_json_bytes)
try:
    jsonschema.validate(cert_doc, CERT_JSON_SCHEMA, 
                        format_checker=jsonschema.FormatChecker())
    print("[HOOK] ✅ JSON schema validation passed")
    sys.exit(0)
except jsonschema.ValidationError as e:
    print(f"[HOOK] ❌ JSON schema FAILED: {e.message}")
    sys.exit(1)  # 커밋 차단
```

### 5.3 Generator가 작성해야 할 신규 파일 목록

```
backend/
├── cert_renderer.py          ← 신규 (PDF + JSON 렌더링 핵심 모듈)
├── validate_cert_schema.py   ← 신규 (pre-commit 훅 진입점)
├── test_cert_renderer.py     ← 신규 (단위 테스트)
├── test_cert_e2e.py          ← 신규 (E2E 적대적 검수 테스트)
└── worker.py                 ← 수정 (_issue_certificates 삽입)

sql/
└── schema_v16_certificate_ledger.sql  ← 신규

.pre-commit-config.yaml       ← 수정 (훅 추가)
requirements.txt              ← 수정 (weasyprint or reportlab, jsonschema 추가)
```

---

## 6. cert_renderer.py 내부 설계 명세

### 6.1 모듈 구조 (Generator 작성 기준)

```python
# backend/cert_renderer.py
"""
Voice Guard 증거 검증서 렌더링 모듈 v1.0.0
- render_pdf_certificate(seal_data) -> bytes
- render_json_certificate(seal_data) -> bytes
- CERT_JSON_SCHEMA: dict (jsonschema Draft-7)
- SAMPLE_SEAL_DATA: dict (pre-commit 훅용 샘플)
"""

ISSUER_VERSION = "vg-cert-v1.0.0"

# seal_data 표준 입력 구조 (worker.py가 전달하는 형식)
# {
#   "ledger_id": str,
#   "seal_event_id": str,
#   "facility_id": str,
#   "beneficiary_id": str,
#   "care_type": str,
#   "ingested_at": str,          # ISO8601 UTC
#   "audio_sha256": str,         # hex64
#   "transcript_sha256": str,    # hex64
#   "chain_hash": str,           # hex64
#   "transcript_text": str,
#   "worm_bucket": str,
#   "worm_object_key": str,
#   "worm_retain_until": str,    # ISO8601 UTC
# }
```

### 6.2 PDF 라이브러리 선택 기준

| 라이브러리 | 장점 | 단점 | 결정 |
|-----------|------|------|------|
| WeasyPrint | HTML→PDF, 한국어 폰트 쉬움 | 설치 무거움 (GTK 의존) | **1순위** |
| ReportLab | 순수 Python, 경량 | HTML 템플릿 없음, 코드 복잡 | 대안 |
| Jinja2 + WeasyPrint | HTML 템플릿으로 가독성 최상 | WeasyPrint 의존 | **채택** |

**채택: Jinja2 HTML 템플릿 → WeasyPrint PDF 변환**  
한국어 폰트: NanumGothic (Google Fonts CDN 또는 로컬 번들)

### 6.3 KST 변환 유틸 (한국어 가독성 필수)

```python
from datetime import timezone, timedelta

KST = timezone(timedelta(hours=9))

def to_kst_str(iso_utc: str) -> str:
    """ISO8601 UTC → 'YYYY-MM-DD HH:MM:SS KST' 변환"""
    from datetime import datetime
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
```

---

## 7. 테스트 하네스 명세 (Evaluator가 직접 실행)

### 7.1 test_cert_renderer.py 필수 케이스

```python
# Generator는 반드시 아래 8개 케이스를 모두 구현해야 한다.

def test_pdf_bytes_not_empty():
    """PDF 렌더링 결과가 1KB 이상이어야 함"""

def test_pdf_contains_chain_hash():
    """PDF 바이트에 chain_hash 앞 16자리가 포함되어야 함"""

def test_json_schema_valid():
    """render_json_certificate() 출력이 CERT_JSON_SCHEMA 통과"""

def test_json_self_hash_correct():
    """cert_self_hash 제외한 문서의 SHA256 == cert_self_hash"""

def test_json_no_secret_key_leak():
    """JSON 출력에 SECRET_KEY 환경변수 값이 포함되지 않아야 함"""

def test_pdf_no_secret_key_leak():
    """PDF 바이트에 SECRET_KEY 환경변수 값이 포함되지 않아야 함"""

def test_kst_conversion():
    """UTC ISO8601 → KST 표시 변환 정확성"""

def test_render_with_empty_transcript():
    """transcript_text가 빈 문자열이어도 PDF/JSON 렌더링 성공"""
```

### 7.2 test_cert_e2e.py 필수 케이스 (적대적 검수)

```python
def test_pdf_failure_causes_seal_rollback():
    """
    PDF 렌더링 실패 주입(monkeypatch) 시
    evidence_seal_event가 DB에 INSERT되지 않아야 한다.
    """

def test_json_failure_causes_seal_rollback():
    """JSON 렌더링 실패 주입 시 동일하게 롤백"""

def test_certificate_ledger_append_only():
    """
    certificate_ledger에 직접 UPDATE/DELETE 시도 시
    DB 트리거로 차단됨 (psycopg2 ProgrammingError)
    """

def test_cert_stored_in_b2_with_worm():
    """
    발급된 PDF/JSON의 B2 head_object ObjectLockMode == 'COMPLIANCE'
    """

def test_duplicate_seal_idempotent():
    """
    동일 ledger_id로 2회 봉인 시도 시
    ON CONFLICT로 certificate_ledger 중복 INSERT 없음
    """
```

---

## 8. 의존성 추가 명세

```
# requirements.txt 추가 항목
weasyprint>=60.0        # PDF 렌더링
Jinja2>=3.1.0           # HTML 템플릿
jsonschema>=4.21.0      # JSON 스키마 검증
pre-commit>=3.7.0       # 훅 관리
```

---

## 9. 최종 완료 판정 기준 (Evaluator 최후 게이트)

Generator가 모든 단계를 완료했다고 주장할 때, Evaluator는 다음을 직접 실행한다:

```bash
# Gate 1: 단위 테스트
pytest backend/test_cert_renderer.py -v
# 기대: 8/8 PASS

# Gate 2: E2E 적대적 테스트
pytest backend/test_cert_e2e.py -v
# 기대: 5/5 PASS

# Gate 3: 보안 스캔
bandit -r backend/cert_renderer.py -c backend/.bandit
# 기대: High 0건, Medium 0건

# Gate 4: pre-commit 훅 강제 검증
# 의도적으로 잘못된 JSON 스키마로 수정 후 커밋 시도 → 차단 확인

# Gate 5: B2 실증 확인
# 실제 봉인 이벤트 발생 후 aws s3api head-object 로 ObjectLockMode=COMPLIANCE 확인

# Gate 6: 채점
# 기능성 + 가독성 + 위변조방지 + 보안성 합산 ≥ 85점
```

**85점 미만 → REJECT. Generator에게 결함 목록 반환 후 재구현 명령.**

---

## 10. CLAUDE.md 절대 규칙 준수 선언

본 설계도는 CLAUDE.md의 모든 절대 규칙을 준수한다:
- ✅ 규칙 1: Firebase + Cloud Run + Cloud SQL 인프라 불변
- ✅ 규칙 3: 파이프라인 순서 불변 (인증서 발급은 봉인 단계 후 삽입)
- ✅ 규칙 0: `evidence_ledger` 스키마 수정 없음, `certificate_ledger`는 신규 Append-Only 테이블
- ✅ Append-Only 유지: certificate_ledger에 UPDATE/DELETE 트리거 봉인
- ✅ 이중 노동 제로: `build_chain()`은 worker.py 기존 함수 재사용
- ✅ Self-evaluation 금지: Generator 자가 판정 없음, Evaluator 게이트 필수

---

*본 설계도는 Generator가 한 치의 오차도 없이 코딩하도록 강제하는 하네스다.*  
*설계도 내용 변경은 최상위 어드바이저(Evaluator)만 권한을 가진다.*
