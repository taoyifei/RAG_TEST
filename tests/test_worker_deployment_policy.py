from __future__ import annotations

import hashlib
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


def test_worker_requires_explicit_index_profile() -> None:
    assert "rag-worker" not in _compose_services()
    assert "rag-worker" in _compose_services(profile="index")


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
