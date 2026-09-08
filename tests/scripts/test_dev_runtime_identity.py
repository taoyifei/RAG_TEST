"""开发源码树 Runtime 身份门禁测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import dev


def _clean_source_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """让命令测试不受正在编辑的共享工作树状态影响。"""
    revision = "a" * 40
    monkeypatch.setattr(
        dev,
        "_source_tree_identity",
        lambda: {
            "status": "PASS",
            "git_head": revision,
            "source_tree_revision": revision,
            "worktree_dirty": False,
            "reason": "SOURCE_TREE_CLEAN",
        },
    )


def _report(
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, dict[str, object]]:
    stdout = capsys.readouterr().out
    return stdout, json.loads(stdout)


def test_source_tree_identity_is_deterministic_and_marks_unbuilt_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dev, "SOURCE_REVISION", "development-unset")
    _clean_source_tree(monkeypatch)

    assert dev.main(["runtime-identity"]) == 0
    first_stdout, first = _report(capsys)
    assert dev.main(["runtime-identity"]) == 0
    second_stdout, second = _report(capsys)

    assert first_stdout == second_stdout
    assert first == second
    assert first["gate"] == "DEV_RUNTIME_IDENTITY_GATE"
    assert first["overall_status"] == "PASS"
    checks = first["checks"]
    assert isinstance(checks, dict)
    source_tree = checks["source_tree"]
    assert source_tree["status"] == "PASS"
    assert source_tree["git_head"] == source_tree["source_tree_revision"]
    assert checks["build_revision"] == {
        "build_revision": "development-unset",
        "matches_source_tree": None,
        "reason": "SOURCE_TREE_BUILD_REVISION_UNSET",
        "status": "NOT_APPLICABLE",
    }
    assert checks["build_context"]["status"] == "NOT_RUN"
    assert checks["runtime"]["status"] == "NOT_RUN"
    assert checks["image"]["status"] == "NOT_RUN"
    assert checks["active_revision"]["status"] == "NOT_RUN"
    assert checks["profile"]["status"] == "NOT_RUN"
    schema = dev._REPOSITORY_ROOT / "docs/public/openapi-v1.json"
    expected_sha256 = hashlib.sha256(schema.read_bytes()).hexdigest()
    assert checks["api_schema"]["sha256"] == expected_sha256
    assert checks["frontend"]["build_identity"] == (
        "universal-rag-console@0.1.0"
    )
    assert checks["frontend"]["matches_compatibility_manifest"] is True


def test_dirty_source_tree_blocks_identity_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未提交源码不能冒充 HEAD 所代表的可复现运行身份。"""
    revision = "b" * 40
    monkeypatch.setattr(
        dev,
        "_source_tree_identity",
        lambda: {
            "status": "BLOCKED",
            "git_head": revision,
            "source_tree_revision": revision,
            "worktree_dirty": True,
            "reason": "SOURCE_TREE_DIRTY",
        },
    )
    monkeypatch.setattr(dev, "SOURCE_REVISION", "development-unset")

    assert dev.main(["runtime-identity"]) == 1
    _, report = _report(capsys)

    assert report["overall_status"] == "BLOCKED"
    assert report["checks"]["source_tree"]["reason"] == "SOURCE_TREE_DIRTY"


def test_explicit_profile_reports_offline_composition_fingerprints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dev, "SOURCE_REVISION", "development-unset")
    _clean_source_tree(monkeypatch)
    profile = dev._REPOSITORY_ROOT / "configs/profiles/dev-offline.json"

    assert dev.main(["runtime-identity", "--profile", str(profile)]) == 0
    _, report = _report(capsys)

    identity = report["checks"]["profile"]
    assert identity["status"] == "PASS"
    assert identity["profile_id"] == "dev-offline"
    assert identity["index_fingerprint"].startswith("sha256:")
    assert identity["serving_fingerprint"].startswith("sha256:")
    assert identity["network_calls"] == 0


def test_explicit_context_runtime_and_image_identities_are_cross_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revision = dev._source_tree_identity()["source_tree_revision"]
    assert isinstance(revision, str)
    monkeypatch.setattr(
        dev,
        "_source_tree_identity",
        lambda: {
            "status": "PASS",
            "git_head": revision,
            "source_tree_revision": revision,
            "worktree_dirty": False,
            "reason": "SOURCE_TREE_CLEAN",
        },
    )
    image_id = "sha256:" + "1" * 64
    fingerprint = "sha256:" + "2" * 64
    context = tmp_path / "context.json"
    runtime = tmp_path / "runtime.json"
    image = tmp_path / "image.json"
    context.write_text(
        json.dumps({"source_revision": revision}), encoding="utf-8"
    )
    runtime.write_text(
        json.dumps(
            {
                "build_revision": revision,
                "image_id": image_id,
                "profile_id": "dev-offline",
                "index_fingerprint": fingerprint,
                "serving_fingerprint": fingerprint,
                "runtime_identity": "product-runtime-v3",
                "active_revision_id": "irev_" + "3" * 32,
            }
        ),
        encoding="utf-8",
    )
    image.write_text(
        json.dumps(
            [
                {
                    "Id": image_id,
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.revision": revision
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dev, "SOURCE_REVISION", revision)

    assert (
        dev.main(
            [
                "runtime-identity",
                "--build-context-manifest",
                str(context),
                "--runtime-status-json",
                str(runtime),
                "--image-inspect-json",
                str(image),
            ]
        )
        == 0
    )
    _, report = _report(capsys)

    checks = report["checks"]
    assert report["overall_status"] == "PASS"
    assert checks["build_revision"]["status"] == "PASS"
    assert checks["build_context"]["status"] == "PASS"
    assert checks["runtime"]["status"] == "PASS"
    assert checks["runtime"]["build_revision_status"] == "PASS"
    assert checks["image"]["status"] == "PASS"
    assert checks["image"]["matches_runtime_image_id"] is True
    assert checks["active_revision"]["status"] == "PASS"


def test_runtime_mismatch_returns_nonzero_without_echoing_unknown_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revision = dev._source_tree_identity()["source_tree_revision"]
    assert isinstance(revision, str)
    mismatch = "0" * 40 if revision != "0" * 40 else "1" * 40
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "build_revision": mismatch,
                "secret": "SENSITIVE_SENTINEL_SHOULD_NOT_APPEAR",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dev, "SOURCE_REVISION", "development-unset")

    assert (
        dev.main(["runtime-identity", "--runtime-status-json", str(runtime)])
        == 1
    )
    stdout, report = _report(capsys)

    assert report["overall_status"] == "BLOCKED"
    assert report["checks"]["runtime"]["status"] == "BLOCKED"
    assert "SENSITIVE_SENTINEL_SHOULD_NOT_APPEAR" not in stdout
