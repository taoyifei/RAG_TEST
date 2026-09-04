"""P11 页面托管真实 Provider 的有界公开合成数据验收。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import cast

import httpx
import pytest

from rag_app.application.provider_health import ProviderCircuitBreaker
from rag_app.core.policies import CircuitBreakerPolicy
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PROVIDER_HOSTS = frozenset({"api.jina.ai", "dashscope.aliyuncs.com"})
_MIN_REQUEST_BUDGET = 21
_MAX_REQUEST_BUDGET = 30
_MAX_TOKEN_BUDGET = 20_000


@dataclass(slots=True)
class _OutboundBudget:
    """统计真实 Provider HTTP 尝试并在发出前执行硬上限。"""

    max_requests: int
    max_tokens: int
    requests: int = 0
    estimated_tokens: int = 0

    def record(self, request: httpx.Request) -> None:
        """记录一次将要发出的 Provider HTTP 请求。

        Args:
            request: httpx 已构造的出站请求。

        Returns:
            无返回值。

        Raises:
            AssertionError: 请求或估算输入 Token 将超过授权上限。

        """
        host = request.url.host
        if host not in _PROVIDER_HOSTS and not (
            host is not None and host.endswith(".maas.aliyuncs.com")
        ):
            return
        estimated_tokens = _estimate_request_tokens(request)
        if self.requests + 1 > self.max_requests:
            raise AssertionError("P11 Live Provider 请求预算已耗尽。")
        if self.estimated_tokens + estimated_tokens > self.max_tokens:
            raise AssertionError("P11 Live Provider Token 预算已耗尽。")
        self.requests += 1
        self.estimated_tokens += estimated_tokens


def _estimate_request_tokens(request: httpx.Request) -> int:
    """保守估算 Provider 请求中公开合成文本的输入 Token。

    Args:
        request: httpx 已构造的 JSON 请求。

    Returns:
        按每四个字符至少一个 Token 计算的保守值。

    """
    payload = cast(dict[str, object], json.loads(request.content))
    texts: list[str] = []
    raw_input = payload.get("input")
    if isinstance(raw_input, list):
        texts.extend(str(item) for item in raw_input)
    elif isinstance(raw_input, dict):
        raw_texts = raw_input.get("texts", [])
        if isinstance(raw_texts, list):
            texts.extend(str(item) for item in raw_texts)
    query = payload.get("query")
    if isinstance(query, str):
        texts.append(query)
    documents = payload.get("documents")
    if isinstance(documents, list):
        texts.extend(str(item) for item in documents)
    return sum(max(1, (len(text) + 3) // 4) for text in texts)


def _required_environment(name: str) -> str:
    """读取 Live Gate 必需变量且不回显值。

    Args:
        name: 环境变量名。

    Returns:
        去除首尾空白的变量值。

    Raises:
        AssertionError: 变量未配置。

    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"P11 Live Gate 缺少变量：{name}")
    return value


def _live_budget() -> _OutboundBudget:
    """验证显式授权并创建本次真实调用预算。

    Args:
        无参数；读取受保护工作流环境。

    Returns:
        已验证的出站预算。

    Raises:
        AssertionError: 授权开关、网络模式或预算不符合任务书。

    """
    if _required_environment("P11_LIVE_AUTHORIZED") != "true":
        raise AssertionError("P11 Live Gate 未获得显式授权。")
    if _required_environment("RAG_TEST_NETWORK") != "live":
        raise AssertionError("P11 Live Gate 未显式开放测试网络。")
    max_requests = int(_required_environment("P11_MAX_REQUESTS"))
    max_tokens = int(_required_environment("P11_MAX_TOKENS"))
    if not _MIN_REQUEST_BUDGET <= max_requests <= _MAX_REQUEST_BUDGET:
        raise AssertionError("P11 Live 请求预算必须在 21—30 之间。")
    if not 1 <= max_tokens <= _MAX_TOKEN_BUDGET:
        raise AssertionError("P11 Live Token 预算必须在 1—20000 之间。")
    return _OutboundBudget(max_requests, max_tokens)


