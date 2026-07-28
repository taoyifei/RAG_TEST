import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from scripts.offline_bundle import (
    safe_extract_bundle,
    verify_file_manifest,
    verify_outer_sidecar,
)


def _write_tar(path: Path, member_name: str, content: bytes) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


def test_outer_sidecar_rejects_wrong_sha(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    archive.write_bytes(b"runtime")
    sidecar = tmp_path / "runtime.tar.gz.sha256"
    sidecar.write_text(
        f"{'0' * 64}  {archive.name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="外层 SHA256"):
        verify_outer_sidecar(archive, sidecar)


def test_safe_extract_rejects_bundle_path_traversal(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_tar(archive, "runtime/../../escaped", b"bad")
    sidecar = tmp_path / "runtime.tar.gz.sha256"
    sidecar.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="越界"):
        safe_extract_bundle(
            archive,
            sidecar,
            tmp_path / "releases",
            expected_top_level="runtime",
        )

    assert not (tmp_path / "escaped").exists()


def test_file_manifest_rejects_wrong_model_digest(
    tmp_path: Path,
) -> None:
    model = tmp_path / "models" / "det" / "inference.json"
    model.parent.mkdir(parents=True)
    model.write_text("fixed", encoding="utf-8")
    manifest = tmp_path / "MODELS.sha256"
    manifest.write_text(
        f"{'0' * 64}  models/det/inference.json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SHA256"):
        verify_file_manifest(tmp_path, manifest)
