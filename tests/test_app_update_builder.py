"""simple app-only 构建器的现行三文件契约。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts import build_app_update
from scripts.build_simple_bundle import SimpleBuildError


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    script = root / "deployment/simple/update-app.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "RAG Test")
    _git(root, "config", "user.email", "rag-test@example.invalid")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def test_app_update_builds_exact_three_file_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, revision = _repository(tmp_path)
    prepared: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        build_app_update,
        "prepare_project_wheel",
        lambda path, value: prepared.append((path, value)),
    )

    def fake_build(
        *,
        repository_root: Path,
        revision: str,
        destination: Path,
    ) -> str:
        assert repository_root == root
        assert revision == _git(root, "rev-parse", "HEAD")
        destination.write_bytes(b"verified-app-archive")
        return f"docx-rag:{revision[:12]}"

    monkeypatch.setattr(build_app_update, "build_app_archive", fake_build)

    output = build_app_update.build_app_update(
        repository_root=root,
        output_parent=tmp_path / "updates",
    )

    assert output.name == revision[:12]
    assert prepared == [(root, revision)]
    assert {path.name for path in output.iterdir()} == {
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "update-app.sh",
    }
    archive = output / "app-image.tar.gz"
    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (output / "app-image.tar.gz.sha256").read_text(
        encoding="ascii"
    ) == f"{expected_digest}  app-image.tar.gz\n"
    assert (output / "update-app.sh").stat().st_mode & 0o111


def test_app_update_rejects_dirty_worktree(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SimpleBuildError, match="clean"):
        build_app_update.build_app_update(
            repository_root=root,
            output_parent=tmp_path / "updates",
        )


def test_app_update_rejects_existing_revision_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, revision = _repository(tmp_path)
    output_parent = tmp_path / "updates"
    (output_parent / revision[:12]).mkdir(parents=True)
    monkeypatch.setattr(
        build_app_update,
        "prepare_project_wheel",
        lambda *_: None,
    )

    with pytest.raises(SimpleBuildError, match="输出已存在"):
        build_app_update.build_app_update(
            repository_root=root,
            output_parent=output_parent,
        )


def test_app_update_rejects_missing_update_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _repository(tmp_path)
    (root / "deployment/simple/update-app.sh").unlink()
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "remove updater")
    monkeypatch.setattr(
        build_app_update,
        "prepare_project_wheel",
        lambda *_: None,
    )

    def fake_build(**arguments: object) -> str:
        destination = arguments["destination"]
        assert isinstance(destination, Path)
        destination.write_bytes(b"verified-app-archive")
        return "docx-rag:test"

    monkeypatch.setattr(build_app_update, "build_app_archive", fake_build)

    with pytest.raises(SimpleBuildError, match=r"update-app\.sh"):
        build_app_update.build_app_update(
            repository_root=root,
            output_parent=tmp_path / "updates",
        )
