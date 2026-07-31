from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml

_PIPELINE_SHA256 = (
    "87734d37e2fab9d08585b84adf65a61751af1021b74f888195cc3c5f37d54bbf"
)
_RETRIEVAL_SHA256 = (
    "267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3"
)


def _root() -> Path:
    return Path(__file__).parents[1]


def _compose_services(*, profile: str | None = None) -> set[str]:
    root = _root()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(root / "deployment/.env.example"),
        "-f",
        str(root / "deployment/compose.yaml"),
    ]
    if profile is not None:
        command.extend(("--profile", profile))
    command.extend(("config", "--services"))
    completed = subprocess.run(  # noqa: S603
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(completed.stdout.splitlines())


def _compose_environment(
    service_name: str,
    *,
    profile: str | None = None,
) -> dict[str, str]:
    root = _root()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(root / "deployment/.env.example"),
        "-f",
        str(root / "deployment/compose.yaml"),
    ]
    if profile is not None:
        command.extend(("--profile", profile))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(  # noqa: S603
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    compose = json.loads(completed.stdout)
    environment = compose["services"][service_name].get("environment", {})
    return {
        str(name): str(value)
        for name, value in environment.items()
    }


def _assert_shared_access_mode(
    service_name: str,
    *,
    profile: str | None = None,
) -> None:
    example_lines = (
        _root() / "deployment/.env.example"
    ).read_text(encoding="utf-8").splitlines()
    assert "RAG_ACCESS_MODE=shared_corpus" in example_lines
    environment = _compose_environment(service_name, profile=profile)
    assert environment.get("RAG_ACCESS_MODE") == "shared_corpus"


def test_worker_requires_explicit_index_profile() -> None:
    assert "rag-worker" not in _compose_services()
    assert "rag-worker" in _compose_services(profile="index")


def test_app_receives_explicit_shared_access_mode() -> None:
    _assert_shared_access_mode("rag-app")


def test_worker_receives_explicit_shared_access_mode() -> None:
    _assert_shared_access_mode("rag-worker", profile="index")


def test_access_mode_mapping_is_limited_to_app_and_worker() -> None:
    compose = yaml.safe_load(
        (_root() / "deployment/compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    recipients = {
        service_name
        for service_name, service in services.items()
        if "RAG_ACCESS_MODE" in service.get("environment", {})
    }

    assert recipients == {"rag-app", "rag-worker"}
    for service_name in recipients:
        assert (
            services[service_name]["environment"]["RAG_ACCESS_MODE"]
            == "${RAG_ACCESS_MODE:?required}"
        )


def test_compose_rejects_missing_access_mode(tmp_path: Path) -> None:
    root = _root()
    example_lines = (
        root / "deployment/.env.example"
    ).read_text(encoding="utf-8").splitlines()
    missing_access_mode_env = tmp_path / "missing-access-mode.env"
    missing_access_mode_env.write_text(
        "\n".join(
            line
            for line in example_lines
            if not line.startswith("RAG_ACCESS_MODE=")
        )
        + "\n",
        encoding="utf-8",
    )
    process_environment = os.environ.copy()
    process_environment.pop("RAG_ACCESS_MODE", None)

    for profile in (None, "index"):
        command = [
            "docker",
            "compose",
            "--env-file",
            str(missing_access_mode_env),
            "-f",
            str(root / "deployment/compose.yaml"),
        ]
        if profile is not None:
            command.extend(("--profile", profile))
        command.extend(("config", "--quiet"))
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=process_environment,
            text=True,
        )

        assert completed.returncode != 0
        assert "RAG_ACCESS_MODE" in completed.stderr


def test_worker_keeps_strict_image_and_finite_restart_policy() -> None:
    compose = yaml.safe_load(
        (_root() / "deployment/compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    app = services["rag-app"]
    worker = services["rag-worker"]

    assert worker["profiles"] == ["index"]
    assert worker["image"] == app["image"]
    assert worker.get("restart") != "unless-stopped"
    assert "rag-worker" not in app.get("depends_on", {})
    assert sum("ports" in service for service in services.values()) == 1


def test_provisional_configuration_files_remain_unchanged() -> None:
    root = _root()
    pipeline = (root / "deployment/config/pipeline.json").read_bytes()
    retrieval = (root / "deployment/config/retrieval.json").read_bytes()

    assert hashlib.sha256(pipeline).hexdigest() == _PIPELINE_SHA256
    assert hashlib.sha256(retrieval).hexdigest() == _RETRIEVAL_SHA256
