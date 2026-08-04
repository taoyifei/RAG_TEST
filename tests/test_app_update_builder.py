from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import build_app_update
from scripts.docker_archive_identity import DockerArchiveIdentity

_ROOT = Path(__file__).parents[1]
_MANIFEST_DIGEST = "sha256:" + "1" * 64
_CONFIG_DIGEST = "sha256:" + "2" * 64


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    (root / "deployment/config").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "src/rag_app").mkdir(parents=True)
    for name in ("pipeline.json", "retrieval.json"):
        source = _ROOT / "deployment/config" / name
        (root / "deployment/config" / name).write_bytes(source.read_bytes())
    (root / "frontend/index.html").write_text("base\n", encoding="utf-8")
    (root / "src/rag_app/example.py").write_text(
        "VALUE = 'base'\n",
        encoding="utf-8",
    )
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "RAG Test")
    _git(root, "config", "user.email", "rag-test@example.invalid")
    return root, _commit(root, "base")


def _stub_image_build(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        build_app_update,
        "_prepare_project_wheel",
        lambda _root, _revision: None,
    )

    def fake_run(arguments: tuple[str, ...], *, root: Path) -> str:
        assert root == root.resolve()
        commands.append(arguments)
        if arguments[:3] == ("docker", "image", "save"):
            output = Path(arguments[arguments.index("--output") + 1])
            output.write_bytes(b"verified-oci-archive")
        return ""

    monkeypatch.setattr(build_app_update, "_run_checked", fake_run)
    monkeypatch.setattr(
        build_app_update,
        "inspect_docker_archive",
        lambda _path, **kwargs: DockerArchiveIdentity(
            manifest_digest=_MANIFEST_DIGEST,
            config_digest=_CONFIG_DIGEST,
            tag=kwargs["expected_tag"],
            platform=kwargs["expected_platform"],
            revision=kwargs["expected_revision"],
        ),
    )
    return commands


@pytest.mark.parametrize(
    ("path", "category"),
    (
        ("src/rag_app/api/app.py", "app_python"),
        ("frontend/index.html", "frontend"),
        ("Dockerfile", "app_build"),
        ("pyproject.toml", "app_dependencies"),
        ("requirements.lock", "app_dependencies"),
        ("requirements.runtime.lock", "app_dependencies"),
        ("deployment/ASSETS.sha256", "app_assets"),
        (
            "deployment/assets/tokenizers/llm/tokenizer.json",
            "app_assets",
        ),
        (
            "deployment/config/retrieval.json",
            "app_serving_config",
        ),
        ("tests/test_query_api.py", "verification_only"),
        ("PROGRESS.md", "verification_only"),
    ),
)
def test_app_update_classifies_allowed_changes(
    path: str,
    category: str,
) -> None:
    classification = build_app_update.classify_changed_paths((path,))

    assert classification.allowed
    assert classification.categories == (category,)
    assert classification.rejected_paths == ()


@pytest.mark.parametrize(
    "path",
    (
        "deployment/compose.yaml",
        "deployment/deploy.sh",
        "deployment/ocr/Dockerfile",
        "deployment/qdrant-policy.sh",
        "deployment/config/corpus-policy.json",
        "docs/private.docx",
        "corpus-manifests/production.json",
        "deployment/model-services/compose.yaml",
    ),
)
def test_app_update_rejects_non_app_changes(path: str) -> None:
    classification = build_app_update.classify_changed_paths((path,))

    assert not classification.allowed
    assert classification.rejected_paths == (path,)


