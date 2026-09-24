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
        "RAG_TRUSTED_ORIGINS": "http://127.0.0.1:8288",
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
            "NetworkID": "a" * 64,
            "Aliases": (
                ["wanshitong-sso-candidate-app", "app"]
                if candidate
                else ["wanshitong-app"]
            ),
        },
        "egress": {
            "NetworkID": "b" * 64,
            "Aliases": (
                ["wanshitong-sso-candidate-app", "app"]
                if candidate
                else ["wanshitong-app"]
            ),
        },
    }
    config = _config("candidate" if candidate else "baseline")
    if candidate:
        config["Labels"] = {
            **config["Labels"],
            "com.docker.compose.project": "wanshitong-sso-candidate",
        }
    return {
        "Id": "candidate-id" if candidate else "baseline-id",
        "Name": (
            "/wanshitong-sso-candidate-app"
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


def _enable_sso(rendered: dict[str, object]) -> None:
    environment = rendered["services"]["app"]["environment"]
    environment.update(
        {
            "RAG_ROOT_PATH": "/kb",
            "RAG_TRUSTED_ORIGINS": (
                "http://127.0.0.1:8288,http://kb.test:8289"
            ),
            "RAG_WANSHITONG_AUTH_MODE": "sso",
            "RAG_WANSHITONG_SSO_CLIENT_ID": "kb",
            "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE": (
                "/run/rag-secrets/sso-client-secret"
            ),
            "RAG_WANSHITONG_SSO_DEPLOYMENT_ID": "candidate_8289",
            "RAG_WANSHITONG_SSO_ENTRIES": json.dumps(
                [
                    {
                        "id": "internal",
                        "origin": "http://kb.test:8289",
                        "authorize_url": (
                            "http://rdms.test:21000/sso/authorize"
                        ),
                    }
                ]
            ),
            "RAG_WANSHITONG_SSO_VALIDATE_URL": (
                "http://rdms.test:21000/sso/validate"
            ),
        }
    )


def _inputs(tmp_path: Path, candidate: object) -> tuple[Path, Path, Path]:
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

    assert report["ready"] is True, report
    assert report["missing_required_keys"] == []
    assert report["semantic_mismatches"] == []
    assert report["mount_mismatches"] == []
    assert len(report["allowed_changes"]) >= 5


def test_parent_child_chunker_requires_exact_isolated_candidate_transition(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    rendered["services"]["app"]["environment"][  # type: ignore[index]
        "RAG_WK_CHUNKER_MODE"
    ] = "parent-child"
    baseline, candidate, image = _inputs(tmp_path, rendered)
    arguments = {
        "stage": "rendered_spec",
        "baseline_inspect": baseline,
        "candidate_input": candidate,
        "target_image_inspect": image,
        "candidate_root": PurePosixPath("/candidate"),
        "forbidden_root": PurePosixPath("/production"),
    }

    assert guard.compare_runtime(**arguments)["ready"] is False
    assert (
        guard.compare_runtime(**arguments, allow_weknora_parent_child=True)[
            "ready"
        ]
        is True
    )
    rendered["services"]["app"]["environment"][  # type: ignore[index]
        "RAG_WK_CHUNKER_MODE"
    ] = "legacy"
    _write_json(candidate, rendered)
    assert guard.compare_runtime(**arguments)["ready"] is True
    rendered["services"]["app"]["environment"][  # type: ignore[index]
        "RAG_WK_CHUNKER_MODE"
    ] = "other"
    _write_json(candidate, rendered)
    invalid = guard.compare_runtime(
        **arguments, allow_weknora_parent_child=True
    )
    assert invalid["ready"] is False
    assert invalid["semantic_mismatches"]


def test_natural_public_requires_explicit_isolated_candidate_enable(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    environment = rendered["services"]["app"]["environment"]  # type: ignore[index]
    environment["RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED"] = "true"
    environment["RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE"] = "wk-standard-pc-v1"
    baseline, candidate, image = _inputs(tmp_path, rendered)
    arguments = {
        "stage": "rendered_spec",
        "baseline_inspect": baseline,
        "candidate_input": candidate,
        "target_image_inspect": image,
        "candidate_root": PurePosixPath("/candidate"),
        "forbidden_root": PurePosixPath("/production"),
    }
    assert guard.compare_runtime(**arguments)["ready"] is False
    assert (
        guard.compare_runtime(**arguments, allow_natural_public=True)["ready"]
        is True
    )
    environment["RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE"] = "unknown"
    _write_json(candidate, rendered)
    assert (
        guard.compare_runtime(**arguments, allow_natural_public=True)["ready"]
        is False
    )


def test_phase04_allows_only_explicit_department_shadow_enable(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    rendered["services"]["app"]["environment"][
        "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
    ] = "true"
    baseline, candidate, image = _inputs(tmp_path, rendered)

    rejected = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
    )
    accepted = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_department_shadow_enable=True,
    )

    assert rejected["ready"] is False
    assert rejected["unclassified_changes"][0]["field_path"] == (
        "services.app.environment.RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
    )
    assert accepted["ready"] is True
    assert any(
        item["field_path"].endswith("RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED")
        for item in accepted["allowed_changes"]
    )


def test_phase04_rejects_declared_shadow_enable_without_true_value(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    rendered["services"]["app"]["environment"][
        "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
    ] = "false"
    baseline, candidate, image = _inputs(tmp_path, rendered)

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_department_shadow_enable=True,
    )

    assert report["ready"] is False
    assert report["semantic_mismatches"][0]["field_path"].endswith(
        "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
    )


def test_later_candidate_preserves_enabled_department_shadow(
    tmp_path: Path,
) -> None:
    baseline_value = _container(candidate=False)
    baseline_value["Config"]["Env"].append(
        "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED=true"
    )
    rendered = _rendered()
    rendered["services"]["app"]["environment"][
        "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
    ] = "true"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    image = tmp_path / "image.json"
    _write_json(baseline, [baseline_value])
    _write_json(candidate, rendered)
    _write_json(image, [_target_image()])

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_department_shadow_enable=True,
    )

    assert report["ready"] is True, report
    assert report["semantic_mismatches"] == []


def test_f06_popular_questions_toggle_requires_declared_boolean(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    rendered["services"]["app"]["environment"][
        "RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED"
    ] = "true"
    baseline, candidate, image = _inputs(tmp_path, rendered)
    arguments = {
        "stage": "rendered_spec",
        "baseline_inspect": baseline,
        "candidate_input": candidate,
        "target_image_inspect": image,
        "candidate_root": PurePosixPath("/candidate"),
        "forbidden_root": PurePosixPath("/production"),
    }

    rejected = guard.compare_runtime(**arguments)
    accepted = guard.compare_runtime(
        **arguments, allow_popular_questions_toggle=True
    )
    assert rejected["ready"] is False
    assert accepted["ready"] is True, accepted
    assert any(
        item["field_path"].endswith("RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED")
        for item in accepted["allowed_changes"]
    )

    rendered["services"]["app"]["environment"][
        "RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED"
    ] = "invalid"
    _write_json(candidate, rendered)
    invalid = guard.compare_runtime(
        **arguments, allow_popular_questions_toggle=True
    )
    assert invalid["ready"] is False
    assert invalid["semantic_mismatches"]


def test_q1_private_capture_requires_exact_bounded_profile(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    environment = rendered["services"]["app"]["environment"]
    environment.update(guard._Q1_CAPTURE_KEYS)
    baseline_value = _container(candidate=False)
    baseline_value["Config"]["Env"].append(
        "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_MAX_FILES=32"
    )
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    image = tmp_path / "image.json"
    _write_json(baseline, [baseline_value])
    _write_json(candidate, rendered)
    _write_json(image, [_target_image()])
    arguments = {
        "stage": "rendered_spec",
        "baseline_inspect": baseline,
        "candidate_input": candidate,
        "target_image_inspect": image,
        "candidate_root": PurePosixPath("/candidate"),
        "forbidden_root": PurePosixPath("/production"),
    }

    assert guard.compare_runtime(**arguments)["ready"] is False
    accepted = guard.compare_runtime(**arguments, allow_q1_private_capture=True)
    assert accepted["ready"] is True, accepted

    environment["RAG_PRIVATE_REPLAY_CAPTURE_LIMIT"] = "33"
    _write_json(candidate, rendered)
    rejected = guard.compare_runtime(**arguments, allow_q1_private_capture=True)
    assert rejected["ready"] is False
    assert any(
        item["field_path"].endswith("RAG_PRIVATE_REPLAY_CAPTURE_LIMIT")
        for item in rejected["semantic_mismatches"]
    )


def test_sso_changes_require_explicit_guard_flag(tmp_path: Path) -> None:
    rendered = _rendered()
    _enable_sso(rendered)
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
    changed_fields = {
        item["field_path"] for item in report["unclassified_changes"]
    }
    assert "services.app.environment.RAG_WANSHITONG_AUTH_MODE" in changed_fields


def test_sso_guard_accepts_only_complete_registered_configuration(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    _enable_sso(rendered)
    baseline, candidate, image = _inputs(tmp_path, rendered)

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_sso_enable=True,
    )

    assert report["ready"] is True, report
    assert report["semantic_mismatches"] == []
    assert any(
        item["field_path"].endswith("RAG_WANSHITONG_AUTH_MODE")
        for item in report["allowed_changes"]
    )


def test_later_candidate_preserves_enabled_sso_configuration(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    _enable_sso(rendered)
    candidate_environment = rendered["services"]["app"]["environment"]
    baseline_value = _container(candidate=False)
    baseline_value["Config"]["Env"] = [
        f"{key}={value}" for key, value in candidate_environment.items()
    ] + ["PATH=/usr/local/bin"]
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    image = tmp_path / "image.json"
    _write_json(baseline, [baseline_value])
    _write_json(candidate, rendered)
    _write_json(image, [_target_image()])

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_sso_enable=True,
    )

    assert report["ready"] is True, report
    assert report["semantic_mismatches"] == []
    assert not any(
        item["field_path"].endswith("RAG_WANSHITONG_AUTH_MODE")
        for item in report["allowed_changes"]
    )


def test_later_candidate_rejects_changes_to_enabled_sso_configuration(
    tmp_path: Path,
) -> None:
    rendered = _rendered()
    _enable_sso(rendered)
    candidate_environment = rendered["services"]["app"]["environment"]
    baseline_value = _container(candidate=False)
    baseline_value["Config"]["Env"] = [
        f"{key}={value}" for key, value in candidate_environment.items()
    ] + ["PATH=/usr/local/bin"]
    candidate_environment["RAG_WANSHITONG_SSO_CLIENT_ID"] = "changed"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    image = tmp_path / "image.json"
    _write_json(baseline, [baseline_value])
    _write_json(candidate, rendered)
    _write_json(image, [_target_image()])

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_sso_enable=True,
    )

    assert report["ready"] is False
    assert any(
        item["field_path"].endswith("RAG_WANSHITONG_SSO_CLIENT_ID")
        for item in report["semantic_mismatches"]
    )


def test_sso_guard_rejects_untrusted_entry_origin(tmp_path: Path) -> None:
    rendered = _rendered()
    _enable_sso(rendered)
    rendered["services"]["app"]["environment"]["RAG_TRUSTED_ORIGINS"] = (
        "http://127.0.0.1:8288"
    )
    baseline, candidate, image = _inputs(tmp_path, rendered)

    report = guard.compare_runtime(
        stage="rendered_spec",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
        allow_sso_enable=True,
    )

    assert report["ready"] is False
    assert any(
        item["field_path"].endswith("RAG_TRUSTED_ORIGINS")
        for item in report["semantic_mismatches"]
    )


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


def test_created_candidate_normalizes_prestart_network_id_key(
    tmp_path: Path,
) -> None:
    candidate_value = _container(candidate=True)
    networks = candidate_value["NetworkSettings"]["Networks"]
    internal = networks.pop("internal")
    internal["NetworkID"] = ""
    networks["a" * 64] = internal
    baseline, candidate, image = _inputs(tmp_path, [candidate_value])

    report = guard.compare_runtime(
        stage="created_container",
        baseline_inspect=baseline,
        candidate_input=candidate,
        target_image_inspect=image,
        candidate_root=PurePosixPath("/candidate"),
        forbidden_root=PurePosixPath("/production"),
    )

    assert report["ready"] is True
    assert report["semantic_mismatches"] == []


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
        department_shadow_enabled=True,
    )

    content = output.read_text(encoding="utf-8")
    assert "RAG_PRODUCT_MODE='wanshitong'" in content
    assert "CANDIDATE_DATA_DIR='/candidate/data'" in content
    assert "WANSHITONG_SECRET_DIR='/baseline/secrets'" in content
    assert "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED='true'" in content
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
