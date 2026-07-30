from pathlib import Path


def test_package_requires_external_manifest_without_fixed_corpus_size() -> None:
    package = (
        Path(__file__).parents[1] / "deployment/package.sh"
    ).read_text(encoding="utf-8")

    assert 'corpus_manifest_input="${CORPUS_MANIFEST:-}"' in package
    assert "freeze_corpus_manifest" in package
    assert "CORPUS_MANIFEST.json" in package
    assert "-ne 6" not in package
    assert "22358173" not in package


def test_package_publishes_one_complete_no_clobber_release_directory() -> None:
    package = (
        Path(__file__).parents[1] / "deployment/package.sh"
    ).read_text(encoding="utf-8")

    assert 'release_parent="${artifact_root}/releases"' in package
    assert "scripts.offline_bundle" in package
    assert "RELEASE_MANIFEST.sha256" in package
    assert "scripts.offline_bundle" in package
    assert "rag-runtime-${release_id}.tar.gz" in package
    assert "rag-corpus-${corpus_id}.tar.gz" in package