@pytest.mark.parametrize(
    ("changed_path", "category"),
    (
        ("frontend/index.html", "frontend"),
        ("src/rag_app/example.py", "app_python"),
    ),
)
def test_app_change_builds_exact_four_file_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_path: str,
    category: str,
) -> None:
    root, base_revision = _repository(tmp_path)
    (root / changed_path).write_text(
        "target\n",
        encoding="utf-8",
    )
    target_revision = _commit(root, category)
    commands = _stub_image_build(monkeypatch)

    output = build_app_update.build_update(
        repository_root=root,
        base_revision=base_revision,
        output_parent=tmp_path / "updates",
    )

    archive_name = f"docx-rag-app-{target_revision[:12]}.tar.gz"
    assert sorted(path.name for path in output.iterdir()) == [
        "APP_UPDATE.json",
        "APP_UPDATE_MANIFEST.sha256",
        archive_name,
        f"{archive_name}.sha256",
    ]
    metadata = json.loads(
        (output / "APP_UPDATE.json").read_text(encoding="utf-8")
    )
    assert metadata == {
        "archive": archive_name,
        "base_revision": base_revision,
        "change_categories": [category],
        "changed_path_count": 1,
        "config_digest": _CONFIG_DIGEST,
        "image_tag": f"docx-rag:{target_revision[:12]}",
        "index_fingerprint": metadata["index_fingerprint"],
        "manifest_digest": _MANIFEST_DIGEST,
        "platform": "linux/amd64",
        "reindex_required": False,
        "schema_version": "1",
        "serving_fingerprint": metadata["serving_fingerprint"],
        "target_revision": target_revision,
    }
    assert metadata["index_fingerprint"]["base"] == (
        metadata["index_fingerprint"]["target"]
    )
    assert metadata["serving_fingerprint"]["base"] == (
        metadata["serving_fingerprint"]["target"]
    )
    archive = output / archive_name
    sidecar = (output / f"{archive_name}.sha256").read_text(
        encoding="ascii"
    )
    assert sidecar == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  "
        f"{archive_name}\n"
    )
    manifest_lines = (
        output / "APP_UPDATE_MANIFEST.sha256"
    ).read_text(encoding="ascii").splitlines()
    assert len(manifest_lines) == 3
    flattened = "\n".join(" ".join(command) for command in commands)
    assert "docker buildx build" in flattened
    assert "asset-selfcheck" in flattened
    assert flattened.count("docker image save") == 1
    assert "docx-rag-ocr" not in flattened
    assert "qdrant" not in flattened.casefold()


def test_index_fingerprint_change_builds_reindex_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, base_revision = _repository(tmp_path)
    pipeline_path = root / "deployment/config/pipeline.json"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    pipeline["chunker_revision"] = "section-pack-v3-reindex"
    pipeline_path.write_text(json.dumps(pipeline) + "\n", encoding="utf-8")
    _commit(root, "index contract")
    _stub_image_build(monkeypatch)

    output = build_app_update.build_update(
        repository_root=root,
        base_revision=base_revision,
        output_parent=tmp_path / "updates",
    )

    metadata = json.loads(
        (output / "APP_UPDATE.json").read_text(encoding="utf-8")
    )
    assert metadata["change_categories"] == ["app_serving_config"]
    assert metadata["reindex_required"] is True
    assert metadata["index_fingerprint"]["base"] != (
        metadata["index_fingerprint"]["target"]
    )


def test_build_rejects_non_ancestor_base(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)

    with pytest.raises(
        build_app_update.AppUpdateError,
        match="祖先",
    ):
        build_app_update.build_update(
            repository_root=root,
            base_revision="f" * 40,
            output_parent=tmp_path / "updates",
        )


def test_build_rejects_dirty_worktree(tmp_path: Path) -> None:
    root, base_revision = _repository(tmp_path)
    (root / "src/rag_app/example.py").write_text(
        "VALUE = 'dirty'\n",
        encoding="utf-8",
    )

    with pytest.raises(
        build_app_update.AppUpdateError,
        match="clean",
    ):
        build_app_update.build_update(
            repository_root=root,
            base_revision=base_revision,
            output_parent=tmp_path / "updates",
        )


def test_build_rejects_forbidden_changed_path(tmp_path: Path) -> None:
    root, base_revision = _repository(tmp_path)
    (root / "deployment/compose.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    _commit(root, "compose")

    with pytest.raises(
        build_app_update.AppUpdateError,
        match="完整 release",
    ):
        build_app_update.build_update(
            repository_root=root,
            base_revision=base_revision,
            output_parent=tmp_path / "updates",
        )
