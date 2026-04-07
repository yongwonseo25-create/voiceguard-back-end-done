"""
Voice Guard — 2단계 증거 브리지 통합 테스트
angel_bridge + angel_export 전체 파이프라인 검증

테스트 시나리오:
  1. evidence_ledger에 테스트 데이터 INSERT
  2. angel_review_event DETECTED → APPROVED_FOR_EXPORT 전이
  3. Export ZIP 생성 → CSV/JSON 내용 검증
  4. bridge_export_batch 원장 기록 검증
  5. 상태가 EXPORTED로 전이되었는지 검증
  6. 클린업
"""

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from dotenv import load_dotenv
load_dotenv()

import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

TEST_FACILITY = "TEST_FAC_EXPORT"
TEST_BENEFICIARY = "TEST_BENE_001"
TEST_SHIFT = f"TEST_SHIFT_{uuid4().hex[:8]}"


def test_full_export_pipeline():
    """전체 Export 파이프라인 통합 테스트."""
    errors = []
    ledger_id = str(uuid4())
    idem_key = hashlib.sha256(
        f"{TEST_FACILITY}::{TEST_BENEFICIARY}::{TEST_SHIFT}"
        .encode()
    ).hexdigest()

    print(f"\n{'='*60}")
    print("2단계 증거 브리지 통합 테스트")
    print(f"{'='*60}")

    # ── 1. evidence_ledger 테스트 데이터 ────────────────────────
    print("\n[1] evidence_ledger 테스트 데이터 INSERT...")
    now = datetime.now(timezone.utc)
    rnd = uuid4().hex.encode()
    audio_sha = hashlib.sha256(b"test_audio" + rnd).hexdigest()
    tx_sha = hashlib.sha256(b"test_transcript" + rnd).hexdigest()
    chain = hashlib.sha256(b"test_chain" + rnd).hexdigest()

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO evidence_ledger (
                    id, session_id, recorded_at, ingested_at,
                    device_id, facility_id,
                    audio_sha256, transcript_sha256, chain_hash,
                    transcript_text, language_code,
                    case_type, is_flagged,
                    beneficiary_id, shift_id, idempotency_key,
                    care_type, audio_size_kb,
                    worm_bucket, worm_object_key,
                    worm_retain_until
                ) VALUES (
                    :id, :sid, :ts, :ts,
                    'test_device', :fid,
                    :asha, :tsha, :chain,
                    'test transcript', 'ko',
                    :care, false,
                    :bid, :shift, :idem,
                    :care, 100,
                    'voice-guard-korea',
                    :worm_key, :ts
                )
            """), {
                "id": ledger_id,
                "sid": str(uuid4()),
                "ts": now,
                "fid": TEST_FACILITY,
                "asha": audio_sha,
                "tsha": tx_sha,
                "chain": chain,
                "care": "식사 보조",
                "bid": TEST_BENEFICIARY,
                "shift": TEST_SHIFT,
                "idem": idem_key,
                "worm_key": f"evidence/test/{ledger_id}.wav",
            })
        print(f"  ledger_id: {ledger_id}")
    except Exception as e:
        errors.append(f"[1] INSERT 실패: {e}")
        print(f"  FAIL: {e}")
        _cleanup(ledger_id)
        return errors

    # ── 2. angel_review_event: DETECTED → APPROVED ─────────────
    print("\n[2] 상태 전이: DETECTED → APPROVED_FOR_EXPORT...")
    try:
        with engine.begin() as conn:
            # DETECTED (t=now)
            conn.execute(text("""
                INSERT INTO angel_review_event
                    (id, ledger_id, status, created_at)
                VALUES (:id, :lid, 'DETECTED', :ts)
            """), {
                "id": str(uuid4()),
                "lid": ledger_id,
                "ts": now,
            })
            # APPROVED_FOR_EXPORT (t=now+1s, 정렬 보장)
            conn.execute(text("""
                INSERT INTO angel_review_event
                    (id, ledger_id, status, reviewer_id,
                     decision_note, created_at)
                VALUES (:id, :lid, 'APPROVED_FOR_EXPORT',
                        'test_admin', 'test approval', :ts)
            """), {
                "id": str(uuid4()),
                "lid": ledger_id,
                "ts": now + timedelta(seconds=1),
            })
        print("  DETECTED + APPROVED_FOR_EXPORT inserted")
    except Exception as e:
        errors.append(f"[2] 상태 전이 실패: {e}")
        print(f"  FAIL: {e}")
        _cleanup(ledger_id)
        return errors

    # ── 3. Export 대상 조회 ─────────────────────────────────────
    print("\n[3] Export 대상 조회 (APPROVED_FOR_EXPORT)...")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT latest.ledger_id, latest.status,
                       e.care_type, e.audio_sha256
                FROM (
                    SELECT DISTINCT ON (ledger_id) *
                    FROM angel_review_event
                    ORDER BY ledger_id, created_at DESC
                ) latest
                JOIN evidence_ledger e ON e.id = latest.ledger_id
                WHERE latest.status = 'APPROVED_FOR_EXPORT'
                  AND e.facility_id = :fid
            """), {"fid": TEST_FACILITY}).fetchall()

        if len(rows) == 0:
            errors.append("[3] Export 대상 0건")
            print("  FAIL: 대상 없음")
        else:
            print(f"  대상: {len(rows)}건")
            for r in rows:
                print(f"    {r.ledger_id} / {r.care_type}")
    except Exception as e:
        errors.append(f"[3] 조회 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 4. CSV 생성 테스트 ─────────────────────────────────────
    print("\n[4] CSV + ZIP 생성 테스트...")
    from angel_export import (
        _build_angel_csv, _build_proof_csv,
        _build_receipt, _sha256_bytes,
    )

    items = [dict(r._mapping) for r in rows]
    angel_bytes = _build_angel_csv(items)
    proof_bytes = _build_proof_csv(items)

    angel_sha = _sha256_bytes(angel_bytes)
    proof_sha = _sha256_bytes(proof_bytes)

    # CSV 내용 검증
    angel_content = angel_bytes.decode("utf-8-sig")
    proof_content = proof_bytes.decode("utf-8-sig")

    if "서비스일자" not in angel_content:
        errors.append("[4] angel_import.csv 헤더 누락")
    else:
        print("  angel_import.csv 헤더 OK")

    if "ledger_id" not in proof_content:
        errors.append("[4] proof_manifest.csv 헤더 누락")
    else:
        print("  proof_manifest.csv 헤더 OK")

    if audio_sha not in proof_content:
        errors.append("[4] proof에 audio_sha256 미포함")
    else:
        print("  proof에 audio_sha256 포함 확인")

    # ZIP 패키징
    batch_id = str(uuid4())
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("angel_import.csv", angel_bytes)
        zf.writestr("proof_manifest.csv", proof_bytes)
    zip_sha = _sha256_bytes(zip_buf.getvalue())

    receipt = _build_receipt(
        batch_id, TEST_FACILITY, len(items),
        [str(i["ledger_id"]) for i in items],
        angel_sha, proof_sha, zip_sha, now.isoformat(),
    )
    receipt_json = json.loads(receipt)

    if receipt_json["export_batch_id"] != batch_id:
        errors.append("[4] receipt batch_id 불일치")
    else:
        print("  receipt JSON batch_id 일치")

    # 최종 ZIP (receipt 포함)
    final_buf = io.BytesIO()
    with zipfile.ZipFile(final_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("angel_import.csv", angel_bytes)
        zf.writestr("proof_manifest.csv", proof_bytes)
        zf.writestr("export_receipt.json", receipt)

    final_zip = final_buf.getvalue()
    final_sha = _sha256_bytes(final_zip)

    # ZIP 내부 파일 목록 검증
    with zipfile.ZipFile(io.BytesIO(final_zip), "r") as zf:
        names = zf.namelist()
    expected = {"angel_import.csv", "proof_manifest.csv",
                "export_receipt.json"}
    if set(names) != expected:
        errors.append(f"[4] ZIP 파일 목록 불일치: {names}")
    else:
        print(f"  ZIP 파일 3개 확인: {names}")

    # ── 5. bridge_export_batch INSERT 검증 ─────────────────────
    print("\n[5] bridge_export_batch 원장 기록...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO bridge_export_batch (
                    id, facility_id, status,
                    ledger_ids, item_count,
                    zip_sha256, angel_csv_sha256,
                    proof_csv_sha256,
                    exported_by, created_at
                ) VALUES (
                    :id, :fid, 'CREATED',
                    CAST(:lids AS UUID[]), :cnt,
                    :zsha, :asha, :psha,
                    'test', :ts
                )
            """), {
                "id": batch_id,
                "fid": TEST_FACILITY,
                "lids": [ledger_id],
                "cnt": 1,
                "zsha": final_sha,
                "asha": angel_sha,
                "psha": proof_sha,
                "ts": now,
            })

        # 조회 검증
        with engine.connect() as conn:
            batch = conn.execute(text("""
                SELECT * FROM bridge_export_batch
                WHERE id = :id
            """), {"id": batch_id}).fetchone()

        if batch is None:
            errors.append("[5] batch 조회 실패")
        elif str(batch.zip_sha256).strip() != final_sha:
            errors.append(
                f"[5] zip_sha256 불일치: "
                f"{batch.zip_sha256} != {final_sha}"
            )
        else:
            print(f"  batch_id: {batch_id[:8]}...")
            print(f"  zip_sha256: {final_sha[:16]}...")
            print(f"  item_count: {batch.item_count}")
    except Exception as e:
        errors.append(f"[5] batch 기록 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 6. EXPORTED 상태 전이 검증 ──────────────────────────────
    print("\n[6] EXPORTED 상태 전이...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO angel_review_event
                    (id, ledger_id, status, reviewer_id,
                     decision_note, export_batch_id, created_at)
                VALUES (:id, :lid, 'EXPORTED', 'test',
                        'test export', :bid, :ts)
            """), {
                "id": str(uuid4()),
                "lid": ledger_id,
                "bid": batch_id,
                "ts": now + timedelta(seconds=2),
            })

        with engine.connect() as conn:
            latest = conn.execute(text("""
                SELECT status FROM angel_review_event
                WHERE ledger_id = :lid
                ORDER BY created_at DESC LIMIT 1
            """), {"lid": ledger_id}).fetchone()

        if latest.status != "EXPORTED":
            errors.append(
                f"[6] 최종 상태 불일치: {latest.status}"
            )
        else:
            print("  최종 상태: EXPORTED")
    except Exception as e:
        errors.append(f"[6] 상태 전이 실패: {e}")
        print(f"  FAIL: {e}")

    # ── 7. Append-Only 검증 (UPDATE 차단) ──────────────────────
    print("\n[7] Append-Only 검증 (UPDATE 차단)...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE angel_review_event
                SET status = 'DETECTED'
                WHERE ledger_id = :lid
            """), {"lid": ledger_id})
        errors.append("[7] UPDATE가 차단되지 않음!")
        print("  FAIL: UPDATE 성공 (차단 실패)")
    except Exception as e:
        if "UPDATE 차단" in str(e):
            print("  UPDATE 차단 트리거 정상 작동")
        else:
            errors.append(f"[7] 예상 외 에러: {e}")

    # ── 8. DELETE 차단 검증 ─────────────────────────────────────
    print("\n[8] DELETE 차단 검증...")
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM bridge_export_batch WHERE id = :id
            """), {"id": batch_id})
        errors.append("[8] batch DELETE가 차단되지 않음!")
        print("  FAIL: DELETE 성공 (차단 실패)")
    except Exception as e:
        if "삭제 차단" in str(e) or "DELETE" in str(e):
            print("  DELETE 차단 트리거 정상 작동")
        else:
            errors.append(f"[8] 예상 외 에러: {e}")

    # ── 클린업 ─────────────────────────────────────────────────
    _cleanup(ledger_id)

    # ── 결과 ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if errors:
        print(f"FAIL: {len(errors)}개 에러")
        for e in errors:
            print(f"  - {e}")
    else:
        print("ALL PASSED: 2단계 증거 브리지 통합 테스트 전체 통과")
    print(f"{'='*60}\n")

    return errors


