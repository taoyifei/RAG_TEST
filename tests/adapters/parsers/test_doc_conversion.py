"""DOC converter 的并发、取消和进程树资源边界。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rag_app.adapters.parsers import doc_conversion, doc_sandbox_runner
from rag_app.adapters.parsers.doc_conversion import (
    DocConversion,
    DocConversionTimeoutError,
    SandboxedLibreOfficeConverter,
)
from rag_app.core.errors import JobCancelled
from rag_app.core.policies import ParsingPolicy


class _TrackingConverter(SandboxedLibreOfficeConverter):
    """只替换实际进程步骤，保留生产并发闸门。"""

    def __init__(self) -> None:
        super().__init__(executable="/synthetic/libreoffice")
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def _convert_acquired(
        self,
        _content: bytes,
        _policy: ParsingPolicy,
        *,
        deadline: float,
        cancel_check: object,
    ) -> DocConversion:
        del deadline, cancel_check
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.05)
            return DocConversion(b"derived", "synthetic", "1", "recipe")
        finally:
            with self._lock:
                self.active -= 1


def test_converter_limits_global_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doc_conversion,
        "_require_fixed_runtime",
        lambda _executable: None,
    )
    converter = _TrackingConverter()
    policy = ParsingPolicy(parse_timeout_seconds=2)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = tuple(
            executor.map(
                lambda _: converter.convert(b"synthetic", policy),
                range(6),
            )
        )

    assert len(results) == 6
    assert converter.peak == 2


def test_converter_cancellation_kills_process_group(tmp_path: Path) -> None:
    converter = SandboxedLibreOfficeConverter()
    process = subprocess.Popen(
        ("/bin/sh", "-c", "sleep 30"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def cancel() -> None:
        raise JobCancelled("合成取消", stage="test.cancel")

    with pytest.raises(JobCancelled):
        converter._wait(
            process,
            deadline=time.monotonic() + 5,
            cancel_check=cancel,
            workspace_limits=doc_conversion._WorkspaceLimits(
                root=tmp_path,
                max_files=10,
                max_bytes=1024,
            ),
        )

    assert process.poll() is not None


def test_converter_wall_timeout_kills_process_group(tmp_path: Path) -> None:
    converter = SandboxedLibreOfficeConverter()
    process = subprocess.Popen(
        ("/bin/sh", "-c", "sleep 30"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    with pytest.raises(DocConversionTimeoutError):
        converter._wait(
            process,
            deadline=time.monotonic() + 0.01,
            cancel_check=None,
            workspace_limits=doc_conversion._WorkspaceLimits(
                root=tmp_path,
                max_files=10,
                max_bytes=1024,
            ),
        )

    assert process.poll() is not None


def test_converter_workspace_limit_kills_process_group(tmp_path: Path) -> None:
    converter = SandboxedLibreOfficeConverter()
    process = subprocess.Popen(
        ("/bin/sh", "-c", "sleep 30"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (tmp_path / "one").write_bytes(b"a")
    (tmp_path / "two").write_bytes(b"b")

    with pytest.raises(
        doc_conversion.DocConversionFailedError,
        match="文件数",
    ):
        converter._wait(
            process,
            deadline=time.monotonic() + 5,
            cancel_check=None,
            workspace_limits=doc_conversion._WorkspaceLimits(
                root=tmp_path,
                max_files=1,
                max_bytes=1024,
            ),
        )

    assert process.poll() is not None


def test_landlock_regular_file_access_excludes_directory_permission(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "runtime-config"
    regular_file.write_bytes(b"public")

    access = doc_sandbox_runner._compatible_path_access(
        regular_file,
        doc_sandbox_runner._READ_ONLY_ACCESS,
    )

    assert access & doc_sandbox_runner._ACCESS_READ_FILE
    assert access & doc_sandbox_runner._ACCESS_EXECUTE
    assert not access & doc_sandbox_runner._ACCESS_READ_DIR


def test_temporary_socket_access_does_not_allow_regular_file_writes() -> None:
    access = doc_sandbox_runner._TEMPORARY_SOCKET_ACCESS

    assert access & doc_sandbox_runner._ACCESS_MAKE_SOCK
    assert access & doc_sandbox_runner._ACCESS_REMOVE_FILE
    assert not access & doc_sandbox_runner._ACCESS_READ_FILE
    assert not access & doc_sandbox_runner._ACCESS_READ_DIR
    assert not access & doc_sandbox_runner._ACCESS_WRITE_FILE
    assert not access & doc_sandbox_runner._ACCESS_MAKE_REG


def test_seccomp_allows_local_ipc_and_denies_ip_sockets() -> None:
    script = """
import socket
from rag_app.adapters.parsers import doc_sandbox_runner

doc_sandbox_runner._restrict_syscalls()
left, right = socket.socketpair(socket.AF_UNIX)
left.close()
right.close()
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        socket.socket(family, socket.SOCK_STREAM)
    except PermissionError:
        continue
    raise SystemExit(3)
"""

    completed = subprocess.run(  # noqa: S603
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
