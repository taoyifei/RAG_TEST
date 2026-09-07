"""Product Compose、镜像资源与兼容清单回归。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from rag_app.product.compatibility import CompatibilityManifest

_ROOT = Path(__file__).resolve().parents[2]


def test_product_compose_has_only_minimal_runtime_contract() -> None:
    compose = yaml.safe_load(
        (_ROOT / "compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert set(services) == {"app", "qdrant"}
    assert set(compose["volumes"]) == {
        "qdrant_data",
        "rag_data",
        "rag_secrets",
    }
    application = services["app"]
    assert "command" not in application
    environment = application["environment"]
    assert environment["RAG_DATA_DIR"] == "/data"
    assert environment["RAG_QDRANT_MODE"] == "url"
    assert environment["RAG_QDRANT_URL"] == "http://qdrant:6333"
    assert environment["RAG_QDRANT_API_KEY_FILE"].endswith("qdrant-api-key")
    assert environment["RAG_TRUST_LOOPBACK_HOST_PROXY"] == "true"
    assert application["ports"] == ["127.0.0.1:${RAG_PORT:-8088}:8088"]
    assert "/live" in " ".join(application["healthcheck"]["test"])
    assert "ports" not in services["qdrant"]
    assert "QDRANT__SERVICE__API_KEY" not in str(services["qdrant"])
    assert services["qdrant"]["command"][-1].endswith("qdrant.yaml")
    for forbidden in (
        "RAG_OCR_ENDPOINTS",
        "RAG_PIPELINE_PATH",
        "RAG_RELEASE_REVISION",
        "RAG_RETRIEVAL_PATH",
    ):
        assert forbidden not in environment


def test_image_contains_product_runtime_resources_and_default_command() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "AS frontend-build" in dockerfile
    assert "AS python-build" in dockerfile
    assert "AS runtime" in dockerfile
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "python -m pip wheel" in dockerfile
    assert "COPY --chown=rag:rag migrations/ ./migrations/" in dockerfile
    assert "compatibility-manifest.json" in dockerfile
    assert "USER rag:rag" in dockerfile
    assert "/live" in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    runtime = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime", maxsplit=1)[1]
    assert "node_modules" not in runtime
    assert "frontend-build" in runtime


def test_default_env_contains_no_secret_values() -> None:
    example = (_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "API_KEY=" not in example
    assert "TOKEN=" not in example
    assert "qdrant/qdrant:v1.18.3" in example


def test_source_revision_is_trace_only_for_compatible_manifest() -> None:
    payload = json.loads(
        (_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8")
    )
    payload["source_revision"] = "different-but-traceable-revision"
    manifest = CompatibilityManifest.model_validate(payload)

    manifest.require_compatible()
