from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest


def _write_sidecar(directory: Path, *, digest: str, filename: str) -> None:
    (directory / "offline_bundle.py.sha256").write_text(
        f"{digest}  {filename}\n",
        encoding="ascii",
    )


def _verify_sidecar(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/sha256sum", "-c", "offline_bundle.py.sha256"],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
    )


def test_package_declares_external_unpacker_sidecar_and_summary() -> None:
    root = Path(__file__).parents[1]
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")

    assert 'unpacker="${artifact_root}/offline_bundle.py"' in package
    assert (
        'unpacker_sidecar="${artifact_root}/offline_bundle.py.sha256"'
        in package
    )
    assert 'write_sidecar "${unpacker}"' in package
    assert "unpacker_sha=" in package


@pytest.mark.parametrize(
    "tamper",
    ("none", "script", "digest", "filename"),
)
def test_unpacker_sidecar_detects_all_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    unpacker = tmp_path / "offline_bundle.py"
    unpacker.write_text("print('verified')\n", encoding="utf-8")
    digest = hashlib.sha256(unpacker.read_bytes()).hexdigest()
    filename = unpacker.name
    if tamper == "digest":
        digest = "0" * 64
    elif tamper == "filename":
        filename = "unexpected_unpacker.py"
    _write_sidecar(tmp_path, digest=digest, filename=filename)
    if tamper == "script":
        unpacker.write_text("print('tampered')\n", encoding="utf-8")

    completed = _verify_sidecar(tmp_path)

    if tamper == "none":
        assert completed.returncode == 0
        assert "offline_bundle.py: OK" in completed.stdout
    else:
        assert completed.returncode != 0


def test_public_upload_verifies_unpacker_before_python() -> None:
    root = Path(__file__).parents[1]
    tutorial = (
        root / "design/public/offline-build-and-server-deployment.md"
    ).read_text(encoding="utf-8")

    assert "artifacts/offline_bundle.py.sha256" in tutorial
    verification = tutorial.index("sha256sum -c offline_bundle.py.sha256")
    execution = tutorial.index("python3 offline_bundle.py")
    assert verification < execution
