"""P11 统一发布命令的纯离线回归。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

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


@pytest.fixture
def campaign_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    target: dict[str, object] = {
        "id": "target-id",
        "image": "sha256:candidate",
        "running": False,
        "pid": 0,
        "mounts": [
            {
                "Type": "volume",
                "Name": "rag-data",
                "RW": True,
                "Destination": "/data",
                "Source": "/volumes/rag-data/_data",
            },
            {
                "Type": "volume",
                "Name": "rag-secrets",
                "RW": True,
                "Destination": "/run/rag-secrets",
                "Source": "/volumes/rag-secrets/_data",
            },
        ],
    }
    report = {
        "campaign_binding": {"status": "PASS"},
        "steps": {"config_check": {"status": "PASS"}},
        "P11_READY": False,
    }
    state: dict[str, object] = {
        "target": target,
        "peers": {},
        "calls": [],
        "verifications": [],
        "report": report,
        "image": "sha256:candidate",
    }

    def capture(
        command: Sequence[str], *, input_text: str | None = None
    ) -> str:
        cast(list[tuple[str, ...]], state["calls"]).append(tuple(command))
        if command[1:3] == ("image", "inspect"):
            return str(state["image"])
        if command[1:3] == ("ps", "-q"):
            return "\n".join(cast(dict[str, object], state["peers"]))
        if command[1] == "inspect":
            peers = cast(dict[str, object], state["peers"])
            return json.dumps(peers.get(command[-1], state["target"]))
        if command[1] == "run":
            state["payload"] = json.loads(input_text or "null")
            return "P11_ACCEPTANCE_RESULT=" + json.dumps(state["report"])
        pytest.fail(f"不允许的 Docker 操作：{command}")

    def verify(docker: str) -> None:
        cast(list[str], state["verifications"]).append(docker)
        callback = cast(Callable[[], None] | None, state.get("after_verify"))
        if callback is not None:
            callback()

    monkeypatch.setattr(release, "_capture", capture)
    monkeypatch.setattr(release, "_verify_image_contract", verify)
    monkeypatch.setattr(release, "_required_executable", lambda name: name)
    monkeypatch.setattr(release, "provider_operation_identities", lambda _: {})
    monkeypatch.setattr(
        release,
        "_current_identity",
        lambda: dict.fromkeys(
            ("runtime", "image", "migrations", "evaluation"), "identity"
        ),
    )
    return state


def _binding_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    config = tmp_path / "campaign.json"
    config.write_text(
        json.dumps({"data_dir": "/data", "maintenance_confirmed": True}),
        encoding="utf-8",
    )
    return release._parser().parse_args(
        [
            "acceptance",
            "--bind-campaign",
            "--container",
            "target",
            "--config",
            str(config),
            *extra,
        ]
    )


def test_campaign_binding_uses_only_offline_data_volume(
    tmp_path: Path, campaign_docker: dict[str, object]
) -> None:
    args = _binding_args(tmp_path, "--resume", "--steps", "config_check")
    result = release._run_live_acceptance(args)
    assert result == campaign_docker["report"]
    calls = cast(list[tuple[str, ...]], campaign_docker["calls"])
    helper = next(command for command in calls if command[1] == "run")
    assert helper[helper.index("--network") + 1] == "none"
    assert helper[helper.index("--mount") + 1] == (
        "type=volume,src=rag-data,dst=/data"
    )
    assert helper[helper.index("--entrypoint") + 1 :][:2] == (
        "python",
        "sha256:candidate",
    )
    assert "--read-only" in helper
    assert "--volumes-from" not in helper
    assert "rag-secrets" not in " ".join(helper)
    assert not {"exec", "start", "stop", "restart"}.intersection(
        command[1] for command in calls
    )
    payload = cast(dict[str, object], campaign_docker["payload"])
    assert payload["live"] is False
    assert payload["resume"] is False
    assert payload["steps"] == ["config_check"]
    config = cast(dict[str, object], payload["config"])
    assert config["maintenance_confirmed"] is True
    assert config["bind_campaign"] is True
    assert campaign_docker["verifications"] == ["docker"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("running", True, "BLOCKED_MAINTENANCE_REQUIRED"),
        ("pid", 100, "BLOCKED_MAINTENANCE_REQUIRED"),
        ("image", "sha256:old", "BLOCKED_CANDIDATE_IMAGE"),
    ],
)
def test_campaign_binding_requires_stopped_candidate(
    tmp_path: Path,
    campaign_docker: dict[str, object],
    field: str,
    value: object,
    reason: str,
) -> None:
    cast(dict[str, object], campaign_docker["target"])[field] = value
    with pytest.raises(RuntimeError, match=reason):
        release._run_live_acceptance(_binding_args(tmp_path))
    assert "payload" not in campaign_docker


@pytest.mark.parametrize("mount_change", [{"Type": "bind"}, {"RW": False}])
def test_campaign_binding_rejects_uncontrolled_data_mount(
    tmp_path: Path,
    campaign_docker: dict[str, object],
    mount_change: dict[str, object],
) -> None:
    target = cast(dict[str, object], campaign_docker["target"])
    cast(list[dict[str, object]], target["mounts"])[0].update(mount_change)
    with pytest.raises(RuntimeError, match="BLOCKED_DATA_VOLUME"):
        release._run_live_acceptance(_binding_args(tmp_path))
    assert "payload" not in campaign_docker


def test_campaign_binding_rejects_shared_volume_at_other_destination(
    tmp_path: Path, campaign_docker: dict[str, object]
) -> None:
    campaign_docker["peers"] = {
        "peer": {
            "running": True,
            "mounts": [
                {"Name": "rag-data", "Destination": "/other-data", "RW": False}
            ],
        }
    }
    with pytest.raises(RuntimeError, match="BLOCKED_SHARED_DATA"):
        release._run_live_acceptance(_binding_args(tmp_path))
    assert "payload" not in campaign_docker


@pytest.mark.parametrize("change", ["target", "image"])
def test_campaign_binding_rechecks_state_after_image_validation(
    tmp_path: Path, campaign_docker: dict[str, object], change: str
) -> None:
    def mutate() -> None:
        if change == "target":
            cast(dict[str, object], campaign_docker["target"])["running"] = True
        else:
            campaign_docker["image"] = "sha256:replacement"

    campaign_docker["after_verify"] = mutate
    with pytest.raises(RuntimeError, match="BLOCKED_"):
        release._run_live_acceptance(_binding_args(tmp_path))
    assert "payload" not in campaign_docker


@pytest.mark.parametrize(
    "extra", [("--live",), ("--steps", "config_check,jina_connection")]
)
def test_campaign_binding_rejects_live_or_paid_steps_before_docker(
    tmp_path: Path, campaign_docker: dict[str, object], extra: tuple[str, ...]
) -> None:
    with pytest.raises(RuntimeError, match="BLOCKED_BIND_ARGUMENTS"):
        release._run_live_acceptance(_binding_args(tmp_path, *extra))
    assert campaign_docker["calls"] == []


def test_campaign_binding_in_config_cannot_bypass_stopped_check(
    tmp_path: Path, campaign_docker: dict[str, object]
) -> None:
    args = _binding_args(tmp_path)
    args.bind_campaign = False
    args.config.write_text(
        json.dumps(
            {
                "data_dir": "/data",
                "bind_campaign": True,
                "maintenance_confirmed": True,
            }
        ),
        encoding="utf-8",
    )
    cast(dict[str, object], campaign_docker["target"])["running"] = True
    with pytest.raises(RuntimeError, match="BLOCKED_MAINTENANCE_REQUIRED"):
        release._run_live_acceptance(args)
    assert "payload" not in campaign_docker


def test_direct_release_cli_writes_blocked_report_without_import_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("RAG_TEST_NETWORK", "0")
    report_path = tmp_path / "report.json"
    with pytest.raises(subprocess.CalledProcessError) as captured:
        release._capture(
            [
                sys.executable,
                str(Path(release.__file__)),
                "acceptance",
                "--resume",
                "--steps",
                "config_check",
                "--evidence-file",
                str(tmp_path / "evidence.json"),
                "--report-output",
                str(report_path),
            ],
            cwd=tmp_path,
        )
    error = captured.value
    assert error.returncode == 2, str(error.stdout) + str(error.stderr)
    assert "Traceback" not in error.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["P11_READY"] is False
    assert report["live_runner"]["steps"]["config_check"]["status"] == "BLOCKED"
