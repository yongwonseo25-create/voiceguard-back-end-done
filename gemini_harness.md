# 📝 Gemini 파이프라인 하네스 설계도 v1.0
> **Voice Guard B2B — AI 정제 레이어 Gap 분석 + 강제 규약**
> 작성 기준일: 2026-04-18 | 동적 컨텍스트 기반 실코드 스캔 결과

---

## 0. 하네스 사용 원칙 (이 문서가 코드보다 우선한다)

| 번호 | 원칙 |
|---|---|
| H-01 | Gemini 시스템 프롬프트는 이 문서의 정의를 **단 1글자도 벗어나지 않고** 구현해야 한다 |
| H-02 | JSON 스키마는 Pydantic 모델과 1:1 대응 — 필드 추가/삭제 시 양쪽 동시 변경 |
| H-03 | Generator(Gemini 호출)와 Evaluator(검수 에이전트)는 **엄격히 분리**된 실행 단위 |
| H-04 | 검수 에이전트는 4대 채점 기준 합격(≥70점) 없이 JSON을 Notion에 적재하지 않는다 |
| H-05 | 컨텍스트 포크 — 무거운 검수 작업은 독립 세션에서 처리, 결과 요약만 메인 창에 보고 |

---

## 1. ⚡ Gap 분석 — 현황(AS-IS) vs 목표(TO-BE)

### 1-1. Fork B 데이터 흐름도 (현황)

```
POST /api/v8/handover
  body.text (Whisper 날것 OR 사용자 직접 입력)
    │
    ├─ [Fork A] evidence_ledger INSERT → WORM 봉인 (변경 없음, 완벽)
    │
    └─ [Fork B] care_record_ledger INSERT (raw_voice_text = body.text 그대로)
                    │
                    └─ care_record_outbox → 워커 → call_gemini_care_record()
                                                        │
                                                        └─ notion_pipeline.run_pipeline()
                                                                │
                                                                ├─ Hot Path: VG_운영_대시보드_DB
                                                                └─ Cold Path: VG_Atomic_Care_Events
```

### 1-2. 식별된 GAP 목록 (코드 실증 스캔 결과)

| GAP-ID | 위치 | 현황(AS-IS) | 목표(TO-BE) | 심각도 |
|---|---|---|---|---|
| **G-01** | `gemini_processor.py:280-313` | 시스템 프롬프트에 오탈자 교정·의료용어 보정·요약 구조 없음 | 오탈자 교정 + 의료용어 정규화 + 3단 요약(상황/조치/특이사항) 강제 | 🔴 HIGH |
| **G-02** | `notion_pipeline.py:351-354` | Hot Path 체크박스 **3개만** 매핑 (식사·투약·배설) | ✅ **코드 구현 완료** — `체위변경`·`위생`·`정제발화`·`특이사항` 속성명 추가. ⚠️ **속성명 가정 미검증**: `NOTION_OPS_DASHBOARD_DB_ID` 미설정 상태. DB ID 입력 후 `GET /databases/{id}` 스캔으로 실제 속성명 대조 필수 — 불일치 시 400 에러로 즉시 감지됨. `VG_Atomic_Care_Events` DB의 `케어 내용` 속성명도 동일하게 검증 필요. | 🟡 MED |
| **G-03** | `main.py:2258-2264` | Fork B가 `facility_id="handover"`, `beneficiary_id="resident_user"` 하드코딩 | Notion Relation 조회 실패 원인 — 실제 수급자/보호사 ID 매핑 필요 | 🔴 HIGH |
| **G-04** | `gemini_processor.py:278` | `CARE_RECORD_SCHEMA_VERSION = "2.1"` | `special_notes.detail`의 Notion 필드 매핑 정의 없음 | 🟡 MED |
| **G-05** | `gemini_processor.py:45-94` | 인수인계 프롬프트(Pipeline A)에 3단 요약 구조 없음 | `summary_situation`, `summary_action`, `summary_notes` 3필드 강제 | 🟡 MED |
| **G-06** | `notion_pipeline.py:57-58` | `NOTION_OPS_DASHBOARD_DB_ID`, `NOTION_ATOMIC_EVENTS_DB_ID` 빈값 | 사령관 환경변수 입력 대기 — 입력 전까지 Hot/Cold Path 전체 무력화 | 🟡 MED |

---

## 2. Gemini 시스템 프롬프트 설계 (파이프라인 B: 6대 의무기록 전용)

### 2-1. 설계 원칙
1. **오탈자 교정**: 요양보호사 현장 발화의 발음 오류, 축약어 교정
2. **의료용어 보정**: 표준 요양 용어로 정규화 (매핑 테이블 내장)
3. **3단 객관 요약**: 상황(situation) → 조치(action) → 특이사항(notes)
4. **추측 금지**: 발화에 없는 내용은 절대 생성하지 않음
5. **JSON 순수 출력**: 마크다운 블록, 설명 텍스트 일체 금지

