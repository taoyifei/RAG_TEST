"""Product Compose、镜像资源与兼容清单回归。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from rag_app.product.compatibility import CompatibilityManifest

_ROOT = Path(__file__).resolve().parents[2]


def test_product_compose_has_only_minimal_runtime_contract() -> None:
    compose = yaml.safe_load(
        (_ROOT / "deployment/product/compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert set(services) == {"rag-app"}
    application = services["rag-app"]
    assert "command" not in application
    environment = application["environment"]
    assert environment["RAG_DATA_DIR"] == "/data"
    assert environment["RAG_QDRANT_MODE"] == "${RAG_QDRANT_MODE:-memory}"
    for forbidden in (
        "RAG_OCR_ENDPOINTS",
        "RAG_PIPELINE_PATH",
        "RAG_RELEASE_REVISION",
        "RAG_RETRIEVAL_PATH",
    ):
        assert forbidden not in environment


def test_image_contains_product_runtime_resources_and_default_command() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=rag:rag migrations ./migrations" in dockerfile
    assert "compatibility-manifest.json" in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    legacy = yaml.safe_load(
        (_ROOT / "deployment/compose.yaml").read_text(encoding="utf-8")
    )
    assert legacy["services"]["rag-app"]["command"] == ["legacy-serve"]


def test_source_revision_is_trace_only_for_compatible_manifest() -> None:
    payload = json.loads(
        (_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8")
    )
    payload["source_revision"] = "different-but-traceable-revision"
    manifest = CompatibilityManifest.model_validate(payload)

    manifest.require_compatible()
