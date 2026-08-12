from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from deployment.industry import serving_runtime_check
from deployment.industry.serving_last_good import (
    promote_last_good,
    resolve_last_good,
)
from scripts import build_industry_app_update
from scripts.build_industry_bundle import IndustrySourceIdentity
from scripts.industry_bundle.images import ImageArtifact

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_NEW_REVISION = "b" * 40
_OLD_IMAGE = f"docx-rag:{_OLD_REVISION[:12]}"
_NEW_IMAGE = f"docx-rag:{_NEW_REVISION[:12]}"
_OLD_IMAGE_ID = (
    "sha256:"
    "430e9df36c64a6596d43b1f463b5542b36623dc1adeb1d7d0d26357ed3f725a9"
)
_DRIFTED_SOURCE_TAG_IMAGE_ID = "sha256:" + "6" * 64
_NEW_IMAGE_ID = "sha256:" + "2" * 64
_INDEX_FINGERPRINT = (
    "sha256:d2497bc2813f9281d3cb5bf5f6ac9c9ed36e7aec5b96f1333039a220018b6b58"
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


def _create_legacy_trace_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(serving_runtime_check._LEGACY_2C4_SCHEMA)
        connection.executemany(
            """
            INSERT INTO traces (
                trace_id, schema_version, mode, created_at,
                pipeline_fingerprint, serving_fingerprint,
                release_revision, active_collection,
                index_manifest_sha256, payload_schema_version,
                status, capture_complete, expires_at
            ) VALUES (?, '2', 'SAFE', '2026-08-07T00:00:00+00:00',
                      ?, ?, ?, 'rag-docx-active', ?, 1,
                      'ANSWERED', 1, '2026-08-14T00:00:00+00:00')
            """,
            (
                (
                    f"trace-{index:03d}",
                    _INDEX_FINGERPRINT,
                    "sha256:" + "a" * 64,
                    _OLD_REVISION,
                    "b" * 64,
                )
                for index in range(93)
            ),
        )


def _build_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_revision: str = _NEW_REVISION,
    target_image_id: str = _NEW_IMAGE_ID,
    archive_payload: bytes = b"fake-image-archive",
) -> Path:
    target_image = f"docx-rag:{target_revision[:12]}"

    monkeypatch.setattr(
        build_industry_app_update,
        "require_industry_source",
        lambda _root: IndustrySourceIdentity(
            git_sha=target_revision,
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
        config_directory: Path | None = None,
        assets_manifest_path: Path | None = None,
    ) -> ImageArtifact:
        assert repository_root == _ROOT.resolve()
        assert revision == target_revision
        assert config_directory is not None
        assert assets_manifest_path is not None
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
            ref=target_image,
            image_id=target_image_id,
            platform="linux/amd64",
            revision=target_revision,
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
        source = (
            _ROOT
            / "artifacts/industry-deploy/2c4cf220c7cf-87860c8b7496/config"
            / config_name
        ).read_bytes()
        target = old_config / config_name
        target.write_bytes(source)
        target.chmod(0o600)
    trace_database = state_path / "traces.sqlite3"
    _create_legacy_trace_database(trace_database)
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
                "container_exists": True,
                "container_health": "healthy",
                "container_running": True,
                "image": _OLD_IMAGE,
                "image_id": _OLD_IMAGE_ID,
                "image_tags": {_OLD_IMAGE: _OLD_IMAGE_ID},
                "project": "rag-industry",
                "revision": _OLD_REVISION,
                "service": "rag-industry-app",
                "target_serving": json.loads(
                    (package / "UPDATE_MANIFEST.json").read_bytes()
                )["serving_fingerprint"]["target"],
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "commands.log"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_fake_docker(binaries / "docker", docker_state, log)
    docker_state_literal = json.dumps(str(docker_state))
    _write_executable(
        binaries / "curl",
        f"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> {log!s}
if [[ "${{FAKE_READY_FAIL:-0}}" == "1" && "$*" == *'/ready'* ]]; then
  revision="$(/usr/bin/python3 -c 'import json, pathlib
path = pathlib.Path({docker_state_literal})
print(json.loads(path.read_text())["revision"])')"
  [[ "${{revision}}" != {_NEW_REVISION!r} ]] || exit 22
fi
exit 0
""",
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
if [[ "${{1:-}}" == "-" \
  && "${{3:-}}" == "${{FAKE_TRANSACTION_STATE_WRITE_FAIL:-never}}" ]]; then
  exit 1
fi
if [[ "${{1:-}}" == "-" \
  && "${{2:-}}" == */attempt-[0-9][0-9][0-9][0-9] \
  && "${{FAKE_CRASH_BEFORE_ACTIVATION_INTENT:-0}}" == "1" \
  && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
  touch "${{FAKE_CRASH_MARKER}}"
  kill -KILL "$PPID"
  exit 137
fi
if [[ "${{1:-}}" == "-" \
  && "${{2:-}}" == */candidate-rag-industry.env \
  && "${{FAKE_CRASH_AFTER_ENV_SWAP:-0}}" == "1" \
  && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
  /usr/bin/python3 "$@" || exit $?
  touch "${{FAKE_CRASH_MARKER}}"
  kill -KILL "$PPID"
  exit 137
fi
if [[ "${{1:-}}" == "-" \
  && "${{3:-}}" == "activated" \
  && "${{FAKE_CRASH_AFTER_ACTIVATED_STATE:-0}}" == "1" \
  && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
  /usr/bin/python3 "$@" || exit $?
  touch "${{FAKE_CRASH_MARKER}}"
  kill -KILL "$PPID"
  exit 137
fi
if [[ "${{1:-}}" == "-" \
  && "${{3:-}}" == "validated" \
  && "${{FAKE_CRASH_AFTER_VALIDATED_STATE:-0}}" == "1" \
  && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
  /usr/bin/python3 "$@" || exit $?
  touch "${{FAKE_CRASH_MARKER}}"
  kill -KILL "$PPID"
  exit 137
fi
case "${{1:-}}" in
  */last_good.py)
    if [[ "$*" == *' finalize-target '* \
      && "${{FAKE_CRASH_AFTER_LAST_GOOD_PROMOTION:-0}}" == "1" \
      && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
      /usr/bin/python3 "$@" || exit $?
      touch "${{FAKE_CRASH_MARKER}}"
      update_pid="$(ps -o ppid= -p "$PPID" | tr -d ' ')"
      kill -KILL "${{update_pid}}"
      exit 137
    fi
    ;;
  */runtime_check.py)
    if [[ "$*" == *' validate-runtime-state '* \
      && "$*" == *'/verified-state.json '* \
      && "${{FAKE_CRASH_AFTER_VERIFIED_STATE:-0}}" == "1" \
      && ! -e "${{FAKE_CRASH_MARKER:-/nonexistent}}" ]]; then
      /usr/bin/python3 "$@" || exit $?
      touch "${{FAKE_CRASH_MARKER}}"
      update_pid="$(ps -o ppid= -p "$PPID" | tr -d ' ')"
      kill -KILL "${{update_pid}}"
      exit 137
    fi
    ;;
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
import time

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
image_tags = current.setdefault("image_tags", {{OLD_IMAGE: OLD_ID}})

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
        if (
            current.get("FAKE_CANDIDATE_COMPOSE_FAIL")
            and env["RAG_RELEASE_REVISION"] == NEW_REVISION
        ):
            raise SystemExit(1)
        app_environment = {{
            "RAG_QDRANT_ALIAS": "rag-industry-active",
            "RAG_RELEASE_REVISION": env["RAG_RELEASE_REVISION"],
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
        if (
            current.get("FAKE_COMPOSE_CONTRACT_FAIL")
            and env["RAG_RELEASE_REVISION"] == NEW_REVISION
        ):
            app_environment["RAG_MAX_LLM_CONCURRENCY"] = "99"
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
                    "ports": [{{
                        "host_ip": "0.0.0.0",
                        "mode": "ingress",
                        "protocol": "tcp",
                        "published": "8188",
                        "target": 8088,
                    }}],
                    "volumes": [
                        {{
                            "read_only": True,
                            "source": env["RAG_DOCS_PATH"],
                            "target": "/data/docs",
                            "type": "bind",
                        }},
                        {{
                            "read_only": True,
                            "source": env["RAG_CONFIG_PATH"],
                            "target": "/config",
                            "type": "bind",
                        }},
                        {{
                            "read_only": False,
                            "source": env["RAG_STATE_PATH"],
                            "target": "/state",
                            "type": "bind",
                        }},
                        {{
                            "read_only": False,
                            "source": env["RAG_LOGS_PATH"],
                            "target": "/logs",
                            "type": "bind",
                        }},
                    ],
                }},
                "rag-industry-ocr": {{"image": "docx-rag-ocr:fixed"}},
                "rag-industry-qdrant": {{"image": "qdrant/qdrant:v1.18.3"}},
            }},
        }}))
        raise SystemExit(0)
    if "run" in args:
        if "pre-update-filesystem-state" in args:
            if current.get("FAKE_PRE_FILESYSTEM_FAIL"):
                raise SystemExit(1)
            hold_marker = current.get("FAKE_HOLD_UPDATE_MARKER")
            release_marker = current.get("FAKE_RELEASE_UPDATE_MARKER")
            if hold_marker and release_marker:
                pathlib.Path(hold_marker).write_text("held\\n")
                deadline = time.monotonic() + 20
                while (
                    not pathlib.Path(release_marker).exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                if not pathlib.Path(release_marker).exists():
                    raise SystemExit(1)
            config_path = pathlib.Path(env["RAG_CONFIG_PATH"])
            profile = args[-1]
            print(json.dumps({{
                "config": {{
                    "files": {{
                        item.name: {{
                            "gid": item.stat().st_gid,
                            "mode": format(item.stat().st_mode & 0o7777, "04o"),
                            "sha256": __import__("hashlib").sha256(
                                item.read_bytes()
                            ).hexdigest(),
                            "uid": item.stat().st_uid,
                        }}
                        for item in config_path.iterdir()
                    }},
                    "profile": profile,
                }},
                "trace": {{
                    "filename": "traces.sqlite3",
                    "has_question_columns": False,
                    "mode": "0600",
                    "quick_check": "ok",
                    "schema_profile": "industry-trace-2c4-v0",
                    "sqlite_user_version": 0,
                    "trace_count": 93,
                }},
            }}, separators=(",", ":"), sort_keys=True))
            raise SystemExit(0)
        if "backup-trace-database" in args:
            if current.get("FAKE_TRACE_BACKUP_FAIL"):
                raise SystemExit(1)
            transaction_mount = next(
                item for item in args if item.endswith(":/update-backup")
            )
            transaction = pathlib.Path(transaction_mount.rsplit(":", 1)[0])
            destination = transaction / "traces-before.sqlite3"
            source = pathlib.Path(env["RAG_STATE_PATH"]) / "traces.sqlite3"
            with (
                sqlite3.connect(source) as source_connection,
                sqlite3.connect(destination) as destination_connection,
            ):
                source_connection.backup(destination_connection)
            destination.chmod(0o600)
            print(json.dumps({{
                "backup_filename": destination.name,
                "bytes": destination.stat().st_size,
                "created_at": "2026-08-10T00:00:00+00:00",
                "mode": "0600",
                "owner": {{"gid": os.getgid(), "uid": os.getuid()}},
                "page_count": 3,
                "schema_version": "2",
                "sha256": "7" * 64,
                "source_changed_during_backup": False,
                "source_database_identity": {{
                    "device": source.stat().st_dev,
                    "file_type": "regular",
                    "gid": source.stat().st_gid,
                    "inode": source.stat().st_ino,
                    "mode": "0600",
                    "uid": source.stat().st_uid,
                }},
                "source_database_observation": {{
                    "after": {{
                        "bytes": source.stat().st_size,
                        "mtime_ns": source.stat().st_mtime_ns,
                        "wal_bytes": None,
                    }},
                    "before": {{
                        "bytes": source.stat().st_size,
                        "mtime_ns": source.stat().st_mtime_ns,
                        "wal_bytes": None,
                    }},
                }},
                "source_filename": source.name,
                "sqlite_user_version": 0,
                "target_revision": NEW_REVISION,
                "trace_count": 93,
            }}, separators=(",", ":"), sort_keys=True))
            raise SystemExit(0)
        if "pre-update-index-state" in args:
            if current.get("FAKE_PRE_INDEX_FAIL"):
                raise SystemExit(1)
            hold_rollback = current.get("FAKE_HOLD_ROLLBACK_MARKER")
            release_rollback = current.get("FAKE_RELEASE_ROLLBACK_MARKER")
            if hold_rollback and release_rollback:
                pathlib.Path(hold_rollback).write_text("held\\n")
                deadline = time.monotonic() + 20
                while (
                    not pathlib.Path(release_rollback).exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                if not pathlib.Path(release_rollback).exists():
                    raise SystemExit(1)
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
        if "trace-schema" in args:
            print('{{"has_question_columns":true,"quick_check":"ok",'
                  '"schema_profile":"trace-v2","sqlite_user_version":2,'
                  '"trace_count":93}}')
            raise SystemExit(0)
    if "exec" in args and "runtime-state" in args:
        if current.get("runtime_state_unavailable"):
            raise SystemExit(1)
        if current["revision"] == OLD_REVISION:
            print("unknown command: runtime-state", file=sys.stderr)
            raise SystemExit(2)
        serving = "sha256:" + "9" * 64
        print(json.dumps({{
            "active_collection": "rag-docx-active",
            "alias": current.get(
                "FAKE_RUNTIME_ALIAS", "rag-industry-active"
            ),
            "index_fingerprint": FINGERPRINT,
            "installed_revision": NEW_REVISION,
            "manifest_sha256": "5" * 64,
            "point_count": int(current.get("FAKE_RUNTIME_POINTS", "139")),
            "production_ready": False,
            "release_matches": True,
            "release_revision": current.get(
                "FAKE_RUNTIME_REVISION", NEW_REVISION
            ),
            "run_mode": "demo",
            "schema_version": current.get("FAKE_RUNTIME_SCHEMA", "2"),
            "serving_fingerprint": current.get(
                "FAKE_TARGET_SERVING", current["target_serving"]
            ),
            "trace_question_capture": current.get(
                "FAKE_RUNTIME_TRACE_CAPTURE", "plaintext"
            ),
            "trace_question_retention_seconds": 604800,
            "trace_schema_version": 2,
            "ui_cookie_secure": False,
            "ui_query_auth_mode": current.get(
                "FAKE_RUNTIME_UI_MODE", "same_origin_session"
            ),
        }}, separators=(",", ":"), sort_keys=True))
        raise SystemExit(0)
    if "up" in args:
        if (
            current.get("fail_rollback")
            and env["RAG_RELEASE_REVISION"] == OLD_REVISION
        ):
            raise SystemExit(1)
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
                if "question_sha256" not in columns:
                    connection.execute(
                        "ALTER TABLE traces ADD COLUMN question_sha256 TEXT"
                    )
                connection.execute("PRAGMA user_version=2")
        source_id = image_tags.get(env["RAG_APP_IMAGE"])
        next_state = {{
            "image": env["RAG_APP_IMAGE"],
            "image_id": (
                NEW_ID
                if env["RAG_RELEASE_REVISION"] == NEW_REVISION
                else source_id or OLD_ID
            ),
            "image_tags": image_tags,
            "revision": env["RAG_RELEASE_REVISION"],
        }}
        for flag in (
            "fail_app_start",
            "fail_rollback",
            "target_serving",
            "FAKE_RUNTIME_ALIAS",
            "FAKE_RUNTIME_POINTS",
            "FAKE_RUNTIME_REVISION",
            "FAKE_RUNTIME_SCHEMA",
            "FAKE_RUNTIME_TRACE_CAPTURE",
            "FAKE_RUNTIME_UI_MODE",
            "FAKE_TARGET_SERVING",
            "source_tag_image_id",
        ):
            if current.get(flag):
                next_state[flag] = current[flag]
        STATE.write_text(json.dumps(next_state))
        if (
            current.get("FAKE_CRASH_AFTER_TARGET_COMPOSE")
            and env["RAG_RELEASE_REVISION"] == NEW_REVISION
            and not pathlib.Path(
                current.get("FAKE_CRASH_MARKER", "/nonexistent")
            ).exists()
        ):
            pathlib.Path(current["FAKE_CRASH_MARKER"]).write_text(
                "injected\\n"
            )
            os.kill(os.getppid(), 9)
            time.sleep(1)
        raise SystemExit(0)

if args[:2] == ["container", "inspect"]:
    name = args[-1]
    template = args[args.index("--format") + 1] if "--format" in args else ""
    if name == "rag-industry-app" and not current.get(
        "container_exists", True
    ):
        raise SystemExit(1)
    if ".State.Running" in template:
        if name == "rag-industry-worker":
            print("false")
        elif name == "rag-industry-app":
            print(str(current.get("container_running", True)).lower())
        else:
            print("true")
    elif ".Id}}}}|{{{{.State.StartedAt" in template:
        if (
            current.get("FAKE_DEPENDENCY_DRIFT")
            and name == "rag-industry-qdrant"
        ):
            print("drifted-qdrant|2026-08-07T00:00:01Z")
        else:
            print("id-" + name + "|2026-08-07T00:00:00Z")
    elif template == "{{{{.Id}}}}":
        print("id-" + name)
    elif template == "{{{{.State.StartedAt}}}}":
        print("2026-08-07T00:00:00Z")
    elif ".State.Health.Status" in template:
        print(current.get("container_health", "healthy"))
    elif template == "{{{{.Config.Image}}}}":
        print(current.get("container_image", current["image"]))
    elif template == "{{{{.Image}}}}":
        print(current.get("container_image_id", current["image_id"]))
    elif "compose.project" in template:
        print(current.get("project", "rag-industry"))
    elif "compose.service" in template:
        print(current.get("service", "rag-industry-app"))
    elif ".Config.Env" in template:
        print(
            "RAG_RELEASE_REVISION="
            + current.get("container_revision", current["revision"])
        )
    elif ".NetworkSettings.Ports" in template:
        print(
            '{{"8088/tcp":['
            '{{"HostIp":"0.0.0.0","HostPort":"8188"}},'
            '{{"HostIp":"::","HostPort":"8188"}}]}}'
        )
    elif ".Mounts" in template:
        print('[{{"Source":"stable","Destination":"/state"}}]')
    raise SystemExit(0)

if args[:2] == ["container", "ls"]:
    if current.get("container_exists", True):
        print("rag-industry-app")
    raise SystemExit(0)

if args[:1] == ["inspect"]:
    template = args[args.index("--format") + 1]
    if ".State.Health" in template:
        if not current.get("container_exists", True):
            raise SystemExit(1)
        if not current.get("container_running", True):
            print("exited")
        elif current.get("FAKE_APP_UNHEALTHY"):
            print("unhealthy")
        else:
            print(current.get("container_health", "healthy"))
        raise SystemExit(0)

if args[:2] == ["image", "load"]:
    if os.environ.get("FAKE_IMAGE_LOAD_FAIL") == "1":
        raise SystemExit(1)
    sys.stdin.buffer.read()
    image_tags[NEW_IMAGE] = NEW_ID
    current["image_tags"] = image_tags
    STATE.write_text(json.dumps(current))
    print("Loaded image: " + NEW_IMAGE)
    raise SystemExit(0)
if args[:2] == ["image", "tag"]:
    source, target = args[-2:]
    source_id = image_tags.get(source, source if source == OLD_ID else None)
    if source_id is None:
        raise SystemExit(1)
    image_tags[target] = source_id
    current["image_tags"] = image_tags
    STATE.write_text(json.dumps(current))
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    if os.environ.get("FAKE_IMAGE_INSPECT_FAIL") == "1":
        raise SystemExit(1)
    image = args[-1]
    if image == OLD_IMAGE:
        inspected_id = current.get("source_tag_image_id", OLD_ID)
    elif image == OLD_ID:
        inspected_id = OLD_ID
    elif image == NEW_IMAGE:
        inspected_id = NEW_ID
    else:
        inspected_id = image_tags.get(image)
    if inspected_id is None:
        raise SystemExit(1)
    template = args[args.index("--format") + 1]
    if template == "{{{{.Id}}}}":
        expected = (
            current.get("target_image_id", inspected_id)
            if image == NEW_IMAGE
            else inspected_id
        )
        print(expected)
    elif ".Os" in template:
        print(
            current.get("target_platform", "linux/amd64")
            if image == NEW_IMAGE
            else "linux/amd64"
        )
    elif ".Config.Entrypoint" in template:
        print(
            current.get("target_entrypoint", '["rag-app"]')
            if image == NEW_IMAGE
            else '["rag-app"]'
        )
    else:
        expected = NEW_REVISION if image == NEW_IMAGE else OLD_REVISION
        print(
            current.get("target_oci_revision", expected)
            if image == NEW_IMAGE
            else expected
        )
    raise SystemExit(0)
if args[:1] == ["run"]:
    if "build-info" in args:
        print(json.dumps({{
            "expected_revision": NEW_REVISION,
            "installed_revision": NEW_REVISION,
            "matches": True,
        }}, separators=(",", ":"), sort_keys=True))
    else:
        if os.environ.get("FAKE_ASSET_SELFCHECK_FAIL") == "1":
            raise SystemExit(1)
        print(json.dumps({{"pipeline_fingerprint": FINGERPRINT}}))
    raise SystemExit(0)
if args[:1] == ["exec"]:
    if (
        not current.get("container_exists", True)
        or not current.get("container_running", True)
    ):
        raise SystemExit(1)
    if "build-info" in args:
        if (
            os.environ.get("FAKE_OLD_IDENTITY_FAIL") == "1"
            and current["revision"] == OLD_REVISION
        ):
            raise SystemExit(1)
        print(json.dumps({{
            "expected_revision": current["revision"],
            "installed_revision": current["revision"],
            "matches": True,
        }}, separators=(",", ":"), sort_keys=True))
        raise SystemExit(0)
    if "runtime-state" in args:
        if current.get("runtime_state_unavailable"):
            raise SystemExit(1)
        if current["revision"] == OLD_REVISION:
            print("unknown command: runtime-state", file=sys.stderr)
            raise SystemExit(2)
        serving = "sha256:" + "9" * 64
        print(json.dumps({{
            "active_collection": "rag-docx-active",
            "alias": current.get(
                "FAKE_RUNTIME_ALIAS", "rag-industry-active"
            ),
            "index_fingerprint": FINGERPRINT,
            "installed_revision": NEW_REVISION,
            "manifest_sha256": "5" * 64,
            "point_count": int(current.get("FAKE_RUNTIME_POINTS", "139")),
            "production_ready": False,
            "release_matches": True,
            "release_revision": current.get(
                "FAKE_RUNTIME_REVISION", NEW_REVISION
            ),
            "run_mode": "demo",
            "schema_version": current.get("FAKE_RUNTIME_SCHEMA", "2"),
            "serving_fingerprint": current.get(
                "FAKE_TARGET_SERVING", current["target_serving"]
            ),
            "trace_question_capture": current.get(
                "FAKE_RUNTIME_TRACE_CAPTURE", "plaintext"
            ),
            "trace_question_retention_seconds": 604800,
            "trace_schema_version": 2,
            "ui_cookie_secure": False,
            "ui_query_auth_mode": current.get(
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
    state = json.loads(sandbox.docker_state.read_bytes())
    for flag in (
        "FAKE_CANDIDATE_COMPOSE_FAIL",
        "FAKE_COMPOSE_CONTRACT_FAIL",
        "FAKE_APP_UNHEALTHY",
        "FAKE_PRE_FILESYSTEM_FAIL",
        "FAKE_PRE_INDEX_FAIL",
        "FAKE_TRACE_BACKUP_FAIL",
    ):
        state[flag] = bool(extra and extra.get(flag) == "1")
    for key in (
        "FAKE_HOLD_UPDATE_MARKER",
        "FAKE_RELEASE_UPDATE_MARKER",
        "FAKE_CRASH_AFTER_TARGET_COMPOSE",
        "FAKE_CRASH_MARKER",
    ):
        if extra and key in extra:
            state[key] = extra[key]
        else:
            state.pop(key, None)
    for key in (
        "FAKE_RUNTIME_ALIAS",
        "FAKE_RUNTIME_POINTS",
        "FAKE_RUNTIME_REVISION",
        "FAKE_RUNTIME_SCHEMA",
        "FAKE_RUNTIME_TRACE_CAPTURE",
        "FAKE_RUNTIME_UI_MODE",
        "FAKE_TARGET_SERVING",
    ):
        if extra and key in extra:
            state[key] = extra[key]
        else:
            state.pop(key, None)
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")
    if extra and extra.get("FAKE_APP_START_FAIL") == "1":
        state["fail_app_start"] = True
        sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")
    if extra and extra.get("FAKE_ROLLBACK_FAIL") == "1":
        state["fail_rollback"] = True
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


def _start(
    sandbox: _Sandbox,
    *,
    extra: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
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
    state = json.loads(sandbox.docker_state.read_bytes())
    for key in (
        "FAKE_HOLD_UPDATE_MARKER",
        "FAKE_RELEASE_UPDATE_MARKER",
    ):
        if extra and key in extra:
            state[key] = extra[key]
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")
    return subprocess.Popen(  # noqa: S603
        [
            "/usr/bin/bash",
            str(sandbox.package / "update-app.sh"),
            str(sandbox.env_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def _run_manual_rollback(
    sandbox: _Sandbox,
    transaction: Path,
    *,
    extra: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    rollback_env = (
        transaction / "candidate-rag-industry.env"
        if (transaction / "candidate-rag-industry.env").is_file()
        else sandbox.env_file
    )
    compose = next(
        line.split("=", 1)[1]
        for line in rollback_env.read_text(encoding="utf-8").splitlines()
        if line.startswith("RAG_INDUSTRY_COMPOSE_FILE=")
    )
    runtime_dir = Path(compose).parent
    return subprocess.run(  # noqa: S603
        [
            "/usr/bin/bash",
            str(runtime_dir / "rollback-app-update.sh"),
            str(sandbox.env_file),
            str(transaction),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            **(extra or {}),
        },
        timeout=timeout,
    )


def _start_manual_rollback(
    sandbox: _Sandbox,
    transaction: Path,
    *,
    hold_marker: Path,
    release_marker: Path,
) -> subprocess.Popen[str]:
    state = json.loads(sandbox.docker_state.read_bytes())
    state["FAKE_HOLD_ROLLBACK_MARKER"] = str(hold_marker)
    state["FAKE_RELEASE_ROLLBACK_MARKER"] = str(release_marker)
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")
    compose = next(
        line.split("=", 1)[1]
        for line in sandbox.env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("RAG_INDUSTRY_COMPOSE_FILE=")
    )
    return subprocess.Popen(  # noqa: S603
        [
            "/usr/bin/bash",
            str(Path(compose).parent / "rollback-app-update.sh"),
            str(sandbox.env_file),
            str(transaction),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
        },
    )


def _transaction(sandbox: _Sandbox, attempt: int = 1) -> Path:
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    return update_root / f"attempt-{attempt:04d}"


def _source_recovery_env(transaction: Path) -> str:
    return (transaction / "source-recovery.env").read_text(encoding="utf-8")


def _force_recreate_count(sandbox: _Sandbox) -> int:
    return sum(
        "--force-recreate rag-industry-app" in line
        for line in sandbox.log.read_text(encoding="utf-8").splitlines()
        if line.startswith("docker ")
    )


def _configure_target_container(
    sandbox: _Sandbox,
    **updates: object,
) -> None:
    state = json.loads(sandbox.docker_state.read_bytes())
    state.update(updates)
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")


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
    update_roots = list((sandbox.backup_path / "serving-updates").iterdir())
    assert len(update_roots) == 1
    transactions = list(update_roots[0].glob("attempt-*"))
    assert len(transactions) == 1
    transaction = transactions[0]
    assert (transaction / "traces-before.sqlite3").is_file()
    snapshot = json.loads(
        (transaction / "pre-update-snapshot.json").read_bytes()
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
        (transaction / "trace-backup.json").read_bytes()
    )
    assert trace_backup["target_revision"] == _NEW_REVISION
    assert trace_backup["page_count"] > 0
    assert trace_backup["sqlite_user_version"] == 0
    assert trace_backup["trace_count"] == 93
    assert isinstance(trace_backup["source_database_identity"], dict)
    with sqlite3.connect(
        sandbox.env_file.parent / "state/traces.sqlite3"
    ) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM traces").fetchone() == (
            93,
        )
    transaction_state = json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )
    assert transaction_state["state"] == "verified"


def test_shared_source_tag_drift_uses_running_release_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    state = json.loads(sandbox.docker_state.read_bytes())
    state["container_image_id"] = _OLD_IMAGE_ID
    state["source_tag_image_id"] = _DRIFTED_SOURCE_TAG_IMAGE_ID
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")

    result = _run(sandbox)

    assert result.returncode == 0, result.stderr
    transaction = _transaction(sandbox)
    snapshot = json.loads(
        (transaction / "pre-update-snapshot.json").read_bytes()
    )
    assert snapshot["app"]["image_id"] == _OLD_IMAGE_ID
    assert snapshot["app"]["configured_tag_image_id"] == (
        _DRIFTED_SOURCE_TAG_IMAGE_ID
    )
    recovery = json.loads(
        (transaction / "source-recovery-image.json").read_bytes()
    )
    assert recovery["id"] == _OLD_IMAGE_ID
    assert recovery["configured_ref"] == _OLD_IMAGE
    assert recovery["configured_tag_image_id"] == (
        _DRIFTED_SOURCE_TAG_IMAGE_ID
    )


def test_shared_source_tag_drift_rolls_back_without_retagging_training(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    state = json.loads(sandbox.docker_state.read_bytes())
    state["container_image_id"] = _OLD_IMAGE_ID
    state["source_tag_image_id"] = _DRIFTED_SOURCE_TAG_IMAGE_ID
    sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")

    result = _run(sandbox, extra={"FAKE_SMOKE_FAIL": "1"})

    assert result.returncode != 0
    transaction = _transaction(sandbox)
    transaction_state = json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )
    assert transaction_state["state"] == "rolled_back", result.stderr
    recovery = json.loads(
        (transaction / "source-recovery-image.json").read_bytes()
    )
    env = sandbox.env_file.read_text(encoding="utf-8")
    assert f"RAG_APP_IMAGE={recovery['recovery_ref']}" in env
    final_state = json.loads(sandbox.docker_state.read_bytes())
    assert final_state["image_id"] == _OLD_IMAGE_ID
    assert final_state["source_tag_image_id"] == (
        _DRIFTED_SOURCE_TAG_IMAGE_ID
    )


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
    assert sandbox.env_file.read_text(encoding="utf-8") == (
        _source_recovery_env(_transaction(sandbox))
    )
    state = json.loads(sandbox.docker_state.read_text())
    assert state["revision"] == _OLD_REVISION
    assert not (sandbox.backup_path / "last-good-pointer.json").exists()
    commands = sandbox.log.read_text(encoding="utf-8")
    assert commands.count("--force-recreate rag-industry-app") >= 1
    assert "pre-update-index-state" in commands


def test_failed_update_keeps_audit_and_allows_a_new_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    first = _run(sandbox, extra={"FAKE_SMOKE_FAIL": "1"})
    assert first.returncode != 0

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    update_roots = list((sandbox.backup_path / "serving-updates").iterdir())
    assert len(update_roots) == 1
    attempts = sorted(update_roots[0].glob("attempt-*"))
    assert [path.name for path in attempts] == [
        "attempt-0001",
        "attempt-0002",
    ]
    states = [
        json.loads((path / "transaction-state.json").read_bytes())["state"]
        for path in attempts
    ]
    assert states == ["rolled_back", "verified"]
    assert all(
        (path / "pre-update-snapshot.json").is_file() for path in attempts
    )


def test_rollback_failed_blocks_automatic_retry_and_keeps_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    first = _run(
        sandbox,
        extra={"FAKE_ROLLBACK_FAIL": "1", "FAKE_SMOKE_FAIL": "1"},
    )
    assert first.returncode == 70

    second = _run(sandbox)

    assert second.returncode != 0
    assert "RECOVERY_REQUIRES_MANUAL_INTERVENTION" in second.stderr
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    attempts = sorted(update_root.glob("attempt-*"))
    assert len(attempts) == 1
    state = json.loads(
        (attempts[0] / "transaction-state.json").read_bytes()
    )
    assert state["state"] == "rollback_failed"
    assert (attempts[0] / "pre-update-snapshot.json").is_file()


def test_unknown_transaction_state_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    first = _run(sandbox, extra={"FAKE_SMOKE_FAIL": "1"})
    assert first.returncode != 0
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    attempt = next(update_root.glob("attempt-*"))
    state_path = attempt / "transaction-state.json"
    state = json.loads(state_path.read_bytes())
    state["state"] = "unknown"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    second = _run(sandbox)

    assert second.returncode != 0
    assert "RECOVERY_TRANSACTION_INVALID" in second.stderr
    assert len(list(update_root.glob("attempt-*"))) == 1


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


@pytest.mark.parametrize(
    "failure",
    (
        "FAKE_PRE_FILESYSTEM_FAIL",
        "FAKE_OLD_IDENTITY_FAIL",
        "FAKE_PRE_INDEX_FAIL",
        "FAKE_TRACE_BACKUP_FAIL",
        "FAKE_CANDIDATE_COMPOSE_FAIL",
        "FAKE_COMPOSE_CONTRACT_FAIL",
        "FAKE_IMAGE_LOAD_FAIL",
        "FAKE_IMAGE_INSPECT_FAIL",
        "FAKE_ASSET_SELFCHECK_FAIL",
    ),
)
def test_pre_activation_failure_is_audited_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)

    first = _run(sandbox, extra={failure: "1"})

    assert first.returncode != 0
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    first_attempt = update_root / "attempt-0001"
    first_state = json.loads(
        (first_attempt / "transaction-state.json").read_bytes()
    )
    assert first_state["state"] == "precheck_failed"
    assert isinstance(first_state["failure_stage"], str)
    assert isinstance(first_state["error_code"], str)
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.old_env

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    states = [
        json.loads((path / "transaction-state.json").read_bytes())["state"]
        for path in sorted(update_root.glob("attempt-*"))
    ]
    assert states == ["precheck_failed", "verified"]


def test_validated_attempt_recovers_after_promotion_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "promotion-crash.injected"

    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_LAST_GOOD_PROMOTION": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )

    assert first.returncode != 0
    assert marker.is_file()
    assert (sandbox.backup_path / "last-good-pointer.json").is_file()
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    attempt = update_root / "attempt-0001"
    state = json.loads((attempt / "transaction-state.json").read_bytes())
    assert state["state"] == "validated"

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    assert "ALREADY_CURRENT" in second.stdout
    state = json.loads((attempt / "transaction-state.json").read_bytes())
    assert state["state"] == "verified"
    commands = sandbox.log.read_text(encoding="utf-8")
    assert sum(
        "--force-recreate rag-industry-app" in line
        for line in commands.splitlines()
        if line.startswith("docker ")
    ) == 1


def test_corrupt_last_good_pointer_fails_closed_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    pointer = sandbox.backup_path / "last-good-pointer.json"
    pointer.write_text("{}\n", encoding="utf-8")
    pointer.chmod(0o600)
    before = pointer.read_bytes()

    result = _run(sandbox)

    assert result.returncode != 0
    assert pointer.read_bytes() == before
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.old_env
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    state = json.loads(
        (update_root / "attempt-0001" / "transaction-state.json").read_bytes()
    )
    assert state["state"] == "precheck_failed"
    assert state["failure_stage"] == "last_good_precheck"


def test_rollback_success_is_not_claimed_when_state_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)

    result = _run(
        sandbox,
        extra={
            "FAKE_SMOKE_FAIL": "1",
            "FAKE_TRANSACTION_STATE_WRITE_FAIL": "rolled_back",
        },
    )

    assert result.returncode == 70
    assert "ROLLBACK_STATE_WRITE_FAILED" in result.stderr
    assert "RAG_INDUSTRY_SERVING_UPDATE_ROLLED_BACK\n" not in result.stderr


def test_real_2c4_env_only_last_good_is_checkpointed_without_rewriting_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = subprocess.run(  # noqa: S603
        [
            "/usr/bin/git",
            "show",
            f"{_OLD_REVISION}:deployment/industry/deploy.sh",
        ],
        check=True,
        capture_output=True,
        cwd=_ROOT,
        text=True,
    ).stdout
    assert 'last_good="${backup_path}/last-good.env"' in historical
    assert "last-good.json" not in historical
    sandbox = _prepare(tmp_path, monkeypatch)
    legacy = sandbox.backup_path / "last-good.env"
    legacy.write_text(sandbox.old_env, encoding="utf-8")
    legacy.chmod(0o600)
    before = legacy.read_bytes()

    result = _run(sandbox)

    assert result.returncode == 0, result.stderr
    assert legacy.read_bytes() == before
    assert not (sandbox.backup_path / "last-good.json").exists()
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    transaction = update_root / "attempt-0001"
    source_state = json.loads(
        (transaction / "pre-update-source-state.json").read_bytes()
    )
    assert source_state["revision"] == _OLD_REVISION
    assert source_state["update_kind"] == "pre_update_source_checkpoint"


def test_validated_attempt_promotes_target_when_pointer_still_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    source_state = tmp_path / "source-state.json"
    source_state.write_text(
        json.dumps(
            {
                "revision": _OLD_REVISION,
                "schema_version": "1",
                "stage": "last_good",
                "update_kind": "historical_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source_state.chmod(0o600)
    promote_last_good(
        sandbox.backup_path,
        sandbox.env_file,
        source_state,
        _OLD_REVISION,
    )
    marker = tmp_path / "validated-before-promote.injected"

    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_VALIDATED_STATE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )

    assert first.returncode != 0
    assert marker.is_file()
    assert resolve_last_good(sandbox.backup_path)["revision"] == _OLD_REVISION
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    attempt = update_root / "attempt-0001"
    state = json.loads((attempt / "transaction-state.json").read_bytes())
    assert state["state"] == "validated"

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    assert resolve_last_good(sandbox.backup_path)["revision"] == _NEW_REVISION
    state = json.loads((attempt / "transaction-state.json").read_bytes())
    assert state["state"] == "verified"


def test_global_update_lock_rejects_second_process_before_transaction_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    held = tmp_path / "update-held"
    release = tmp_path / "update-release"
    first = _start(
        sandbox,
        extra={
            "FAKE_HOLD_UPDATE_MARKER": str(held),
            "FAKE_RELEASE_UPDATE_MARKER": str(release),
        },
    )
    try:
        for _ in range(500):
            if held.exists():
                break
            if first.poll() is not None:
                break
            time.sleep(0.01)
        assert held.is_file()
        attempts_before = list(
            sandbox.backup_path.glob("serving-updates/*/attempt-*")
        )
        alternate_package = _build_package(
            tmp_path,
            monkeypatch,
            target_revision="c" * 40,
            target_image_id="sha256:" + "c" * 64,
            archive_payload=b"alternate-image-archive",
        )
        assert alternate_package != sandbox.package

        second = subprocess.run(  # noqa: S603
            [
                "/usr/bin/bash",
                str(alternate_package / "update-app.sh"),
                str(sandbox.env_file),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
            },
            timeout=10,
        )

        assert second.returncode != 0
        assert "SERVING_UPDATE_ALREADY_RUNNING" in second.stderr
        assert list(
            sandbox.backup_path.glob("serving-updates/*/attempt-*")
        ) == attempts_before
    finally:
        release.write_text("release\n", encoding="utf-8")
        first_stdout, first_stderr = first.communicate(timeout=30)
    assert first.returncode == 0, first_stderr or first_stdout

    third = _run(sandbox)

    assert third.returncode == 0, third.stderr


def test_verifying_with_complete_verified_state_recovers_same_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "verified-state-window.injected"

    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_VERIFIED_STATE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )

    assert first.returncode != 0
    assert marker.is_file()
    update_root = next((sandbox.backup_path / "serving-updates").iterdir())
    attempt = update_root / "attempt-0001"
    assert (attempt / "verified-state.json").is_file()
    assert json.loads(
        (attempt / "transaction-state.json").read_bytes()
    )["state"] == "verifying"

    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    assert json.loads(
        (attempt / "transaction-state.json").read_bytes()
    )["state"] == "verified"
    assert not (update_root / "attempt-0002").exists()
    commands = sandbox.log.read_text(encoding="utf-8")
    assert sum(
        "--force-recreate rag-industry-app" in line
        for line in commands.splitlines()
        if line.startswith("docker ")
    ) == 1


def test_validated_absent_pointer_recovers_only_from_recorded_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "validated-absent.injected"

    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_VALIDATED_STATE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )

    assert first.returncode != 0
    assert not (sandbox.backup_path / "last-good-pointer.json").exists()
    second = _run(sandbox)

    assert second.returncode == 0, second.stderr
    assert resolve_last_good(sandbox.backup_path)["revision"] == _NEW_REVISION


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("FAKE_RUNTIME_ALIAS", "third-alias"),
        ("FAKE_RUNTIME_POINTS", "138"),
        ("FAKE_RUNTIME_UI_MODE", "browser_bearer"),
        ("FAKE_RUNTIME_TRACE_CAPTURE", "hash_only"),
    ),
)
def test_validated_recovery_rejects_runtime_drift_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "validated-runtime-drift.injected"
    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_VALIDATED_STATE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert first.returncode != 0

    second = _run(sandbox, extra={field: value})

    assert second.returncode != 0
    assert "RECOVERY_RUNTIME_STATE_MISMATCH" in second.stderr
    assert not (sandbox.backup_path / "last-good-pointer.json").exists()


def test_validated_recovery_rejects_corrupt_or_third_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    source_state = tmp_path / "source-state.json"
    source_state.write_text(
        json.dumps({"revision": _OLD_REVISION}) + "\n",
        encoding="utf-8",
    )
    source_state.chmod(0o600)
    promote_last_good(
        sandbox.backup_path,
        sandbox.env_file,
        source_state,
        _OLD_REVISION,
    )
    marker = tmp_path / "validated-pointer-drift.injected"
    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_VALIDATED_STATE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert first.returncode != 0
    pointer = sandbox.backup_path / "last-good-pointer.json"
    pointer.write_text("{}\n", encoding="utf-8")
    pointer.chmod(0o600)

    second = _run(sandbox)

    assert second.returncode != 0
    assert "LAST_GOOD_TARGET_FINALIZE_FAILED" in second.stderr


def test_update_lock_path_is_private_empty_and_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    lock_path = sandbox.backup_path / "serving-update.lock"
    target = tmp_path / "lock-target"
    target.write_text("do-not-touch\n", encoding="utf-8")
    lock_path.symlink_to(target)

    rejected = _run(sandbox)

    assert rejected.returncode != 0
    assert "SERVING_UPDATE_LOCK_INVALID" in rejected.stderr
    assert not list(sandbox.backup_path.glob("serving-updates/*/attempt-*"))
    lock_path.unlink()
    successful = _run(sandbox)
    assert successful.returncode == 0, successful.stderr
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert lock_path.read_bytes() == b""


def test_backup_symlink_and_missing_flock_fail_before_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    real_backup = tmp_path / "real-backup"
    sandbox.backup_path.rename(real_backup)
    sandbox.backup_path.symlink_to(real_backup, target_is_directory=True)

    symlink_result = _run(sandbox)

    assert symlink_result.returncode != 0
    assert "BACKUP_PATH_INVALID" in symlink_result.stderr
    sandbox.backup_path.unlink()
    real_backup.rename(sandbox.backup_path)
    no_flock = tmp_path / "no-flock-bin"
    no_flock.mkdir()
    for name in ("bash", "dirname", "realpath"):
        (no_flock / name).symlink_to(Path("/usr/bin") / name)
    environment = {
        **os.environ,
        "PATH": f"{sandbox.binaries}:{no_flock}",
        "FAKE_TARGET_SERVING": json.loads(
            (sandbox.package / "UPDATE_MANIFEST.json").read_bytes()
        )["serving_fingerprint"]["target"],
    }

    missing_flock = subprocess.run(  # noqa: S603
        [
            "/usr/bin/bash",
            str(sandbox.package / "update-app.sh"),
            str(sandbox.env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert missing_flock.returncode != 0
    assert "FLOCK_NOT_FOUND" in missing_flock.stderr
    assert not list(sandbox.backup_path.glob("serving-updates/*/attempt-*"))


def test_post_verified_manual_rollback_restores_source_pointer_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    checkpoint = json.loads(
        (transaction / "source-checkpoint.json").read_bytes()
    )
    source_snapshot_id = checkpoint["source_snapshot"]["snapshot_id"]
    target = resolve_last_good(sandbox.backup_path)
    target_snapshot = (
        sandbox.backup_path
        / "last-good-snapshots"
        / str(target["snapshot_id"])
    )
    assert target["revision"] == _NEW_REVISION
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"

    rolled_back = _run_manual_rollback(sandbox, transaction)

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert sandbox.env_file.read_text(encoding="utf-8") == (
        _source_recovery_env(transaction)
    )
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        _OLD_REVISION
    )
    source = resolve_last_good(sandbox.backup_path)
    assert source["revision"] == _OLD_REVISION
    assert source["snapshot_id"] == source_snapshot_id
    assert target_snapshot.is_dir()
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "rolled_back"
    precheck = json.loads(
        (transaction / "manual-rollback-precheck.json").read_bytes()
    )
    assert precheck == {
        "created_at": precheck["created_at"],
        "dependency_identity_checked": True,
        "index_identity_checked": True,
        "schema_version": "1",
        "target_container_state": "healthy",
        "target_pointer_checked": True,
        "target_revision": _NEW_REVISION,
        "target_runtime_state_checked": True,
        "target_static_identity_checked": True,
        "transaction_state": "verified",
    }
    assert stat.S_IMODE(
        (transaction / "manual-rollback-precheck.json").stat().st_mode
    ) == 0o600
    precheck_bytes = (
        transaction / "manual-rollback-precheck.json"
    ).read_bytes()
    assert b"http://" not in precheck_bytes
    assert ("q" * 32).encode() not in precheck_bytes
    assert ("a" * 32).encode() not in precheck_bytes
    repeated = _run_manual_rollback(sandbox, transaction)
    assert repeated.returncode != 0
    assert "MANUAL_ROLLBACK_REQUIRES_VERIFIED" in repeated.stderr
    assert resolve_last_good(sandbox.backup_path)["revision"] == _OLD_REVISION

    retried = _run(sandbox)

    assert retried.returncode == 0, retried.stderr
    assert _transaction(sandbox, attempt=2).is_dir()


@pytest.mark.parametrize(
    "scenario",
    (
        (
            "unhealthy",
            {
                "container_health": "unhealthy",
                "runtime_state_unavailable": True,
            },
            {},
            "unhealthy",
        ),
        (
            "ready-503",
            {},
            {"FAKE_READY_FAIL": "1"},
            "unhealthy",
        ),
        (
            "stopped",
            {"container_running": False},
            {},
            "stopped",
        ),
        (
            "missing",
            {"container_exists": False},
            {},
            "missing",
        ),
        (
            "runtime-state-unavailable",
            {"runtime_state_unavailable": True},
            {},
            "healthy",
        ),
    ),
)
def test_manual_rollback_restores_source_when_target_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: tuple[str, dict[str, object], dict[str, str], str],
) -> None:
    case, updates, environment, expected_state = scenario
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    target_snapshot = resolve_last_good(sandbox.backup_path)
    _configure_target_container(sandbox, **updates)

    rolled_back = _run_manual_rollback(
        sandbox,
        transaction,
        extra=environment,
        timeout=5 if case == "ready-503" else 60,
    )

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert sandbox.env_file.read_text(encoding="utf-8") == (
        _source_recovery_env(transaction)
    )
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        _OLD_REVISION
    )
    assert resolve_last_good(sandbox.backup_path)["revision"] == _OLD_REVISION
    assert (
        sandbox.backup_path
        / "last-good-snapshots"
        / str(target_snapshot["snapshot_id"])
    ).is_dir()
    precheck = json.loads(
        (transaction / "manual-rollback-precheck.json").read_bytes()
    )
    assert precheck["target_container_state"] == expected_state
    assert precheck["target_runtime_state_checked"] is (
        case not in {
            "unhealthy",
            "ready-503",
            "stopped",
            "missing",
            "runtime-state-unavailable",
        }
    )
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "rolled_back"
    assert _force_recreate_count(sandbox) == 2


@pytest.mark.parametrize(
    "updates",
    (
        {"container_image": "docx-rag:wrong-target"},
        {"container_image_id": "sha256:" + "8" * 64},
        {"container_revision": "c" * 40},
        {"project": "wrong-project"},
        {"service": "wrong-service"},
        {"target_image_id": "sha256:" + "7" * 64},
        {"target_oci_revision": "d" * 40},
    ),
)
def test_manual_rollback_wrong_target_identity_fails_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, object],
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_env = sandbox.env_file.read_bytes()
    before_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()
    _configure_target_container(sandbox, **updates)
    before_app = sandbox.docker_state.read_bytes()

    rejected = _run_manual_rollback(sandbox, transaction)

    assert rejected.returncode != 0
    assert "RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED" in rejected.stderr
    assert sandbox.env_file.read_bytes() == before_env
    assert sandbox.docker_state.read_bytes() == before_app
    assert (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes() == before_pointer
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"


def test_manual_rollback_current_env_drift_fails_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_app = sandbox.docker_state.read_bytes()
    before_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()
    sandbox.env_file.write_text(
        sandbox.env_file.read_text(encoding="utf-8")
        + "RAG_UNEXPECTED_DRIFT=1\n",
        encoding="utf-8",
    )
    sandbox.env_file.chmod(0o600)
    drifted_env = sandbox.env_file.read_bytes()

    rejected = _run_manual_rollback(sandbox, transaction)

    assert rejected.returncode != 0
    assert "RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED" in rejected.stderr
    assert sandbox.env_file.read_bytes() == drifted_env
    assert sandbox.docker_state.read_bytes() == before_app
    assert (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes() == before_pointer
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"


def test_manual_rollback_transient_precheck_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_env = sandbox.env_file.read_bytes()
    before_app = sandbox.docker_state.read_bytes()
    before_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()
    _configure_target_container(sandbox, FAKE_PRE_INDEX_FAIL=True)

    first = _run_manual_rollback(sandbox, transaction)

    assert first.returncode != 0
    assert "RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED" in first.stderr
    assert sandbox.env_file.read_bytes() == before_env
    first_app = json.loads(sandbox.docker_state.read_bytes())
    original_app = json.loads(before_app)
    assert first_app["revision"] == original_app["revision"]
    assert (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes() == before_pointer
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"

    _configure_target_container(sandbox, FAKE_PRE_INDEX_FAIL=False)
    second = _run_manual_rollback(sandbox, transaction)

    assert second.returncode == 0, second.stderr
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "rolled_back"


def test_manual_rollback_rolling_back_state_write_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_env = sandbox.env_file.read_bytes()
    before_app = sandbox.docker_state.read_bytes()
    before_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()

    rejected = _run_manual_rollback(
        sandbox,
        transaction,
        extra={"FAKE_TRANSACTION_STATE_WRITE_FAIL": "rolling_back"},
    )

    assert rejected.returncode != 0
    assert "RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED" in rejected.stderr
    assert sandbox.env_file.read_bytes() == before_env
    assert sandbox.docker_state.read_bytes() == before_app
    assert (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes() == before_pointer
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"


def test_manual_rollback_failure_after_env_restore_is_rollback_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    target_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()
    _configure_target_container(sandbox, fail_rollback=True)

    rejected = _run_manual_rollback(sandbox, transaction)

    assert rejected.returncode != 0
    assert "OLD_APP_RECREATE_FAILED" in rejected.stderr
    assert sandbox.env_file.read_text(encoding="utf-8") == (
        _source_recovery_env(transaction)
    )
    assert (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes() == target_pointer
    state = json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )
    assert state["state"] == "rollback_failed"
    assert state["error_code"] == "OLD_APP_RECREATE_FAILED"


def test_manual_rollback_obeys_global_update_lock_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_env = sandbox.env_file.read_bytes()
    before_app = json.loads(sandbox.docker_state.read_bytes())
    before_pointer = (
        sandbox.backup_path / "last-good-pointer.json"
    ).read_bytes()
    before_state = (transaction / "transaction-state.json").read_bytes()
    lock_path = sandbox.backup_path / "serving-update.lock"

    with lock_path.open("r+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        rejected = _run_manual_rollback(sandbox, transaction)

    assert rejected.returncode != 0
    assert "SERVING_UPDATE_ALREADY_RUNNING" in rejected.stderr
    assert sandbox.env_file.read_bytes() == before_env
    after_app = json.loads(sandbox.docker_state.read_bytes())
    assert after_app["image"] == before_app["image"]
    assert after_app["image_id"] == before_app["image_id"]
    assert after_app["revision"] == before_app["revision"]
    assert (sandbox.backup_path / "last-good-pointer.json").read_bytes() == (
        before_pointer
    )
    assert (transaction / "transaction-state.json").read_bytes() == before_state


def test_manual_rollback_holds_global_lock_against_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    held = tmp_path / "rollback-held"
    release = tmp_path / "rollback-release"
    rollback = _start_manual_rollback(
        sandbox,
        transaction,
        hold_marker=held,
        release_marker=release,
    )
    try:
        for _ in range(500):
            if held.exists() or rollback.poll() is not None:
                break
            time.sleep(0.01)
        assert held.is_file()

        rejected = _run(sandbox)

        assert rejected.returncode != 0
        assert "SERVING_UPDATE_ALREADY_RUNNING" in rejected.stderr
    finally:
        release.write_text("release\n", encoding="utf-8")
        rollback_stdout, rollback_stderr = rollback.communicate(timeout=30)
    assert rollback.returncode == 0, rollback_stderr or rollback_stdout
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "rolled_back"


@pytest.mark.parametrize(
    "failure",
    ("target-pointer", "source-snapshot", "index", "dependency"),
)
def test_manual_rollback_fails_closed_on_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)
    before_env = sandbox.env_file.read_bytes()
    before_app_revision = json.loads(sandbox.docker_state.read_bytes())[
        "revision"
    ]
    if failure == "target-pointer":
        pointer = sandbox.backup_path / "last-good-pointer.json"
        pointer.write_text("{}\n", encoding="utf-8")
        pointer.chmod(0o600)
    elif failure == "source-snapshot":
        checkpoint = json.loads(
            (transaction / "source-checkpoint.json").read_bytes()
        )
        source_env = (
            sandbox.backup_path
            / "last-good-snapshots"
            / checkpoint["source_snapshot"]["snapshot_id"]
            / "rag-industry.env"
        )
        source_env.write_text("tampered\n", encoding="utf-8")
        source_env.chmod(0o600)
    else:
        state = json.loads(sandbox.docker_state.read_bytes())
        state[
            "FAKE_PRE_INDEX_FAIL"
            if failure == "index"
            else "FAKE_DEPENDENCY_DRIFT"
        ] = True
        sandbox.docker_state.write_text(json.dumps(state), encoding="utf-8")

    rejected = _run_manual_rollback(sandbox, transaction)

    assert rejected.returncode != 0
    assert "RAG_INDUSTRY_APP_ROLLBACK_OK" not in rejected.stdout
    assert sandbox.env_file.read_bytes() == before_env
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        before_app_revision
    )
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"
    assert "RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED" in rejected.stderr


def test_manual_rollback_state_write_failure_never_claims_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    transaction = _transaction(sandbox)

    rejected = _run_manual_rollback(
        sandbox,
        transaction,
        extra={"FAKE_TRANSACTION_STATE_WRITE_FAIL": "rolled_back"},
    )

    assert rejected.returncode == 70
    assert "RAG_INDUSTRY_APP_ROLLBACK_OK" not in rejected.stdout
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "rollback_failed"


@pytest.mark.parametrize(
    ("failure_flag", "expected_state", "expected_app_revision"),
    (
        ("FAKE_CRASH_AFTER_ENV_SWAP", "activating", _OLD_REVISION),
        ("FAKE_CRASH_AFTER_ACTIVATED_STATE", "activated", _OLD_REVISION),
        ("FAKE_CRASH_AFTER_TARGET_COMPOSE", "activated", _NEW_REVISION),
    ),
)
def test_activation_hard_crash_recovers_same_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_flag: str,
    expected_state: str,
    expected_app_revision: str,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / f"{failure_flag}.injected"

    interrupted = _run(
        sandbox,
        extra={
            failure_flag: "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )

    assert interrupted.returncode != 0
    assert marker.is_file()
    transaction = _transaction(sandbox)
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == expected_state
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        expected_app_revision
    )

    recovered = _run(sandbox)

    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(
        (transaction / "transaction-state.json").read_bytes()
    )["state"] == "verified"
    assert not _transaction(sandbox, attempt=2).exists()


def test_healthy_target_activated_reentry_does_not_force_recreate_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "target-compose.inflight"
    interrupted = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_TARGET_COMPOSE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert interrupted.returncode != 0
    assert marker.is_file()
    assert _force_recreate_count(sandbox) == 1
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        _NEW_REVISION
    )

    recovered = _run(sandbox)

    assert recovered.returncode == 0, recovered.stderr
    assert _force_recreate_count(sandbox) == 1
    assert json.loads(
        (_transaction(sandbox) / "transaction-state.json").read_bytes()
    )["state"] == "verified"


def test_unhealthy_target_activated_reentry_recreates_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "target-unhealthy.inflight"
    interrupted = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_TARGET_COMPOSE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert interrupted.returncode != 0
    assert _force_recreate_count(sandbox) == 1

    recovered = _run(sandbox, extra={"FAKE_APP_UNHEALTHY": "1"})

    assert recovered.returncode == 0, recovered.stderr
    assert _force_recreate_count(sandbox) == 2
    repeated = _run(sandbox)
    assert repeated.returncode == 0, repeated.stderr
    assert _force_recreate_count(sandbox) == 2


def test_prechecking_crash_before_activation_intent_becomes_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "before-activation-intent.injected"
    interrupted = _run(
        sandbox,
        extra={
            "FAKE_CRASH_BEFORE_ACTIVATION_INTENT": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert interrupted.returncode != 0
    assert marker.is_file()
    first_attempt = _transaction(sandbox)
    assert not (first_attempt / "activation-intent.json").exists()
    assert json.loads(
        (first_attempt / "transaction-state.json").read_bytes()
    )["state"] == "prechecking"

    recovered = _run(sandbox)

    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(
        (first_attempt / "transaction-state.json").read_bytes()
    )["state"] == "precheck_failed"
    second_state = _transaction(sandbox, attempt=2) / "transaction-state.json"
    assert json.loads(
        second_state.read_bytes()
    )["state"] == "verified"


def test_activation_recovery_survives_a_second_hard_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    first_marker = tmp_path / "first-env-swap.injected"
    first = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_ENV_SWAP": "1",
            "FAKE_CRASH_MARKER": str(first_marker),
        },
    )
    assert first.returncode != 0
    assert first_marker.is_file()
    second_marker = tmp_path / "second-compose.injected"

    second = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_TARGET_COMPOSE": "1",
            "FAKE_CRASH_MARKER": str(second_marker),
        },
    )

    assert second.returncode != 0
    assert second_marker.is_file()
    assert json.loads(
        (_transaction(sandbox) / "transaction-state.json").read_bytes()
    )["state"] == "activated"
    third = _run(sandbox)
    assert third.returncode == 0, third.stderr
    assert not _transaction(sandbox, attempt=2).exists()
    assert _force_recreate_count(sandbox) == 1


def test_activation_mixed_source_env_target_app_rolls_back_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "mixed-identity.injected"
    interrupted = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_TARGET_COMPOSE": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert interrupted.returncode != 0
    sandbox.env_file.write_text(sandbox.old_env, encoding="utf-8")
    sandbox.env_file.chmod(0o600)

    rejected = _run(sandbox)

    assert rejected.returncode != 0
    assert "ACTIVATION_MIXED_IDENTITY_ROLLED_BACK" in rejected.stderr
    assert json.loads(sandbox.docker_state.read_bytes())["revision"] == (
        _OLD_REVISION
    )
    assert json.loads(
        (_transaction(sandbox) / "transaction-state.json").read_bytes()
    )["state"] == "rolled_back"


def test_activation_unknown_env_sha_fails_closed_without_app_or_pointer_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    marker = tmp_path / "unknown-env.injected"
    interrupted = _run(
        sandbox,
        extra={
            "FAKE_CRASH_AFTER_ENV_SWAP": "1",
            "FAKE_CRASH_MARKER": str(marker),
        },
    )
    assert interrupted.returncode != 0
    sandbox.env_file.write_text(
        sandbox.env_file.read_text(encoding="utf-8") + "UNKNOWN_DRIFT=1\n",
        encoding="utf-8",
    )
    sandbox.env_file.chmod(0o600)
    before_app = json.loads(sandbox.docker_state.read_bytes())

    rejected = _run(sandbox)

    assert rejected.returncode != 0
    assert "ACTIVATION_IDENTITY_UNKNOWN" in rejected.stderr
    after_app = json.loads(sandbox.docker_state.read_bytes())
    assert after_app["image"] == before_app["image"]
    assert after_app["image_id"] == before_app["image_id"]
    assert after_app["revision"] == before_app["revision"]
    assert not (sandbox.backup_path / "last-good-pointer.json").exists()
    assert json.loads(
        (_transaction(sandbox) / "transaction-state.json").read_bytes()
    )["state"] == "rollback_failed"


def test_activation_intent_is_private_complete_and_contains_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _prepare(tmp_path, monkeypatch)
    installed = _run(sandbox)
    assert installed.returncode == 0, installed.stderr
    intent_path = _transaction(sandbox) / "activation-intent.json"
    raw = intent_path.read_bytes()
    intent = json.loads(raw)

    assert stat.S_IMODE(intent_path.stat().st_mode) == 0o600
    assert set(intent) == {
        "attempt",
        "candidate_env_sha256",
        "created_at",
        "schema_version",
        "source_checkpoint",
        "source_compose_sha256",
        "source_config",
        "source_env_sha256",
        "source_image",
        "source_recovery_env_sha256",
        "source_recovery_image",
        "source_revision",
        "target_compose_sha256",
        "target_config",
        "target_image",
        "target_revision",
        "update_id",
    }
    assert b"q" * 32 not in raw
    assert b"a" * 32 not in raw
    assert b"embedding:8091" not in raw