### 2-2. 의료용어 보정 매핑 테이블

| 현장 발화 (비표준) | 표준 표기 |
|---|---|
| 이상 없음, 별이상없음, 별다른 없음 | 특이소견 없음 |
| 밥 드셨어요, 식사하셨어요 | 식사 수행 |
| 약 드렸어요, 약 먹었어요 | 투약 완료 |
| 화장실 다녀오셨어요, 볼일 봤어요 | 배설 수행 |
| 자세 바꿔드렸어요, 뒤집어드렸어요 | 체위변경 수행 |
| 씻겨드렸어요, 목욕시켜드렸어요 | 위생관리 수행 |
| 넘어지실 뻔 했어요, 쓰러지려고 했어요 | 낙상 위험 발생 |
| 열 있어요, 열나는 것 같아요 | 발열 증상 관찰 |

### 2-3. 강화된 시스템 프롬프트 (완성본 — 코드 구현 필수)

```
당신은 요양보호사의 현장 발화를 6대 의무기록으로 정제하는 법적 증거 기록 시스템입니다.

[Step 1: 오탈자·발음오류 교정]
발화 원문에서 명백한 오탈자(예: '시거', '시켜' → '시켜')와 발음 오류를 먼저 교정하십시오.
교정 사항은 detail 필드에 교정된 문장으로 기재하십시오.

[Step 2: 의료용어 표준화]
아래 매핑 규칙을 적용하여 표준 요양 용어로 변환하십시오:
- "이상 없음/별이상없음" → "특이소견 없음"
- "약 드렸어요/약 먹었어요" → "투약 완료"
- "화장실/볼일" → "배설 수행"
- "자세 바꿔드렸어요/뒤집어드렸어요" → "체위변경 수행"
- "씻겨드렸어요/목욕" → "위생관리 수행"
- "열 있어요/열나는 것 같아요" → "발열 증상 관찰"
- "넘어지실 뻔" → "낙상 위험 발생"

[Step 3: 3단 객관 요약 생성]
다음 3단 구조로 detail 필드를 작성하십시오:
- 상황(Situation): 관찰된 사실 (예: "오전 9시 식사 보조")
- 조치(Action): 수행한 케어 내용 (예: "죽 200ml 전량 섭취 보조")
- 특이사항(Notes): 비정상 소견 (없으면 "특이소견 없음")

[필수 지침]
1. 반드시 아래 JSON 스키마를 100% 준수하십시오. 임의 필드 추가 금지.
2. 6대 카테고리(meal, medication, excretion, repositioning, hygiene, special_notes)는
   반드시 모두 존재해야 합니다.
   음성에서 언급이 없으면 반드시 done=false, detail=null로 채우십시오.
3. done 값은 반드시 boolean(true/false)만 사용하십시오. 문자열 "true" 금지.
4. detail은 Step 3의 3단 요약 형식으로 작성하십시오.
5. special_notes(특이사항)는 5개 표준 카테고리 외 중요 관찰/사건만 기재하십시오.

[폴백 규칙 — 절대 준수]
- 언급 없는 카테고리 → done=false, detail=null
- 수급자 ID 불명 → beneficiary_id="" (null 금지)
- 기록 일시 불명 → recorded_at="" (null 금지)

[출력 JSON 스키마 v2.2 — 3단 요약 통합]
{
  "schema_version": "2.2",
  "beneficiary_id": "string",
  "recorded_at":    "YYYY-MM-DDTHH:MM:SS",
  "corrected_transcript": "string (오탈자 교정된 전체 발화)",
  "meal":           { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } },
  "medication":     { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } },
  "excretion":      { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } },
  "repositioning":  { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } },
  "hygiene":        { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } },
  "special_notes":  { "done": true/false, "detail": { "situation": "...", "action": "...", "notes": "..." } }
}

[출력 형식]
순수 JSON만 반환하십시오. 마크다운 코드 블록(```), 설명 텍스트 일체 금지.
```

---

## 3. JSON 스키마 v2.2 — 엄격한 필드 정의

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CareRecord_v2.2",
  "type": "object",
  "required": [
    "schema_version", "beneficiary_id", "recorded_at",
    "corrected_transcript",
    "meal", "medication", "excretion",
    "repositioning", "hygiene", "special_notes"
  ],
  "additionalProperties": false,
  "properties": {
    "schema_version":         { "type": "string", "const": "2.2" },
    "beneficiary_id":         { "type": "string", "maxLength": 100 },
    "recorded_at":            { "type": "string", "pattern": "^[0-9TZ:.-]{0,30}$" },
    "corrected_transcript":   { "type": "string", "maxLength": 5000 },
    "meal":          { "$ref": "#/$defs/CareFlag" },
    "medication":    { "$ref": "#/$defs/CareFlag" },
    "excretion":     { "$ref": "#/$defs/CareFlag" },
    "repositioning": { "$ref": "#/$defs/CareFlag" },
    "hygiene":       { "$ref": "#/$defs/CareFlag" },
    "special_notes": { "$ref": "#/$defs/CareFlag_Long" }
  },
  "$defs": {
    "CareDetail": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "properties": {
        "situation": { "type": "string", "maxLength": 200 },
        "action":    { "type": "string", "maxLength": 200 },
        "notes":     { "type": "string", "maxLength": 200 }
      }
    },
    "CareFlag": {
      "type": "object",
      "required": ["done"],
      "additionalProperties": false,
      "properties": {
        "done":   { "type": "boolean" },
        "detail": { "$ref": "#/$defs/CareDetail" }
      }
    },
    "CareFlag_Long": {
      "type": "object",
      "required": ["done"],
      "additionalProperties": false,
      "properties": {
        "done":   { "type": "boolean" },
        "detail": {
          "type": ["object", "null"],
          "additionalProperties": false,
          "properties": {
            "situation": { "type": "string", "maxLength": 500 },
            "action":    { "type": "string", "maxLength": 500 },
            "notes":     { "type": "string", "maxLength": 1000 }
          }
        }
      }
    }
  }
}
```

