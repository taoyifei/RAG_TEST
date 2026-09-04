"""P11 页面托管真实 Provider 的有界公开合成数据验收。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep
from typing import cast

import httpx
import pytest

from rag_app.application.provider_health import ProviderCircuitBreaker
from rag_app.core.models import CircuitKey, CircuitState
from rag_app.core.policies import CircuitBreakerPolicy
from rag_app.core.tokenization import estimate_tokens
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
_JINA_CIRCUIT_KEY = CircuitKey(
    "jina-embedding",
    "embedding",
    "jina-embeddings-v5-text-small",
)
_PLANNED_REQUEST_BUDGET = 23
_MIN_REQUEST_BUDGET = 23
_MAX_REQUEST_BUDGET = 25
_MAX_TOKEN_BUDGET = 1_000
_MAX_PROVIDER_TOKEN_BUDGET = 600


@dataclass(slots=True)
class _ProviderLedger:
    """单个 Provider 的不含正文与密钥的预算账本。"""

    total: int = 0
    forwarded: int = 0
    blocked: int = 0
    estimated_input_tokens: int = 0


@dataclass(slots=True)
class _OutboundBudget:
    """统计真实 Provider HTTP 尝试并在发出前执行硬上限。"""

    max_requests: int
    max_tokens: int
    max_provider_tokens: int
    total: int = 0
    forwarded: int = 0
    blocked: int = 0
    estimated_input_tokens: int = 0
    providers: dict[str, _ProviderLedger] = field(default_factory=dict)

    def reserve(self, request: httpx.Request) -> str | None:
        """在一次 Provider HTTP 尝试发送前预留预算。

        Args:
            request: httpx 已构造的出站请求。

        Returns:
            Provider 安全名称；非目标 Provider 请求返回 None。

        Raises:
            AssertionError: 请求或估算输入 Token 将超过授权上限。

        """
        provider = _provider_name(request)
        if provider is None:
            return None
        estimated_tokens = _estimate_request_tokens(request)
        ledger = self.providers.setdefault(provider, _ProviderLedger())
        if self.total + 1 > self.max_requests:
            raise AssertionError("P11 Live Provider 请求预算已耗尽。")
        if self.estimated_input_tokens + estimated_tokens > self.max_tokens:
            raise AssertionError("P11 Live Provider Token 预算已耗尽。")
        if (
            ledger.estimated_input_tokens + estimated_tokens
            > self.max_provider_tokens
        ):
            raise AssertionError(f"P11 Live {provider} Token 预算已耗尽。")
        self.total += 1
        self.estimated_input_tokens += estimated_tokens
        ledger.total += 1
        ledger.estimated_input_tokens += estimated_tokens
        return provider

    def mark_forwarded(self, provider: str) -> None:
        """记录一次已转发给 Provider 的尝试。"""
        self.forwarded += 1
        self.providers[provider].forwarded += 1

    def mark_blocked(self, provider: str) -> None:
        """记录一次由验收代理本地阻断的尝试。"""
        self.blocked += 1
        self.providers[provider].blocked += 1

    def safe_summary(self) -> dict[str, object]:
        """返回只含安全名称和数值的账本摘要。"""
        return {
            "total": self.total,
            "forwarded": self.forwarded,
            "blocked": self.blocked,
            "estimated_input_tokens": self.estimated_input_tokens,
            "providers": {
                name: {
                    "total": ledger.total,
                    "forwarded": ledger.forwarded,
                    "blocked": ledger.blocked,
                    "estimated_input_tokens": (ledger.estimated_input_tokens),
                }
                for name, ledger in sorted(self.providers.items())
            },
        }


def _provider_name(request: httpx.Request) -> str | None:
    """将允许的 Provider 主机映射为不含凭据的稳定名称。"""
    host = request.url.host
    if host == "api.jina.ai":
        return "jina"
    if host == "dashscope.aliyuncs.com" or (
        host is not None and host.endswith(".maas.aliyuncs.com")
    ):
        return "aliyun"
    return None


def _estimate_request_tokens(request: httpx.Request) -> int:
    """保守估算 Provider 请求中公开合成文本的输入 Token。

    Args:
        request: httpx 已构造的 JSON 请求。

    Returns:
        按每个 Unicode code point 一个 Token 计算的保守上界。

    """
    payload = cast(dict[str, object], json.loads(request.content))
    texts: list[str] = []
    input_count = 0
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        input_count = 1
        texts.append(raw_input)
    elif isinstance(raw_input, list):
        input_count = len(raw_input)
        texts.extend(str(item) for item in raw_input)
    elif isinstance(raw_input, dict):
        raw_texts = raw_input.get("texts", [])
        if isinstance(raw_texts, list):
            input_count = len(raw_texts)
            texts.extend(str(item) for item in raw_texts)
    raw_texts = payload.get("texts")
    if isinstance(raw_texts, str):
        input_count += 1
        texts.append(raw_texts)
    elif isinstance(raw_texts, list):
        input_count += len(raw_texts)
        texts.extend(str(item) for item in raw_texts)
    query = payload.get("query")
    if isinstance(query, str):
        texts.append(query)
    documents = payload.get("documents")
    if isinstance(documents, list):
        texts.extend(str(item) for item in documents)
    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        instruct = parameters.get("instruct")
        if isinstance(instruct, str):
            texts.extend(instruct for _ in range(input_count))
    return sum(estimate_tokens(text) for text in texts)


def test_request_token_estimate_counts_chinese_and_rerank_fields() -> None:
    """离线验证中文输入与 rerank 字段使用核心估算器。"""
    raw_input = "公开输入"
    raw_texts = ["顶层文本"]
    query = "公开合成查询"
    documents = ["甲文档", "乙文档"]
    request = httpx.Request(
        "POST",
        "https://api.jina.ai/v1/rerank",
        json={
            "input": raw_input,
            "texts": raw_texts,
            "query": query,
            "documents": documents,
        },
    )

    assert _estimate_request_tokens(request) == sum(
        estimate_tokens(text)
        for text in (raw_input, *raw_texts, query, *documents)
    )


def test_request_token_estimate_multiplies_aliyun_instruct_per_input() -> None:
    """离线验证百炼 instruct 按每个输入分别计入。"""
    inputs = ["公开文本一", "公开文本二", "公开文本三"]
    instruct = "生成检索向量"
    request = httpx.Request(
        "POST",
        "https://text-embedding-v4.cn-beijing.maas.aliyuncs.com/api/v1",
        json={
            "input": {"texts": inputs},
            "parameters": {"instruct": instruct},
        },
    )

    assert _estimate_request_tokens(request) == sum(
        estimate_tokens(text) for text in inputs
    ) + len(inputs) * estimate_tokens(instruct)


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
        raise AssertionError("P11 Live 请求预算必须在 23—25 之间。")
    if not 1 <= max_tokens <= _MAX_TOKEN_BUDGET:
        raise AssertionError("P11 Live Token 预算必须在 1—1000 之间。")
    return _OutboundBudget(
        max_requests,
        max_tokens,
        _MAX_PROVIDER_TOKEN_BUDGET,
    )


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
    fault = {
        "block_jina_query": False,
        "expect_half_open": False,
        "observed_half_open": False,
    }
    clock = {"value": 0.0}
    circuits: list[ProviderCircuitBreaker] = []
    half_open_circuit: list[ProviderCircuitBreaker] = []
    original_handle_request = httpx.HTTPTransport.handle_request

    def _acceptance_proxy(
        transport: httpx.HTTPTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        provider = budget.reserve(request)
        if provider is None:
            return original_handle_request(transport, request)
        is_jina_query = (
            provider == "jina"
            and request.url.path.endswith("/embeddings")
            and json.loads(request.content).get("task") == "retrieval.query"
        )
        if fault["expect_half_open"] and is_jina_query:
            matching_circuits = [
                circuit
                for circuit in circuits
                if circuit.snapshot(_JINA_CIRCUIT_KEY).state
                is CircuitState.HALF_OPEN
            ]
            assert len(matching_circuits) == 1
            half_open_circuit[:] = matching_circuits
            fault["observed_half_open"] = True
        if fault["block_jina_query"] and is_jina_query:
            budget.mark_blocked(provider)
            raise httpx.ConnectTimeout(
                "P11 acceptance proxy blocked Jina query",
                request=request,
            )
        budget.mark_forwarded(provider)
        return original_handle_request(transport, request)

    def _circuit() -> ProviderCircuitBreaker:
        circuit = ProviderCircuitBreaker(
            CircuitBreakerPolicy(
                failure_threshold=2,
                open_cooldown_seconds=10,
                recovery_success_threshold=1,
            ),
            clock=lambda: clock["value"],
        )
        circuits.append(circuit)
        return circuit

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
        open_circuits = [
            circuit
            for circuit in circuits
            if circuit.snapshot(_JINA_CIRCUIT_KEY).state is CircuitState.OPEN
        ]
        assert len(open_circuits) == 1

        fault["block_jina_query"] = False
        clock["value"] = 11.0
        fault["expect_half_open"] = True
        recovered = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "公开合成恢复探测", "limit": 5},
        )
        recovered.raise_for_status()
        fault["expect_half_open"] = False
        assert recovered.json()["selected_embedding_slot"] == "primary"
        assert recovered.json()["selected_vector_name"] == "dense_primary"
        assert fault["observed_half_open"] is True
        assert len(half_open_circuit) == 1
        assert (
            half_open_circuit[0].snapshot(_JINA_CIRCUIT_KEY).state
            is CircuitState.CLOSED
        )

        after_recovery = harness.client.post(
            search_path,
            headers=harness.write_headers,
            json={"query": "公开合成恢复后主路查询", "limit": 5},
        )
        after_recovery.raise_for_status()
        assert after_recovery.json()["selected_embedding_slot"] == "primary"
        assert after_recovery.json()["selected_vector_name"] == "dense_primary"
        assert (
            half_open_circuit[0].snapshot(_JINA_CIRCUIT_KEY).state
            is CircuitState.CLOSED
        )

        health = harness.runtime.sdk.health()
        assert health.primary_live_evaluation_status == "live_validated"
        assert health.standby_live_evaluation_status == "live_validated"
        assert health.reranker_live_evaluation_status == "live_validated"
        assert health.remote_production_profile_ready is True
        database = tmp_path / "data" / "universal-rag.sqlite3"
        database_bytes = database.read_bytes()
        assert jina_key.encode() not in database_bytes
        assert aliyun_key.encode() not in database_bytes
        assert budget.total == _PLANNED_REQUEST_BUDGET
        assert budget.forwarded == 17
        assert budget.blocked == 6
        assert budget.total == budget.forwarded + budget.blocked
        assert budget.estimated_input_tokens <= budget.max_tokens
        assert set(budget.providers) == {"aliyun", "jina"}
        jina_ledger = budget.providers["jina"]
        aliyun_ledger = budget.providers["aliyun"]
        assert (
            jina_ledger.total,
            jina_ledger.forwarded,
            jina_ledger.blocked,
        ) == (18, 12, 6)
        assert (
            aliyun_ledger.total,
            aliyun_ledger.forwarded,
            aliyun_ledger.blocked,
        ) == (5, 5, 0)
        assert all(
            ledger.estimated_input_tokens <= budget.max_provider_tokens
            for ledger in budget.providers.values()
        )
    finally:
        print(
            "P11_LIVE_BUDGET="
            + json.dumps(budget.safe_summary(), sort_keys=True)
        )
        harness.close()
