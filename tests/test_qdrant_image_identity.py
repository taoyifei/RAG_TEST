from pathlib import Path


def test_package_separates_qdrant_provenance_from_portable_identity() -> None:
    root = Path(__file__).parents[1]
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")
    policy = (root / "deployment/qdrant-policy.sh").read_text(
        encoding="utf-8"
    )

    assert ".RepoDigests" in package
    assert "qdrant/qdrant:v1.18.3@sha256:" in policy
    assert "RAG_APPROVED_QDRANT_REPO_DIGEST" in policy
    assert 'qdrant_image##*@' not in package
    assert (
        "'images/qdrant-linux-amd64.tar'"
        in package
    )
    assert "qdrant_manifest_id" in package
    assert "qdrant_config_id" in package
    assert "qdrant_platform" in package
    assert '"${qdrant_manifest_id}"' in package
    assert '"${qdrant_config_id}"' in package
    assert '"${RAG_APPROVED_QDRANT_REPO_DIGEST}"' in package
    assert '"${qdrant_image_id}"' not in package


def test_rollback_uses_runtime_tsv_qdrant_manifest_id() -> None:
    root = Path(__file__).parents[1]
    rollback = (root / "deployment/rollback.sh").read_text(encoding="utf-8")

    assert "IMAGE_ARCHIVES.tsv" in rollback
    assert "images/qdrant-linux-amd64.tar" in rollback
    assert "images/qdrant-linux-amd64.tar 3)" in rollback
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