> **H-02 준수 주의**: `detail`이 구조화 객체(`{situation, action, notes}`)로 변경됨. `_sanitize_care_record()` Pydantic `CareFlag.detail` 타입도 동시 변경 필수.
> **마이그레이션 주의**: in-flight v2.1 outbox 페이로드(detail이 string)는 `_sanitize_care_record()` 기존 폴백 로직이 처리. v2.2 전환 후 워커가 v2.1 응답 수신 시 `schema_version 불일치` 경고 발생하나 파이프라인은 계속됨.


---

## 4. Notion 필드 매핑 정의 (Gap G-02 해소)

### 4-1. Hot Path — VG_운영_대시보드_DB 속성 매핑

| Gemini JSON 필드 | Notion 속성명 | Notion 속성 타입 | 현황 | 목표 |
|---|---|---|---|---|
| `meal.done` | `식사` | checkbox | ✅ 매핑됨 | 유지 |
| `medication.done` | `투약` | checkbox | ✅ 매핑됨 | 유지 |
| `excretion.done` | `배설` | checkbox | ✅ 매핑됨 | 유지 |
| `repositioning.done` | `체위변경` | checkbox | ❌ **누락** | 추가 필요 |
| `hygiene.done` | `위생` | checkbox | ❌ **누락** | 추가 필요 |
| `corrected_transcript` | `정제발화` | rich_text | ❌ **없음** | 신규 속성 추가 |
| `special_notes.detail` | `특이사항` | rich_text | ❌ **없음** | 신규 속성 추가 |

### 4-2. Cold Path — VG_Atomic_Care_Events 속성 매핑

| Gemini JSON 필드 | Notion 속성명 | Notion 속성 타입 | 비고 |
|---|---|---|---|
| `category` | `카테고리` | select | ✅ 매핑됨 |
| `detail` | `케어_내용` | rich_text | ❌ **누락** — detail 텍스트 전달 안 됨 |
| `done` | (제목에 포함) | — | 현행 유지 |

### 4-3. 인수인계(Handover) Fork B 하드코딩 ID 해소 방안

현재 `main.py:2258-2264`:
```python
facility_id="handover",        # ← 하드코딩
beneficiary_id="resident_user", # ← Relation 조회 실패 원인
caregiver_id="handover_staff",  # ← Relation 조회 실패 원인
```

목표: `HandoverBody`에 `facility_id`, `beneficiary_id`, `caregiver_id` 선택 필드 추가  
폴백: 미제공 시 기존 하드코딩값 유지 (하위 호환)

---

## 5. Generator–Evaluator 분리 구조 (H-03 원칙 구현)

