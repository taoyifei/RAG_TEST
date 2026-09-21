"""准备阶段候选配置门禁保留旧语义并隔离可写路径。"""

from __future__ import annotations

import json
import stat
from pathlib import Path, PurePosixPath

from scripts import wb08r_prep_runtime_guard as guard


def _environment() -> dict[str, str]:
    return {
        "FORWARDED_ALLOW_IPS": "127.0.0.1",
        "RAG_PRODUCT_MODE": "wanshitong",
        "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_DIR": "/private-diagnostics",
        "RAG_WANSHITONG_LLM_STRUCTURED_OUTPUT_MODE": "guided_json",
    }


def _host_config() -> dict[str, object]:
    return {
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": "size=64m,mode=1777"},  # noqa: S108
        "PidsLimit": 256,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ExtraHosts": ["host.docker.internal:host-gateway"],
        "RestartPolicy": {"Name": "unless-stopped"},
        "PortBindings": {
            "8088/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8289"}]
        },
    }


def _config(revision: str) -> dict[str, object]:
    environment = [f"{key}={value}" for key, value in _environment().items()]
    environment.append("PATH=/usr/local/bin")
    return {
        "Env": environment,
        "User": "rag:rag",
        "WorkingDir": "/app",
        "Entrypoint": ["rag-app"],
        "Cmd": ["serve"],
        "Healthcheck": {"Test": ["CMD", "health"]},
        "Labels": {
            "org.opencontainers.image.base.digest": "sha256:base",
            "org.opencontainers.image.base.name": "python:3.11",
            "org.opencontainers.image.revision": revision,
        },
    }


def _mounts(root: str) -> list[dict[str, object]]:
    return [
        {
            "Destination": "/data",
            "Source": f"{root}/data",
            "Type": "bind",
            "RW": True,
        },
        {
            "Destination": "/logs",
            "Source": f"{root}/logs",
            "Type": "bind",
            "RW": True,
        },
        {
            "Destination": "/private-diagnostics",
            "Source": f"{root}/private-diagnostics",
            "Type": "bind",
            "RW": True,
        },
        {
            "Destination": "/run/rag-secrets",
            "Source": "/baseline/secrets",
            "Type": "bind",
            "RW": False,
        },
    ]


def _container(*, candidate: bool) -> dict[str, object]:
    root = "/candidate" if candidate else "/baseline"
    network_settings = {
        "internal": {
            "Aliases": (
                ["wanshitong-prep-candidate-app", "app"]
                if candidate
                else ["wanshitong-app"]
            )
        },
        "egress": {
            "Aliases": (
                ["wanshitong-prep-candidate-app", "app"]
                if candidate
                else ["wanshitong-app"]
            )
        },
    }
    config = _config("candidate" if candidate else "baseline")
    if candidate:
        config["Labels"] = {
            **config["Labels"],
            "com.docker.compose.project": "wanshitong-candidate",
        }
    return {
        "Id": "candidate-id" if candidate else "baseline-id",
        "Name": (
            "/wanshitong-prep-candidate-app"
            if candidate
            else "/wanshitong-wb08r01-app"
        ),
        "Image": "candidate-image" if candidate else "baseline-image",
        "Config": config,
        "HostConfig": _host_config(),
        "Mounts": _mounts(root),
        "NetworkSettings": {"Networks": network_settings},
    }


def _target_image() -> dict[str, object]:
    return {
        "Id": "candidate-image",
        "RepoTags": ["rag-test-wanshitong:candidate"],
        "Config": _config("candidate"),
    }


def _rendered() -> dict[str, object]:
    return {
        "services": {
            "app": {
                "image": "rag-test-wanshitong:candidate",
                "environment": _environment(),
                "volumes": [
                    {
                        "target": item["Destination"],
                        "source": item["Source"],
                        "type": item["Type"],
                        "read_only": not item["RW"],
                    }
                    for item in _mounts("/candidate")
                ],
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "published": "8289",
                        "target": 8088,
                        "protocol": "tcp",
                    }
                ],
                "networks": {"candidate-internal": {}, "candidate-egress": {}},
                "read_only": True,
                "tmpfs": ["/tmp:size=64m,mode=1777"],  # noqa: S108
                "pids_limit": 256,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "restart": "unless-stopped",
                "extra_hosts": ["host.docker.internal=host-gateway"],
            }
        },
        "networks": {
            "candidate-internal": {"name": "internal", "external": True},
            "candidate-egress": {"name": "egress", "external": True},
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _inputs(
    tmp_path: Path, candidate: object
) -> tuple[Path, Path, Path]:
    baseline = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    image = tmp_path / "image.json"
    _write_json(baseline, [_container(candidate=False)])
    _write_json(candidate_path, candidate)
    _write_json(image, [_target_image()])
    return baseline, candidate_path, image


def test_rendered_candidate_preserves_semantics_and_isolates_writes(
    tmp_path: Path,
) -> None:
    baseline, candidate, image = _inputs(tmp_path, _rendered())

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
    )

    assert report["ready"] is True
    assert report["missing_required_keys"] == []
    assert report["semantic_mismatches"] == []
    assert report["mount_mismatches"] == []
    assert len(report["allowed_changes"]) >= 5


def test_rendered_candidate_reports_exact_missing_key_and_mount(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    service = rendered["services"]["app"]
    del service["environment"]["RAG_PRODUCT_MODE"]
    service["volumes"] = [
        item
        for item in service["volumes"]
        if item["target"] != "/private-diagnostics"
    ]
    baseline, candidate, image = _inputs(tmp_path, rendered)

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
    )

    assert report["ready"] is False
    assert report["missing_required_keys"][0]["field_path"] == (
        "services.app.environment.RAG_PRODUCT_MODE"
    )
    assert report["mount_mismatches"][0]["field_path"] == (
        "services.app.mounts./private-diagnostics"
    )


def test_created_candidate_rejects_old_network_alias(tmp_path: Path) -> None:
    candidate_value = _container(candidate=True)
    candidate_value["NetworkSettings"]["Networks"]["internal"][
        "Aliases"
    ].append("wanshitong-app")
    baseline, candidate, image = _inputs(tmp_path, [candidate_value])

    report = guard.compare_runtime(
        stage="created_container",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
    )

    assert report["ready"] is False
    assert report["unclassified_changes"][0]["field_path"] == (
        "container.networks.internal.aliases"
    )


def test_write_candidate_environment_is_private_and_uses_baseline(
    tmp_path: Path,
) -> None:
    baseline, _, _ = _inputs(tmp_path, _rendered())
    output = tmp_path / "candidate.env"

    guard.write_candidate_environment(
        baseline_inspect=baseline,
        output=output,
        image="rag-test-wanshitong:candidate",
        candidate_root=PurePosixPath("/candidate"),
        secret_directory=PurePosixPath("/baseline/secrets"),
        internal_network="internal",
        egress_network="egress",
    )

    content = output.read_text(encoding="utf-8")
    assert "RAG_PRODUCT_MODE='wanshitong'" in content
    assert "CANDIDATE_DATA_DIR='/candidate/data'" in content
    assert "WANSHITONG_SECRET_DIR='/baseline/secrets'" in content
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
