"""
erp_integration/credential_manager.py
봉투 암호화 자격증명 관리 — 설계도 결단 4 구현

보안 원칙:
  - DB에 평문 자격증명 절대 저장 금지
  - DEK(Data Encryption Key)로 평문 암호화 → Ops DB에 암호문만 저장
  - DEK 자체는 KEK(Cloud KMS 마스터 키)로 재암호화 → Secret Manager 저장
  - 런타임 시 OIDC Workload Identity로 JIT 복호화
  - 복호화된 평문은 메모리에만 로드 → 컨테이너 소멸 시 즉각 파기

로컬 개발 환경 (CREDENTIAL_BACKEND=local):
  - Secret Manager 없이 환경변수 기반 시뮬레이션 (프로덕션 절대 금지)
프로덕션 환경 (CREDENTIAL_BACKEND=gcp):
  - GCP Secret Manager + Cloud KMS CMEK 실사용
"""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger("voice_guard.erp_integration.credentials")

_BACKEND = os.environ.get("CREDENTIAL_BACKEND", "local")


@dataclass
class ErpCredential:
    """런타임 인메모리 자격증명 — 디스크/로그에 절대 기록 금지."""
    username: str
    login_url: str
    mfa_mode: str = "none"
    account_scope: str = "write_records_only"
    _password: str = ""

    def get_password(self) -> str:
        return self._password

    def __repr__(self) -> str:
        return f"ErpCredential(username={self.username!r}, password=***MASKED***)"

    def __str__(self) -> str:
        return self.__repr__()


