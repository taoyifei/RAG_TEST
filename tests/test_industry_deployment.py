"""Industry 第二套 simple 部署的硬隔离专项。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deployment.industry import runtime_check
from rag_app.manifest import ManifestRepository


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _compose_payload(
    *,
    env_file: Path | None = None,
    profiles: tuple[str, ...] = ("index", "dedicated-ocr"),
) -> dict[str, object]:
    docker = shutil.which("docker")
    assert docker is not None
    root = _root()
    arguments = [
        docker,
        "compose",
        "--env-file",
        str(env_file or root / "deployment/industry/.env.example"),
        "-f",
        str(root / "deployment/industry/compose.yaml"),
    ]
    for profile in profiles:
        arguments.extend(("--profile", profile))
    arguments.extend(("config", "--format", "json"))
    completed = subprocess.run(  # noqa: S603
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _volume_map(service: dict[str, object]) -> dict[str, dict[str, object]]:
    volumes = service["volumes"]
    assert isinstance(volumes, list)
    return {
        item["target"]: item
        for item in volumes
        if isinstance(item, dict) and isinstance(item.get("target"), str)
    }


def test_industry_compose_is_a_hard_isolated_second_simple() -> None:
    root = _root()
    industry = _compose_payload()
    simple_docker = shutil.which("docker")
    assert simple_docker is not None
    simple_result = subprocess.run(  # noqa: S603
        [
            simple_docker,
            "compose",
            "--env-file",
            str(root / "deployment/simple/.env.example"),
            "-f",
            str(root / "deployment/simple/compose.yaml"),
            "--profile",
            "index",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    simple = json.loads(simple_result.stdout)
    services = industry["services"]
    assert isinstance(services, dict)
    expected_services = {
        "rag-industry-app",
        "rag-industry-worker",
        "rag-industry-qdrant",
        "rag-industry-ocr",
    }

    assert industry["name"] == "rag-industry"
    assert simple["name"] == "rag-simple"
    assert set(services) == expected_services
    container_names = {
        service["container_name"] for service in services.values()
    }
    simple_container_names = {
        service["container_name"] for service in simple["services"].values()
    }
    assert container_names.isdisjoint(simple_container_names)
    industry_network_names = {
        network["name"] for network in industry["networks"].values()
    }
    simple_network_names = {
        network["name"] for network in simple["networks"].values()
    }
    assert industry_network_names.isdisjoint(simple_network_names)

    app = services["rag-industry-app"]
    worker = services["rag-industry-worker"]
    qdrant = services["rag-industry-qdrant"]
    ocr = services["rag-industry-ocr"]
    assert app["image"] == worker["image"]
    assert worker["profiles"] == ["index"]
    assert ocr["profiles"] == ["dedicated-ocr"]
    assert app["ports"][0]["published"] == "8188"
    assert all("ports" not in service for service in (worker, qdrant, ocr))
    assert app["environment"]["RAG_QDRANT_ALIAS"] == "rag-industry-active"
    assert worker["environment"]["RAG_QDRANT_ALIAS"] == "rag-industry-active"
    assert app["environment"]["RAG_QDRANT_URL"] == (
        "http://rag-industry-qdrant:6333"
    )

    app_mounts = _volume_map(app)
    worker_mounts = _volume_map(worker)
    assert app_mounts == worker_mounts
    assert app_mounts["/data/docs"]["read_only"] is True
    assert app_mounts["/config"]["read_only"] is True
    assert "/reference" not in app_mounts
    assert "/data/reference" not in app_mounts
    assert qdrant["volumes"][0]["target"] == "/qdrant/storage"
    assert qdrant["volumes"][0]["source"] != (
        simple["services"]["rag-qdrant"]["volumes"][0]["source"]
    )


def test_default_stack_does_not_start_worker_or_dedicated_ocr() -> None:
    docker = shutil.which("docker")
    assert docker is not None
    root = _root()
    completed = subprocess.run(  # noqa: S603
        [
            docker,
            "compose",
            "--env-file",
            str(root / "deployment/industry/.env.example"),
            "-f",
            str(root / "deployment/industry/compose.yaml"),
            "config",
            "--services",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert set(completed.stdout.splitlines()) == {
        "rag-industry-app",
        "rag-industry-qdrant",
    }


def test_app_and_worker_keep_read_only_security_contract() -> None:
    services = _compose_payload()["services"]
    assert isinstance(services, dict)

    for name in ("rag-industry-app", "rag-industry-worker"):
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] == 256
        assert service["tmpfs"] == [
            "/tmp:size=64m,mode=1777"  # noqa: S108
        ]
        assert service["pull_policy"] == "never"
    assert services["rag-industry-ocr"]["cap_drop"] == ["ALL"]
    assert services["rag-industry-qdrant"]["environment"][
        "QDRANT__TELEMETRY_DISABLED"
    ] == "true"


def test_dedicated_and_external_ocr_envs_both_validate(
    tmp_path: Path,
) -> None:
    root = _root()
    source = (root / "deployment/industry/.env.example").read_text(
        encoding="utf-8"
    )
    dedicated = tmp_path / "dedicated.env"
    external = tmp_path / "external.env"
    dedicated.write_text(source, encoding="utf-8")
    external.write_text(
        source.replace(
            "RAG_OCR_MODE=dedicated",
            "RAG_OCR_MODE=external",
        ).replace(
            "RAG_OCR_ENDPOINTS='[\"http://rag-industry-ocr:8090\"]'",
            "RAG_OCR_ENDPOINTS='[\"http://verified-ocr:8090\"]'",
        ),
        encoding="utf-8",
    )

    dedicated_payload = _compose_payload(env_file=dedicated)
    external_payload = _compose_payload(env_file=external, profiles=("index",))

    assert dedicated_payload["services"]["rag-industry-worker"][
        "environment"
    ]["RAG_OCR_ENDPOINTS"] == '["http://rag-industry-ocr:8090"]'
    assert external_payload["services"]["rag-industry-worker"][
        "environment"
    ]["RAG_OCR_ENDPOINTS"] == '["http://verified-ocr:8090"]'
    assert "rag-industry-ocr" not in external_payload["services"]


def test_industry_env_paths_tokens_and_alias_are_independent() -> None:
    root = _root()
    lines = (
        root / "deployment/industry/.env.example"
    ).read_text(encoding="utf-8").splitlines()
    values = {
        key: value
        for line in lines
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", maxsplit=1),)
    }
    path_keys = (
        "RAG_STATE_PATH",
        "RAG_QDRANT_PATH",
        "RAG_DOCS_PATH",
        "RAG_REFERENCE_PATH",
        "RAG_CONFIG_PATH",
        "RAG_LOGS_PATH",
        "RAG_BACKUP_PATH",
        "RAG_RELEASE_ROOT",
    )
    token_keys = (
        "RAG_QUERY_TOKEN",
        "RAG_ADMIN_TOKEN",
        "RAG_QDRANT_API_KEY",
        "RAG_OCR_API_TOKEN",
    )

    assert len({values[key] for key in path_keys}) == len(path_keys)
    assert all(value.startswith("/data/tyf/RAG-industry/") for value in (
        values[key] for key in path_keys
    ))
    assert len({values[key] for key in token_keys}) == len(token_keys)
    assert all("INDUSTRY" in values[key] for key in token_keys)
    assert values["RAG_PORT"] == "8188"
    assert values["RAG_QDRANT_ALIAS"] == "rag-industry-active"


def test_industry_compose_has_no_training_runtime_identity() -> None:
    compose = (
        _root() / "deployment/industry/compose.yaml"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "rag-simple",
        "container_name: rag-app",
        "container_name: rag-worker",
        "container_name: rag-qdrant",
        "container_name: rag-ocr",
        "rag-docx-active",
        "/data/tyf/RAG/data",
        "docs/RAG资料库",
    ):
        assert forbidden not in compose


def test_rollback_is_strictly_scoped_to_industry_project() -> None:
    root = _root()
    rollback = (
        root / "deployment/industry/rollback.sh"
    ).read_text(encoding="utf-8")

    library = (root / "deployment/industry/lib.sh").read_text(
        encoding="utf-8"
    )

    assert "-p rag-industry" in library
    assert "run_industry_compose" in rollback
    assert "rag-industry-qdrant" in rollback
    assert "rag-industry-app" in rollback
    assert "docker compose down" not in rollback
    assert "rag-simple" not in rollback
    assert "rag-app " not in rollback
    assert "rag-qdrant " not in rollback


@pytest.mark.parametrize(
    "script_name",
    ("run-index.sh", "rollback.sh"),
)
def test_index_rollback_one_off_has_only_required_capabilities(
    script_name: str,
) -> None:
    script = (
        _root() / f"deployment/industry/{script_name}"
    ).read_text(encoding="utf-8")

    assert "--user 0:0" in script
    assert script.count("--cap-add DAC_OVERRIDE") == 1
    assert script.count("--cap-add CHOWN") == 1
    assert "--privileged" not in script


def test_root_owned_runtime_checks_use_explicit_root_user() -> None:
    root = _root() / "deployment/industry"
    run_index = (root / "run-index.sh").read_text(encoding="utf-8")
    verify = (root / "verify.sh").read_text(encoding="utf-8")

    assert run_index.count("--user 0:0") >= 2
    assert "--user 0:0" in verify
    assert 'runtime_check="${script_dir}/runtime_check.py"' in run_index
    assert "INDEX_RESUME_CORPUS_MISMATCH" in run_index


def test_training_simple_deployment_remains_scoped_to_training_identity(
) -> None:
    simple = _root() / "deployment/simple"
    compose = (simple / "compose.yaml").read_text(encoding="utf-8")
    deploy = (simple / "deploy.sh").read_text(encoding="utf-8")
    update = (simple / "update-app.sh").read_text(encoding="utf-8")

    assert "name: rag-simple" in compose
    assert "rag-industry" not in compose
    assert "-p rag-simple" in deploy
    assert "-p rag-simple" in update
    assert "--force-recreate rag-app" in deploy
    assert "--force-recreate rag-app" in update
    assert "env -i" in deploy
    assert "env -i" in update


def test_industry_smoke_and_expected_corpus_match_user_decision() -> None:
    root = _root()
    expected = json.loads(
        (root / "evaluation/industry/expected-corpus.json").read_bytes()
    )
    smoke = [
        json.loads(line)
        for line in (
            root / "evaluation/industry/smoke.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]

    assert len(expected["active_documents"]) == 10
    assert expected["reference_documents"] == []
    assert all(name.startswith("GM-") for name in expected["active_documents"])
    assert len(smoke) == 20
    assert len({case["id"] for case in smoke}) == 20
    assert sum(
        case["expected_outcome"] == "not_found_or_refused" for case in smoke
    ) == 4


def test_index_rollback_capture_and_restore_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state/manifest.sqlite3"
    database.parent.mkdir()
    ManifestRepository(database).initialize()
    backup = tmp_path / "backup"
    backup.mkdir()
    snapshot = backup / "manifest-before-job.sqlite3"

    class FakeQdrantClient:
        def get_aliases(self) -> SimpleNamespace:
            return SimpleNamespace(aliases=[])

        def update_collection_aliases(self, operations: object) -> None:
            raise AssertionError(operations)

        def get_collection(self, collection: str) -> None:
            raise AssertionError(collection)

        def close(self) -> None:
            return None

    monkeypatch.setenv("RAG_MANIFEST_DATABASE", str(database))
    monkeypatch.setenv("RAG_QDRANT_ALIAS", "rag-industry-active")
    monkeypatch.setattr(
        runtime_check,
        "_qdrant_client",
        FakeQdrantClient,
    )
    captured = runtime_check.capture_index_rollback(snapshot)
    assert captured["previous_collection"] is None
    assert snapshot.is_file()
    assert runtime_check.describe_index_rollback(snapshot) == captured

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE drift(value TEXT)")
    captured["current_revision"] = "a" * 40
    descriptor = backup / "last-index-rollback.json"
    descriptor.write_text(
        json.dumps(captured, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restored = runtime_check.restore_index_rollback(descriptor)
    assert restored == {
        "alias": "rag-industry-active",
        "collection_restored": False,
    }
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "drift" not in tables


def test_job_state_is_validated_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request_json(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {"job_id": "job_expected", "state": "succeeded"}

    monkeypatch.setattr(runtime_check, "_request_json", request_json)

    assert runtime_check.get_job_state(
        "http://127.0.0.1:8188",
        "job_expected",
        "token",
    ) == "succeeded"

    def invalid_request(
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        del args, kwargs
        return {"job_id": "job_other", "state": "unknown"}

    monkeypatch.setattr(runtime_check, "_request_json", invalid_request)
    with pytest.raises(runtime_check.RuntimeCheckError):
        runtime_check.get_job_state(
            "http://127.0.0.1:8188",
            "job_expected",
            "token",
        )


def test_preflight_allows_only_owned_industry_app_on_busy_port() -> None:
    preflight = (
        _root() / "deployment/industry/preflight.sh"
    ).read_text(encoding="utf-8")

    assert "INDUSTRY_PORT_UNAVAILABLE" in preflight
    assert 'project}" == "rag-industry"' in preflight
    assert "docker port rag-industry-app 8088/tcp" in preflight


def test_reuse_package_verifies_existing_images_before_install() -> None:
    root = _root() / "deployment/industry"
    preflight = (root / "preflight.sh").read_text(encoding="utf-8")
    install = (root / "install.sh").read_text(encoding="utf-8")
    contract = json.loads(
        (root / "package-contract-reuse-images.json").read_bytes()
    )

    assert contract["release_kind"] == (
        "industry-first-deploy-reuse-images"
    )
    assert set(contract["required_archives"]) == {
        "app-image.tar.gz",
        "corpus.tar.gz",
    }
    assert "ocr-image.tar.gz" not in contract["required_files"]
    assert "qdrant-image.tar.gz" not in contract["required_files"]
    assert "SERVER_IMAGE_IDENTITY_MISMATCH" in preflight
    assert 'image["delivery"] == "server-existing"' in preflight
    assert "case \"${delivery}\"" in install
    assert "server-existing)" in install


def test_upload_commands_use_shared_server_transfer_directory() -> None:
    commands = (
        _root() / "deployment/industry/SERVER_UPLOAD_COMMANDS.txt"
    ).read_text(encoding="utf-8")

    assert "~/rag-industry-transfer" not in commands
    assert "/data/tyf/RAG/industry-transfer" in commands
    assert "user4a@10.242.180.54" in commands
    assert "user4a@10.242.180.60" in commands


def test_rollback_restores_only_industry_alias_and_manifest_state() -> None:
    root = _root()
    rollback = (
        root / "deployment/industry/rollback.sh"
    ).read_text(encoding="utf-8")
    runtime = (
        root / "deployment/industry/runtime_check.py"
    ).read_text(encoding="utf-8")

    assert "restore-index-rollback" in rollback
    assert "last-index-rollback.json" in rollback
    assert "rag-industry-active" in runtime
    assert "RAG_MANIFEST_DATABASE" in runtime
    assert "rag-docx-active" not in rollback
