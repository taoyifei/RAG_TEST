"""受限 LibreOffice DOC→DOCX 转换 adapter。"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import BoundedSemaphore
from typing import Protocol

from rag_app.core.errors import JobCancelled
from rag_app.core.policies import ParsingPolicy

_LIBREOFFICE_EXECUTABLE = "/usr/bin/libreoffice"
_LIBREOFFICE_BINARY_VERSION = "25.2.3.2"
_LIBREOFFICE_PACKAGE_VERSION = "4:25.2.3-2+deb13u6"
_CONVERTER_RECIPE = "libreoffice-landlock-seccomp-v2"
_SANDBOX_UNAVAILABLE = 78
_MAX_ADDRESS_SPACE_BYTES = 1536 * 1024 * 1024
_MAX_CONCURRENT_CONVERSIONS = 2
_MAX_WORKSPACE_FILES = 4096
_MAX_WORKSPACE_OVERHEAD_BYTES = 32 * 1024 * 1024
_POLL_SECONDS = 0.05
_CONVERSION_SEMAPHORE = BoundedSemaphore(_MAX_CONCURRENT_CONVERSIONS)


@dataclass(frozen=True, slots=True)
class DocConversion:
    """一次转换的派生 DOCX 与固定配方身份。"""

    content: bytes
    converter: str
    converter_version: str
    recipe: str


@dataclass(frozen=True, slots=True)
class _WorkspaceLimits:
    """一次转换临时工作区的总量边界。"""

    root: Path
    max_files: int
    max_bytes: int


class DocConverter(Protocol):
    """同步、有界且不隐式放宽隔离的 DOC 转换端口。"""

    def convert(
        self,
        content: bytes,
        policy: ParsingPolicy,
        cancel_check: Callable[[], None] | None = None,
    ) -> DocConversion:
        """转换一个旧版 Word 文档。

        Args:
            content: 已验证为旧版 Word 的来源字节。
            policy: 不可放宽的解析资源边界。
            cancel_check: 可选的持久化作业取消检查。

        Returns:
            派生 DOCX 与转换配方身份。

        """
        ...


class DocConversionUnavailableError(RuntimeError):
    """目标运行环境没有满足合同的转换沙箱。"""


class DocConversionFailedError(RuntimeError):
    """转换已启动，但未产生可信的唯一输出。"""


class DocConversionTimeoutError(DocConversionFailedError):
    """转换排队或运行超过 ParsingPolicy 时间上限。"""


class SandboxedLibreOfficeConverter:
    """用 Landlock、seccomp 与 rlimit 约束 LibreOffice。"""

    def __init__(
        self,
        *,
        executable: str = _LIBREOFFICE_EXECUTABLE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存固定 executable 与可测试时钟。

        Args:
            executable: 目标镜像内固定的 LibreOffice 入口。
            clock: 排队、运行与取消轮询使用的单调时钟。

        Returns:
            无返回值。

        """
        self._executable = executable
        self._clock = clock

    def convert(
        self,
        content: bytes,
        policy: ParsingPolicy,
        cancel_check: Callable[[], None] | None = None,
    ) -> DocConversion:
        """在隔离子进程中转换 DOC，并只接受唯一普通 DOCX。

        Args:
            content: 已通过真实 OLE Word 签名验证的来源字节。
            policy: 文件大小、输出大小与 wall timeout 上限。
            cancel_check: 可选的持久化作业取消检查。

        Returns:
            未替换原始文档身份的派生 DOCX 与转换配方。

        Raises:
            DocConversionUnavailableError: 固定组件或沙箱不可用。
            DocConversionTimeoutError: 排队或转换超过 wall timeout。
            DocConversionFailedError: 转换失败或输出合同无效。
            JobCancelled: 外部作业已取消。

        """
        if len(content) > policy.max_file_bytes:
            raise DocConversionFailedError("DOC 输入超过资源上限。")
        _require_fixed_runtime(self._executable)
        deadline = self._clock() + policy.parse_timeout_seconds
        self._acquire(deadline, cancel_check)
        try:
            return self._convert_acquired(
                content,
                policy,
                deadline=deadline,
                cancel_check=cancel_check,
            )
        finally:
            _CONVERSION_SEMAPHORE.release()

    def _acquire(
        self,
        deadline: float,
        cancel_check: Callable[[], None] | None,
    ) -> None:
        while True:
            _check_cancel(cancel_check)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DocConversionTimeoutError("DOC 转换排队超过时间上限。")
            if _CONVERSION_SEMAPHORE.acquire(
                timeout=min(_POLL_SECONDS, remaining)
            ):
                return

    def _convert_acquired(
        self,
        content: bytes,
        policy: ParsingPolicy,
        *,
        deadline: float,
        cancel_check: Callable[[], None] | None,
    ) -> DocConversion:
        runner = Path(__file__).with_name("doc_sandbox_runner.py")
        if not runner.is_file():
            raise DocConversionUnavailableError("DOC 转换沙箱入口不可用。")
        with tempfile.TemporaryDirectory(prefix="rag-doc-convert-") as root:
            root_path = Path(root)
            input_directory = root_path / "input"
            output_directory = root_path / "output"
            profile_directory = root_path / "profile"
            temporary_directory = root_path / "temporary"
            for directory in (
                input_directory,
                output_directory,
                profile_directory,
                temporary_directory,
            ):
                directory.mkdir(mode=0o700)
            source_path = input_directory / "source.doc"
            source_path.write_bytes(content)
            source_path.chmod(0o400)
            _write_macro_policy(profile_directory)
            command = (
                sys.executable,
                "-I",
                str(runner),
                "--executable",
                self._executable,
                "--input",
                str(source_path),
                "--output",
                str(output_directory),
                "--profile",
                str(profile_directory),
                "--temporary",
                str(temporary_directory),
                "--address-space-bytes",
                str(_MAX_ADDRESS_SPACE_BYTES),
                "--file-size-bytes",
                str(policy.max_file_bytes),
                "--cpu-seconds",
                str(max(1, math.ceil(policy.parse_timeout_seconds))),
            )
            try:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as error:
                raise DocConversionUnavailableError(
                    "DOC 转换沙箱无法启动。"
                ) from error
            return_code = self._wait(
                process,
                deadline=deadline,
                cancel_check=cancel_check,
                workspace_limits=_WorkspaceLimits(
                    root=root_path,
                    max_files=min(
                        policy.max_entries,
                        _MAX_WORKSPACE_FILES,
                    ),
                    max_bytes=min(
                        policy.max_uncompressed_bytes,
                        len(content)
                        + policy.max_file_bytes
                        + _MAX_WORKSPACE_OVERHEAD_BYTES,
                    ),
                ),
            )
            if return_code == _SANDBOX_UNAVAILABLE:
                raise DocConversionUnavailableError("DOC 转换隔离不可用。")
            if return_code != 0:
                raise DocConversionFailedError("DOC 转换进程失败。")
            entries = tuple(output_directory.iterdir())
            expected = output_directory / "source.docx"
            if (
                entries != (expected,)
                or not expected.is_file()
                or expected.is_symlink()
            ):
                raise DocConversionFailedError("DOC 转换输出数量或类型无效。")
            size = expected.stat().st_size
            if size <= 0 or size > policy.max_file_bytes:
                raise DocConversionFailedError("DOC 转换输出超过资源上限。")
            return DocConversion(
                content=expected.read_bytes(),
                converter="libreoffice",
                converter_version=_LIBREOFFICE_PACKAGE_VERSION,
                recipe=_CONVERTER_RECIPE,
            )

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        *,
        deadline: float,
        cancel_check: Callable[[], None] | None,
        workspace_limits: _WorkspaceLimits,
    ) -> int:
        try:
            while True:
                _check_cancel(cancel_check)
                _enforce_workspace_limits(
                    workspace_limits.root,
                    max_files=workspace_limits.max_files,
                    max_bytes=workspace_limits.max_bytes,
                )
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise DocConversionTimeoutError("DOC 转换超过时间上限。")
                try:
                    return_code = process.wait(
                        timeout=min(_POLL_SECONDS, remaining)
                    )
                except subprocess.TimeoutExpired:
                    continue
                _enforce_workspace_limits(
                    workspace_limits.root,
                    max_files=workspace_limits.max_files,
                    max_bytes=workspace_limits.max_bytes,
                )
                return return_code
        except (DocConversionFailedError, JobCancelled):
            _kill_process_group(process)
            raise


