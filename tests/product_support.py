"""P10.5 产品测试的隔离 Runtime 与 API 辅助函数。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import (
    ProductRuntime,
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.product.crypto import initialize_master_key
from rag_app.product.provider_runtime import (
    TransportFactory,
    build_offline_mock_transport,
)


@dataclass(slots=True)
class ProductHarness:
    """拥有测试 Runtime、同源客户端与 CSRF 的资源句柄。"""

    runtime: ProductRuntime
    client: TestClient
    csrf: str
    bootstrap_token: str

    @property
    def write_headers(self) -> dict[str, str]:
        """返回浏览器写请求使用的 CSRF Header。"""
        return {"X-CSRF-Token": self.csrf}

    def close(self) -> None:
        """关闭客户端和 Product Runtime。"""
        self.client.close()
        self.runtime.close()


def build_product_harness(
    tmp_path: Path,
    *,
    transport_factory: TransportFactory | None = build_offline_mock_transport,
    master_key: bool = True,
) -> ProductHarness:
    """构建不访问网络的完整产品测试环境。

    Args:
        tmp_path: pytest 隔离目录。
        transport_factory: Provider Transport 工厂。
        master_key: 是否配置页面托管 Secret 主密钥。

    Returns:
        已登录的 Product Harness。

    """
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>知识库工作台</title>", encoding="utf-8"
    )
    bootstrap_token = "-".join(("synthetic", "bootstrap", "credential"))
    bootstrap = tmp_path / "bootstrap-token"
    bootstrap.write_text(bootstrap_token, encoding="utf-8")
    bootstrap.chmod(0o600)
    key_path = tmp_path / "master-key"
    if master_key:
        initialize_master_key(key_path)
    settings = ProductRuntimeSettings(
        data_dir=tmp_path / "data",
        frontend_dir=frontend,
        bootstrap_token_file=bootstrap,
        master_key_file=key_path if master_key else None,
    )
    runtime = build_product_runtime(
        settings,
        transport_factory=transport_factory,
    )
    client = TestClient(create_product_app(runtime))
    response = client.post(
        "/api/v1/console/session",
        json={"bootstrap_token": bootstrap_token},
    )
    response.raise_for_status()
    return ProductHarness(
        runtime=runtime,
        client=client,
        csrf=str(response.json()["csrf_token"]),
        bootstrap_token=bootstrap_token,
    )


def create_project_and_knowledge_base(
    harness: ProductHarness,
) -> tuple[str, str]:
    """通过 Product API 创建测试项目和知识库。

    Args:
        harness: 已登录的测试环境。

    Returns:
        Project ID 与 Knowledge Base ID。

    """
    project = harness.client.post(
        "/api/v1/projects",
        headers={**harness.write_headers, "Idempotency-Key": "project-1"},
        json={"name": "产品测试"},
    )
    project.raise_for_status()
    project_id = str(project.json()["project_id"])
    knowledge_base = harness.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases",
        headers={**harness.write_headers, "Idempotency-Key": "kb-1"},
        json={"name": "产品知识库", "description": "合成公开内容"},
    )
    knowledge_base.raise_for_status()
    return project_id, str(knowledge_base.json()["knowledge_base_id"])


def create_provider_connections(
    harness: ProductHarness,
) -> tuple[str, str, str, str]:
    """创建页面托管 Jina 与环境托管百炼连接。

    Args:
        harness: 已登录的测试环境。

    Returns:
        Jina Credential、百炼 Credential、Jina Connection、百炼 Connection。

    """
    jina_credential = harness.client.post(
        "/api/v1/provider-credentials",
        headers=harness.write_headers,
        json={
            "provider_type": "jina",
            "source": "database_encrypted",
            "secret_value": "synthetic-jina-value",
        },
    )
    jina_credential.raise_for_status()
    aliyun_environment = "RAG_TEST_ALIYUN_CREDENTIAL"
    aliyun_credential = harness.client.post(
        "/api/v1/provider-credentials",
        headers=harness.write_headers,
        json={
            "provider_type": "aliyun-model-studio",
            "source": "environment_managed",
            "environment_name": aliyun_environment,
        },
    )
    aliyun_credential.raise_for_status()
    jina_id = str(jina_credential.json()["credential_id"])
    aliyun_id = str(aliyun_credential.json()["credential_id"])
    jina_connection = harness.client.post(
        "/api/v1/provider-connections",
        headers=harness.write_headers,
        json={
            "display_name": "Jina 主连接",
            "provider_type": "jina",
            "credential_id": jina_id,
        },
    )
    jina_connection.raise_for_status()
    aliyun_connection = harness.client.post(
        "/api/v1/provider-connections",
        headers=harness.write_headers,
        json={
            "display_name": "百炼备用连接",
            "provider_type": "aliyun-model-studio",
            "credential_id": aliyun_id,
            "workspace_id": "synthetic-workspace",
            "region": "cn-beijing",
        },
    )
    aliyun_connection.raise_for_status()
    return (
        jina_id,
        aliyun_id,
        str(jina_connection.json()["connection_id"]),
        str(aliyun_connection.json()["connection_id"]),
    )


def validate_five_operations(
    harness: ProductHarness,
    jina_connection_id: str,
    aliyun_connection_id: str,
) -> None:
    """运行产品拓扑要求的五项离线 Provider 验证。

    Args:
        harness: 已登录的测试环境。
        jina_connection_id: Jina 连接。
        aliyun_connection_id: 百炼连接。

    Returns:
        五项全部成功时无返回值。

    """
    operations = (
        (
            jina_connection_id,
            "embedding.document",
            "jina-embeddings-v5-text-small",
            1024,
        ),
        (
            jina_connection_id,
            "embedding.query",
            "jina-embeddings-v5-text-small",
            1024,
        ),
        (jina_connection_id, "reranking", "jina-reranker-v3.5", None),
        (
            aliyun_connection_id,
            "embedding.document",
            "qwen3.7-text-embedding",
            1024,
        ),
        (
            aliyun_connection_id,
            "embedding.query",
            "qwen3.7-text-embedding",
            1024,
        ),
    )
    for connection_id, operation, model, dimension in operations:
        response = harness.client.post(
            f"/api/v1/provider-connections/{connection_id}:validate",
            headers=harness.write_headers,
            json={
                "operation": operation,
                "model": model,
                "expected_dimension": dimension,
            },
        )
        response.raise_for_status()
        assert response.json()["status"] == "succeeded"


__all__ = [
    "ProductHarness",
    "build_product_harness",
    "create_project_and_knowledge_base",
    "create_provider_connections",
    "validate_five_operations",
]
