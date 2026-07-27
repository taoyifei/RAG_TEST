"""以固定命令和资源限制把 EMF 光栅化为 PNG。"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "EmfRasterizer",
    "RasterizationError",
    "SandboxedEmfRasterizer",
]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
_MAX_OPEN_FILES = 32


class RasterizationError(RuntimeError):
    """EMF 光栅化被拒绝、超时或未产生安全 PNG。"""


class EmfRasterizer(Protocol):
    """EMF 安全光栅化的可替换接口。"""

    def rasterize(self, emf_bytes: bytes) -> bytes:
        """把原始 EMF 转换为受限 PNG。

        Args:
            emf_bytes: 不可信的原始 EMF 字节。

        Returns:
            通过实现层大小与签名校验的 PNG 字节。

        Raises:
            RasterizationError: 转换失败、超时或输出不安全。

        """


@dataclass(frozen=True, slots=True)
class SandboxedEmfRasterizer:
    """通过无 shell 子进程调用已审计的本地转换器。"""

    executable: Path
    timeout_seconds: float
    max_output_bytes: int
    limit_executable: Path = Path("/usr/bin/prlimit")

    def __post_init__(self) -> None:
        """验证转换器和所有资源预算。"""
        resolved = self.executable.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError("EMF 转换器必须是可执行的普通文件。")
        limiter = self.limit_executable.resolve(strict=True)
        if not limiter.is_file() or not os.access(limiter, os.X_OK):
            raise ValueError("EMF 资源限制器必须是可执行的普通文件。")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("EMF 超时和输出上限必须为正数。")
        object.__setattr__(self, "executable", resolved)
        object.__setattr__(self, "limit_executable", limiter)

    def rasterize(self, emf_bytes: bytes) -> bytes:
        """在临时目录内执行固定转换器。

        Args:
            emf_bytes: 原始 EMF 字节。

        Returns:
            通过签名和大小校验的 PNG 字节。

        Raises:
            RasterizationError: 转换超时、失败或输出不安全。

        """
        if not emf_bytes:
            raise RasterizationError("EMF_EMPTY")
        with tempfile.TemporaryDirectory(prefix="rag-ocr-emf-") as directory:
            workdir = Path(directory)
            source = workdir / "input.emf"
            output = workdir / "output.png"
            source.write_bytes(emf_bytes)
            source.chmod(0o400)
            try:
                cpu_seconds = max(1, math.ceil(self.timeout_seconds))
                completed = subprocess.run(  # noqa: S603
                    [
                        str(self.limit_executable),
                        f"--cpu={cpu_seconds}:{cpu_seconds}",
                        (
                            "--as="
                            f"{_MAX_ADDRESS_SPACE_BYTES}:"
                            f"{_MAX_ADDRESS_SPACE_BYTES}"
                        ),
                        (
                            "--fsize="
                            f"{self.max_output_bytes}:"
                            f"{self.max_output_bytes}"
                        ),
                        f"--nofile={_MAX_OPEN_FILES}:{_MAX_OPEN_FILES}",
                        "--core=0:0",
                        "--",
                        str(self.executable),
                        str(source),
                        str(output),
                    ],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout_seconds,
                    env={"PATH": "/usr/bin:/bin", "HOME": directory},
                    cwd=directory,
                )
            except subprocess.TimeoutExpired as error:
                raise RasterizationError(
                    "EMF_RASTERIZE_TIMEOUT"
                ) from error
            if completed.returncode != 0:
                raise RasterizationError("EMF_RASTERIZE_FAILED")
            if output.is_symlink() or not output.is_file():
                raise RasterizationError("EMF_OUTPUT_MISSING")
            if output.stat().st_size > self.max_output_bytes:
                raise RasterizationError("EMF_OUTPUT_TOO_LARGE")
            png_bytes = output.read_bytes()
        if not png_bytes.startswith(_PNG_SIGNATURE):
            raise RasterizationError("EMF_OUTPUT_NOT_PNG")
        return png_bytes
