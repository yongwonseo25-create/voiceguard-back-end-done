"""
erp_integration — Voice Guard 범용 ERP 자동 이관 시스템
VG_Universal_ERP_Execution_Blueprint_v2 설계도 완전 구현체

모듈 구조:
  cto.py               — Canonical Transfer Object (표준 모델)
  idempotency.py       — SHA-256 멱등성 키 생성
  orchestrator.py      — 상태 머신 (APPROVED → COMMITTED)
  credential_manager.py — 봉투 암호화 런타임 주입
  adapter_registry.py  — 어댑터 레지스트리
  adapters/            — Tier 1/2/3 어댑터 구현체
"""
