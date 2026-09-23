"""显式开启、严格有界且不进入普通 Trace 的 Provider 私有诊断。"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ACK_VALUE = "private-provider-diagnostic-v1"
_FILE_PREFIX = "provider-http-private-"
_MAX_RECORD_BYTES = 192 * 1024
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_DEFAULT_MAX_FILES = 32
_DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
_MAX_FILES = 128
_MIN_RETENTION_SECONDS = 60
_MAX_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SECRET_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "set-cookie"}
)


@dataclass(frozen=True, slots=True)
class PrivateHttpDiagnostic:
    """一次受控 Provider HTTP 入出的有界私有诊断输入。"""

    request_id: str
    attempt_id: str
    operation: str
    method: str
    endpoint: str
    request_headers: Mapping[str, str]
    request_payload: object
    response_status: int
    response_headers: Mapping[str, str]
    response_body: bytes
    response_truncated: bool


class PrivateProviderDiagnosticRecorder:
    """仅在双重显式配置后写入 Linux 私有目录。"""

    def __init__(
        self,
        directory: Path,
        *,
        max_files: int = _DEFAULT_MAX_FILES,
        retention_seconds: int = _DEFAULT_RETENTION_SECONDS,
        capture_success: bool = False,
    ) -> None:
        """校验诊断目录和固定保留上限。

        Args:
            directory: 必须已存在、非符号链接且权限为 ``0700`` 的目录。
            max_files: 目录内最多保留的本模块诊断文件数。
            retention_seconds: 本模块文件的最长保留时间。
            capture_success: 是否额外记录聊天模型成功响应，仅用于受控复查。

        Raises:
            ValueError: 路径、权限或上限不符合私有诊断合同。

        """
        resolved = directory.expanduser()
        if not resolved.is_absolute() or resolved.is_symlink():
            raise ValueError("私有诊断目录必须是绝对路径且不能是符号链接。")
        resolved = resolved.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("私有诊断路径必须是已有目录。")
        if _inside_git_worktree(resolved):
            raise ValueError("私有诊断目录不能位于 Git worktree 内。")
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if os.name == "posix" and mode != _PRIVATE_DIRECTORY_MODE:
            raise ValueError("Linux 私有诊断目录权限必须为 0700。")
        if not 1 <= max_files <= _MAX_FILES:
            raise ValueError("私有诊断文件上限必须位于 1 到 128。")
        if (
            not _MIN_RETENTION_SECONDS
            <= retention_seconds
            <= _MAX_RETENTION_SECONDS
        ):
            raise ValueError("私有诊断保留时间必须位于 60 秒到 7 天。")
        self._directory = resolved
        self._max_files = max_files
        self._retention_seconds = retention_seconds
        self.capture_success = capture_success

    @classmethod
    def from_environment(cls) -> PrivateProviderDiagnosticRecorder | None:
        """从显式目录与确认口令构造；任一缺失即保持关闭。"""
        directory = os.environ.get("RAG_PRIVATE_PROVIDER_DIAGNOSTIC_DIR", "")
        acknowledgement = os.environ.get(
            "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_ACK", ""
        )
        success_setting = os.environ.get(
            "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_CAPTURE_SUCCESS", "false"
        )
        if success_setting not in {"true", "false"}:
            raise ValueError("私有 Provider 成功捕获开关无效。")
        if not directory and not acknowledgement:
            if success_setting == "true":
                raise ValueError("私有 Provider 成功捕获缺少受控目录。")
            return None
        if acknowledgement != _ACK_VALUE or not directory:
            raise ValueError("私有 Provider 诊断配置不完整或确认口令无效。")
        return cls(
            Path(directory),
            max_files=_bounded_env_int(
                "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_MAX_FILES",
                _DEFAULT_MAX_FILES,
                minimum=1,
                maximum=_MAX_FILES,
            ),
            retention_seconds=_bounded_env_int(
                "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_RETENTION_SECONDS",
                _DEFAULT_RETENTION_SECONDS,
                minimum=_MIN_RETENTION_SECONDS,
                maximum=_MAX_RETENTION_SECONDS,
            ),
            capture_success=success_setting == "true",
        )

    def record(self, diagnostic: PrivateHttpDiagnostic) -> Path:
        """以 ``0600`` 原子写入一个有界记录并执行精确保留策略。"""
        now = int(time.time())
        self._remove_expired(now)
        files = self._files()
        while len(files) >= self._max_files:
            files[0].unlink(missing_ok=True)
            files = files[1:]
        request_bytes = _json_bytes(diagnostic.request_payload)
        request_truncated = len(request_bytes) > _MAX_REQUEST_BYTES
        request_bytes = request_bytes[:_MAX_REQUEST_BYTES]
        response_bytes = diagnostic.response_body[:_MAX_RESPONSE_BYTES]
        response_truncated = (
            diagnostic.response_truncated
            or len(diagnostic.response_body) > _MAX_RESPONSE_BYTES
        )
        record = {
            "record_revision": "private-provider-http-v1",
            "created_unix": now,
            "request_id": diagnostic.request_id,
            "attempt_id": diagnostic.attempt_id,
            "operation": diagnostic.operation,
            "method": diagnostic.method,
            "endpoint": diagnostic.endpoint,
            "request_headers": _redacted_headers(diagnostic.request_headers),
            "request_payload_base64": base64.b64encode(request_bytes).decode(
                "ascii"
            ),
            "request_truncated": request_truncated,
            "response_status": diagnostic.response_status,
            "response_headers": _redacted_headers(diagnostic.response_headers),
            "response_body_base64": base64.b64encode(response_bytes).decode(
                "ascii"
            ),
            "response_truncated": response_truncated,
        }
        encoded = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("私有 Provider 诊断记录超过固定大小上限。")
        target = self._directory / (
            f"{_FILE_PREFIX}{now}-{diagnostic.attempt_id}.json"
        )
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _PRIVATE_FILE_MODE,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if (
            os.name == "posix"
            and stat.S_IMODE(target.stat().st_mode) != _PRIVATE_FILE_MODE
        ):
            target.unlink(missing_ok=True)
            raise ValueError("私有 Provider 诊断文件权限不是 0600。")
        return target

    def _files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self._directory.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.name.startswith(_FILE_PREFIX)
                and path.suffix == ".json"
            ),
            key=lambda path: path.stat().st_mtime,
        )

    def _remove_expired(self, now: int) -> None:
        cutoff = now - self._retention_seconds
        for path in self._files():
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def _inside_git_worktree(directory: Path) -> bool:
    """拒绝位于任一含 ``.git`` 标记祖先下的目录。"""
    return any(
        (parent / ".git").exists() for parent in (directory, *directory.parents)
    )


def _redacted_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """保留有界结构头，认证和 Cookie 永远只写 REDACTED。"""
    safe: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.casefold()
        if normalized in _SECRET_HEADERS:
            safe[key] = "REDACTED"
        elif normalized in {"content-type", "accept", "user-agent"}:
            safe[key] = value[:256]
    return safe


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _bounded_env_int(
    name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 超出允许范围。")
    return value
