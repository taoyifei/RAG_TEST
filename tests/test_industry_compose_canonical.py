from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_DOCKER = "/usr/bin/docker"
_GIT = "/usr/bin/git"


def _compose_check() -> ModuleType:
    path = (
        _ROOT
        / "deployment"
        / "industry"
        / "serving_compose_check.py"
    )
    spec = importlib.util.spec_from_file_location("serving_compose_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(
    compose: Path,
    env_file: Path | None = None,
) -> dict[str, object]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RAG_")
    }
    result = subprocess.run(  # noqa: S603
        [
            _DOCKER,
            "compose",
            "--env-file",
            str(
                env_file
                or _ROOT / "deployment" / "industry" / ".env.example"
            ),
            "-f",
            str(compose),
            "--profile",
            "index",
            "--profile",
            "dedicated-ocr",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _minimal_models() -> tuple[dict[str, object], dict[str, object]]:
    old = {
        "name": "rag-industry",
        "networks": {"internal": {"internal": True}},
        "services": {
            "rag-industry-app": {
                "environment": {
                    "RAG_RELEASE_REVISION": _OLD_REVISION,
                    "RAG_RUN_MODE": "demo",
                },
                "image": "old-image",
                "ports": [{"published": "8188", "target": 8088}],
                "volumes": [
                    {
                        "read_only": read_only,
                        "source": source,
                        "target": target,
                        "type": "bind",
                    }
                    for source, target, read_only in (
                        ("/old/config", "/config", True),
                        ("/docs", "/data/docs", True),
                        ("/logs", "/logs", False),
                        ("/state", "/state", False),
                    )
                ],
            },
            "rag-industry-ocr": {"image": "ocr"},
            "rag-industry-qdrant": {"image": "qdrant"},
        },
    }
    new = json.loads(json.dumps(old))
    app = new["services"]["rag-industry-app"]
    app["image"] = "new-image"
    app["volumes"][0]["source"] = "/new/config"
    app["environment"].update(
        {
            "RAG_RELEASE_REVISION": "b" * 40,
            "RAG_TRACE_QUESTION_CAPTURE": "plaintext",
            "RAG_TRACE_QUESTION_RETENTION_SECONDS": "604800",
            "RAG_UI_ALLOW_INSECURE_HTTP": "true",
            "RAG_UI_COOKIE_SECURE": "false",
            "RAG_UI_QUERY_AUTH_MODE": "same_origin_session",
            "RAG_UI_SESSION_TTL_SECONDS": "1800",
        }
    )
    return old, new


def test_port_normalization_accepts_real_canonical_default_fields() -> None:
    module = _compose_check()
    compare = getattr(module, "compare_compose_models", None)
    assert callable(compare)
    old = {
        "name": "rag-industry",
        "networks": {"internal": {"internal": True}},
        "services": {
            "rag-industry-app": {
                "environment": {
                    "RAG_RELEASE_REVISION": _OLD_REVISION,
                    "RAG_RUN_MODE": "demo",
                },
                "image": "old-image",
                "ports": [
                    {
                        "host_ip": "0.0.0.0",  # noqa: S104
                        "mode": "ingress",
                        "protocol": "tcp",
                        "published": "8188",
                        "target": 8088,
                    }
                ],
                "volumes": [
                    {
                        "read_only": True,
                        "source": "/old/config",
                        "target": "/config",
                        "type": "bind",
                    },
                    {
                        "read_only": False,
                        "source": "/state",
                        "target": "/state",
                        "type": "bind",
                    },
                    {
                        "read_only": True,
                        "source": "/docs",
                        "target": "/data/docs",
                        "type": "bind",
                    },
                    {
                        "read_only": False,
                        "source": "/logs",
                        "target": "/logs",
                        "type": "bind",
                    },
                ],
            },
            "rag-industry-ocr": {"image": "ocr"},
            "rag-industry-qdrant": {"image": "qdrant"},
        },
    }
    new = json.loads(json.dumps(old))
    app = new["services"]["rag-industry-app"]
    app["image"] = "new-image"
    app["volumes"][0]["source"] = "/new/config"
    app["environment"].update(
        {
            "RAG_RELEASE_REVISION": "b" * 40,
            "RAG_TRACE_QUESTION_CAPTURE": "plaintext",
            "RAG_TRACE_QUESTION_RETENTION_SECONDS": "604800",
            "RAG_UI_ALLOW_INSECURE_HTTP": "true",
            "RAG_UI_COOKIE_SECURE": "false",
            "RAG_UI_QUERY_AUTH_MODE": "same_origin_session",
            "RAG_UI_SESSION_TTL_SECONDS": "1800",
        }
    )

    report = compare(
        old,
        new,
        source_image="old-image",
        source_revision=_OLD_REVISION,
        target_config="/new/config",
        target_image="new-image",
        target_revision="b" * 40,
        source_config="/old/config",
    )

    assert report["app_port"] == {
        "host_ip": "",
        "mode": "ingress",
        "protocol": "tcp",
        "published": "8188",
        "target": 8088,
    }


def test_real_old_and_new_compose_canonical_json_are_supported(
    tmp_path: Path,
) -> None:
    module = _compose_check()
    compare = getattr(module, "compare_compose_models", None)
    assert callable(compare)
    old_compose = tmp_path / "old-compose.yaml"
    old_source = subprocess.run(  # noqa: S603
        [
            _GIT,
            "show",
            f"{_OLD_REVISION}:deployment/industry/compose.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=_ROOT,
    ).stdout
    old_compose.write_text(old_source, encoding="utf-8")
    example = (
        _ROOT / "deployment" / "industry" / ".env.example"
    ).read_text(encoding="utf-8")
    old_env = tmp_path / "old.env"
    target_env = tmp_path / "target.env"
    old_env.write_text(
        example.replace("REPLACE_APP_IMAGE", "old-image")
        .replace("REPLACE_FULL_GIT_SHA", _OLD_REVISION)
        .replace(
            "/data/tyf/RAG-industry/data/REPLACE_RELEASE_ID/config",
            "/old/config",
        ),
        encoding="utf-8",
    )
    target_env.write_text(
        example.replace("REPLACE_APP_IMAGE", "new-image")
        .replace("REPLACE_FULL_GIT_SHA", "b" * 40)
        .replace(
            "/data/tyf/RAG-industry/data/REPLACE_RELEASE_ID/config",
            "/new/config",
        ),
        encoding="utf-8",
    )
    old = _render(old_compose, old_env)
    new = _render(
        _ROOT / "deployment" / "industry" / "compose.yaml", target_env
    )
    old_app = old["services"]["rag-industry-app"]
    new_app = new["services"]["rag-industry-app"]
    old_config = next(
        item["source"]
        for item in old_app["volumes"]
        if item["target"] == "/config"
    )
    new_config = next(
        item["source"]
        for item in new_app["volumes"]
        if item["target"] == "/config"
    )

    report = compare(
        old,
        new,
        source_image=old_app["image"],
        source_revision=_OLD_REVISION,
        target_config=new_config,
        target_image=new_app["image"],
        target_revision="b" * 40,
        source_config=old_config,
    )

    assert report["app_port"]["published"] == "8188"


@pytest.mark.parametrize(
    "mutation",
    ("extra_port", "host_ip", "protocol", "unknown_field"),
)
def test_port_security_changes_fail_closed(mutation: str) -> None:
    module = _compose_check()
    old, new = _minimal_models()
    port = new["services"]["rag-industry-app"]["ports"][0]
    if mutation == "extra_port":
        new["services"]["rag-industry-app"]["ports"].append(
            {"published": "8288", "target": 8088}
        )
    elif mutation == "host_ip":
        port["host_ip"] = "127.0.0.1"
    elif mutation == "protocol":
        port["protocol"] = "udp"
    else:
        port["security_opt"] = "unexpected"

    with pytest.raises(module.ComposeCheckError):
        module.compare_compose_models(
            old,
            new,
            source_image="old-image",
            source_revision=_OLD_REVISION,
            target_config="/new/config",
            target_image="new-image",
            target_revision="b" * 40,
            source_config="/old/config",
        )


def test_volume_permission_change_fails_closed() -> None:
    module = _compose_check()
    old, new = _minimal_models()
    new["services"]["rag-industry-app"]["volumes"][1]["read_only"] = False

    with pytest.raises(module.ComposeCheckError, match="APP_VOLUME"):
        module.compare_compose_models(
            old,
            new,
            source_image="old-image",
            source_revision=_OLD_REVISION,
            target_config="/new/config",
            target_image="new-image",
            target_revision="b" * 40,
            source_config="/old/config",
        )


def test_real_update_rejects_equal_source_and_target_revision() -> None:
    module = _compose_check()
    old, new = _minimal_models()
    new["services"]["rag-industry-app"]["environment"][
        "RAG_RELEASE_REVISION"
    ] = _OLD_REVISION

    with pytest.raises(
        module.ComposeCheckError,
        match="RELEASE_REVISION_TRANSITION_INVALID",
    ):
        module.compare_compose_models(
            old,
            new,
            source_config="/old/config",
            source_image="old-image",
            source_revision=_OLD_REVISION,
            target_config="/new/config",
            target_image="new-image",
            target_revision=_OLD_REVISION,
        )
