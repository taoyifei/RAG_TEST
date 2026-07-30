from pathlib import Path


def test_runtime_requires_atomic_install_and_backup_metadata_helpers() -> None:
    root = Path(__file__).parents[1]
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")
    verifier = (
        root / "deployment/verify-offline.sh"
    ).read_text(encoding="utf-8")

    for filename in (
        "install.sh",
        "offline_bundle.py",
        "freeze_corpus_manifest.py",
    ):
        assert filename in package
        assert f'"{filename}"' in verifier


def test_install_contract_is_immutable_and_no_clobber() -> None:
    install = (
        Path(__file__).parents[1] / "deployment/install.sh"
    ).read_text(encoding="utf-8")

    assert 'project_root="/data/tyf/RAG"' in install
    assert "verify-offline.sh" in install
    assert "/usr/bin/id -u" in install
    assert "chown -R root:root" in install
    assert "chown -R 10001:10001" in install
    assert "CORPUS_MANIFEST.json" in install
    assert "offline_bundle.py" in install
    assert "chmod 0555" in install
    assert "chmod 0444" in install
    assert "! -uid 0" in install
    assert "! -gid 0" in install
    assert "shared/env/rag.env" in install
    assert "0600" in install
    assert "rm -rf" not in install
