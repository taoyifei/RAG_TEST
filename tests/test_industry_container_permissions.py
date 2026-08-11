from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_DEFAULT_IMAGE = "docx-rag:d5c03cf9b97e"
_DOCKER = "/usr/bin/docker"
_REVISION = "b" * 40
_CONFIG_NAMES = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}


def _image() -> str:
    return os.environ.get("RAG_TEST_APP_IMAGE", _DEFAULT_IMAGE)


def _container_python(
    mounts: list[str],
    *arguments: str,
    user: str | None = None,
    capabilities: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [_DOCKER, "run", "--rm", "--network", "none"]
    if user is not None:
        command.extend(("--user", user))
    for capability in capabilities:
        command.extend(("--cap-add", capability))
    for key, value in (environment or {}).items():
        command.extend(("--env", f"{key}={value}"))
    for mount in mounts:
        command.extend(("--volume", mount))
    command.extend(("--entrypoint", "python", _image(), *arguments))
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _set_fixture_owner(root: Path, uid: int, gid: int) -> None:
    code = (
        "import os,pathlib;"
        "root=pathlib.Path('/fixture');"
        "paths=sorted([root,*root.rglob('*')],"
        "key=lambda p:len(p.parts),reverse=True);"
        "[(os.chown(p,UID,GID),os.chmod(p,0o700 if p.is_dir() "
        "else 0o600)) for p in paths]"
    ).replace("UID", str(uid)).replace("GID", str(gid))
    result = _container_python(
        [f"{root}:/fixture"], "-c", code, user="0:0"
    )
    assert result.returncode == 0, result.stderr


def _restore_fixture_owner(root: Path) -> None:
    code = (
        "import os,pathlib;"
        "root=pathlib.Path('/fixture');"
        "paths=sorted([root,*root.rglob('*')],"
        "key=lambda p:len(p.parts),reverse=True);"
        f"[(os.chown(p,{os.getuid()},{os.getgid()}),os.chmod(p,0o700 if "
        "p.is_dir() else 0o600)) for p in paths]"
    )
    _container_python([f"{root}:/fixture"], "-c", code, user="0:0")


def test_updater_never_host_reads_container_owned_config_or_trace() -> None:
    source = (
        _ROOT / "deployment" / "industry" / "update-app.sh"
    ).read_text(encoding="utf-8")
    verify = (
        _ROOT / "deployment" / "industry" / "verify-app-update.sh"
    ).read_text(encoding="utf-8")

    assert "config_path.iterdir()" not in source
    assert 'backup-trace-database "${state_path}/traces.sqlite3"' not in source
    assert 'trace-schema \\\n+  "${state_path}/traces.sqlite3"' not in verify


def test_uid_10001_helper_reads_private_sources_and_exports_private_backup(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    config = tmp_path / "config"
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    config.mkdir()
    state.mkdir()
    backup.mkdir(mode=0o700)
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
        path.chmod(0o600)
    database = state / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE traces (trace_id TEXT PRIMARY KEY, "
            "question_sha256 TEXT NOT NULL)"
        )
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)
    helper = _ROOT / "deployment" / "industry" / "serving_runtime_check.py"
    original_stat = database.stat()

    try:
        _set_fixture_owner(config, 10001, 10001)
        _set_fixture_owner(state, 10001, 10001)
        with pytest.raises(PermissionError):
            database.read_bytes()
        with pytest.raises(PermissionError):
            next(config.iterdir())

        filesystem = _container_python(
            [
                f"{config}:/config:ro",
                f"{state}:/state:ro",
                f"{helper}:/update/runtime_check.py:ro",
            ],
            "/update/runtime_check.py",
                "pre-update-filesystem-state",
                "/config",
                "/state/traces.sqlite3",
                "first-deploy-private-v1",
                user="10001:10001",
            )
        assert filesystem.returncode == 0, filesystem.stderr
        filesystem_report = json.loads(filesystem.stdout)
        assert set(filesystem_report["config"]["files"]) == _CONFIG_NAMES
        assert filesystem_report["trace"] == {
            "filename": "traces.sqlite3",
            "mode": "0600",
            "sqlite_user_version": 1,
        }

        trace_backup = backup / "traces-before.sqlite3"
        backup_result = _container_python(
            [
                f"{state}:/state:ro",
                f"{backup}:/update-backup",
                f"{helper}:/update/runtime_check.py:ro",
            ],
            "/update/runtime_check.py",
            "backup-trace-database",
            "/state/traces.sqlite3",
            "/update-backup/traces-before.sqlite3",
            _REVISION,
            user="0:0",
            capabilities=("DAC_OVERRIDE", "CHOWN"),
            environment={
                "RAG_UPDATE_OWNER_GID": str(os.getgid()),
                "RAG_UPDATE_OWNER_UID": str(os.getuid()),
            },
        )
        assert backup_result.returncode == 0, backup_result.stderr
        report = json.loads(backup_result.stdout)
        assert report["owner"] == {"gid": os.getgid(), "uid": os.getuid()}
        assert trace_backup.read_bytes()
        assert stat.S_IMODE(trace_backup.stat().st_mode) == 0o600
        assert trace_backup.stat().st_uid == os.getuid()
        source_identity = report["source_database_identity"]
        assert source_identity["uid"] == 10001
        assert source_identity["gid"] == 10001
        assert source_identity["mode"] == "0600"
        source_observation = report["source_database_observation"]
        assert source_observation["before"]["bytes"] == (
            original_stat.st_size
        )
        assert source_observation["before"]["mtime_ns"] == (
            original_stat.st_mtime_ns
        )
    finally:
        _restore_fixture_owner(config)
        _restore_fixture_owner(state)


def test_real_compose_run_supports_bounded_capability_override(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    private = fixture / "private.txt"
    private.write_text("bounded capability fixture\n", encoding="utf-8")
    private.chmod(0o600)
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "\n".join(
            (
                "name: rag-industry-capability-test",
                "services:",
                "  helper:",
                f"    image: {_image()}",
                "    entrypoint: [\"python\"]",
                "    user: \"10001:10001\"",
                "    cap_drop: [\"ALL\"]",
                "    read_only: true",
                "    network_mode: none",
                "    volumes:",
                f"      - {fixture}:/fixture:ro",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    project = f"rag-capability-{os.getpid()}"
    try:
        _set_fixture_owner(fixture, 10001, 10001)
        result = subprocess.run(  # noqa: S603
            [
                _DOCKER,
                "compose",
                "-p",
                project,
                "-f",
                str(compose),
                "run",
                "--rm",
                "--no-deps",
                "--user",
                "0:0",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "CHOWN",
                "helper",
                "-c",
                "open('/fixture/private.txt', encoding='utf-8').read()",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
    finally:
        subprocess.run(  # noqa: S603
            [
                _DOCKER,
                "compose",
                "-p",
                project,
                "-f",
                str(compose),
                "down",
                "--remove-orphans",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        _restore_fixture_owner(fixture)
