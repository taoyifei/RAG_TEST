"""管理员 Cookie 会话与作用域 API Token。"""

from __future__ import annotations

import hmac
import json
import secrets
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Row

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import PolicyDenied
from rag_app.product.crypto import SecretCipher
from rag_app.product.models import AccessTokenIssue, AccessTokenSummary

SESSION_COOKIE = "rag_console_session"
_SESSION_PREFIX = "rags_"
_ACCESS_KEY_PREFIX = "ragk_"
_MIN_BOOTSTRAP_LENGTH = 16
_MAX_SECRET_LENGTH = 4096
_MIN_SESSION_TTL = 60
_MAX_SESSION_TTL = 86_400
_MAX_TOKEN_NAME_LENGTH = 200
_LOGIN_WINDOW_SECONDS = 60
_MAX_LOGIN_FAILURES = 5
_ALLOWED_SCOPES = frozenset(
    {"query:read", "knowledge:read", "knowledge:write", "system:read"}
)


def load_bootstrap_token(path: str | Path) -> str:
    """读取 0600、非 symlink 的 Bootstrap Secret。

    Args:
        path: 部署侧 Secret 文件。

    Returns:
        去除行尾后的 Bootstrap Secret。

    Raises:
        ValueError: 文件类型、权限或长度不安全。

    """
    token_path = Path(path)
    if token_path.is_symlink() or not token_path.is_file():
        raise ValueError("Bootstrap Token 必须是非 symlink 普通文件。")
    if stat.S_IMODE(token_path.stat().st_mode) != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError("Bootstrap Token 文件权限必须严格为 0600。")
    token = token_path.read_text(encoding="utf-8").strip()
    if not _MIN_BOOTSTRAP_LENGTH <= len(token) <= _MAX_SECRET_LENGTH:
        raise ValueError("Bootstrap Token 长度必须在 16 到 4096。")
    return token


