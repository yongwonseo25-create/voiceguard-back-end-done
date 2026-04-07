"""
Voice Guard — 3단계 RPA 연계 통합 테스트

테스트 시나리오:
  1. 테스트 데이터 준비 (evidence → review → export batch)
  2. RPA 시작: batch CREATED → RPA_IN_PROGRESS 전이
  3. RPA 콜백 SUCCESS: execution_log INSERT + APPLIED_CONFIRMED 전이
  4. RPA 콜백 FAILED 테스트 (별도 배치)
  5. Append-Only 검증: execution_log UPDATE/DELETE 차단
  6. 이력 조회 검증
  7. 클린업
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
load_dotenv()

import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

TEST_FACILITY = "TEST_FAC_RPA"


def _create_test_batch(conn, facility_id, batch_status="CREATED"):
    """헬퍼: evidence + review + export batch 생성."""
    ledger_id = str(uuid4())
    batch_id = str(uuid4())
    now = datetime.now(timezone.utc)
    rnd = uuid4().hex.encode()

    # evidence_ledger
    conn.execute(text("""
        INSERT INTO evidence_ledger (
            id, session_id, recorded_at, ingested_at,
            device_id, facility_id,
            audio_sha256, transcript_sha256, chain_hash,
            transcript_text, language_code,
            case_type, is_flagged,
            beneficiary_id, shift_id, idempotency_key,
            care_type, audio_size_kb,
            worm_bucket, worm_object_key, worm_retain_until
        ) VALUES (
            :id, :sid, :ts, :ts,
            'test', :fid,
            :a, :t, :c,
            '', 'ko', 'test', false,
            'TEST_BENE', :shift, :idem,
            'test', 50,
            'test-bucket', :wk, :ts
        )
    """), {
        "id": ledger_id, "sid": str(uuid4()),
        "ts": now, "fid": facility_id,
        "a": hashlib.sha256(b"a" + rnd).hexdigest(),
        "t": hashlib.sha256(b"t" + rnd).hexdigest(),
        "c": hashlib.sha256(b"c" + rnd).hexdigest(),
        "shift": f"S_{uuid4().hex[:6]}",
        "idem": hashlib.sha256(rnd).hexdigest(),
        "wk": f"test/{ledger_id}.wav",
    })

    # bridge_export_batch
    zip_sha = hashlib.sha256(b"zip" + rnd).hexdigest()
    conn.execute(text("""
        INSERT INTO bridge_export_batch (
            id, facility_id, status,
            ledger_ids, item_count,
            zip_sha256, angel_csv_sha256,
            proof_csv_sha256,
            exported_by, created_at
        ) VALUES (
            :id, :fid, :status,
            CAST(:lids AS UUID[]), 1,
            :zsha, :asha, :psha,
            'test', :ts
        )
    """), {
        "id": batch_id, "fid": facility_id,
        "status": batch_status,
        "lids": [ledger_id],
        "zsha": zip_sha,
        "asha": hashlib.sha256(b"angel" + rnd).hexdigest(),
        "psha": hashlib.sha256(b"proof" + rnd).hexdigest(),
        "ts": now,
    })

    return ledger_id, batch_id


def test_full_rpa_pipeline():
    errors = []
    ledger_ids = []
    batch_ids = []

    print(f"\n{'='*60}")
    print("3단계 RPA 연계 통합 테스트")
    print(f"{'='*60}")

    # ── 1. 테스트 데이터 준비 ──────────────────────────────────
    print("\n[1] 테스트 데이터 준비...")
    try:
        with engine.begin() as conn:
            lid1, bid1 = _create_test_batch(conn, TEST_FACILITY)
            lid2, bid2 = _create_test_batch(conn, TEST_FACILITY)
        ledger_ids.extend([lid1, lid2])
        batch_ids.extend([bid1, bid2])
        print(f"  batch1: {bid1[:8]} (SUCCESS 테스트)")
        print(f"  batch2: {bid2[:8]} (FAILED 테스트)")
    except Exception as e:
        errors.append(f"[1] 데이터 준비 실패: {e}")
        print(f"  FAIL: {e}")
        _cleanup(ledger_ids, batch_ids)
        return errors

    # ── 2. RPA 시작: CREATED → RPA_IN_PROGRESS ────────────────
    print("\n[2] RPA 시작 (CREATED → RPA_IN_PROGRESS)...")
    try:
        with engine.begin() as conn:
            # 상태 전이
            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = 'RPA_IN_PROGRESS'
                WHERE id = :bid AND status = 'CREATED'
            """), {"bid": bid1})

            # 검증
            row = conn.execute(text(
                "SELECT status FROM bridge_export_batch "
                "WHERE id = :bid"
            ), {"bid": bid1}).fetchone()

        if row.status != "RPA_IN_PROGRESS":
            errors.append(
                f"[2] 상태 전이 실패: {row.status}"
            )
        else:
            print("  RPA_IN_PROGRESS 전이 성공")
    except Exception as e:
        errors.append(f"[2] RPA 시작 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 3. RPA 콜백 SUCCESS → APPLIED_CONFIRMED ───────────────
    print("\n[3] RPA 콜백 SUCCESS → APPLIED_CONFIRMED...")
    ss_hash = hashlib.sha256(b"screenshot_success").hexdigest()
    log_id_1 = str(uuid4())
    now = datetime.now(timezone.utc)

    try:
        with engine.begin() as conn:
            # execution_log INSERT
            conn.execute(text("""
                INSERT INTO bridge_rpa_execution_log (
                    id, batch_id, status,
                    screenshot_hash, angel_receipt,
                    items_applied, items_failed,
                    executed_by, executed_at
                ) VALUES (
                    :id, :bid, 'SUCCESS',
                    :ss, CAST(:receipt AS jsonb),
                    1, 0,
                    'playwright-bot-1', :ts
                )
            """), {
                "id": log_id_1, "bid": bid1,
                "ss": ss_hash,
                "receipt": json.dumps(
                    {"angel_response": "OK", "rows_inserted": 1}
                ),
                "ts": now,
            })

            # batch 최종 상태 전이
            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = 'APPLIED_CONFIRMED'
                WHERE id = :bid
            """), {"bid": bid1})

        # 검증
        with engine.connect() as conn:
            batch = conn.execute(text(
                "SELECT status FROM bridge_export_batch "
                "WHERE id = :bid"
            ), {"bid": bid1}).fetchone()
            log = conn.execute(text(
                "SELECT * FROM bridge_rpa_execution_log "
                "WHERE id = :id"
            ), {"id": log_id_1}).fetchone()

        if batch.status != "APPLIED_CONFIRMED":
            errors.append(
                f"[3] batch 상태: {batch.status}"
            )
        elif log is None:
            errors.append("[3] execution_log 없음")
        elif log.screenshot_hash.strip() != ss_hash:
            errors.append("[3] screenshot_hash 불일치")
        else:
            print("  APPLIED_CONFIRMED 전이 성공")
            print(f"  screenshot_hash: {ss_hash[:16]}...")
            print(f"  execution_log id: {log_id_1[:8]}")
    except Exception as e:
        errors.append(f"[3] SUCCESS 콜백 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 4. RPA 콜백 FAILED → APPLY_FAILED ─────────────────────
    print("\n[4] RPA 콜백 FAILED → APPLY_FAILED...")
    log_id_2 = str(uuid4())

    try:
        with engine.begin() as conn:
            # batch2도 먼저 RPA_IN_PROGRESS로
            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = 'RPA_IN_PROGRESS'
                WHERE id = :bid
            """), {"bid": bid2})

            conn.execute(text("""
                INSERT INTO bridge_rpa_execution_log (
                    id, batch_id, status,
                    error_msg,
                    items_applied, items_failed,
                    executed_by, executed_at
                ) VALUES (
                    :id, :bid, 'FAILED',
                    :err, 0, 1,
                    'playwright-bot-1', :ts
                )
            """), {
                "id": log_id_2, "bid": bid2,
                "err": "엔젤시스템 로그인 실패: 세션 만료",
                "ts": now,
            })

            conn.execute(text("""
                UPDATE bridge_export_batch
                SET status = 'APPLY_FAILED'
                WHERE id = :bid
            """), {"bid": bid2})

        with engine.connect() as conn:
            batch = conn.execute(text(
                "SELECT status FROM bridge_export_batch "
                "WHERE id = :bid"
            ), {"bid": bid2}).fetchone()
            log = conn.execute(text(
                "SELECT * FROM bridge_rpa_execution_log "
                "WHERE id = :id"
            ), {"id": log_id_2}).fetchone()

        if batch.status != "APPLY_FAILED":
            errors.append(f"[4] batch 상태: {batch.status}")
        elif log.error_msg is None:
            errors.append("[4] error_msg 없음")
        else:
            print("  APPLY_FAILED 전이 성공")
            print(f"  error: {log.error_msg[:30]}...")
    except Exception as e:
        errors.append(f"[4] FAILED 콜백 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 5. Append-Only: UPDATE 차단 ────────────────────────────
    print("\n[5] Append-Only: execution_log UPDATE 차단...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE bridge_rpa_execution_log
                SET status = 'FAILED'
                WHERE id = :id
            """), {"id": log_id_1})
        errors.append("[5] UPDATE 미차단!")
        print("  FAIL: UPDATE 성공 (차단 실패)")
    except Exception as e:
        if "UPDATE 차단" in str(e):
            print("  UPDATE 차단 트리거 정상 작동")
        else:
            errors.append(f"[5] 예상 외: {e}")

    # ── 6. Append-Only: DELETE 차단 ────────────────────────────
    print("\n[6] Append-Only: execution_log DELETE 차단...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM bridge_rpa_execution_log
                WHERE id = :id
            """), {"id": log_id_1})
        errors.append("[6] DELETE 미차단!")
        print("  FAIL: DELETE 성공 (차단 실패)")
    except Exception as e:
        if "삭제 차단" in str(e):
            print("  DELETE 차단 트리거 정상 작동")
        else:
            errors.append(f"[6] 예상 외: {e}")

    # ── 7. 이력 조회 ──────────────────────────────────────────
    print("\n[7] RPA 이력 조회...")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT r.id, r.batch_id, r.status,
                       r.screenshot_hash, r.items_applied,
                       b.status AS batch_status
                FROM bridge_rpa_execution_log r
                JOIN bridge_export_batch b ON b.id = r.batch_id
                WHERE b.facility_id = :fid
                ORDER BY r.executed_at DESC
            """), {"fid": TEST_FACILITY}).fetchall()

        if len(rows) < 2:
            errors.append(f"[7] 이력 {len(rows)}건 (2건 기대)")
        else:
            print(f"  이력 {len(rows)}건 조회 성공")
            for r in rows:
                print(
                    f"    {str(r.id)[:8]} | "
                    f"rpa={r.status} | "
                    f"batch={r.batch_status}"
                )
    except Exception as e:
        errors.append(f"[7] 이력 조회 실패: {e}")

    # ── 클린업 ─────────────────────────────────────────────────
    _cleanup(ledger_ids, batch_ids)

    # ── 결과 ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if errors:
        print(f"FAIL: {len(errors)}개 에러")
        for e in errors:
            print(f"  - {e}")
    else:
        print(
            "ALL PASSED: 3단계 RPA 연계 "
            "통합 테스트 전체 통과"
        )
    print(f"{'='*60}\n")
    return errors


def _cleanup(ledger_ids, batch_ids):
    try:
        with engine.begin() as conn:
            # rpa_execution_log
            conn.execute(text(
                "ALTER TABLE bridge_rpa_execution_log "
                "DISABLE TRIGGER USER"
            ))
            for bid in batch_ids:
                conn.execute(text(
                    "DELETE FROM bridge_rpa_execution_log "
                    "WHERE batch_id = :bid"
                ), {"bid": bid})
            conn.execute(text(
                "ALTER TABLE bridge_rpa_execution_log "
                "ENABLE TRIGGER USER"
            ))

            # export_batch
            conn.execute(text(
                "ALTER TABLE bridge_export_batch "
                "DISABLE TRIGGER USER"
            ))
            for bid in batch_ids:
                conn.execute(text(
                    "DELETE FROM bridge_export_batch "
                    "WHERE id = :bid"
                ), {"bid": bid})
            conn.execute(text(
                "ALTER TABLE bridge_export_batch "
                "ENABLE TRIGGER USER"
            ))

            # evidence_ledger
            conn.execute(text(
                "ALTER TABLE evidence_ledger "
                "DISABLE TRIGGER USER"
            ))
            for lid in ledger_ids:
                conn.execute(text(
                    "DELETE FROM evidence_ledger "
                    "WHERE id = :lid"
                ), {"lid": lid})
            conn.execute(text(
                "ALTER TABLE evidence_ledger "
                "ENABLE TRIGGER USER"
            ))
    except Exception as e:
        print(f"  [CLEANUP] 경고: {e}")


if __name__ == "__main__":
    test_full_rpa_pipeline()
