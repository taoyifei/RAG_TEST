from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "a" * 40
_NEW_REVISION = "b" * 40
_OLD_IMAGE = f"docx-rag:{_OLD_REVISION[:12]}"
_NEW_IMAGE = f"docx-rag:{_NEW_REVISION[:12]}"
_OLD_IMAGE_ID = "sha256:" + "1" * 64
_NEW_IMAGE_ID = "sha256:" + "2" * 64
_INDEX_FINGERPRINT = "sha256:" + "3" * 64


@dataclass(frozen=True, slots=True)
class _Sandbox:
    package: Path
    env_file: Path
    binaries: Path
    state: Path
    log: Path
    fail_new: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare(tmp_path: Path) -> _Sandbox:
    package = tmp_path / "update"
    package.mkdir()
    script = package / "update-app.sh"
    script.write_bytes(
        (_ROOT / "deployment/industry/update-app.sh").read_bytes()
    )
    script.chmod(0o700)
    archive = package / "app-image.tar.gz"
    with gzip.open(archive, "wb") as output:
        output.write(b"fake-app-image")
    sidecar = package / "app-image.tar.gz.sha256"
    sidecar.write_text(
        f"{_sha256(archive)}  app-image.tar.gz\n",
        encoding="ascii",
    )
    manifest = {
        "branch": "Industry",
        "files": {
            "app-image.tar.gz": _sha256(archive),
            "app-image.tar.gz.sha256": _sha256(sidecar),
            "update-app.sh": _sha256(script),
        },
        "image": {
            "id": _NEW_IMAGE_ID,
            "platform": "linux/amd64",
            "ref": _NEW_IMAGE,
            "revision": _NEW_REVISION,
        },
        "index_fingerprint": {
            "reindex_required": False,
            "target": _INDEX_FINGERPRINT,
        },
        "revision": _NEW_REVISION,
        "schema_version": "1",
        "target": {
            "alias": "rag-industry-active",
            "project": "rag-industry",
            "service": "rag-industry-app",
        },
    }
    (package / "UPDATE_MANIFEST.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    deployment = tmp_path / "deployment"
    state_dir = deployment / "state"
    docs = deployment / "docs"
    config = deployment / "config"
    backups = deployment / "backups"
    for directory in (state_dir, docs, config, backups):
        directory.mkdir(parents=True)
    compose = deployment / "compose.yaml"
    compose.write_text("name: rag-industry\nservices: {}\n", encoding="utf-8")
    env_file = deployment / "rag-industry.env"
    env_file.write_text(
        "\n".join(
            (
                f"RAG_APP_IMAGE={_OLD_IMAGE}",
                f"RAG_RELEASE_REVISION={_OLD_REVISION}",
                f"RAG_INDUSTRY_COMPOSE_FILE={compose}",
                f"RAG_DOCS_PATH={docs}",
                f"RAG_CONFIG_PATH={config}",
                f"RAG_STATE_PATH={state_dir}",
                f"RAG_BACKUP_PATH={backups}",
                "RAG_PORT=8188",
                "RAG_QDRANT_ALIAS=rag-industry-active",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    state = tmp_path / "docker-state.json"
    state.write_text(
        json.dumps(
            {
                "image": _OLD_IMAGE,
                "image_id": _OLD_IMAGE_ID,
                "revision": _OLD_REVISION,
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "commands.log"
    fail_new = tmp_path / "fail-new"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_fake_docker(binaries / "docker", state, log, fail_new)
    _write_executable(
        binaries / "curl",
        f"#!/usr/bin/env bash\nprintf 'curl %s\\n' \"$*\" >> {log!s}\nexit 0\n",
    )
    return _Sandbox(
        package=package,
        env_file=env_file,
        binaries=binaries,
        state=state,
        log=log,
        fail_new=fail_new,
    )


def _write_fake_docker(
    path: Path,
    state: Path,
    log: Path,
    fail_new: Path,
) -> None:
    _write_executable(
        path,
        f'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

STATE = pathlib.Path({str(state)!r})
LOG = pathlib.Path({str(log)!r})
FAIL_NEW = pathlib.Path({str(fail_new)!r})
OLD_IMAGE = {_OLD_IMAGE!r}
NEW_IMAGE = {_NEW_IMAGE!r}
OLD_ID = {_OLD_IMAGE_ID!r}
NEW_ID = {_NEW_IMAGE_ID!r}
OLD_REVISION = {_OLD_REVISION!r}
NEW_REVISION = {_NEW_REVISION!r}
FINGERPRINT = {_INDEX_FINGERPRINT!r}
args = sys.argv[1:]
with LOG.open("a", encoding="utf-8") as output:
    output.write("docker " + " ".join(args) + " pollution=" + os.environ.get("RAG_PORT", "unset") + "\\n")
current = json.loads(STATE.read_text())

def env_value(path, key):
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise SystemExit(2)

if args[:1] == ["compose"]:
    env_path = args[args.index("--env-file") + 1]
    if "config" in args:
        image = env_value(env_path, "RAG_APP_IMAGE")
        docs = env_value(env_path, "RAG_DOCS_PATH")
        config = env_value(env_path, "RAG_CONFIG_PATH")
        state_path = env_value(env_path, "RAG_STATE_PATH")
        print(json.dumps({{
            "name": "rag-industry",
            "services": {{
                "rag-industry-app": {{
                    "environment": {{"RAG_QDRANT_ALIAS": "rag-industry-active"}},
                    "image": image,
                    "ports": [{{"published": "8188", "target": 8088}}],
                    "volumes": [
                        {{"source": docs, "target": "/data/docs"}},
                        {{"source": config, "target": "/config"}},
                        {{"source": state_path, "target": "/state"}},
                    ],
                }}
            }},
        }}))
        raise SystemExit(0)
    if "up" in args:
        image = env_value(env_path, "RAG_APP_IMAGE")
        revision = env_value(env_path, "RAG_RELEASE_REVISION")
        STATE.write_text(json.dumps({{
            "image": image,
            "image_id": NEW_ID if image == NEW_IMAGE else OLD_ID,
            "revision": revision,
        }}))
        raise SystemExit(0)

kind = args[0] if args else ""
if kind == "image" and args[1] == "load":
    sys.stdin.buffer.read()
    print("Loaded image: " + NEW_IMAGE)
    raise SystemExit(0)
if kind == "image" and args[1] == "inspect":
    image = args[-1]
    if "--format" not in args:
        raise SystemExit(0)
    template = args[args.index("--format") + 1]
    if template == "{{{{.Id}}}}":
        print(NEW_ID if image == NEW_IMAGE else OLD_ID)
    elif ".Os" in template:
        print("linux/amd64")
    else:
        print(NEW_REVISION if image == NEW_IMAGE else OLD_REVISION)
    raise SystemExit(0)
if kind == "run":
    if "asset-selfcheck" in args:
        print(json.dumps({{"pipeline_fingerprint": FINGERPRINT}}))
    raise SystemExit(0)
if kind == "container" and args[1] == "inspect":
    name = args[-1]
    if "--format" not in args:
        raise SystemExit(0)
    template = args[args.index("--format") + 1]
    if name != "rag-industry-app":
        if ".Name" in template:
            print("/" + name + "|id-" + name + "|2026-08-01T00:00:00Z")
        raise SystemExit(0)
    if template == "{{{{.Config.Image}}}}": print(current["image"])
    elif template == "{{{{.Image}}}}": print(current["image_id"])
    elif "compose.project" in template: print("rag-industry")
    elif "compose.service" in template: print("rag-industry-app")
    elif ".Config.Env" in template: print("RAG_RELEASE_REVISION=" + current["revision"])
    elif ".NetworkSettings.Ports" in template:
        print(json.dumps({{"8088/tcp": [{{"HostIp": "", "HostPort": "8188"}}]}}))
    elif ".Mounts" in template:
        print(json.dumps([{{"Source": "stable", "Destination": "/state"}}]))
    raise SystemExit(0)
if kind == "exec":
    if "build-info" in args and current["revision"] == NEW_REVISION and FAIL_NEW.exists():
        raise SystemExit(1)
    if "runtime-state" in args:
        print(json.dumps({{
            "active_collection": "rag-docx-active-1",
            "alias": "rag-industry-active",
            "index_fingerprint": FINGERPRINT,
            "manifest_sha256": "4" * 64,
            "point_count": 139,
            "release_revision": current["revision"],
        }}))
    raise SystemExit(0)
raise SystemExit(3)
''',
    )


def _run(sandbox: _Sandbox) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
        "RAG_PORT": "9999",
        "RAG_APP_IMAGE": "polluted:image",
        "RAG_QDRANT_ALIAS": "polluted-alias",
    }
    return subprocess.run(  # noqa: S603
        [
            "/usr/bin/bash",
            str(sandbox.package / "update-app.sh"),
            str(sandbox.package / "app-image.tar.gz"),
            str(sandbox.package / "app-image.tar.gz.sha256"),
            str(sandbox.package / "UPDATE_MANIFEST.json"),
            str(sandbox.env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )


def test_update_is_hermetic_force_recreates_only_app_and_preserves_index(
    tmp_path: Path,
) -> None:
    sandbox = _prepare(tmp_path)

    result = _run(sandbox)

    assert result.returncode == 0, result.stderr
    assert "RAG_INDUSTRY_APP_UPDATE_OK" in result.stdout
    assert "reindex_required=false" in result.stdout
    assert f"RAG_APP_IMAGE={_NEW_IMAGE}" in sandbox.env_file.read_text()
    state = json.loads(sandbox.state.read_text())
    assert state["image"] == _NEW_IMAGE
    commands = sandbox.log.read_text()
    assert commands.count(
        "--no-deps --no-build --pull never --force-recreate rag-industry-app"
    ) == 1
    assert "pollution=unset" in commands
    assert "pollution=9999" not in "\n".join(
        line for line in commands.splitlines() if "docker compose" in line
    )
    candidate = sandbox.env_file.parent / "backups/app-candidate.json"
    assert json.loads(candidate.read_text())["stage"] == "candidate"


def test_failed_new_identity_restores_and_reverifies_old_app(
    tmp_path: Path,
) -> None:
    sandbox = _prepare(tmp_path)
    sandbox.fail_new.touch()

    result = _run(sandbox)

    assert result.returncode == 1
    assert "旧 app 已完整恢复" in result.stderr
    assert f"RAG_APP_IMAGE={_OLD_IMAGE}" in sandbox.env_file.read_text()
    state = json.loads(sandbox.state.read_text())
    assert state["image"] == _OLD_IMAGE
    commands = sandbox.log.read_text()
    assert commands.count(
        "--no-deps --no-build --pull never --force-recreate rag-industry-app"
    ) == 2
