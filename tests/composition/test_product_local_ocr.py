"""Product Composition Root 的可选本地 OCR 配置边界。"""

from pathlib import Path

import pytest

from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.ocr_adapters import LOCAL_OCR_CONNECTION_ID
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


def test_product_runtime_can_select_injected_local_ocr(tmp_path: Path) -> None:
    """端点与 0600 Token 齐备时，本地 OCR 可通过 Product 设置选择。"""
    token = tmp_path / "local-ocr-token"
    token.write_text("s" * 32, encoding="utf-8")
    token.chmod(0o600)
    harness = build_product_harness(
        tmp_path,
        local_ocr_endpoints=("http://127.0.0.1:18090",),
        local_ocr_token_file=token,
    )
    try:
        _, knowledge_base_id = create_project_and_knowledge_base(harness)
        saved = harness.runtime.models.save(
            knowledge_base_id,
            KnowledgeBaseModelSettings(
                ocr_enabled=True,
                ocr_connection_id=LOCAL_OCR_CONNECTION_ID,
                ocr_model="pp-ocrv5-server",
            ),
        )
        assert saved.ocr_connection_id == LOCAL_OCR_CONNECTION_ID
        assert harness.runtime.providers.local_ocr_available is True
        settings = harness.client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings"
        )
        assert settings.status_code == 200
        assert settings.json()["ocr_configured"] is True
        assert settings.json()["local_ocr_available"] is True
    finally:
        harness.close()


def test_local_ocr_configuration_fails_closed(tmp_path: Path) -> None:
    """端点、Token 或固定模型身份不完整时不得伪装为可用。"""
    with pytest.raises(ValueError, match="RAG_OCR_API_TOKEN_FILE"):
        build_product_harness(
            tmp_path / "missing-token",
            local_ocr_endpoints=("http://rag-ocr:8090",),
        )

    token = tmp_path / "bad-permissions-token"
    token.write_text("s" * 32, encoding="utf-8")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        build_product_harness(
            tmp_path / "bad-token",
            local_ocr_endpoints=("http://rag-ocr:8090",),
            local_ocr_token_file=token,
        )