def _create_live_connections(
    harness: ProductHarness,
    *,
    jina_key: str,
    aliyun_key: str,
    workspace_id: str,
) -> tuple[str, str]:
    """通过页面后端创建两个加密托管连接。

    Args:
        harness: 已登录 Product Runtime。
        jina_key: 用户授权提供的 Jina Key。
        aliyun_key: 用户授权提供的百炼 Key。
        workspace_id: 用户授权提供的百炼业务空间 ID。

    Returns:
        Jina 与百炼 Connection ID。

    """
    connection_ids: list[str] = []
    for provider_type, display_name, secret_value, extra in (
        ("jina", "Jina Live 主连接", jina_key, {}),
        (
            "aliyun-model-studio",
            "百炼 Live 备用连接",
            aliyun_key,
            {"workspace_id": workspace_id, "region": "cn-beijing"},
        ),
    ):
        credential = harness.client.post(
            "/api/v1/provider-credentials",
            headers=harness.write_headers,
            json={
                "provider_type": provider_type,
                "source": "database_encrypted",
                "secret_value": secret_value,
            },
        )
        credential.raise_for_status()
        connection = harness.client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "credential_id": credential.json()["credential_id"],
                "display_name": display_name,
                "provider_type": provider_type,
                **extra,
            },
        )
        connection.raise_for_status()
        connection_ids.append(str(connection.json()["connection_id"]))
    return connection_ids[0], connection_ids[1]


def _validate_live_operations(
    harness: ProductHarness,
    jina_connection: str,
    aliyun_connection: str,
) -> None:
    """真实验证主备 Embedding 与 Jina Reranker 五项操作。

    Args:
        harness: 已登录 Product Runtime。
        jina_connection: Jina Connection ID。
        aliyun_connection: 百炼 Connection ID。

    Returns:
        五项真实验证均成功时无返回值。

    """
    operations = (
        (
            jina_connection,
            "embedding.document",
            "jina-embeddings-v5-text-small",
            1024,
        ),
        (
            jina_connection,
            "embedding.query",
            "jina-embeddings-v5-text-small",
            1024,
        ),
        (jina_connection, "reranking", "jina-reranker-v3.5", None),
        (
            aliyun_connection,
            "embedding.document",
            "qwen3.7-text-embedding",
            1024,
        ),
        (aliyun_connection, "embedding.query", "qwen3.7-text-embedding", 1024),
    )
    for connection_id, operation, model, expected_dimension in operations:
        response = harness.client.post(
            f"/api/v1/provider-connections/{connection_id}:validate",
            headers=harness.write_headers,
            json={
                "expected_dimension": expected_dimension,
                "model": model,
                "operation": operation,
            },
        )
        response.raise_for_status()
        payload = response.json()
        assert payload["status"] == "succeeded", payload["safe_error_code"]
        assert payload["http_category"] == "live_200"


def _wait_for_job(harness: ProductHarness, job_id: str) -> dict[str, object]:
    """等待真实双槽索引任务结束。

    Args:
        harness: 已登录 Product Runtime。
        job_id: Product Job ID。

    Returns:
        成功的 Job JSON。

    Raises:
        AssertionError: 任务失败或 120 秒内未结束。

    """
    deadline = monotonic() + 120
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        job = cast(dict[str, object], response.json())
        if job["state"] == "succeeded":
            return job
        if job["state"] not in {"queued", "running"}:
            raise AssertionError("P11 Live 双槽索引任务失败。")
        sleep(0.1)
    raise AssertionError("P11 Live 双槽索引任务超时。")


