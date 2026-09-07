"""主密钥文件、AES-256-GCM 与安全 Token 摘要。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from rag_app.core.identifiers import canonical_json

_MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12
_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass(frozen=True, slots=True, repr=False)
class MasterKey:
    """仅在进程内持有的 32 字节主密钥。"""

    value: bytes
    key_id: str

    def __repr__(self) -> str:
        """返回不含密钥的安全表示。

        Args:
            无参数；读取当前对象。

        Returns:
            仅含 Key ID 的字符串。

        """
        return f"MasterKey(key_id={self.key_id!r})"


@dataclass(frozen=True, slots=True)
class SecretAad:
    """Credential 加密必须绑定的四个 AAD 字段。"""

    credential_id: str
    provider_type: str
    field_name: str
    key_version: int

    def encode(self) -> bytes:
        """返回稳定 JSON 字节。

        Args:
            无参数；读取当前 AAD。

        Returns:
            字段排序后的 UTF-8 JSON。

        """
        return canonical_json(
            {
                "credential_id": self.credential_id,
                "field_name": self.field_name,
                "key_version": self.key_version,
                "provider_type": self.provider_type,
            }
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProductSecretBundle:
    """首次容器启动需要的 Secret 文件安全摘要。"""

    directory: Path
    master_key_id: str
    bootstrap_token_id: str
    qdrant_api_key_id: str


def load_master_key(path: str | Path) -> MasterKey:
    """从 0600、非 symlink 普通文件读取主密钥。

    Args:
        path: 独立主密钥文件路径。

    Returns:
        进程内主密钥与稳定 Key ID。

    Raises:
        ValueError: 路径、权限或密钥长度不安全。

    """
    resolved = Path(path)
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("主密钥必须是现有非 symlink 普通文件。")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError("主密钥文件权限必须严格为 0600。")
    value = resolved.read_bytes()
    if len(value) != _MASTER_KEY_BYTES:
        raise ValueError("主密钥必须正好是 32 bytes。")
    return MasterKey(value=value, key_id=_key_id(value))


def initialize_master_key(path: str | Path) -> MasterKey:
    """以排他方式创建一个新的 0600 主密钥文件。

    Args:
        path: 调用方控制的新文件路径。

    Returns:
        新主密钥的进程内摘要。

    Raises:
        FileExistsError: 目标已经存在。
        ValueError: 父目录或目标路径不安全。

    """
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FileExistsError("目标主密钥文件已存在。")
    parent = target.parent.resolve(strict=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("主密钥父目录必须是非 symlink 目录。")
    value = os.urandom(_MASTER_KEY_BYTES)
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _PRIVATE_FILE_MODE,
    )
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    target.chmod(_PRIVATE_FILE_MODE)
    return MasterKey(value=value, key_id=_key_id(value))


def initialize_product_secret_bundle(
    directory: str | Path,
) -> ProductSecretBundle:
    """排他创建容器运行所需的三个 Secret 与 Qdrant 配置。

    Args:
        directory: 已存在、非 symlink 且仅由部署管理员控制的目录。

    Returns:
        只含路径和不可逆 SHA256 标识的初始化摘要。

    Raises:
        FileExistsError: 任一目标文件已经存在。
        ValueError: 目录不是安全的现有目录。

    """
    target = Path(directory)
    if target.is_symlink() or not target.is_dir():
        raise ValueError("Secret 目录必须是现有非 symlink 目录。")
    resolved = target.resolve(strict=True)
    paths = {
        "master": resolved / "master-key",
        "bootstrap": resolved / "admin-bootstrap-token",
        "qdrant": resolved / "qdrant-api-key",
        "config": resolved / "qdrant.yaml",
    }
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise FileExistsError("Secret bundle 目标文件已经存在。")

    bootstrap = secrets.token_urlsafe(32)
    qdrant_api_key = secrets.token_urlsafe(32)
    created: list[Path] = []
    try:
        master_key = initialize_master_key(paths["master"])
        created.append(paths["master"])
        _write_private_text(paths["bootstrap"], bootstrap)
        created.append(paths["bootstrap"])
        _write_private_text(paths["qdrant"], qdrant_api_key)
        created.append(paths["qdrant"])
        qdrant_config = (
            "telemetry_disabled: true\n"
            "service:\n"
            "  host: 0.0.0.0\n"
            "  http_port: 6333\n"
            f"  api_key: {json.dumps(qdrant_api_key)}\n"
        )
        _write_private_text(paths["config"], qdrant_config)
        created.append(paths["config"])
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return ProductSecretBundle(
        directory=resolved,
        master_key_id=master_key.key_id,
        bootstrap_token_id=_secret_id(bootstrap),
        qdrant_api_key_id=_secret_id(qdrant_api_key),
    )


class SecretCipher:
    """使用显式 AAD 的 AES-256-GCM 密钥包装器。"""

    def __init__(self, master_key: MasterKey) -> None:
        """保存 AESGCM 实例而不暴露密钥。

        Args:
            master_key: 已通过文件安全检查的主密钥。

        Returns:
            无返回值。

        """
        self._key_id = master_key.key_id
        self._cipher = AESGCM(master_key.value)
        self._hmac_key = hashlib.sha256(
            b"rag-product-token-hmac-v1\x00" + master_key.value
        ).digest()

    @property
    def key_id(self) -> str:
        """返回不会泄漏主密钥的稳定 ID。

        Args:
            无参数；读取当前 Cipher 身份。

        Returns:
            主密钥的稳定 SHA256 标识。

        """
        return self._key_id

    def encrypt(
        self,
        plaintext: str,
        *,
        aad: SecretAad,
    ) -> tuple[str, str]:
        """使用随机 96-bit nonce 和固定字段 AAD 加密。

        Args:
            plaintext: 只在调用栈内短暂存在的密钥明文。
            aad: Credential ID、Provider、字段与版本组成的 AAD。

        Returns:
            Base64 ciphertext 与 nonce。

        """
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext.encode("utf-8"),
            aad.encode(),
        )
        return (
            base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            base64.urlsafe_b64encode(nonce).decode("ascii"),
        )

    def decrypt(
        self,
        ciphertext: str,
        nonce: str,
        *,
        aad: SecretAad,
    ) -> str:
        """只在 Provider 调用边界解密并校验 AAD。

        Args:
            ciphertext: Base64 AES-GCM ciphertext。
            nonce: Base64 96-bit nonce。
            aad: Credential ID、Provider、字段与版本组成的 AAD。

        Returns:
            已通过完整性校验的明文。

        Raises:
            cryptography.exceptions.InvalidTag: AAD、nonce 或密文错误。

        """
        plaintext = self._cipher.decrypt(
            base64.urlsafe_b64decode(nonce),
            base64.urlsafe_b64decode(ciphertext),
            aad.encode(),
        )
        return plaintext.decode("utf-8")

    def token_digest(self, token: str) -> str:
        """计算数据库可保存的 keyed HMAC。

        Args:
            token: Session、CSRF 或 API Token 明文。

        Returns:
            带算法前缀的 HMAC-SHA256。

        """
        digest = hmac.new(
            self._hmac_key,
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"


def _key_id(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _secret_id(value: str) -> str:
    return _key_id(value.encode("utf-8"))


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _PRIVATE_FILE_MODE,
    )
    try:
        os.write(descriptor, value.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(_PRIVATE_FILE_MODE)


__all__ = [
    "MasterKey",
    "ProductSecretBundle",
    "SecretAad",
    "SecretCipher",
    "initialize_master_key",
    "initialize_product_secret_bundle",
    "load_master_key",
]
