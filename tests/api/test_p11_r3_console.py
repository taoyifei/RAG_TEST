"""R3 控制台读取权威能力与验证时效，不触发 Provider 请求。"""

from pathlib import Path

from tests.product_support import build_product_harness


def test_catalog_capabilities_and_current_validation(tmp_path: Path):
    harness = build_product_harness(tmp_path)
    try:
        client = harness.client
        catalog = client.get("/api/v1/provider-catalog").json()
        jina = next(
            item
            for item in catalog["providers"]
            if item["provider_type"] == "jina"
        )
        assert jina["operation_models"]["reranking"] == ["jina-reranker-v3.5"]
        response = client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "display_name": "合成 R3 连接",
                "provider_type": "jina",
                "credential": {
                    "provider_type": "jina",
                    "source": "database_encrypted",
                    "secret_value": "synthetic-r3-credential",
                },
            },
        )
        assert response.status_code == 201, response.text
        connection = response.json()
        run = harness.runtime.providers.validate(
            connection["connection_id"],
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert run.status == "succeeded"
        path = f"/api/v1/provider-connections/{connection['connection_id']}"
        history = client.get(f"{path}/validations").json()["items"]
        assert history[0]["is_current"] is True
        patched = client.patch(
            path,
            headers=harness.write_headers,
            json={"expected_version": 1, "display_name": "合成连接新名称"},
        )
        assert patched.status_code == 200
        history = client.get(f"{path}/validations").json()["items"]
        assert len(history) == 1
        assert history[0]["is_current"] is False
        assert history[0]["validation_id"] == run.validation_id
    finally:
        harness.close()