@pytest.mark.live_provider
def test_page_managed_live_dual_slot_failover_and_recovery(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收真实五项 Provider、双槽索引、切换和恢复。

    Args:
        tmp_path: pytest 隔离数据目录。
        monkeypatch: 仅在 acceptance 测试内安装网络故障注入。

    Returns:
        无返回值。

    """
    budget = _live_budget()
    jina_key = _required_environment("JINA_API_KEY")
    aliyun_key = _required_environment("DASHSCOPE_API_KEY")
    workspace_id = _required_environment("ALIYUN_MODEL_STUDIO_WORKSPACE_ID")
    if _required_environment("ALIYUN_MODEL_STUDIO_REGION") != "cn-beijing":
        raise AssertionError("P11 V1 Live Gate 只允许 cn-beijing。")
    qdrant_url = _required_environment("RAG_TEST_QDRANT_URL")
    qdrant_key_file = Path(_required_environment("RAG_TEST_QDRANT_KEY_FILE"))
    fault = {"block_jina_query": False}
    clock = {"value": 0.0}
    original_handle_request = httpx.HTTPTransport.handle_request

    def _acceptance_proxy(
        transport: httpx.HTTPTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        budget.record(request)
        if (
            fault["block_jina_query"]
            and request.url.host == "api.jina.ai"
            and request.url.path.endswith("/embeddings")
            and json.loads(request.content).get("task") == "retrieval.query"
        ):
            raise httpx.ConnectTimeout(
                "P11 acceptance proxy blocked Jina query",
                request=request,
            )
        return original_handle_request(transport, request)

    def _circuit() -> ProviderCircuitBreaker:
        return ProviderCircuitBreaker(
            CircuitBreakerPolicy(
                failure_threshold=2,
                open_cooldown_seconds=10,
                recovery_success_threshold=1,
            ),
            clock=lambda: clock["value"],
        )

    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        _acceptance_proxy,
    )
    harness = build_product_harness(
        tmp_path,
        transport_factory=None,
        circuit_factory=_circuit,
        qdrant_url=qdrant_url,
        qdrant_api_key_file=qdrant_key_file,
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        jina_connection, aliyun_connection = _create_live_connections(
            harness,
            jina_key=jina_key,
            aliyun_key=aliyun_key,
            workspace_id=workspace_id,
        )
        _validate_live_operations(harness, jina_connection, aliyun_connection)
        activate_hot_standby_profile(
            harness,
            knowledge_base_id,
            jina_connection,
            aliyun_connection,
        )
        upload = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents",
            params={"display_name": "P11-公开合成验收.docx"},
            content=build_package(
                "<w:p><w:r><w:t>公开合成文本：青岛啤酒采购申请经过审批后归档。</w:t></w:r></w:p>"
            ),
            headers={
                **harness.write_headers,
                "Content-Type": _MEDIA_TYPE,
                "Idempotency-Key": "p11-live-synthetic-document",
            },
        )
        upload.raise_for_status()
        job = _wait_for_job(harness, str(upload.json()["job_id"]))
        assert job["state"] == "succeeded"

        search_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search"
        )
        normal = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "采购申请如何归档", "limit": 5},
        )
        normal.raise_for_status()
        assert normal.json()["selected_embedding_slot"] == "primary"
        assert normal.json()["selected_vector_name"] == "dense_primary"
        assert normal.json()["evidence"]

        fault["block_jina_query"] = True
        first_failover = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "公开合成切换查询一", "limit": 5},
        )
        second_failover = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "公开合成切换查询二", "limit": 5},
        )
        first_failover.raise_for_status()
        second_failover.raise_for_status()
        assert first_failover.json()["selected_embedding_slot"] == "standby"
        assert second_failover.json()["selected_embedding_slot"] == "standby"
        assert first_failover.json()["selected_vector_name"] == "dense_standby"

        fault["block_jina_query"] = False
        clock["value"] = 11.0
        recovered = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "公开合成恢复探测", "limit": 5},
        )
        recovered.raise_for_status()
        assert recovered.json()["selected_embedding_slot"] == "primary"
        assert recovered.json()["selected_vector_name"] == "dense_primary"

        health = harness.runtime.sdk.health()
        assert health.primary_live_evaluation_status == "live_validated"
        assert health.standby_live_evaluation_status == "live_validated"
        assert health.reranker_live_evaluation_status == "live_validated"
        assert health.remote_production_profile_ready is True
        database = tmp_path / "data" / "universal-rag.sqlite3"
        database_bytes = database.read_bytes()
        assert jina_key.encode() not in database_bytes
        assert aliyun_key.encode() not in database_bytes
        assert budget.requests <= budget.max_requests
        assert budget.estimated_tokens <= budget.max_tokens
    finally:
        harness.close()
