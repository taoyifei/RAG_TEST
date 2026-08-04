import hashlib
import io
import stat
import tarfile
from pathlib import Path

import pytest

from scripts.offline_bundle import (
    safe_extract_bundle,
    verify_file_manifest,
    verify_outer_sidecar,
)


def _write_tar(
    path: Path,
    member_name: str,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(content)
        member.mode = mode
        archive.addfile(member, io.BytesIO(content))


def _write_sidecar(archive: Path) -> Path:
    sidecar = archive.with_name(f"{archive.name}.sha256")
    sidecar.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  "
        f"{archive.name}\n",
        encoding="ascii",
    )
    return sidecar


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


def test_safe_extract_preserves_registered_executable_mode(
    tmp_path: Path,
) -> None:
    script = b"#!/usr/bin/env bash\nexit 0\n"
    script_digest = hashlib.sha256(script).hexdigest()
    manifest = f"{script_digest}  app-update.sh\n".encode()
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, mode="w:gz") as bundle:
        directory = tarfile.TarInfo("runtime")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        bundle.addfile(directory)
        for name, content, mode in (
            ("runtime/app-update.sh", script, 0o700),
            ("runtime/MANIFEST.sha256", manifest, 0o600),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            bundle.addfile(member, io.BytesIO(content))
    sidecar = _write_sidecar(archive)

    extracted = safe_extract_bundle(
        archive,
        sidecar,
        tmp_path / "output",
        expected_top_level="runtime",
    )

    assert stat.S_IMODE((extracted / "app-update.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (extracted / "MANIFEST.sha256").stat().st_mode
    ) == 0o600


@pytest.mark.parametrize("mode", (0o4755, 0o0666))
def test_safe_extract_rejects_unsafe_regular_file_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_tar(archive, "runtime/payload", b"unsafe", mode=mode)

    with pytest.raises(ValueError, match="权限"):
        safe_extract_bundle(
            archive,
            _write_sidecar(archive),
            tmp_path / "output",
            expected_top_level="runtime",
        )


@pytest.mark.parametrize(
    "member_type",
    (tarfile.SYMTYPE, tarfile.CHRTYPE),
)
def test_safe_extract_rejects_symlink_and_special_file(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, mode="w:gz") as bundle:
        member = tarfile.TarInfo("runtime/unsafe")
        member.type = member_type
        member.linkname = "../outside"
        bundle.addfile(member)

    with pytest.raises(ValueError, match="普通文件和目录"):
        safe_extract_bundle(
            archive,
            _write_sidecar(archive),
            tmp_path / "output",
            expected_top_level="runtime",
        )
