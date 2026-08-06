from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.download_ocr_assets import safe_extract_tar


def _tar_with_file(path: Path, member_name: str, content: bytes) -> None:
    with tarfile.open(path, mode="w:") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


def test_safe_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    _tar_with_file(archive, "model/../../escaped", b"bad")

    with pytest.raises(ValueError, match="越界"):
        safe_extract_tar(
            archive,
            tmp_path / "output",
            expected_top_level="model",
        )

    assert not (tmp_path / "escaped").exists()


def test_safe_extract_is_reentrant_and_rejects_drift(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "model.tar"
    _tar_with_file(archive, "model/inference.json", b"fixed")
    output = tmp_path / "output"

    safe_extract_tar(
        archive,
        output,
        expected_top_level="model",
    )
    safe_extract_tar(
        archive,
        output,
        expected_top_level="model",
    )
    (output / "model" / "inference.json").chmod(0o644)
    (output / "model" / "inference.json").write_bytes(b"drift")

    with pytest.raises(ValueError, match="拒绝覆盖"):
        safe_extract_tar(
            archive,
            output,
            expected_top_level="model",
        )


def test_runtime_deb_sources_match_frozen_manifest() -> None:
    root = Path(__file__).parents[1]
    payload = json.loads(
        (root / "deployment/ocr/ASSET_SOURCES.json").read_text("utf-8")
    )
    runtime_debs = {
        item["name"]: item["sha256"]
        for item in payload["assets"]
        if item["kind"] == "runtime_deb"
    }
    manifest = {
        name: digest
        for digest, name in (
            line.split("  ", maxsplit=1)
            for line in (
                root / "deployment/ocr/DEBS.sha256"
            ).read_text("ascii").splitlines()
        )
    }

    assert runtime_debs == manifest
