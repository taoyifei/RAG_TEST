"""显式受控候选重放的私有模型草稿记录器。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from threading import Lock
from typing import Final, Self

from rag_app.core.models import AnswerDraft
from rag_app.core.ports.generator import GenerationRequest

_CAPTURE_DIRECTORY_ENV: Final = "RAG_PRIVATE_REPLAY_CAPTURE_DIR"
_CAPTURE_ACK_ENV: Final = "RAG_PRIVATE_REPLAY_CAPTURE_ACK"
_CAPTURE_LIMIT_ENV: Final = "RAG_PRIVATE_REPLAY_CAPTURE_LIMIT"
_CAPTURE_ACK: Final = "private-replay-v1"
_OUTPUT_NAME: Final = "raw-generation-drafts.ndjson"
_MAX_CAPTURE_LIMIT: Final = 32
_MAX_RECORD_BYTES: Final = 2 * 1024 * 1024


class PrivateReplayDraftRecorder:
    """把模型实际草稿写入仓库外的所有者专用目录。"""

    def __init__(self, directory: Path, *, capture_limit: int) -> None:
        """校验受控目录并准备一个空的私有 NDJSON 文件。

        Args:
            directory: 已存在、仅所有者可访问的绝对目录。
            capture_limit: 本次候选进程最多允许记录的模型草稿数。

        Raises:
            ValueError: 目录、权限、所有者、上限或旧输出不满足合同。

        """
        self.directory = _validated_private_directory(directory)
        if not 1 <= capture_limit <= _MAX_CAPTURE_LIMIT:
            raise ValueError("PRIVATE_REPLAY_CAPTURE_LIMIT_INVALID")
        self.capture_limit = capture_limit
        self.output_path = self.directory / _OUTPUT_NAME
        _prepare_private_output(self.output_path)
        self._captured = 0
        self._lock = Lock()

    @classmethod
    def from_environment(cls) -> Self | None:
        """仅在三个显式环境变量同时配置时启用私有记录。

        Returns:
            未配置时返回 ``None``；配置完整时返回受控记录器。

        Raises:
            ValueError: 只配置部分变量、确认值错误或上限不是整数。

        """
        directory = os.environ.get(_CAPTURE_DIRECTORY_ENV)
        acknowledgement = os.environ.get(_CAPTURE_ACK_ENV)
        raw_limit = os.environ.get(_CAPTURE_LIMIT_ENV)
        configured = (directory, acknowledgement, raw_limit)
        if not any(configured):
            return None
        if not all(configured):
            raise ValueError("PRIVATE_REPLAY_CONFIGURATION_INCOMPLETE")
        if acknowledgement != _CAPTURE_ACK:
            raise ValueError("PRIVATE_REPLAY_ACK_INVALID")
        try:
            capture_limit = int(raw_limit or "")
        except ValueError as error:
            raise ValueError("PRIVATE_REPLAY_CAPTURE_LIMIT_INVALID") from error
        return cls(Path(directory or ""), capture_limit=capture_limit)

    def record(self, request: GenerationRequest, draft: AnswerDraft) -> int:
        """原样记录模型请求、NaturalClaim、Quote 与来源跨度。

        Args:
            request: 实际交给回答模型的有界生成请求。
            draft: Provider 返回并完成结构解析、尚未业务校验的草稿。

        Returns:
            本进程内从 1 开始的记录序号。

        Raises:
            RuntimeError: 超过显式记录上限或单条记录过大。

        """
        with self._lock:
            if self._captured >= self.capture_limit:
                raise RuntimeError("PRIVATE_REPLAY_CAPTURE_LIMIT_REACHED")
            sequence = self._captured + 1
            payload = {
                "schema_version": "private-grounded-draft-v1",
                "sequence": sequence,
                "query_sha256": hashlib.sha256(
                    request.query.encode("utf-8")
                ).hexdigest(),
                "request": request.model_dump(mode="json"),
                "draft": draft.model_dump(mode="json"),
            }
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            if len(encoded) > _MAX_RECORD_BYTES:
                raise RuntimeError("PRIVATE_REPLAY_RECORD_TOO_LARGE")
            _append_private_record(self.output_path, encoded)
            self._captured = sequence
            return sequence


def _validated_private_directory(directory: Path) -> Path:
    """拒绝相对路径、符号链接、错误所有者和开放权限。"""
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError("PRIVATE_REPLAY_DIRECTORY_INVALID")
    try:
        resolved = directory.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("PRIVATE_REPLAY_DIRECTORY_NOT_FOUND") from error
    metadata = resolved.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("PRIVATE_REPLAY_DIRECTORY_INVALID")
    if metadata.st_uid != os.getuid():
        raise ValueError("PRIVATE_REPLAY_DIRECTORY_OWNER_MISMATCH")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("PRIVATE_REPLAY_DIRECTORY_PERMISSIONS")
    return resolved


def _prepare_private_output(path: Path) -> None:
    """创建新输出；只允许复用同所有者、权限正确的空文件。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ValueError("PRIVATE_REPLAY_OUTPUT_INVALID") from None
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("PRIVATE_REPLAY_OUTPUT_INVALID") from None
        if metadata.st_size:
            raise ValueError("PRIVATE_REPLAY_OUTPUT_NOT_EMPTY") from None
        return
    os.close(descriptor)
    path.chmod(0o600)


def _append_private_record(path: Path, encoded: bytes) -> None:
    """在进程锁内追加一条完整记录并强制落盘。"""
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["PrivateReplayDraftRecorder"]
