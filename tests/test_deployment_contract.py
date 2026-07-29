from pathlib import Path


def test_compose_exposes_only_app_and_uses_rag_names() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "deployment/compose.yaml").read_text(
        encoding="utf-8"
    )

    assert "container_name: rag-app" in compose
    assert "container_name: rag-ocr" in compose
    assert "container_name: rag-qdrant" in compose
    assert compose.count("ports:") == 1
    assert "${RAG_PORT:-8088}:8088" in compose
    assert "pull_policy: never" in compose
    assert "build:" not in compose
    assert "/qdrant/storage" in compose
    assert "${RAG_QDRANT_PATH:?required}:/qdrant/storage" in compose
    assert compose.count("${RAG_STATE_PATH:?required}:/state") == 2
    assert compose.count("${RAG_DOCS_PATH:?required}:/data/docs:ro") == 2
    assert "\nvolumes:" not in compose
    assert "device_ids:" in compose
    assert "cap_drop:" in compose
    assert "RAG_OCR_ENDPOINTS" in compose


def test_server_scripts_never_build_pull_install_or_delete_volumes() -> None:
    root = Path(__file__).parents[1]
    scripts = tuple(
        (root / "deployment" / name).read_text(encoding="utf-8")
        for name in (
            "deploy.sh",
            "rollback.sh",
            "verify-offline.sh",
            "backup.sh",
        )
    )
    joined = "\n".join(scripts)

    for forbidden in (
        "docker build",
        "docker pull",
        "pip install",
        "apt install",
        "apt-get",
        "down -v",
        "volume rm",
        "rm -rf",
        "|| true",
    ):
        assert forbidden not in joined
    assert "--no-build --pull never" in joined
    assert "sha256sum -c MANIFEST.sha256" in joined
    assert "ROLLBACK_OCR_IMAGE" in joined
    assert "/data/tyf/RAG" in joined
    assert "current" in joined
    assert "docker load --input" in scripts[0]
    assert "*.tar" not in scripts[0]


def test_images_and_package_are_bound_to_git_revision() -> None:
    root = Path(__file__).parents[1]
    app_dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    ocr_dockerfile = (
        root / "deployment/ocr/Dockerfile"
    ).read_text(encoding="utf-8")
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")

    for dockerfile in (app_dockerfile, ocr_dockerfile):
        assert "org.opencontainers.image.revision" in dockerfile
        assert "ARG VCS_REF" in dockerfile
    assert "org.opencontainers.image.revision" in package
    assert "git rev-parse HEAD" in package
    assert "MODELS.sha256" in ocr_dockerfile
    assert "rag-runtime-" in package
    assert "rag-corpus-" in package
    assert ".tar.gz.sha256" in package
    assert 'for archive in "${' not in package


def test_public_tutorial_uses_server_variable_and_complete_layout() -> None:
    root = Path(__file__).parents[1]
    tutorial = (
        root / "design/public/offline-build-and-server-deployment.md"
    ).read_text(encoding="utf-8")

    assert "${RAG_SERVER}" in tutorial
    assert "10.242.180." not in tutorial
    for path in (
        "incoming/",
        "releases/<release-id>/",
        "current -> releases/",
        "shared/env/",
        "shared/corpora/<corpus-id>/",
        "data/state/",
        "data/qdrant/",
        "backups/",
        "logs/",
    ):
        assert path in tutorial
    for stage in (
        "prepare_runtime_wheels.py",
        "docker buildx build",
        "offline_bundle.py",
        "scp",
        "/live",
        "rollback.sh",
    ):
        assert stage in tutorial
