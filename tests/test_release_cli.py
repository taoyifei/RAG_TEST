"""P11 统一发布命令的纯离线回归。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import release
from scripts.release import (
    _assert_osv_audit_clean,
    _audit_frontend_dependencies,
    _audit_python_dependencies,
    _write_license_inventory,
    main,
)


def test_license_inventory_is_sorted_and_does_not_invent_license(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.json"
    output = tmp_path / "licenses.json"
    sbom.write_text(
        json.dumps(
            {
                "components": [
                    {"name": "z", "version": "1"},
                    {
                        "name": "a",
                        "version": "2",
                        "purl": "pkg:pypi/a@2",
                        "licenses": [{"license": {"id": "MIT"}}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    _write_license_inventory(sbom, output)

    inventory = json.loads(output.read_text(encoding="utf-8"))["components"]
    assert [item["name"] for item in inventory] == ["a", "z"]
    assert inventory[0]["licenses"] == ["MIT"]
    assert inventory[1]["licenses"] == []


def test_release_cli_rejects_unknown_command_without_running_work() -> None:
    with pytest.raises(SystemExit):
        main(["not-a-command"])


def test_dependency_audit_uses_osv_only_for_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_file = tmp_path / "requirements.runtime.lock"
    lock_file.write_text("example==1.0\n", encoding="utf-8")
    osv_calls: list[tuple[list[tuple[str, str]], str]] = []

    def failed_pip_audit(
        *call_args: object, **_call_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=call_args[0],
            returncode=1,
            stdout="requests.exceptions.SSLError: unexpected EOF",
        )

    def record_osv_call(
        dependencies: list[tuple[str, str]], ecosystem: str
    ) -> None:
        osv_calls.append((dependencies, ecosystem))

    monkeypatch.setattr(
        release.subprocess,
        "run",
        failed_pip_audit,
    )
    monkeypatch.setattr(
        release,
        "_run_osv_dependency_audit",
        record_osv_call,
    )

    _audit_python_dependencies(lock_file)

    assert osv_calls == [([("example", "1.0")], "PyPI")]


def test_dependency_audit_does_not_hide_reported_vulnerability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_file = tmp_path / "requirements.runtime.lock"
    lock_file.write_text("example==1.0\n", encoding="utf-8")

    def failed_pip_audit(
        *call_args: object, **_call_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=call_args[0],
            returncode=1,
            stdout="Found 1 known vulnerability in 1 package",
        )

    monkeypatch.setattr(
        release.subprocess,
        "run",
        failed_pip_audit,
    )
    monkeypatch.setattr(
        release,
        "_run_osv_dependency_audit",
        lambda path: pytest.fail(f"不应回退到 OSV：{path}"),
    )

    with pytest.raises(subprocess.CalledProcessError):
        _audit_python_dependencies(lock_file)


def test_osv_dependency_audit_reports_package_and_vulnerability() -> None:
    with pytest.raises(RuntimeError, match=r"example==1\.0:GHSA-test"):
        _assert_osv_audit_clean(
            [("example", "1.0")],
            {"results": [{"vulns": [{"id": "GHSA-test"}]}]},
        )


def test_frontend_audit_uses_complete_lock_for_transport_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_file = tmp_path / "package-lock.json"
    lock_file.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "frontend", "version": "0.1.0"},
                    "node_modules/example": {"version": "1.0.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    osv_calls: list[tuple[list[tuple[str, str]], str]] = []

    def failed_npm_audit(
        *call_args: object, **_call_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=call_args[0],
            returncode=1,
            stdout="npm error audit endpoint returned an error",
        )

    def record_osv_call(
        dependencies: list[tuple[str, str]], ecosystem: str
    ) -> None:
        osv_calls.append((dependencies, ecosystem))

    monkeypatch.setattr(release.subprocess, "run", failed_npm_audit)
    monkeypatch.setattr(release, "_run_osv_dependency_audit", record_osv_call)

    _audit_frontend_dependencies(lock_file, "npm")

    assert osv_calls == [([("example", "1.0.0")], "npm")]
