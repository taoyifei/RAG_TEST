from pathlib import Path


def test_package_separates_qdrant_repo_digest_from_local_image_id() -> None:
    root = Path(__file__).parents[1]
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")

    assert ".RepoDigests" in package
    assert "qdrant/qdrant@sha256:" in package
    assert 'qdrant_image##*@' not in package
    assert (
        "'images/qdrant-linux-amd64.tar'"
        in package
    )
    assert '"${qdrant_image_id}"' in package


def test_rollback_uses_runtime_tsv_qdrant_image_id() -> None:
    root = Path(__file__).parents[1]
    rollback = (root / "deployment/rollback.sh").read_text(encoding="utf-8")

    assert "IMAGE_ARCHIVES.tsv" in rollback
    assert "images/qdrant-linux-amd64.tar" in rollback
    assert 'qdrant_source_image##*@' not in rollback


def test_compose_and_env_require_release_bound_images_and_revision() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "deployment/compose.yaml").read_text(encoding="utf-8")
    example = (root / "deployment/.env.example").read_text(encoding="utf-8")

    for variable in (
        "RAG_APP_IMAGE",
        "RAG_OCR_IMAGE",
        "RAG_QDRANT_IMAGE",
        "RAG_RELEASE_REVISION",
    ):
        assert f"${{{variable}:?required}}" in compose
    assert "RAG_RELEASE_REVISION=REPLACE_WITH_40_HEX_GIT_SHA" in example
    assert "0.1.0" not in example
