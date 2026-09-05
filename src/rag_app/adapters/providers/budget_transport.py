"""在所有 Provider HTTP 入口共用的发送边界应用持久授权与预算。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from rag_app.adapters.providers.budget_authorization import (
    provider_request_lease,
)
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetRequest,
    ProviderBudgetLedger,
    safe_identifier,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.tokenization import estimate_tokens


@dataclass(frozen=True)
class _Binding:
    ledger: ProviderBudgetLedger
    campaign_id: str
    authorization_id: str
    scope: str
    step_id: str
    local_blocker: Callable[[httpx.Request], bool] | None = None


_BINDING: ContextVar[_Binding | None] = ContextVar(
    "provider_budget", default=None
)
_LOCAL_BLOCKER: ContextVar[Callable[[httpx.Request], bool] | None] = ContextVar(
    "provider_budget_local_blocker", default=None
)
_ALIYUN_WORKSPACE_HOST = re.compile(
    r"[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com\Z"
)
_MAX_USAGE = 2**63 - 1
_MAX_OBSERVATION_BYTES = 4 * 1024 * 1024


@contextmanager
def provider_budget_scope(
    ledger: ProviderBudgetLedger,
    *,
    campaign_id: str,
    authorization_id: str,
    scope: str,
    step_id: str,
) -> Iterator[None]:
    """把同步 SDK、Probe 和验收 Transport 绑定到同一持久授权。

    跨进程或后台任务应通过 RAG_PROVIDER_BUDGET_* 环境绑定同一账本；
    ContextVar 只用于当前调用链的阶段与验收局部故障，不能跨线程授权。

    Args:
        ledger: 已经保存授权与历史的账本。
        campaign_id: 当前活动身份。
        authorization_id: 必须与持久配置一致的授权身份。
        scope: 本次调用的批准范围。
        step_id: 用于续跑和预算统计的阶段身份。

    Returns:
        退出时恢复上层调用范围的上下文管理器。

    """
    token = _BINDING.set(
        _Binding(ledger, campaign_id, authorization_id, scope, step_id)
    )
    try:
        yield
    finally:
        _BINDING.reset(token)


@contextmanager
def provider_budget_fault(
    local_blocker: Callable[[httpx.Request], bool] | None,
) -> Iterator[None]:
    """限定故障注入只作用于当前验收调用链，不发布产品错误开关。

    Args:
        local_blocker: 判断本次请求是否应在本地阻断的验收回调。

    Returns:
        恢复原故障设置的上下文管理器。

    """
    token = _LOCAL_BLOCKER.set(local_blocker)
    try:
        yield
    finally:
        _LOCAL_BLOCKER.reset(token)


def _binding(ledger_path: Path) -> _Binding | None:
    explicit = _explicit_binding()
    if not ledger_path.exists():
        return explicit
    reader = ProviderBudgetLedger(ledger_path, read_only=True)
    campaign = reader.active_campaign()
    if campaign is None:
        return explicit
    persistent = _Binding(
        ProviderBudgetLedger(ledger_path),
        campaign.campaign_id,
        campaign.authorization_id,
        campaign.scope,
        "background",
    )
    if explicit is None:
        return persistent
    if (
        explicit.ledger.path.resolve() != ledger_path.resolve()
        or explicit.campaign_id != persistent.campaign_id
        or explicit.authorization_id != persistent.authorization_id
        or explicit.scope != persistent.scope
    ):
        raise BudgetBlockedError("ACTIVE_CAMPAIGN_BINDING_MISMATCH")
    return replace(
        persistent,
        step_id=explicit.step_id,
        local_blocker=explicit.local_blocker,
    )


def _explicit_binding() -> _Binding | None:
    active = _BINDING.get()
    if active is not None:
        return active
    prefix = "RAG_PROVIDER_BUDGET_"
    values = [
        os.environ.get(prefix + name)
        for name in ("LEDGER", "CAMPAIGN_ID", "AUTHORIZATION_ID", "SCOPE")
    ]
    if not any(values):
        return None
    if not all(values):
        raise BudgetBlockedError("BUDGET_CONFIGURATION_INCOMPLETE")
    ledger_path, campaign_id, authorization_id, scope = (
        str(value) for value in values
    )
    return _Binding(
        ProviderBudgetLedger(ledger_path),
        campaign_id,
        authorization_id,
        scope,
        os.environ.get(prefix + "STEP_ID", "background"),
    )


def payload_contract(
    payload: object,
) -> tuple[str, tuple[str, ...], str | None]:
    """提取准确请求哈希和文本/参数形状批准集，不保存正文。

    支持当前 Jina input、Aliyun input.texts 和 rerank query/documents。
    形状保留模型与所有策略参数，变更 instruct 或模型会改变批准身份。

    Args:
        payload: 待发送或待批准的 JSON 请求对象。

    Returns:
        完整请求哈希、逐文本哈希和保留参数的形状哈希。

    """
    payload_hash = canonical_sha256(payload)
    if not isinstance(payload, dict):
        return payload_hash, (), None
    shape = dict(payload)
    texts: list[str] = []
    if isinstance(payload.get("input"), list):
        values = payload["input"]
        if not all(isinstance(value, str) for value in values):
            return payload_hash, (), None
        texts.extend(values)
        shape["input"] = ["<approved-text>"]
    elif isinstance(payload.get("input"), dict):
        values = payload["input"].get("texts")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            return payload_hash, (), None
        texts.extend(values)
        shape["input"] = {
            **payload["input"],
            "texts": ["<approved-text>"],
        }
    elif isinstance(payload.get("query"), str):
        values = payload.get("documents")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            return payload_hash, (), None
        texts.extend([payload["query"], *values])
        shape["query"] = "<approved-text>"
        shape["documents"] = ["<approved-text>"]
    else:
        return payload_hash, (), None
    return (
        payload_hash,
        tuple(canonical_sha256(text) for text in texts),
        canonical_sha256(shape),
    )


def estimated_input_tokens(payload: object) -> int:
    """使用现有估算器预留文本与逐输入 instruct，不能代表实际计费。

    Args:
        payload: 待估算的供应商请求对象。

    Returns:
        与现有 Token 估算器一致的输入预留值。

    """
    return sum(estimate_tokens(text) for text in _request_texts(payload))


def provider_request_identity(
    endpoint: str,
    model: object,
    identity: Mapping[str, object] | None = None,
    *,
    method: str = "POST",
) -> str:
    """绑定目标端点、模型与连接版本，不含 Secret。

    Args:
        endpoint: 实际受控 Provider HTTP 地址。
        model: 请求中的模型身份。
        identity: 连接、配置及 Credential 版本的安全字段。
        method: 已批准的 HTTP 方法。

    Returns:
        与发送边界一致的请求身份哈希。

    """
    return canonical_sha256(
        {
            "method": method,
            "endpoint": endpoint,
            "model": model,
            **dict(identity or {}),
        }
    )


class BudgetedTransport(httpx.BaseTransport):
    """在真正调用下层 Transport 前原子预留每次重试的额度。"""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        identity: Mapping[str, object]
        | Callable[[], Mapping[str, object]]
        | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        self._transport = transport or httpx.HTTPTransport(trust_env=False)
        self._identity = (
            identity if callable(identity) else dict(identity or {})
        )
        self._ledger_path = ledger_path or (
            Path(os.environ.get("RAG_DATA_DIR", ".data/product"))
            / "provider-budget.sqlite3"
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """执行批准集检查、持久预留和安全结果审计。

        Args:
            request: 底层 httpx 已构造的请求。

        Returns:
            经过预算检查后的供应商响应。

        """
        with provider_request_lease(self._ledger_path.parent):
            return self._handle_request(request)

    def _handle_request(self, request: httpx.Request) -> httpx.Response:
        binding = _binding(self._ledger_path)
        if binding is None:
            return self._transport.handle_request(request)
        descriptor = _request_descriptor(
            request,
            self._identity() if callable(self._identity) else self._identity,
        )
        attempt_id = binding.ledger.reserve(
            binding.campaign_id,
            authorization_id=binding.authorization_id,
            scope=binding.scope,
            step_id=binding.step_id,
            request=descriptor,
        )
        blocker = _LOCAL_BLOCKER.get() or binding.local_blocker
        if blocker is not None and blocker(request):
            binding.ledger.mark_locally_blocked(attempt_id)
            raise httpx.ConnectTimeout(
                "ACCEPTANCE_LOCALLY_BLOCKED", request=request
            )
        binding.ledger.mark_forwarded(attempt_id)
        try:
            response = self._transport.handle_request(request)
            response.read()
        except httpx.HTTPError:
            binding.ledger.finish(attempt_id, status="TRANSPORT_ERROR")
            raise
        observed, request_id = _response_observation(response)
        binding.ledger.finish(
            attempt_id,
            status="HTTP_SUCCESS" if response.is_success else "HTTP_ERROR",
            observed_tokens=observed,
            request_id=request_id,
            http_status=response.status_code,
        )
        return response

    def close(self) -> None:
        """关闭下层连接池。

        Args:
            无参数；操作当前传输实例。

        Returns:
            无返回值。

        """
        self._transport.close()


def budgeted_client(
    client: httpx.Client,
    *,
    identity: Mapping[str, object] | None = None,
) -> httpx.Client:
    """包装已有 SDK 注入客户端的 Transport，保留其超时和 Mock。

    httpx 0.28 没有公开的 transport 替换 API；只对本仓库固定版本的
    同步客户端安装一次包装，且同时覆盖 URL mounts，防止代理分支绕过。

    Args:
        client: 仓库支持版本的同步 httpx 客户端。
        identity: 可选连接与凭据版本的非 Secret 身份。

    Returns:
        已安装共同预算传输的原客户端。

    """
    if not isinstance(client._transport, BudgetedTransport):
        client._transport = BudgetedTransport(
            client._transport, identity=identity
        )
        for pattern, transport in client._mounts.items():
            if transport is not None and not isinstance(
                transport, BudgetedTransport
            ):
                client._mounts[pattern] = BudgetedTransport(
                    transport, identity=identity
                )
    return client


def _request_descriptor(
    request: httpx.Request, identity: Mapping[str, object]
) -> BudgetRequest:
    try:
        payload = json.loads(request.content)
    except (UnicodeDecodeError, ValueError):
        raise BudgetBlockedError("PROVIDER_PAYLOAD_NOT_JSON") from None
    payload_hash, text_hashes, shape_hash = payload_contract(payload)
    if request.url.host == "api.jina.ai":
        provider = "jina"
    elif request.url.host == "dashscope.aliyuncs.com" or (
        _ALIYUN_WORKSPACE_HOST.fullmatch(request.url.host)
    ):
        provider = "aliyun"
    else:
        raise BudgetBlockedError("PROVIDER_ENDPOINT_NOT_APPROVED")
    operation = (
        "reranking" if "rerank" in request.url.path else "embedding.document"
    )
    if isinstance(payload, dict) and (
        payload.get("task") == "retrieval.query"
        or (
            isinstance(payload.get("parameters"), dict)
            and payload["parameters"].get("text_type") == "query"
        )
    ):
        operation = "embedding.query"
    retry = request.extensions.get("rag_provider_retry_index", 0)
    return BudgetRequest(
        provider=provider,
        operation=operation,
        request_identity=provider_request_identity(
            str(request.url),
            payload.get("model") if isinstance(payload, dict) else None,
            identity,
            method=request.method,
        ),
        payload_identity=payload_hash,
        estimated_input_tokens=estimated_input_tokens(payload),
        retry_index=retry if type(retry) is int else 0,
        text_hashes=text_hashes,
        shape_identity=shape_hash,
    )


def _request_texts(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    values = payload.get("input", [])
    if isinstance(values, dict):
        values = values.get("texts", [])
    texts = (
        [value for value in values if isinstance(value, str)]
        if isinstance(values, list)
        else []
    )
    if isinstance(payload.get("query"), str):
        texts.append(payload["query"])
    documents = payload.get("documents", [])
    if isinstance(documents, list):
        texts.extend(value for value in documents if isinstance(value, str))
    parameters = payload.get("parameters", {})
    if isinstance(parameters, dict) and isinstance(
        parameters.get("instruct"), str
    ):
        texts.extend([parameters["instruct"]] * len(texts))
    return texts


def _response_observation(
    response: httpx.Response,
) -> tuple[int | None, str | None]:
    if len(response.content) > _MAX_OBSERVATION_BYTES:
        return None, None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    observed: int | None = None
    if isinstance(usage, dict):
        for field in ("total_tokens", "input_tokens", "prompt_tokens"):
            value = usage.get(field)
            if type(value) is int and 0 <= value <= _MAX_USAGE:
                observed = value
                break
    request_id = safe_identifier(
        payload.get("request_id") or response.headers.get("x-request-id")
    )
    return observed, request_id
