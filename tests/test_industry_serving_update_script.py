from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import build_industry_app_update
from scripts.build_industry_bundle import IndustrySourceIdentity
from scripts.industry_bundle.images import ImageArtifact

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_NEW_REVISION = "b" * 40
_OLD_IMAGE = f"docx-rag:{_OLD_REVISION[:12]}"
_NEW_IMAGE = f"docx-rag:{_NEW_REVISION[:12]}"
_OLD_IMAGE_ID = "sha256:" + "1" * 64
_NEW_IMAGE_ID = "sha256:" + "2" * 64
_INDEX_FINGERPRINT = (
    "sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a"
)


@dataclass(frozen=True, slots=True)
class _Sandbox:
    package: Path
    env_file: Path
    old_env: str
    release_root: Path
    backup_path: Path
    docker_state: Path
    log: Path
    binaries: Path


def _build_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    archive_payload = b"fake-image-archive"

    monkeypatch.setattr(
        build_industry_app_update,
        "require_industry_source",
        lambda _root: IndustrySourceIdentity(
            git_sha=_NEW_REVISION,
            main_sha="a" * 40,
            source_date_epoch=1_786_000_000,
        ),
    )
    monkeypatch.setattr(
        build_industry_app_update,
        "prepare_project_wheel",
        lambda _root, _revision: None,
    )

    def build_image(
        *,
        repository_root: Path,
        revision: str,
        output_dir: Path,
    ) -> ImageArtifact:
        assert repository_root == _ROOT.resolve()
        assert revision == _NEW_REVISION
        archive = output_dir / "app-image.tar.gz"
        with (
            archive.open("wb") as raw_output,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=0
            ) as output,
        ):
            output.write(archive_payload)
        return ImageArtifact(
            name="app",
            ref=_NEW_IMAGE,
            image_id=_NEW_IMAGE_ID,
            platform="linux/amd64",
            revision=_NEW_REVISION,
            archive_name=archive.name,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            manifest_digest="sha256:" + "3" * 64,
            config_digest="sha256:" + "4" * 64,
        )

    monkeypatch.setattr(
        build_industry_app_update, "build_app_image_archive", build_image
    )
    monkeypatch.setattr(
        build_industry_app_update, "_git_output", lambda *_args: ""
    )
    return build_industry_app_update.build_industry_app_update(
        repository_root=_ROOT,
        output_parent=tmp_path / "package-output",
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sandbox:
    package = _build_package(tmp_path, monkeypatch)
    release_root = tmp_path / "releases"
    backup_path = tmp_path / "backups"
    state_path = tmp_path / "state"
    old_config = tmp_path / "old-config"
    old_release = release_root / "old-release"
    for path in (
        release_root,
        backup_path,
        state_path,
        old_config,
        old_release,
        tmp_path / "docs",
        tmp_path / "reference",
        tmp_path / "qdrant",
        tmp_path / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    old_compose = old_release / "compose.yaml"
    old_compose.write_text(
        "name: rag-industry\nservices:\n  rag-industry-app:\n"
        "    image: ${RAG_APP_IMAGE}\n",
        encoding="utf-8",
    )
    for config_name in (
        "corpus-policy.json",
        "intent-router-calibration.json",
        "intent-router.json",
        "pipeline.json",
        "retrieval.json",
    ):
        (old_config / config_name).write_text(
            json.dumps({"name": config_name, "revision": "old"}) + "\n",
            encoding="utf-8",
        )
    trace_database = state_path / "traces.sqlite3"
    connection = sqlite3.connect(trace_database)
    connection.execute(
        "CREATE TABLE traces ("
        "trace_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, "
        "question_sha256 TEXT NOT NULL)"
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()
    trace_database.chmod(0o600)

    env_file = tmp_path / "rag-industry.env"
    old_env = (
        "\n".join(
            (
                f"RAG_APP_IMAGE={_OLD_IMAGE}",
                "RAG_OCR_IMAGE=docx-rag-ocr:fixed",
                "RAG_QDRANT_IMAGE=qdrant/qdrant:v1.18.3",
                f"RAG_RELEASE_REVISION={_OLD_REVISION}",
                f"RAG_INDUSTRY_COMPOSE_FILE={old_compose}",
                f"RAG_RELEASE_ROOT={release_root}",
                f"RAG_BACKUP_PATH={backup_path}",
                f"RAG_STATE_PATH={state_path}",
                f"RAG_QDRANT_PATH={tmp_path / 'qdrant'}",
                f"RAG_DOCS_PATH={tmp_path / 'docs'}",
                f"RAG_REFERENCE_PATH={tmp_path / 'reference'}",
                f"RAG_CONFIG_PATH={old_config}",
                f"RAG_LOGS_PATH={tmp_path / 'logs'}",
                "RAG_PORT=8188",
                "RAG_QDRANT_ALIAS=rag-industry-active",
                "RAG_ACCESS_MODE=shared_corpus",
                "RAG_TRACE_MODE=SAFE",
                "RAG_OCR_MODE=dedicated",
                "RAG_OCR_ENDPOINTS='[\"http://rag-industry-ocr:8090\"]'",
                "RAG_INDUSTRY_OCR_GPU_DEVICE_ID=3",
                f"RAG_QUERY_TOKEN={'q' * 32}",
                f"RAG_ADMIN_TOKEN={'a' * 32}",
                f"RAG_QDRANT_API_KEY={'k' * 32}",
                f"RAG_OCR_API_TOKEN={'o' * 32}",
                "RAG_EMBEDDING_ENDPOINTS='[\"http://embedding:8091\"]'",
                "RAG_RERANKER_ENDPOINTS='[\"http://reranker:8092\"]'",
                "RAG_LLM_ENDPOINTS='[\"http://llm:8000\"]'",
                "RAG_EMBEDDING_MODEL=Qwen3-Embedding-0.6B",
                "RAG_EMBEDDING_MAX_BATCH_SIZE=8",
                "RAG_RERANKER_MODEL=Qwen3-Reranker-0.6B",
                "RAG_LLM_MODEL=Qwen/Qwen3-8B-AWQ",
                "RAG_MAX_EMBEDDING_CONCURRENCY=4",
                "RAG_MAX_RERANKER_CONCURRENCY=4",
                "RAG_MAX_LLM_CONCURRENCY=4",
                "RAG_LLM_TIMEOUT_SECONDS=180",
            )
        )
        + "\n"
    )
    env_file.write_text(old_env, encoding="utf-8")
    env_file.chmod(0o600)
    docker_state = tmp_path / "docker-state.json"
    docker_state.write_text(
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
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_fake_docker(binaries / "docker", docker_state, log)
    _write_executable(
        binaries / "curl",
        f"#!/usr/bin/env bash\nprintf 'curl %s\\n' \"$*\" >> {log!s}\nexit 0\n",
    )
    _write_python_wrapper(binaries / "python3", log)
    return _Sandbox(
        package=package,
        env_file=env_file,
        old_env=old_env,
        release_root=release_root,
        backup_path=backup_path,
        docker_state=docker_state,
        log=log,
        binaries=binaries,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _write_python_wrapper(path: Path, log: Path) -> None:
    _write_executable(
        path,
        f"""#!/usr/bin/env bash
printf 'python3 %s\n' "$*" >> {log!s}
case "${{1:-}}" in
  */validation_check.py)
    if [[ "$*" == *' smoke '* ]]; then
      [[ "${{FAKE_SMOKE_FAIL:-0}}" != "1" ]] || exit 1
      printf '{{"negative":3,"passed":20,"positive":17}}\n'
      exit 0
    fi
    ;;
  */ui_contract_check.py)
    [[ "${{FAKE_UI_FAIL:-0}}" != "1" ]] || exit 1
    printf '{{"ui_session":"verified"}}\n'
    exit 0
    ;;
esac
exec /usr/bin/python3 "$@"
""",
    )


def _write_fake_docker(path: Path, state: Path, log: Path) -> None:
    _write_executable(
        path,
        f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sqlite3
import sys

STATE = pathlib.Path({str(state)!r})
LOG = pathlib.Path({str(log)!r})
OLD_IMAGE = {_OLD_IMAGE!r}
NEW_IMAGE = {_NEW_IMAGE!r}
OLD_ID = {_OLD_IMAGE_ID!r}
NEW_ID = {_NEW_IMAGE_ID!r}
OLD_REVISION = {_OLD_REVISION!r}
NEW_REVISION = {_NEW_REVISION!r}
FINGERPRINT = {_INDEX_FINGERPRINT!r}
args = sys.argv[1:]
with LOG.open("a", encoding="utf-8") as output:
    output.write(
        "docker " + " ".join(args)
        + " pollution=" + os.environ.get("RAG_PORT", "unset") + "\\n"
    )
current = json.loads(STATE.read_text())

def env_values(path):
    result = {{}}
    for line in pathlib.Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value.strip("\\\"'")
    return result

if args[:1] == ["compose"]:
    env_path = args[args.index("--env-file") + 1]
    env = env_values(env_path)
    if "config" in args:
        app_environment = {{
            "RAG_QDRANT_ALIAS": "rag-industry-active",
            "RAG_RUN_MODE": "demo",
            "RAG_TRACE_QUESTION_CAPTURE": env.get(
                "RAG_TRACE_QUESTION_CAPTURE", "hash_only"
            ),
            "RAG_TRACE_QUESTION_RETENTION_SECONDS": env.get(
                "RAG_TRACE_QUESTION_RETENTION_SECONDS", "604800"
            ),
            "RAG_UI_QUERY_AUTH_MODE": env.get(
                "RAG_UI_QUERY_AUTH_MODE", "browser_bearer"
            ),
            "RAG_UI_COOKIE_SECURE": env.get("RAG_UI_COOKIE_SECURE", "true"),
            "RAG_UI_ALLOW_INSECURE_HTTP": env.get(
                "RAG_UI_ALLOW_INSECURE_HTTP", "false"
            ),
            "RAG_UI_SESSION_TTL_SECONDS": env.get(
                "RAG_UI_SESSION_TTL_SECONDS", "900"
            ),
        }}
        print(json.dumps({{
            "name": "rag-industry",
            "networks": {{
                "rag-industry-egress": {{"name": "rag-industry-egress"}},
                "rag-industry-internal": {{
                    "internal": True,
                    "name": "rag-industry-internal",
                }},
            }},
            "services": {{
                "rag-industry-app": {{
                    "environment": app_environment,
                    "image": env["RAG_APP_IMAGE"],
                    "ports": [{{"published": "8188", "target": 8088}}],
                    "volumes": [
                        {{
                            "source": env["RAG_DOCS_PATH"],
                            "target": "/data/docs",
                        }},
                        {{
                            "source": env["RAG_CONFIG_PATH"],
                            "target": "/config",
                        }},
                        {{"source": env["RAG_STATE_PATH"], "target": "/state"}},
                        {{"source": env["RAG_LOGS_PATH"], "target": "/logs"}},
                    ],
                }},
                "rag-industry-ocr": {{"image": "docx-rag-ocr:fixed"}},
                "rag-industry-qdrant": {{"image": "qdrant/qdrant:v1.18.3"}},
            }},
        }}))
        raise SystemExit(0)
    if "run" in args:
        if "pre-update-index-state" in args:
            print(json.dumps({{
                "active_collection": "rag-docx-active",
                "alias": "rag-industry-active",
                "index_fingerprint": FINGERPRINT,
                "manifest_sha256": "5" * 64,
                "payload_schema": "industry-pre-update-index-state-v1",
                "point_count": 139,
                "release_revision": env["RAG_RELEASE_REVISION"],
                "source_count": 10,
            }}, separators=(",", ":"), sort_keys=True))
            raise SystemExit(0)
        if "index-state" in args:
            print('{{"active_source_count":10,"point_count":139}}')
            raise SystemExit(0)
    if "up" in args:
        if (
            current.get("fail_app_start")
            and env["RAG_RELEASE_REVISION"] == NEW_REVISION
        ):
            raise SystemExit(1)
        if env["RAG_RELEASE_REVISION"] == NEW_REVISION:
            trace_database = (
                pathlib.Path(env["RAG_STATE_PATH"]) / "traces.sqlite3"
            )
            with sqlite3.connect(trace_database) as connection:
                columns = {{
                    row[1]
                    for row in connection.execute("PRAGMA table_info(traces)")
                }}
                if "question_text" not in columns:
                    connection.execute(
                        "ALTER TABLE traces ADD COLUMN question_text TEXT"
                    )
                connection.execute("PRAGMA user_version=2")
        STATE.write_text(json.dumps({{
            "image": env["RAG_APP_IMAGE"],
            "image_id": (
                NEW_ID
                if env["RAG_RELEASE_REVISION"] == NEW_REVISION
                else OLD_ID
            ),
            "revision": env["RAG_RELEASE_REVISION"],
        }}))
        raise SystemExit(0)

if args[:2] == ["container", "inspect"]:
    name = args[-1]
    template = args[args.index("--format") + 1] if "--format" in args else ""
    if ".State.Running" in template:
        print("false" if name == "rag-industry-worker" else "true")
    elif ".Id}}|{{.State.StartedAt" in template:
        print("id-" + name + "|2026-08-07T00:00:00Z")
    elif template == "{{{{.Id}}}}":
        print("id-" + name)
    elif template == "{{{{.State.StartedAt}}}}":
        print("2026-08-07T00:00:00Z")
    elif ".State.Health.Status" in template:
        print("healthy")
    elif template == "{{{{.Config.Image}}}}":
        print(current["image"])
    elif template == "{{{{.Image}}}}":
        print(current["image_id"])
    elif "compose.project" in template:
        print("rag-industry")
    elif "compose.service" in template:
        print("rag-industry-app")
    elif ".Config.Env" in template:
        print("RAG_RELEASE_REVISION=" + current["revision"])
    elif ".NetworkSettings.Ports" in template:
        print('{{"8088/tcp":[{{"HostIp":"","HostPort":"8188"}}]}}')
    elif ".Mounts" in template:
        print('[{{"Source":"stable","Destination":"/state"}}]')
    raise SystemExit(0)

if args[:1] == ["inspect"]:
    template = args[args.index("--format") + 1]
    if ".State.Health" in template:
        print("healthy")
        raise SystemExit(0)

if args[:2] == ["image", "load"]:
    sys.stdin.buffer.read()
    print("Loaded image: " + NEW_IMAGE)
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    image = args[-1]
    template = args[args.index("--format") + 1]
    if template == "{{{{.Id}}}}":
        print(NEW_ID if image == NEW_IMAGE else OLD_ID)
    elif ".Os" in template:
        print("linux/amd64")
    else:
        print(NEW_REVISION if image == NEW_IMAGE else OLD_REVISION)
    raise SystemExit(0)
if args[:1] == ["run"]:
    print(json.dumps({{"pipeline_fingerprint": FINGERPRINT}}))
    raise SystemExit(0)
if args[:1] == ["exec"]:
    if "build-info" in args:
        print(json.dumps({{
            "expected_revision": current["revision"],
            "installed_revision": current["revision"],
            "matches": True,
        }}, separators=(",", ":"), sort_keys=True))
        raise SystemExit(0)
    if "runtime-state" in args:
        if current["revision"] == OLD_REVISION:
            print("unknown command: runtime-state", file=sys.stderr)
            raise SystemExit(2)
        serving = "sha256:" + "9" * 64
        print(json.dumps({{
            "active_collection": "rag-docx-active",
            "alias": os.environ.get(
                "FAKE_RUNTIME_ALIAS", "rag-industry-active"
            ),
            "index_fingerprint": FINGERPRINT,
            "installed_revision": NEW_REVISION,
            "manifest_sha256": "5" * 64,
            "point_count": int(os.environ.get("FAKE_RUNTIME_POINTS", "139")),
            "production_ready": False,
            "release_matches": True,
            "release_revision": os.environ.get(
                "FAKE_RUNTIME_REVISION", NEW_REVISION
            ),
            "run_mode": "demo",
            "schema_version": os.environ.get("FAKE_RUNTIME_SCHEMA", "2"),
            "serving_fingerprint": os.environ.get(
                "FAKE_TARGET_SERVING", serving
            ),
            "trace_question_capture": os.environ.get(
                "FAKE_RUNTIME_TRACE_CAPTURE", "plaintext"
            ),
            "trace_question_retention_seconds": 604800,
            "trace_schema_version": 2,
            "ui_cookie_secure": False,
            "ui_query_auth_mode": os.environ.get(
                "FAKE_RUNTIME_UI_MODE", "same_origin_session"
            ),
        }}, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0)
if args[:1] == ["logs"]:
    print("safe application log")
    raise SystemExit(0)
raise SystemExit(3)
""",
    )


def _run(
    sandbox: _Sandbox,
    *,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    manifest = json.loads(
        (sandbox.package / "UPDATE_MANIFEST.json").read_bytes()
    )
    environment = {
        **os.environ,
        "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
        "RAG_PORT": "9999",
        "RAG_APP_IMAGE": "polluted:image",
        "RAG_QDRANT_ALIAS": "polluted-alias",
        "RAG_CONFIG_PATH": "/polluted",
        "RAG_INDUSTRY_COMPOSE_FILE": "/polluted/compose.yaml",
        "FAKE_TARGET_SERVING": manifest["serving_fingerprint"]["target"],
        **(extra or {}),
    }
    if extra and extra.get("FAKE_APP_START_FAIL") == "1":
        state = json.loads(sandbox.docker_state.read_bytes())
        state["fail_app_start"] = True
        sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")
    return subprocess.run(  # noqa: S603
        [
            "/usr/bin/bash",
            str(sandbox.package / "update-app.sh"),
            str(sandbox.env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


def test_upgrade_from_old_app_installs_runtime_and_only_recreates_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)

    result = _run(sandbox)

    assert result.returncode == 0, result.stderr
    assert "RAG_INDUSTRY_SERVING_UPDATE_OK" in result.stdout
    assert "reindex_required=false" in result.stdout
    env_text = sandbox.env_file.read_text(encoding="utf-8")
    assert f"RAG_APP_IMAGE={_NEW_IMAGE}" in env_text
    assert f"RAG_RELEASE_REVISION={_NEW_REVISION}" in env_text
    assert "RAG_UI_QUERY_AUTH_MODE=same_origin_session" in env_text
    assert "RAG_TRACE_QUESTION_CAPTURE=plaintext" in env_text
    state = json.loads(sandbox.docker_state.read_text())
    assert state["revision"] == _NEW_REVISION
    commands = sandbox.log.read_text(encoding="utf-8")
    docker_commands = [
        line for line in commands.splitlines() if line.startswith("docker ")
    ]
    assert (
        sum(
            "--force-recreate rag-industry-app" in line
            for line in docker_commands
        )
        == 1
    )
    assert "--force-recreate rag-industry-worker" not in commands
    assert "--force-recreate rag-industry-ocr" not in commands
    assert "--force-recreate rag-industry-qdrant" not in commands
    assert "pollution=unset" in commands
    compose_commands = [
        line for line in docker_commands if line.startswith("docker compose ")
    ]
    assert "pollution=9999" not in "\n".join(compose_commands)
    pre_run = commands.index("pre-update-index-state")
    first_runtime_state = commands.index("rag-app runtime-state")
    assert pre_run < first_runtime_state
    assert (sandbox.backup_path / "last-good-pointer.json").is_file()
    transactions = list((sandbox.backup_path / "serving-updates").iterdir())
    assert len(transactions) == 1
    assert (transactions[0] / "traces-before.sqlite3").is_file()
    snapshot = json.loads(
        (transactions[0] / "pre-update-snapshot.json").read_bytes()
    )
    assert snapshot["private_env"]["mode"] == "0600"
    assert snapshot["app"]["image_ref"] == _OLD_IMAGE
    assert snapshot["app"]["oci_revision"] == _OLD_REVISION
    assert set(snapshot["config"]["files"]) == {
        "corpus-policy.json",
        "intent-router-calibration.json",
        "intent-router.json",
        "pipeline.json",
        "retrieval.json",
    }
    trace_backup = json.loads(
        (transactions[0] / "trace-backup.json").read_bytes()
    )
    assert trace_backup["target_revision"] == _NEW_REVISION
    assert trace_backup["page_count"] > 0
    assert isinstance(trace_backup["source_database_identity"], dict)


@pytest.mark.parametrize(
    "failure",
    [
        {"FAKE_APP_START_FAIL": "1"},
        {"FAKE_SMOKE_FAIL": "1"},
        {"FAKE_UI_FAIL": "1"},
        {"FAKE_TARGET_SERVING": "sha256:" + "0" * 64},
        {"FAKE_RUNTIME_UI_MODE": "browser_bearer"},
        {"FAKE_RUNTIME_TRACE_CAPTURE": "hash_only"},
        {"FAKE_RUNTIME_SCHEMA": "1"},
        {"FAKE_RUNTIME_REVISION": "c" * 40},
        {"FAKE_RUNTIME_POINTS": "138"},
        {"FAKE_RUNTIME_ALIAS": "wrong-alias"},
    ],
)
def test_failed_target_contract_restores_old_env_image_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: dict[str, str],
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)

    result = _run(sandbox, extra=failure)

    assert result.returncode != 0
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.old_env
    state = json.loads(sandbox.docker_state.read_text())
    assert state["revision"] == _OLD_REVISION
    assert not (sandbox.backup_path / "last-good-pointer.json").exists()
    commands = sandbox.log.read_text(encoding="utf-8")
    assert commands.count("--force-recreate rag-industry-app") >= 1
    assert "pre-update-index-state" in commands


def test_successful_update_is_idempotent_without_second_recreate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    first = _run(sandbox)
    assert first.returncode == 0, first.stderr

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    assert "ALREADY_CURRENT" in second.stdout
    assert (
        sum(
            "--force-recreate rag-industry-app" in line
            for line in sandbox.log.read_text().splitlines()
            if line.startswith("docker ")
        )
        == 1
    )


def test_existing_runtime_with_different_content_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    manifest = json.loads(
        (sandbox.package / "UPDATE_MANIFEST.json").read_bytes()
    )
    update_id = (
        f"{_NEW_REVISION[:12]}-{manifest['runtime']['archive_sha256'][:12]}"
    )
    collision = sandbox.release_root / "serving-updates" / update_id
    collision.mkdir(parents=True)
    (collision / "compose.yaml").write_text("tampered", encoding="utf-8")

    result = _run(sandbox)

    assert result.returncode != 0
    assert "RUNTIME_REUSE_EXACT_SET_MISMATCH" in result.stderr
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.old_env
