"""简单模块化构建器的输出边界。"""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import build_app_update as app_update
from scripts import build_simple_bundle as simple


def test_build_app_archive_runs_build_selfcheck_and_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    saved: list[str] = []

    def fake_run(arguments: Sequence[str], *, cwd: Path) -> None:
        del cwd
        commands.append(tuple(arguments))

    def fake_save(image: str, destination: Path, root: Path) -> None:
        del root
        saved.append(image)
        destination.write_bytes(b"docker archive")

    monkeypatch.setattr(simple, "_run_checked", fake_run)
    monkeypatch.setattr(simple, "_save_image", fake_save)
    monkeypatch.setattr(
        simple,
        "_require_linux_amd64_image",
        lambda *_: None,
    )
    monkeypatch.setattr(
        simple,
        "_inspect_image_id",
        lambda *_: simple._PYTHON_IMAGE_ID,
    )
    monkeypatch.setattr(
        simple,
        "_prepare_app_build_context",
        lambda *_args, **_kwargs: None,
    )
    destination = tmp_path / "app-image.tar.gz"

    image = simple.build_app_archive(
        repository_root=tmp_path,
        revision="a" * 40,
        destination=destination,
    )

    assert image == f"docx-rag:{'a' * 12}"
    assert commands[0][:4] == (
        "/usr/bin/env",
        "DOCKER_BUILDKIT=0",
        "docker",
        "build",
    )
    assert "--platform" in commands[0]
    assert "linux/amd64" in commands[0]
    assert "--pull=false" in commands[0]
    assert f"PYTHON_IMAGE={simple._PYTHON_LOCAL_IMAGE}" in commands[0]
    assert commands[1][:5] == (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
    )
    assert commands[1][-1] == "asset-selfcheck"
    assert saved == [image]
    with gzip.open(destination, "rb") as stream:
        assert stream.read() == b"docker archive"


def test_app_build_context_applies_config_and_manifest_together(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    destination = tmp_path / "context"
    override = tmp_path / "override"
    for relative in (
        "deployment/assets",
        "deployment/config",
        "deployment/runtime/wheelhouse",
        "frontend",
    ):
        (root / relative).mkdir(parents=True)
    for relative in (
        "Dockerfile",
        "requirements.runtime.lock",
        "deployment/ASSETS.sha256",
        "deployment/runtime/WHEELS.sha256",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source:{relative}\n", encoding="utf-8")
    for name in ("pipeline.json", "retrieval.json"):
        (root / "deployment/config" / name).write_text(
            f"source:{name}\n", encoding="utf-8"
        )
        override.mkdir(exist_ok=True)
        (override / name).write_text(f"target:{name}\n", encoding="utf-8")
    target_manifest = tmp_path / "target-ASSETS.sha256"
    target_manifest.write_text("target manifest\n", encoding="ascii")

    simple._prepare_app_build_context(
        root,
        destination,
        config_directory=override,
        assets_manifest_path=target_manifest,
    )

    assert (
        destination / "deployment/config/pipeline.json"
    ).read_text(encoding="utf-8") == "target:pipeline.json\n"
    assert (
        destination / "deployment/ASSETS.sha256"
    ).read_text(encoding="ascii") == "target manifest\n"


def test_simple_bundle_has_four_independent_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    revision = "b" * 40
    monkeypatch.setattr(simple, "require_clean_revision", lambda _: revision)
    monkeypatch.setattr(simple, "prepare_project_wheel", lambda *_: None)
    monkeypatch.setattr(
        simple,
        "_select_local_ocr_image",
        lambda _: "docx-rag-ocr:fixed",
    )
    monkeypatch.setattr(
        simple,
        "_require_linux_amd64_image",
        lambda *_: None,
    )
    monkeypatch.setattr(simple, "_require_qdrant_image", lambda _: None)

    def fake_app(
        *,
        repository_root: Path,
        revision: str,
        destination: Path,
    ) -> str:
        del repository_root, revision
        destination.write_bytes(b"app")
        return f"docx-rag:{'b' * 12}"

    def fake_image(image: str, destination: Path, root: Path) -> None:
        del root
        destination.write_bytes(image.encode("utf-8"))

    def fake_corpus(docs_root: Path, destination: Path) -> None:
        del docs_root
        destination.write_bytes(b"corpus")

    monkeypatch.setattr(simple, "build_app_archive", fake_app)
    monkeypatch.setattr(simple, "_save_compressed_image", fake_image)
    monkeypatch.setattr(simple, "_build_corpus", fake_corpus)

    output = simple.build_simple_bundle(
        repository_root=root,
        output_parent=tmp_path,
    )

    archives = {
        "app-image.tar.gz",
        "ocr-image.tar.gz",
        "qdrant-image.tar.gz",
        "corpus.tar.gz",
    }
    assert archives.issubset(path.name for path in output.iterdir())
    for name in archives:
        assert (output / f"{name}.sha256").read_text(
            encoding="ascii"
        ).endswith(f"  {name}\n")
    rendered_env = (output / ".env.example").read_text(encoding="utf-8")
    assert f"RAG_RELEASE_REVISION={revision}" in rendered_env
    assert f"RAG_APP_IMAGE=docx-rag:{'b' * 12}" in rendered_env
    assert "RAG_OCR_IMAGE=docx-rag-ocr:fixed" in rendered_env


def test_app_update_has_only_app_archive_sidecar_and_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    revision = "c" * 40
    monkeypatch.setattr(
        app_update,
        "require_clean_revision",
        lambda _: revision,
    )
    monkeypatch.setattr(app_update, "prepare_project_wheel", lambda *_: None)

    def fake_app(
        *,
        repository_root: Path,
        revision: str,
        destination: Path,
    ) -> str:
        del repository_root, revision
        destination.write_bytes(b"app-only")
        return f"docx-rag:{'c' * 12}"

    monkeypatch.setattr(app_update, "build_app_archive", fake_app)

    output = app_update.build_app_update(
        repository_root=root,
        output_parent=tmp_path,
    )

    assert {path.name for path in output.iterdir()} == {
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "update-app.sh",
    }
    source = (output / "update-app.sh").read_text(encoding="utf-8")
    assert "rag-app" in source
    assert "rag-ocr" not in source
    assert "rag-qdrant" not in source


def test_update_script_accepts_package_relative_archives() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "deployment/simple/update-app.sh"
    ).read_text(encoding="utf-8")

    assert '[[ "$3" == /* ]]' in source
    assert '[[ "$1" == /* && "$2" == /* && "$3" == /* ]]' not in source
    assert 'archive="$(realpath "$1")"' in source
    assert 'sidecar="$(realpath "$2")"' in source


def test_builders_do_not_reference_complex_release_entrypoints() -> None:
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "scripts/build_simple_bundle.py",
            "scripts/build_app_update.py",
        )
    )

    for forbidden in (
        "release_smoke.py",
        "check_release_safety.py",
        "package.sh",
        "acceptance.sh",
        "docker sbom",
    ):
        assert forbidden not in source