class CredentialManager:
    """
    봉투 암호화 기반 자격증명 관리자.

    Secret Manager 키 명명 규칙:
      {env}/tenant-{tenant_id}/{erp_system}/login
    예:
      prod/tenant-001/angel/login
      prod/tenant-001/carefo/login
    """

    def __init__(self, env: str = "prod"):
        self._env = env
        self._backend = _BACKEND

    def _secret_name(self, tenant_id: str, erp_system: str) -> str:
        return f"{self._env}/tenant-{tenant_id}/{erp_system}/login"

    @contextmanager
    def get_credential(self, tenant_id: str, erp_system: str):
        """
        컨텍스트 매니저: 진입 시 복호화, 탈출 시 즉각 메모리 파기.

        with credential_manager.get_credential("001", "angel") as cred:
            # cred.username, cred.get_password() 사용
            pass
        # 탈출 후 cred는 파기됨
        """
        cred = self._load_credential(tenant_id, erp_system)
        try:
            yield cred
        finally:
            cred._password = ""
            cred.username = ""
            cred.login_url = ""
            logger.info(
                f"[CRED] 인메모리 파기 완료: tenant={tenant_id} erp={erp_system}"
            )

    def _load_credential(self, tenant_id: str, erp_system: str) -> ErpCredential:
        if self._backend == "gcp":
            return self._load_from_gcp_secret_manager(tenant_id, erp_system)
        return self._load_from_local_env(tenant_id, erp_system)

    def _load_from_gcp_secret_manager(
        self, tenant_id: str, erp_system: str
    ) -> ErpCredential:
        """
        프로덕션 경로:
        1. Cloud Run OIDC Workload Identity 임시 토큰 자동 획득
        2. Secret Manager에서 암호화된 DEK + 암호문 조회
        3. Cloud KMS로 DEK 복호화
        4. DEK로 평문 복호화 → 인메모리 ErpCredential 생성
        """
        try:
            from google.cloud import secretmanager
            from google.cloud import kms

            secret_name = self._secret_name(tenant_id, erp_system)
            sm_client = secretmanager.SecretManagerServiceClient()
            project_id = os.environ["GCP_PROJECT_ID"]

            full_name = (
                f"projects/{project_id}/secrets/{secret_name}/versions/latest"
            )
            response = sm_client.access_secret_version(name=full_name)
            secret_data = json.loads(response.payload.data.decode("utf-8"))

            encrypted_dek_b64 = secret_data["encrypted_dek"]
            ciphertext_b64 = secret_data["ciphertext"]
            kms_key_name = secret_data["kms_key_name"]

            kms_client = kms.KeyManagementServiceClient()
            dek_response = kms_client.decrypt(
                name=kms_key_name,
                ciphertext=base64.b64decode(encrypted_dek_b64),
            )
            dek = dek_response.plaintext

            from cryptography.fernet import Fernet
            f = Fernet(base64.urlsafe_b64encode(dek[:32]))
            plaintext = json.loads(f.decrypt(base64.b64decode(ciphertext_b64)))

            cred = ErpCredential(
                username=plaintext["username"],
                login_url=plaintext.get("login_url", ""),
                mfa_mode=plaintext.get("mfa_mode", "none"),
                account_scope=plaintext.get("account_scope", "write_records_only"),
            )
            cred._password = plaintext["password"]
            return cred

        except Exception as e:
            logger.error(f"[CRED] GCP Secret Manager 조회 실패: {e}")
            raise

    def _load_from_local_env(
        self, tenant_id: str, erp_system: str
    ) -> ErpCredential:
        """
        로컬 개발 전용 — 환경변수 기반 시뮬레이션.
        환경변수: ERP_{SYSTEM}_USERNAME, ERP_{SYSTEM}_PASSWORD, ERP_{SYSTEM}_URL
        프로덕션에서 이 경로 진입 시 예외 발생.
        """
        if os.environ.get("ENVIRONMENT") == "production":
            raise RuntimeError(
                "로컬 credential backend는 프로덕션에서 사용 불가. "
                "CREDENTIAL_BACKEND=gcp 설정 필요."
            )

        system_upper = erp_system.upper()
        username = os.environ.get(f"ERP_{system_upper}_USERNAME", "")
        password = os.environ.get(f"ERP_{system_upper}_PASSWORD", "")
        login_url = os.environ.get(f"ERP_{system_upper}_URL", "")

        if not username or not password:
            raise ValueError(
                f"로컬 환경변수 미설정: "
                f"ERP_{system_upper}_USERNAME, ERP_{system_upper}_PASSWORD"
            )

        cred = ErpCredential(
            username=username,
            login_url=login_url,
            mfa_mode="none",
        )
        cred._password = password
        return cred

    @staticmethod
    def encrypt_and_store(
        tenant_id: str,
        erp_system: str,
        plaintext_creds: dict,
        kms_key_name: str,
        project_id: str,
        env: str = "prod",
    ) -> None:
        """
        고객 ERP 계정 최초 등록 시 봉투 암호화 저장.

        절차:
        1. Cloud KMS → DEK 동적 생성
        2. DEK로 plaintext_creds 암호화 → 암호문
        3. KMS로 DEK 재암호화 → encrypted_dek
        4. {암호문 + encrypted_dek + kms_key_name} → Secret Manager 저장
        5. Ops DB에는 secret_name 참조값만 저장 (평문 0)
        """
        try:
            from cryptography.fernet import Fernet
            from google.cloud import kms, secretmanager

            dek = Fernet.generate_key()
            f = Fernet(dek)
            ciphertext = base64.b64encode(
                f.encrypt(json.dumps(plaintext_creds).encode())
            ).decode()

            kms_client = kms.KeyManagementServiceClient()
            encrypt_response = kms_client.encrypt(
                name=kms_key_name,
                plaintext=base64.b64decode(dek)[:32],
            )
            encrypted_dek = base64.b64encode(
                encrypt_response.ciphertext
            ).decode()

            secret_payload = json.dumps({
                "ciphertext": ciphertext,
                "encrypted_dek": encrypted_dek,
                "kms_key_name": kms_key_name,
            })

            sm_client = secretmanager.SecretManagerServiceClient()
            secret_name = f"{env}/tenant-{tenant_id}/{erp_system}/login"
            parent = f"projects/{project_id}"

            sm_client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_name.replace("/", "-"),
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            sm_client.add_secret_version(
                request={
                    "parent": f"{parent}/secrets/{secret_name.replace('/', '-')}",
                    "payload": {"data": secret_payload.encode()},
                }
            )
            logger.info(
                f"[CRED] 봉투 암호화 저장 완료: "
                f"tenant={tenant_id} erp={erp_system}"
            )

        except Exception as e:
            logger.error(f"[CRED] 저장 실패: {e}")
            raise