class AuthStore:
    """持久化 Session 与 API Token 的 keyed HMAC。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        cipher: SecretCipher,
    ) -> None:
        """保存数据库和不可导出的 HMAC 能力。

        Args:
            connections: 已完成 0014 migration 的连接工厂。
            cipher: 从主密钥派生 HMAC 的安全包装器。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._cipher = cipher

    def create_session(self, *, ttl_seconds: int) -> tuple[str, str, str]:
        """创建 Session 与 CSRF Token，只返回一次明文。

        Args:
            ttl_seconds: 会话 TTL 秒数。

        Returns:
            Session ID、Session Token 与 CSRF Token。

        """
        if not _MIN_SESSION_TTL <= ttl_seconds <= _MAX_SESSION_TTL:
            raise ValueError("Session TTL 必须在 60 秒到 24 小时。")
        session_id = _identifier("sess")
        session_token = f"{_SESSION_PREFIX}{secrets.token_urlsafe(32)}"
        csrf_token = secrets.token_urlsafe(32)
        created = datetime.now(UTC)
        expires = created + timedelta(seconds=ttl_seconds)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO console_sessions("
                "session_id, token_hash, csrf_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    self._cipher.token_digest(session_token),
                    self._cipher.token_digest(csrf_token),
                    created.isoformat(),
                    expires.isoformat(),
                ),
            )
        return session_id, session_token, csrf_token

    def validate_session(
        self,
        session_token: str,
        *,
        csrf_token: str | None = None,
    ) -> str:
        """验证 Session、TTL、吊销状态和可选 CSRF。

        Args:
            session_token: HttpOnly Cookie 中的随机 Token。
            csrf_token: 写操作必须提供的请求头 Token。

        Returns:
            匹配的 Session ID。

        Raises:
            PolicyDenied: Session 无效、过期、吊销或 CSRF 不匹配。

        """
        digest = self._cipher.token_digest(session_token)
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM console_sessions WHERE token_hash=?",
                (digest,),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise PolicyDenied("管理员会话无效。", stage="console.session")
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(UTC):
            raise PolicyDenied("管理员会话已过期。", stage="console.session")
        if csrf_token is not None and not hmac.compare_digest(
            self._cipher.token_digest(csrf_token), str(row["csrf_hash"])
        ):
            raise PolicyDenied("管理员会话验证失败。", stage="console.csrf")
        return str(row["session_id"])

    def revoke_session(self, session_token: str) -> None:
        """吊销当前 Session，重复调用保持幂等。

        Args:
            session_token: Cookie 中的 Session Token。

        Returns:
            无返回值。

        """
        digest = self._cipher.token_digest(session_token)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE console_sessions SET revoked_at=? "
                "WHERE token_hash=? AND revoked_at IS NULL",
                (_now(), digest),
            )

    def rotate_session(
        self,
        session_token: str,
        csrf_token: str,
        *,
        ttl_seconds: int,
    ) -> tuple[str, str, str]:
        """验证并轮换会话，使旧 Cookie 立即失效。

        Args:
            session_token: 当前 HttpOnly Cookie Token。
            csrf_token: 当前会话 CSRF Token。
            ttl_seconds: 新会话 TTL。

        Returns:
            新 Session ID、Cookie Token 与 CSRF Token。

        """
        session_id = self.validate_session(
            session_token,
            csrf_token=csrf_token,
        )
        return self._replace_session(session_id, ttl_seconds=ttl_seconds)

    def resume_session(
        self,
        session_token: str,
        *,
        ttl_seconds: int,
    ) -> tuple[str, str, str]:
        """由同源安全 GET 恢复并轮换内存中的 CSRF Token。

        Args:
            session_token: 当前 HttpOnly Cookie Token。
            ttl_seconds: 新会话 TTL。

        Returns:
            新 Session ID、Cookie Token 与 CSRF Token。

        """
        session_id = self.validate_session(session_token)
        return self._replace_session(session_id, ttl_seconds=ttl_seconds)

    def _replace_session(
        self,
        session_id: str,
        *,
        ttl_seconds: int,
    ) -> tuple[str, str, str]:
        replacement = self.create_session(ttl_seconds=ttl_seconds)
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE console_sessions SET rotated_at=?, revoked_at=? "
                "WHERE session_id=? AND revoked_at IS NULL",
                (now, now, session_id),
            )
        return replacement

    def create_access_token(
        self,
        *,
        name: str,
        scopes: tuple[str, ...],
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        expires_at: str | None = None,
    ) -> AccessTokenIssue:
        """创建仅显示一次的外部 API Token。

        Args:
            name: Token 显示名。
            scopes: 受控作用域集合。
            project_id: 可选项目约束。
            knowledge_base_id: 可选知识库约束。
            expires_at: 可选 UTC ISO 过期时间。

        Returns:
            含一次性明文的创建响应。

        Raises:
            ValueError: Scope、名称、层级或过期时间无效。

        """
        normalized = tuple(sorted(set(scopes)))
        if not name.strip() or len(name) > _MAX_TOKEN_NAME_LENGTH:
            raise ValueError("Token 名称必须为 1 到 200 个字符。")
        if not normalized or any(
            item not in _ALLOWED_SCOPES for item in normalized
        ):
            raise ValueError("API Token Scope 不受支持。")
        if knowledge_base_id is not None and project_id is None:
            raise ValueError("知识库级 Token 必须同时绑定项目。")
        if expires_at is not None:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError("Token 过期时间必须是未来的带时区时间。")
        token_id = _identifier("tok")
        token = f"{_ACCESS_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        created_at = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO api_access_tokens("
                "token_id, name, token_hash, scopes_json, project_id, "
                "knowledge_base_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    name.strip(),
                    self._cipher.token_digest(token),
                    json.dumps(normalized, separators=(",", ":")),
                    project_id,
                    knowledge_base_id,
                    expires_at,
                    created_at,
                ),
            )
        summary = self.get_access_token(token_id)
        return AccessTokenIssue(**summary.model_dump(), token=token)

    def get_access_token(self, token_id: str) -> AccessTokenSummary:
        """按 ID 读取不含完整 Token 的摘要。

        Args:
            token_id: 目标 Token ID。

        Returns:
            安全 Token 摘要。

        Raises:
            PolicyDenied: Token 不存在。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM api_access_tokens WHERE token_id=?", (token_id,)
            ).fetchone()
        if row is None:
            raise PolicyDenied("接口访问 Token 不存在。", stage="token.read")
        return _token_summary(row)

    def list_access_tokens(self) -> tuple[AccessTokenSummary, ...]:
        """列出不含完整值的 Token 摘要。

        Args:
            无参数；读取当前数据库。

        Returns:
            按创建时间倒序的摘要。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM api_access_tokens ORDER BY created_at DESC"
            ).fetchall()
        return tuple(_token_summary(row) for row in rows)

    def revoke_access_token(self, token_id: str) -> AccessTokenSummary:
        """吊销 API Token，完整值无法恢复。

        Args:
            token_id: 目标 Token ID。

        Returns:
            已吊销的摘要。

        """
        self.get_access_token(token_id)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE api_access_tokens SET "
                "revoked_at=COALESCE(revoked_at, ?) "
                "WHERE token_id=?",
                (_now(), token_id),
            )
        return self.get_access_token(token_id)

    def authorize_access_token(
        self,
        token: str,
        *,
        required_scope: str,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> AccessTokenSummary:
        """验证外部 Bearer Token 的 Scope、资源和生命周期。

        Args:
            token: 完整 Bearer Token。
            required_scope: 当前路由要求的作用域。
            project_id: 路由中的可选项目 ID。
            knowledge_base_id: 路由中的可选知识库 ID。

        Returns:
            授权后的安全 Token 摘要。

        Raises:
            PolicyDenied: Token 无效、过期、吊销或越权。

        """
        digest = self._cipher.token_digest(token)
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM api_access_tokens WHERE token_hash=?", (digest,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise PolicyDenied("接口访问 Token 无效。", stage="token.auth")
            summary = _token_summary(row)
            if summary.expires_at is not None and datetime.fromisoformat(
                summary.expires_at
            ) <= datetime.now(UTC):
                raise PolicyDenied(
                    "接口访问 Token 已过期。", stage="token.auth"
                )
            if required_scope not in summary.scopes:
                raise PolicyDenied(
                    "接口访问 Token Scope 不足。", stage="token.scope"
                )
            if (
                summary.project_id is not None
                and summary.project_id != project_id
            ):
                raise PolicyDenied(
                    "接口访问 Token 项目范围不匹配。", stage="token.scope"
                )
            if (
                summary.knowledge_base_id is not None
                and summary.knowledge_base_id != knowledge_base_id
            ):
                raise PolicyDenied(
                    "接口访问 Token 知识库范围不匹配。", stage="token.scope"
                )
            used_at = _now()
            connection.execute(
                "UPDATE api_access_tokens SET last_used_at=? WHERE token_id=?",
                (used_at, summary.token_id),
            )
        return summary.model_copy(update={"last_used_at": used_at})


class ConsoleSessionService:
    """验证 Bootstrap Secret 并限制登录频率。"""

    def __init__(
        self,
        store: AuthStore,
        bootstrap_token: str,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        """保存会话策略与部署侧 Bootstrap Secret。

        Args:
            store: Session 持久化 Store。
            bootstrap_token: 只在进程内保留的部署 Secret。
            ttl_seconds: 会话 TTL。

        Returns:
            无返回值。

        """
        self._store = store
        self._bootstrap_token = bootstrap_token
        self._ttl_seconds = ttl_seconds
        self._attempts: dict[str, list[float]] = {}

    @property
    def ttl_seconds(self) -> int:
        """返回会话 TTL。

        Args:
            无参数；读取当前会话策略。

        Returns:
            TTL 秒数。

        """
        return self._ttl_seconds

    def login(self, token: str, client_key: str) -> tuple[str, str, str]:
        """有限速地交换 Bootstrap Secret 为 Cookie Session。

        Args:
            token: 页面首次输入的 Bootstrap Secret。
            client_key: 不持久化的客户端速率限制键。

        Returns:
            Session ID、Cookie Token 与 CSRF Token。

        Raises:
            PolicyDenied: 速率超限或 Bootstrap Secret 错误。

        """
        now = time.monotonic()
        attempts = [
            value
            for value in self._attempts.get(client_key, [])
            if now - value < _LOGIN_WINDOW_SECONDS
        ]
        if len(attempts) >= _MAX_LOGIN_FAILURES:
            raise PolicyDenied("管理员登录暂不可用。", stage="console.login")
        if not hmac.compare_digest(token, self._bootstrap_token):
            attempts.append(now)
            self._attempts[client_key] = attempts
            raise PolicyDenied("管理员登录失败。", stage="console.login")
        self._attempts.pop(client_key, None)
        return self._store.create_session(ttl_seconds=self._ttl_seconds)

    def rotate(
        self,
        session_token: str,
        csrf_token: str,
    ) -> tuple[str, str, str]:
        """轮换已通过认证的浏览器会话。

        Args:
            session_token: 当前 HttpOnly Cookie Token。
            csrf_token: 当前 CSRF Token。

        Returns:
            新 Session ID、Cookie Token 与 CSRF Token。

        """
        return self._store.rotate_session(
            session_token,
            csrf_token,
            ttl_seconds=self._ttl_seconds,
        )

    def resume(self, session_token: str) -> tuple[str, str, str]:
        """恢复刷新后的同源页面会话并轮换 Cookie。

        Args:
            session_token: 当前 HttpOnly Cookie Token。

        Returns:
            新 Session ID、Cookie Token 与 CSRF Token。

        """
        return self._store.resume_session(
            session_token,
            ttl_seconds=self._ttl_seconds,
        )


def _token_summary(row: Row) -> AccessTokenSummary:
    return AccessTokenSummary(
        token_id=str(row["token_id"]),
        name=str(row["name"]),
        scopes=tuple(json.loads(str(row["scopes_json"]))),
        project_id=None
        if row["project_id"] is None
        else str(row["project_id"]),
        knowledge_base_id=(
            None
            if row["knowledge_base_id"] is None
            else str(row["knowledge_base_id"])
        ),
        expires_at=None
        if row["expires_at"] is None
        else str(row["expires_at"]),
        created_at=str(row["created_at"]),
        last_used_at=(
            None if row["last_used_at"] is None else str(row["last_used_at"])
        ),
        revoked_at=None
        if row["revoked_at"] is None
        else str(row["revoked_at"]),
    )


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "SESSION_COOKIE",
    "AuthStore",
    "ConsoleSessionService",
    "load_bootstrap_token",
]
