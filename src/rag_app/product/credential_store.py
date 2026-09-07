"""Provider Credential 的 SQLite 持久化与最小解密边界。"""

from __future__ import annotations

import os
import re
import secrets
from datetime import UTC, datetime
from sqlite3 import Row
from typing import cast

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import ConfigurationError, NotFound
from rag_app.product.catalog import require_provider
from rag_app.product.crypto import SecretAad, SecretCipher
from rag_app.product.models import CredentialSummary

_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_MAX_SECRET_LENGTH = 4096
_MASKED_SUFFIX_LENGTH = 4


class CredentialStore:
    """保存两种 Credential Source，公共读取永不返回 Secret。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        cipher: SecretCipher | None,
    ) -> None:
        """保存数据库连接与可选页面托管加密器。

        Args:
            connections: 已完成 0011 migration 的连接工厂。
            cipher: 配置主密钥时提供；环境托管可为 None。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._cipher = cipher

    def create_environment(
        self,
        provider_type: str,
        environment_name: str,
    ) -> CredentialSummary:
        """保存环境变量名，不读取或复制变量值。

        Args:
            provider_type: 内置 Provider 类型。
            environment_name: 部署环境中的变量名。

        Returns:
            安全 Credential 摘要。

        Raises:
            ValueError: Provider 或变量名不在受控范围。

        """
        require_provider(provider_type)
        if _ENVIRONMENT_NAME.fullmatch(environment_name) is None:
            raise ValueError("环境变量名必须使用大写字母、数字和下划线。")
        credential_id = _identifier("cred")
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO provider_credentials("
                "credential_id, provider_type, encrypted_payload, nonce, "
                "aad_version, key_id, key_version, masked_hint, source, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, '1', NULL, 1, ?, "
                "'environment_managed', 'configured', ?, ?)",
                (
                    credential_id,
                    provider_type,
                    environment_name,
                    "由部署环境托管",
                    now,
                    now,
                ),
            )
        return self.get(credential_id)

    def create_encrypted(
        self,
        provider_type: str,
        secret_value: str,
    ) -> CredentialSummary:
        """以 AES-256-GCM 保存页面托管的 API Key。

        Args:
            provider_type: 内置 Provider 类型。
            secret_value: 待加密的 API Key。

        Returns:
            不含密文或 Secret 的摘要。

        Raises:
            ConfigurationError: 服务未配置主密钥。
            ValueError: Provider 或 Secret 无效。

        """
        require_provider(provider_type)
        cipher = self._require_cipher()
        if not secret_value or len(secret_value) > _MAX_SECRET_LENGTH:
            raise ValueError("API Key 必须为 1 到 4096 个字符。")
        credential_id = _identifier("cred")
        key_version = 1
        ciphertext, nonce = cipher.encrypt(
            secret_value,
            aad=SecretAad(
                credential_id=credential_id,
                provider_type=provider_type,
                field_name="api_key",
                key_version=key_version,
            ),
        )
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO provider_credentials("
                "credential_id, provider_type, encrypted_payload, nonce, "
                "aad_version, key_id, key_version, masked_hint, source, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '1', ?, ?, ?, "
                "'database_encrypted', 'configured', ?, ?)",
                (
                    credential_id,
                    provider_type,
                    ciphertext,
                    nonce,
                    cipher.key_id,
                    key_version,
                    _masked_hint(secret_value),
                    now,
                    now,
                ),
            )
        return self.get(credential_id)

    def remove_new_orphan(self, credential_id: str) -> None:
        """补偿组合创建流程刚生成的孤立 Credential。

        Args:
            credential_id: 仅限当前创建调用刚生成的 ID，不接受外部删除请求。

        Returns:
            无返回值；已被引用的凭据保持不变。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM provider_credentials WHERE credential_id=? "
                "AND NOT EXISTS (SELECT 1 FROM provider_connections "
                "WHERE credential_id=?)",
                (credential_id, credential_id),
            )

    def rotate(
        self,
        credential_id: str,
        secret_value: str,
    ) -> CredentialSummary:
        """原子轮换数据库托管密钥并增加版本。

        Args:
            credential_id: 目标 Credential ID。
            secret_value: 新 API Key。

        Returns:
            更新后的安全摘要。

        Raises:
            ConfigurationError: 目标不是数据库托管或主密钥缺失。
            NotFound: Credential 不存在。

        """
        row = self._row(credential_id)
        if str(row["source"]) != "database_encrypted":
            raise ConfigurationError(
                "环境托管凭据必须在部署环境中轮换。",
                stage="credential.rotate",
            )
        cipher = self._require_cipher()
        key_version = int(row["key_version"]) + 1
        provider_type = str(row["provider_type"])
        ciphertext, nonce = cipher.encrypt(
            secret_value,
            aad=SecretAad(
                credential_id=credential_id,
                provider_type=provider_type,
                field_name="api_key",
                key_version=key_version,
            ),
        )
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE provider_credentials SET encrypted_payload=?, "
                "nonce=?, key_id=?, key_version=?, masked_hint=?, "
                "status='configured', updated_at=?, rotated_at=?, "
                "disabled_at=NULL WHERE credential_id=?",
                (
                    ciphertext,
                    nonce,
                    cipher.key_id,
                    key_version,
                    _masked_hint(secret_value),
                    now,
                    now,
                    credential_id,
                ),
            )
        return self.get(credential_id)

    def get(self, credential_id: str) -> CredentialSummary:
        """读取不含 Secret、密文与 nonce 的安全摘要。

        Args:
            credential_id: 目标 Credential ID。

        Returns:
            安全 Credential 摘要。

        Raises:
            NotFound: Credential 不存在。

        """
        return self._summary(self._row(credential_id))

    def list(self) -> tuple[CredentialSummary, ...]:
        """列出全部安全 Credential 摘要。

        Args:
            无参数；读取当前数据库。

        Returns:
            按创建时间和 ID 排序的摘要。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_credentials "
                "ORDER BY created_at, credential_id"
            ).fetchall()
        return tuple(self._summary(row) for row in rows)

    def resolve(self, credential_id: str) -> tuple[str, int]:
        """仅在 Provider 调用边界解析 Secret 与版本。

        Args:
            credential_id: 目标 Credential ID。

        Returns:
            Secret 明文与缓存失效使用的版本。

        Raises:
            ConfigurationError: Credential 禁用、环境缺失或主密钥缺失。

        """
        row = self._row(credential_id)
        if str(row["status"]) != "configured":
            raise ConfigurationError(
                "Provider Credential 已禁用。", stage="credential.resolve"
            )
        key_version = int(row["key_version"])
        if str(row["source"]) == "environment_managed":
            environment_name = str(row["encrypted_payload"])
            value = os.environ.get(environment_name)
            if not value:
                raise ConfigurationError(
                    "部署环境尚未配置 Provider Credential。",
                    stage="credential.resolve",
                )
            return value, key_version
        cipher = self._require_cipher()
        nonce = row["nonce"]
        if nonce is None:
            raise ConfigurationError(
                "加密 Credential 缺少 nonce。", stage="credential.resolve"
            )
        return (
            cipher.decrypt(
                str(row["encrypted_payload"]),
                str(nonce),
                aad=SecretAad(
                    credential_id=credential_id,
                    provider_type=str(row["provider_type"]),
                    field_name="api_key",
                    key_version=key_version,
                ),
            ),
            key_version,
        )

    def _row(self, credential_id: str) -> Row:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise NotFound(
                "Provider Credential 不存在。", stage="credential.read"
            )
        return cast(Row, row)

    def _summary(self, row: Row) -> CredentialSummary:
        source = str(row["source"])
        configured = str(row["status"]) == "configured"
        if source == "environment_managed":
            configured = (
                configured and str(row["encrypted_payload"]) in os.environ
            )
        return CredentialSummary(
            credential_id=str(row["credential_id"]),
            provider_type=str(row["provider_type"]),
            configured=configured,
            source=source,
            masked_hint=str(row["masked_hint"]),
            key_version=int(row["key_version"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _require_cipher(self) -> SecretCipher:
        if self._cipher is None:
            raise ConfigurationError(
                "页面托管 Secret 需要 RAG_MASTER_KEY_FILE。",
                stage="credential.master_key",
            )
        return self._cipher


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _masked_hint(value: str) -> str:
    visible = (
        value[-_MASKED_SUFFIX_LENGTH:]
        if len(value) >= _MASKED_SUFFIX_LENGTH
        else "已配置"
    )
    return f"••••{visible}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["CredentialStore"]
