"""
Phase 1 검증 테스트 — care_plan_ledger / billing_ledger
=======================================================
적대적 검증 (Evaluator 관점):
  1. INSERT 정상 작동
  2. plan_hash / billing_hash 중복 차단 (Idempotency)
  3. UPDATE 차단 (care_plan은 is_superseded만 허용)
  4. DELETE 차단
  5. API 라우터 import 정상 확인
"""

import os
import sys
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

import psycopg2

DB_URL = os.getenv("DATABASE_URL")
assert DB_URL, "DATABASE_URL 미설정"

conn = psycopg2.connect(DB_URL, connect_timeout=10)
conn.autocommit = True
cur = conn.cursor()

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  PASS: {name}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {name} -> {e}")
        failed += 1


# ── care_plan_ledger 테스트 ─────────────────────────────────────

def test_care_plan_insert():
    plan_id = str(uuid4())
    plan_hash = f"test_plan_{uuid4().hex[:48]}"[:64]
    cur.execute("""
        INSERT INTO public.care_plan_ledger
            (id, facility_id, beneficiary_id, caregiver_id, plan_date,
             care_type, planned_duration_min, plan_source, plan_hash)
        VALUES (%s, 'F001', 'B001', 'C001', '2026-04-08',
                '방문요양', 120, 'TEST', %s)
    """, (plan_id, plan_hash))
    # cleanup marker: we'll use this plan_id later
    return plan_id, plan_hash


def test_care_plan_duplicate():
    _, plan_hash = test_care_plan_insert.__test_data
    try:
        cur.execute("""
            INSERT INTO public.care_plan_ledger
                (id, facility_id, beneficiary_id, caregiver_id, plan_date,
                 care_type, planned_duration_min, plan_source, plan_hash)
            VALUES (%s, 'F001', 'B001', 'C001', '2026-04-08',
                    '방문요양', 120, 'TEST', %s)
        """, (str(uuid4()), plan_hash))
        raise AssertionError("중복 INSERT가 성공해버림!")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.autocommit = True


def test_care_plan_update_blocked():
    plan_id, _ = test_care_plan_insert.__test_data
    try:
        cur.execute("""
            UPDATE public.care_plan_ledger
            SET care_type = 'HACKED' WHERE id = %s
        """, (plan_id,))
        raise AssertionError("금지된 UPDATE가 성공해버림!")
    except psycopg2.errors.RaiseException as e:
        assert "UPDATE 차단" in str(e) or "수정 금지" in str(e)
        conn.rollback()
        conn.autocommit = True


def test_care_plan_supersede_allowed():
    plan_id, _ = test_care_plan_insert.__test_data
    new_id = str(uuid4())
    new_hash = f"test_plan_{uuid4().hex[:48]}"[:64]
    # 새 계획 INSERT
    cur.execute("""
        INSERT INTO public.care_plan_ledger
            (id, facility_id, beneficiary_id, caregiver_id, plan_date,
             care_type, planned_duration_min, plan_source, plan_hash)
        VALUES (%s, 'F001', 'B001', 'C001', '2026-04-08',
                '방문요양', 150, 'TEST', %s)
    """, (new_id, new_hash))
    # 이전 계획 무효화 (이것만 허용)
    cur.execute("""
        UPDATE public.care_plan_ledger
        SET is_superseded = TRUE, superseded_by = %s
        WHERE id = %s
    """, (new_id, plan_id))


def test_care_plan_delete_blocked():
    plan_id, _ = test_care_plan_insert.__test_data
    try:
        cur.execute("""
            DELETE FROM public.care_plan_ledger WHERE id = %s
        """, (plan_id,))
        raise AssertionError("DELETE가 성공해버림!")
    except psycopg2.errors.RaiseException as e:
        assert "차단" in str(e)
        conn.rollback()
        conn.autocommit = True


# ── billing_ledger 테스트 ───────────────────────────────────────

def test_billing_insert():
    bill_id = str(uuid4())
    bill_hash = f"test_bill_{uuid4().hex[:48]}"[:64]
    cur.execute("""
        INSERT INTO public.billing_ledger
            (id, facility_id, beneficiary_id, billing_month, billing_date,
             care_type, billed_duration_min, billed_amount_krw, upload_source, billing_hash)
        VALUES (%s, 'F001', 'B001', '2026-03', '2026-03-31',
                '방문요양', 120, 150000, 'TEST', %s)
    """, (bill_id, bill_hash))
    return bill_id, bill_hash


def test_billing_update_blocked():
    bill_id, _ = test_billing_insert.__test_data
    try:
        cur.execute("""
            UPDATE public.billing_ledger
            SET billed_amount_krw = 999999 WHERE id = %s
        """, (bill_id,))
        raise AssertionError("UPDATE가 성공해버림!")
    except psycopg2.errors.RaiseException as e:
        assert "UPDATE 차단" in str(e)
        conn.rollback()
        conn.autocommit = True


def test_billing_delete_blocked():
    bill_id, _ = test_billing_insert.__test_data
    try:
        cur.execute("""
            DELETE FROM public.billing_ledger WHERE id = %s
        """, (bill_id,))
        raise AssertionError("DELETE가 성공해버림!")
    except psycopg2.errors.RaiseException as e:
        assert "차단" in str(e)
        conn.rollback()
        conn.autocommit = True


# ── API import 테스트 ──────────────────────────────────────────

def test_api_imports():
    from care_plan_api import router as cpr
    from billing_api import router as br
    assert len(cpr.routes) >= 3, f"care_plan routes: {len(cpr.routes)}"
    assert len(br.routes) >= 2, f"billing routes: {len(br.routes)}"


# ── 실행 ───────────────────────────────────────────────────────

print("=" * 60)
print("Phase 1 Verification - Adversarial Evaluator")
print("=" * 60)

# care_plan 테스트 (순서 의존)
result = test_care_plan_insert()
test_care_plan_insert.__test_data = result
test("care_plan INSERT", lambda: None)  # already done

test("care_plan 중복 차단 (plan_hash UNIQUE)", test_care_plan_duplicate)
test("care_plan UPDATE 차단 (care_type 변경)", test_care_plan_update_blocked)
test("care_plan is_superseded UPDATE 허용", test_care_plan_supersede_allowed)
test("care_plan DELETE 차단", test_care_plan_delete_blocked)

# billing 테스트
result = test_billing_insert()
test_billing_insert.__test_data = result
test("billing INSERT", lambda: None)

test("billing UPDATE 완전 차단", test_billing_update_blocked)
test("billing DELETE 차단", test_billing_delete_blocked)

# API import
test("API 라우터 import", test_api_imports)

print("=" * 60)
print(f"결과: {passed} passed / {failed} failed")
print("=" * 60)

cur.close()
conn.close()

sys.exit(0 if failed == 0 else 1)