def _cleanup(ledger_id: str):
    """테스트 데이터 정리 (트리거 우회)."""
    try:
        with engine.begin() as conn:
            # angel_review_event는 DELETE 트리거 있음
            # → 테스트용이므로 트리거 일시 비활성화
            conn.execute(text(
                "ALTER TABLE angel_review_event "
                "DISABLE TRIGGER trg_angel_review_block_delete"
            ))
            conn.execute(text(
                "DELETE FROM angel_review_event "
                "WHERE ledger_id = :lid"
            ), {"lid": ledger_id})
            conn.execute(text(
                "ALTER TABLE angel_review_event "
                "ENABLE TRIGGER trg_angel_review_block_delete"
            ))

            # bridge_export_batch
            conn.execute(text(
                "ALTER TABLE bridge_export_batch "
                "DISABLE TRIGGER trg_export_batch_block_delete"
            ))
            conn.execute(text(
                "DELETE FROM bridge_export_batch "
                "WHERE facility_id = :fid"
            ), {"fid": TEST_FACILITY})
            conn.execute(text(
                "ALTER TABLE bridge_export_batch "
                "ENABLE TRIGGER trg_export_batch_block_delete"
            ))

            # evidence_ledger (테스트 데이터만)
            # 사용자 트리거만 비활성화 (시스템 FK 트리거 제외)
            conn.execute(text(
                "ALTER TABLE evidence_ledger "
                "DISABLE TRIGGER USER"
            ))
            conn.execute(text(
                "DELETE FROM evidence_ledger WHERE id = :lid"
            ), {"lid": ledger_id})
            conn.execute(text(
                "ALTER TABLE evidence_ledger "
                "ENABLE TRIGGER USER"
            ))
    except Exception as e:
        print(f"  [CLEANUP] 경고: {e}")


if __name__ == "__main__":
    test_full_export_pipeline()
