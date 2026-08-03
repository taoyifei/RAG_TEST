from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_model_deployment_manifest as manifest_builder

_SCRIPT = (
    Path(__file__).parents[1] / "scripts/build_model_deployment_manifest.py"
)


def _arguments(service: str, output: Path) -> list[str]:
    arguments = [
        sys.executable,
        str(_SCRIPT),
        service,
        "--endpoint",
        "http://model.internal:8000",
        "--model",
        f"model-{service}",
        "--model-revision",
        "model-revision-test",
        "--tokenizer-revision",
        "sha256:" + "1" * 64,
        "--runtime-name",
        "runtime-test",
        "--runtime-version",
        "1.9.1",
        "--runtime-revision",
        "2" * 40,
        "--output",
        str(output),
    ]
    if service == "embedding":
        return [*arguments, "--dimension", "1024"]
    if service == "reranker":
        return [
            *arguments,
            "--score-min",
            "0",
            "--score-max",
            "1",
        ]
    return [
        *arguments,
        "--quantization",
        "awq",
        "--max-context-tokens",
        "8192",
        "--chat-template-sha256",
        "sha256:" + "3" * 64,
    ]


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        arguments,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("service", "contract_fields"),
    [
        ("embedding", {"dimension"}),
        ("reranker", {"score_min", "score_max"}),
        (
            "llm",
            {
                "quantization",
                "max_context_tokens",
                "chat_template_sha256",
            },
        ),
    ],
)
def test_builder_writes_service_specific_read_only_manifest(
    tmp_path: Path,
    service: str,
    contract_fields: set[str],
) -> None:
    output = tmp_path / f"{service}.json"

    completed = _run(_arguments(service, output))

    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "service",
        "endpoint",
        "model",
        "model_revision",
        "tokenizer_revision",
        "runtime",
        "service_contract",
        "manifest_sha256",
    }
    assert set(payload["runtime"]) == {"name", "version", "revision"}
    assert set(payload["service_contract"]) == contract_fields
    declared_digest = payload.pop("manifest_sha256")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert declared_digest == (
        f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    )


def test_builder_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "embedding.json"
    output.write_text("preserve", encoding="utf-8")

    completed = _run(_arguments("embedding", output))

    assert completed.returncode != 0
    assert output.read_text(encoding="utf-8") == "preserve"
    assert not tuple(tmp_path.glob(".embedding.json.*.tmp"))


def test_builder_rejects_cross_service_contract_fields(
    tmp_path: Path,
) -> None:
    output = tmp_path / "embedding.json"
    arguments = [
        *_arguments("embedding", output),
        "--score-min",
        "0",
        "--score-max",
        "1",
    ]

    completed = _run(arguments)

    assert completed.returncode != 0
    assert not output.exists()


def test_builder_rejects_non_origin_endpoint(tmp_path: Path) -> None:
    output = tmp_path / "embedding.json"
    arguments = _arguments("embedding", output)
    endpoint_index = arguments.index("--endpoint") + 1
    arguments[endpoint_index] = "http://model.internal:8000/v1"

    completed = _run(arguments)

    assert completed.returncode != 0
    assert "origin 根 URL" in completed.stderr
    assert not output.exists()


def test_builder_refuses_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("preserve", encoding="utf-8")
    output = tmp_path / "embedding.json"
    output.symlink_to(target)

    completed = _run(_arguments("embedding", output))

    assert completed.returncode != 0
    assert output.is_symlink()
    assert target.read_text(encoding="utf-8") == "preserve"


def test_builder_publish_failure_leaves_no_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "embedding.json"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic atomic publish failure")

    monkeypatch.setattr(manifest_builder.os, "link", fail_link)

    exit_code = manifest_builder.main(_arguments("embedding", output)[2:])

    assert exit_code == 1
    assert not output.exists()
    assert not tuple(tmp_path.glob(".embedding.json.*.tmp"))