def _check_cancel(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _enforce_workspace_limits(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    entry_count = 0
    total_bytes = 0
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                continue
            with entries:
                for entry in entries:
                    try:
                        entry_count += 1
                        if entry_count > max_files:
                            raise DocConversionFailedError(
                                "DOC 转换工作区文件数超过资源上限。"
                            )
                        if entry.is_symlink():
                            raise DocConversionFailedError(
                                "DOC 转换工作区出现不允许的符号链接。"
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            raise DocConversionFailedError(
                                "DOC 转换工作区出现不允许的特殊文件。"
                            )
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                        if total_bytes > max_bytes:
                            raise DocConversionFailedError(
                                "DOC 转换工作区字节数超过资源上限。"
                            )
                    except FileNotFoundError:
                        continue
    except OSError as error:
        raise DocConversionFailedError(
            "DOC 转换工作区无法安全盘点。"
        ) from error


@lru_cache(maxsize=8)
def _require_fixed_runtime(executable: str) -> None:
    path = Path(executable)
    if not path.is_absolute() or not path.is_file():
        raise DocConversionUnavailableError("固定 DOC 转换组件不可用。")
    try:
        with tempfile.TemporaryDirectory(prefix="rag-doc-version-") as home:
            completed = subprocess.run(  # noqa: S603
                (executable, "--version"),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=3,
                env={
                    "HOME": home,
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DocConversionUnavailableError(
            "固定 DOC 转换组件无法验证。"
        ) from error
    if (
        completed.returncode != 0
        or _LIBREOFFICE_BINARY_VERSION not in completed.stdout
    ):
        raise DocConversionUnavailableError("DOC 转换组件版本不匹配。")


def _write_macro_policy(profile_directory: Path) -> None:
    user_directory = profile_directory / "user"
    user_directory.mkdir(mode=0o700)
    policy = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Common/Misc">
    <prop oor:name="UpdateLinksWhenOpening" oor:op="fuse">
      <value>0</value>
    </prop>
  </item>
</oor:items>
"""
    target = user_directory / "registrymodifications.xcu"
    target.write_text(policy, encoding="utf-8")
    target.chmod(0o600)


__all__ = [
    "DocConversion",
    "DocConversionFailedError",
    "DocConversionTimeoutError",
    "DocConversionUnavailableError",
    "DocConverter",
    "SandboxedLibreOfficeConverter",
]
