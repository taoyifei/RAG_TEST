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
    assert "device_ids:" in compose
    assert "cap_drop:" in compose
    assert "RAG_OCR_ENDPOINTS" in compose


def test_server_scripts_never_build_pull_install_or_delete_volumes() -> None:
    root = Path(__file__).parents[1]
    scripts = tuple(
        (root / "deployment" / name).read_text(encoding="utf-8")
        for name in ("deploy.sh", "rollback.sh", "verify-offline.sh")
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