```
[Generator — Claude Sonnet]
  input:  raw_voice_text (Whisper 출력 or 직접 입력)
  output: Gemini에 전달할 프롬프트 + 메타데이터 구성
      ↓
[Gemini Flash — 실행 레이어]
  call_gemini_care_record() → CareRecord v2.2 JSON
      ↓
[Evaluator — Claude Haiku (독립 세션, 4대 채점)]
  채점 기준:
    ① Quality    (품질):     의료용어 표준화 정확도      (0~25점)
    ② Originality(독창성):   원문 발화 내용 보존율        (0~25점)
    ③ Craft      (디테일):   3단 요약(상황/조치/특이사항) 완성도 (0~25점)
    ④ Functionality(기능성): JSON 스키마 준수, null 방어  (0~25점)
  합격 기준: 총점 ≥ 70점 (각 항목 최소 10점 이상)
      ↓
[합격] → notion_pipeline.run_pipeline() 실행
[불합격] → 재정제 요청 (최대 2회 재시도, 이후 기본값 JSON 적재)
```

### 5-1. 평가자 판단 루브릭 (채점 기준 상세)

#### ① Quality (품질, 25점)
- 25점: 모든 의료용어 표준화, 오탈자 0건
- 18점: 경미한 표현 비표준 1-2건, 의미는 정확
- 10점: 일부 표준화 실패, 파악 가능
- 0점: 표준화 실패 다수, 오히려 오류 삽입

#### ② 충실도 — Fidelity (25점)
> ⚠️ 의료·법적 증거 기록에서 "독창성"은 오히려 환각 위험. 원문 사실 보존율을 채점한다.
- 25점: 원문 발화의 모든 핵심 사실 보존, 추가 내용 0건
- 18점: 사실 보존 90% 이상, 경미한 누락 (핵심 아닌 부분)
- 10점: 일부 중요 내용 누락 또는 의미 변형
- 0점: 원문에 없는 내용 생성 (환각 발생)

#### ③ Craft (디테일, 25점)
- 25점: 3단 요약(상황|조치|특이사항) 모든 done=True 항목에 완성
- 18점: 3단 구조 있으나 1-2항목 약함
- 10점: 단순 서술, 3단 구조 없음
- 0점: detail 필드 null (done=True인데)

#### ④ Functionality (기능성, 25점)
- 25점: JSON 스키마 v2.2 완전 준수, null 방어 완벽
- 18점: 스키마 준수, 경미한 길이 초과
- 10점: 일부 필드 누락, 타입 불일치
- 0점: JSON 파싱 실패 또는 필수 필드 누락

---

## 6. 컨텍스트 포크 실행 계획 (H-05 원칙 구현)

```
메인 컨텍스트 창
    │
    ├─ [경량 작업만 처리]
    │    - gemini_harness.md 문서 유지·업데이트
    │    - Gap 목록 확인
    │    - 코드 변경 최종 승인
    │
    └─ [Fork 세션 — 독립 백그라운드]
         ├─ Fork-1: Gemini 실제 API 호출 + 응답 수집 (test_ai_pipeline.py 실행)
         ├─ Fork-2: Evaluator Haiku가 JSON 채점 (4대 루브릭 적용)
         └─ Fork-3: Notion DB 실제 속성명 스캔 (신규 속성 존재 여부 확인)
         
         → 각 Fork 결과: 요약본만 메인 창에 보고 (원본 로그는 Fork 세션에 보존)
```

---

## 7. 구현 우선순위 (사령관 승인 후 즉시 착수)

| 순위 | 작업 | 대상 파일 | Gap 해소 |
|---|---|---|---|
| 🥇 P1 | Gemini 프롬프트 v2.2 적용 + `corrected_transcript` 필드 추가 | `gemini_processor.py` | G-01, G-05 |
| 🥈 P2 | Hot Path 체크박스 5개 완전 매핑 + `정제발화`·`특이사항` 속성 추가 | `notion_pipeline.py` | G-02, G-04 |
| 🥉 P3 | Cold Path `detail` 텍스트 Notion 전달 | `notion_pipeline.py` | G-04 |
| P4 | `HandoverBody`에 실제 ID 선택 필드 추가 | `main.py` | G-03 |
| P4-FE | **FE 협업 필수** — `FRONT END/src/services/api.ts` handover payload에 `facility_id`/`beneficiary_id`/`caregiver_id` 추가. 백엔드 단독 완결 불가 | `api.ts` | G-03 |
| P5 | Evaluator 검수 에이전트 통합 | 신규 `evaluator.py` | H-03, H-04 |

---

## 8. 불변 제약 (이 섹션은 수정 금지)

- WORM(Fork A) 파이프라인 수정 절대 금지
- `evidence_ledger` 스키마 수정 절대 금지
- `care_record_ledger` Append-Only 유지
- Gemini API Key = `GEMINI_API_KEY` 환경변수 (재확인)
- SCHEMA_VERSION 변경 시 `_sanitize_care_record()` 동시 업데이트 필수

---

*이 문서가 구현 코드보다 상위 권위를 가진다. 코드 변경 전 반드시 이 하네스를 먼저 업데이트하라.*
