from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_industry_app_update
from scripts.build_industry_bundle import IndustrySourceIdentity
from scripts.industry_bundle.images import ImageArtifact

_REVISION = "a" * 40


def test_industry_app_update_builds_exact_app_only_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[1]
    archive_bytes = b"verified-industry-app-image"

    monkeypatch.setattr(
        build_industry_app_update,
        "require_industry_source",
        lambda _root: IndustrySourceIdentity(
            git_sha=_REVISION,
            main_sha="b" * 40,
            source_date_epoch=1_786_000_000,
        ),
    )
    monkeypatch.setattr(
        build_industry_app_update,
        "prepare_project_wheel",
        lambda _root, _revision: None,
    )

    def build_image(
        *,
        repository_root: Path,
        revision: str,
        output_dir: Path,
    ) -> ImageArtifact:
        assert repository_root == root.resolve()
        assert revision == _REVISION
        archive = output_dir / "app-image.tar.gz"
        archive.write_bytes(archive_bytes)
        return ImageArtifact(
            name="app",
            ref=f"docx-rag:{_REVISION[:12]}",
            image_id="sha256:" + "c" * 64,
            platform="linux/amd64",
            revision=_REVISION,
            archive_name=archive.name,
            archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            manifest_digest="sha256:" + "d" * 64,
            config_digest="sha256:" + "e" * 64,
        )

    monkeypatch.setattr(
        build_industry_app_update,
        "build_app_image_archive",
        build_image,
    )
    monkeypatch.setattr(
        build_industry_app_update,
        "_git_output",
        lambda *_args: "",
    )

    output = build_industry_app_update.build_industry_app_update(
        repository_root=root,
        output_parent=tmp_path,
    )

    assert {path.name for path in output.iterdir()} == {
        "UPDATE_MANIFEST.json",
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "update-app.sh",
    }
    manifest = json.loads((output / "UPDATE_MANIFEST.json").read_bytes())
    assert manifest["branch"] == "Industry"
    assert manifest["target"] == {
        "alias": "rag-industry-active",
        "project": "rag-industry",
        "service": "rag-industry-app",
    }
    assert manifest["image"]["ref"] == f"docx-rag:{_REVISION[:12]}"
    assert manifest["image"]["id"] == "sha256:" + "c" * 64
    assert manifest["image"]["revision"] == _REVISION
    assert manifest["image"]["platform"] == "linux/amd64"
    assert manifest["index_fingerprint"]["reindex_required"] is False
    assert manifest["revision"] == _REVISION
    assert manifest["schema_version"] == "1"
    assert (output / "app-image.tar.gz.sha256").read_text(
        encoding="ascii"
    ) == (
        f"{hashlib.sha256(archive_bytes).hexdigest()}  app-image.tar.gz\n"
    )
    script = (output / "update-app.sh").read_text(encoding="utf-8")
    assert "rag-industry-app" in script
    assert "--force-recreate" in script
    update_command = (
        "--no-deps --no-build --pull never --force-recreate "
        "rag-industry-app"
    )
    assert script.count(update_command) == 2
    assert "up -d --force-recreate rag-industry-worker" not in script
    assert "up -d --force-recreate rag-industry-ocr" not in script
    assert "up -d --force-recreate rag-industry-qdrant" not in script
    assert "worker --once" not in script
