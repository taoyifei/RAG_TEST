"""在所有 Provider HTTP 入口共用的发送边界应用持久授权与预算。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import time
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from rag_app.adapters.providers.budget_authorization import (
    provider_request_lease,
)
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetRequest,
    ProviderBudgetLedger,
    safe_identifier,
)
from rag_app.adapters.providers.offline_mock_transport import (
    BuiltinOfflineMockTransport,
)
from rag_app.adapters.providers.transport_diagnostics import (
    transport_diagnostics,
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


@dataclass(frozen=True)
class ProviderDataScope:
    """仅由已鉴权应用建立的来源上下文，不接受请求 JSON 自报。"""

    project_id: str
    knowledge_base_id: str
    source_hashes: tuple[str, ...]
    media_hashes: tuple[str, ...] = ()


_BINDING: ContextVar[_Binding | None] = ContextVar(
    "provider_budget", default=None
)
_LOCAL_BLOCKER: ContextVar[Callable[[httpx.Request], bool] | None] = ContextVar(
    "provider_budget_local_blocker", default=None
)
_DATA_SCOPE: ContextVar[ProviderDataScope | None] = ContextVar(
    "provider_data_scope", default=None
)
_ALIYUN_WORKSPACE_HOST = re.compile(
    r"[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com\Z"
)
_MAX_USAGE = 2**63 - 1
_MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
_CHAT_OPERATIONS = frozenset({"generation", "query.rewrite", "image.ocr"})
_CHAT_LIMITS = {
    "generation": (6144, 1536),
    "query.rewrite": (1024, 256),
    "image.ocr": (2560, 4096),
}
_IMAGE_TOKEN_RESERVATION = 2048
_MAX_IMAGE_BYTES = 2_097_152
_MAX_OUTPUT_TOKENS = 4096
_MAX_IMAGE_PIXELS = 1_048_576
_OCR_CONTENT_PARTS = 2
_MAX_IMAGE_DIMENSION = 4096
_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


@contextmanager
def provider_data_scope(
    *,
    project_id: str,
    knowledge_base_id: str,
    source_hashes: tuple[str, ...],
    media_hashes: tuple[str, ...] = (),
) -> Iterator[None]:
    """在实际执行线程绑定已经鉴权、按当前来源版本验证的数据范围。

    Args:
        project_id: 当前产品请求的项目。
        knowledge_base_id: 当前产品请求的知识库。
        source_hashes: 本次证据/图片所属原件的实际 SHA256。
        media_hashes: 本次选中且获准识别的媒体 SHA256。

    Returns:
        离开调用链后恢复之前范围的上下文管理器。

    """
    if any(
        safe_identifier(value) is None
        for value in (project_id, knowledge_base_id)
    ):
        raise ValueError("出网项目与知识库身份无效。")
    if any(
        not _HASH.fullmatch(value) for value in (*source_hashes, *media_hashes)
    ):
        raise ValueError("出网来源只能使用实际 SHA256。")
    scope = ProviderDataScope(
        project_id,
        knowledge_base_id,
        tuple(value.removeprefix("sha256:") for value in source_hashes),
        tuple(value.removeprefix("sha256:") for value in media_hashes),
    )
    token = _DATA_SCOPE.set(scope)
    try:
        yield
    finally:
        _DATA_SCOPE.reset(token)


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
    if explicit is not None:
        selected = explicit.ledger.campaign(explicit.campaign_id)
        if selected.scope_mode == "knowledge_base":
            if explicit.ledger.path.resolve() != ledger_path.resolve():
                raise BudgetBlockedError("ACTIVE_CAMPAIGN_BINDING_MISMATCH")
            # 显式业务授权独立累计，不替换或扩大旧合成验收的活动范围。
            return explicit
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
    total = sum(estimate_tokens(text) for text in _request_texts(payload))
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        for message in payload["messages"]:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            total += 64 if isinstance(content, list) else 16
            if isinstance(content, list):
                total += _IMAGE_TOKEN_RESERVATION * sum(
                    isinstance(item, dict) and item.get("type") == "image_url"
                    for item in content
                )
    return total


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
        offline_restore = (
            type(self._transport) is BuiltinOfflineMockTransport
            and (
                self._ledger_path.parent / "provider-budget.restore-blocked"
            ).exists()
        )
        with provider_request_lease(
            self._ledger_path.parent, offline_restore=offline_restore
        ):
            if offline_restore:
                # 恢复标记只限制真实出站；固定内存结果不消耗或重置累计账。
                blocker = _LOCAL_BLOCKER.get()
                if blocker is not None and blocker(request):
                    request.extensions["rag_locally_blocked"] = True
                    raise httpx.ConnectTimeout(
                        "ACCEPTANCE_LOCALLY_BLOCKED", request=request
                    )
                return self._transport.handle_request(request)
            return self._handle_request(request)

    def _handle_request(self, request: httpx.Request) -> httpx.Response:
        binding = _binding(self._ledger_path)
        if binding is None:
            if request.url.path.endswith("/chat/completions") and type(
                self._transport
            ) not in {httpx.MockTransport, BuiltinOfflineMockTransport}:
                raise BudgetBlockedError("CHAT_AUTHORIZATION_REQUIRED")
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
        request.extensions["rag_budget_attempt_id"] = attempt_id
        blocker = _LOCAL_BLOCKER.get() or binding.local_blocker
        if blocker is not None and blocker(request):
            binding.ledger.mark_locally_blocked(attempt_id)
            request.extensions["rag_locally_blocked"] = True
            raise httpx.ConnectTimeout(
                "ACCEPTANCE_LOCALLY_BLOCKED", request=request
            )
        binding.ledger.mark_forwarded(attempt_id)
        started = time.monotonic()
        try:
            response = self._transport.handle_request(request)
            response.read()
        except httpx.HTTPError as error:
            _finish_safely(
                binding.ledger,
                attempt_id,
                outcome={"status": "TRANSPORT_ERROR"},
                diagnostics=transport_diagnostics(
                    error,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    extensions=request.extensions,
                ),
            )
            raise
        observed, request_id = _response_observation(
            response, chat=descriptor.operation in _CHAT_OPERATIONS
        )
        _finish_safely(
            binding.ledger,
            attempt_id,
            outcome={
                "status": "HTTP_SUCCESS"
                if response.is_success
                else "HTTP_ERROR",
                "observed_tokens": observed,
                "request_id": request_id,
                "http_status": response.status_code,
            },
            diagnostics=transport_diagnostics(
                None,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                extensions=request.extensions,
            ),
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


def _finish_safely(
    ledger: ProviderBudgetLedger,
    attempt_id: str,
    *,
    outcome: dict[str, Any],
    diagnostics: dict[str, Any],
) -> None:
    try:
        ledger.finish(attempt_id, **outcome)
        ledger.record_diagnostics(attempt_id, diagnostics)
    except Exception:
        # 预留/转发门禁仍强制执行；终态审计失败不能覆盖已有业务结果。
        # 未完成记录保持已转发、usage 未知，不能释放已消耗的预算。
        return


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
    chat = request.url.path.endswith("/chat/completions")
    if chat:
        raw_operation = request.extensions.get("rag_chat_operation")
        operation = (
            raw_operation
            if isinstance(raw_operation, str)
            and raw_operation in _CHAT_OPERATIONS
            else "chat.unknown"
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
    data_scope = _DATA_SCOPE.get()
    media_hashes = _submitted_media_hashes(payload) if chat else ()
    input_tokens = estimated_input_tokens(payload)
    output_tokens = _output_tokens(payload) if chat else 0
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
        estimated_input_tokens=input_tokens,
        retry_index=retry if type(retry) is int else 0,
        text_hashes=text_hashes,
        shape_identity=shape_hash,
        model=payload.get("model") if isinstance(payload, dict) else None,
        project_id=None if data_scope is None else data_scope.project_id,
        knowledge_base_id=None
        if data_scope is None
        else data_scope.knowledge_base_id,
        source_hashes=() if data_scope is None else data_scope.source_hashes,
        media_hashes=media_hashes,
        provenance_verified=(
            data_scope is not None
            and set(media_hashes) <= set(data_scope.media_hashes)
        ),
        policy_valid=not chat
        or _chat_request_valid(
            payload, operation, input_tokens, output_tokens, media_hashes
        ),
        estimated_output_tokens=output_tokens,
        estimated_image_tokens=len(media_hashes) * _IMAGE_TOKEN_RESERVATION,
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
    messages = payload.get("messages", [])
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    item["text"]
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                )
    return texts


def _output_tokens(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    maximum = payload.get("max_tokens")
    return (
        maximum
        if type(maximum) is int and 0 < maximum <= _MAX_OUTPUT_TOKENS
        else 0
    )


def _submitted_media_hashes(  # noqa: PLR0911
    payload: object,
) -> tuple[str, ...]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("messages"), list
    ):
        return ()
    hashes: list[str] = []
    for message in payload["messages"]:
        if not isinstance(message, dict) or not isinstance(
            message.get("content"), list
        ):
            continue
        for item in message["content"]:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            image = item.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            if not isinstance(url, str) or not url.startswith(
                ("data:image/png;base64,", "data:image/jpeg;base64,")
            ):
                return ()
            if len(url) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 32:
                return ()
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            except (ValueError, binascii.Error):
                return ()
            if not raw or len(raw) > _MAX_IMAGE_BYTES:
                return ()
            if not _image_within_reservation(raw, url.split(";", 1)[0]):
                return ()
            hashes.append(hashlib.sha256(raw).hexdigest())
    return tuple(hashes)


def _image_within_reservation(raw: bytes, prefix: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (
                    image.format
                    != {"data:image/png": "PNG", "data:image/jpeg": "JPEG"}.get(
                        prefix
                    )
                    or image.width * image.height > _MAX_IMAGE_PIXELS
                    or max(image.size) > _MAX_IMAGE_DIMENSION
                    or getattr(image, "n_frames", 1) != 1
                ):
                    return False
                image.verify()
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False
    return True


def _ocr_messages_valid(messages: list[Any]) -> bool:
    if len(messages) != 1 or messages[0].get("role") != "user":
        return False
    content = messages[0].get("content")
    if not isinstance(content, list) or len(content) != _OCR_CONTENT_PARTS:
        return False
    image, text = content
    if not isinstance(image, dict) or not isinstance(text, dict):
        return False
    minimum, maximum = image.get("min_pixels"), image.get("max_pixels")
    return (
        set(image) == {"type", "image_url", "min_pixels", "max_pixels"}
        and image["type"] == "image_url"
        and isinstance(image["image_url"], dict)
        and set(image["image_url"]) == {"url"}
        and type(minimum) is int
        and type(maximum) is int
        and 0 < minimum <= maximum <= _MAX_IMAGE_PIXELS
        and set(text) == {"type", "text"}
        and text["type"] == "text"
        and isinstance(text["text"], str)
    )


def _chat_request_valid(  # noqa: PLR0911
    payload: object,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    media_hashes: tuple[str, ...],
) -> bool:
    if not isinstance(payload, dict) or operation not in _CHAT_LIMITS:
        return False
    input_limit, output_limit = _CHAT_LIMITS[operation]
    if (
        input_tokens > input_limit
        or not 0 < output_tokens <= output_limit
        or set(payload)
        - {
            "model",
            "messages",
            "stream",
            "max_tokens",
            "enable_thinking",
            "response_format",
        }
        or payload.get("stream") is not False
        or payload.get("enable_thinking") not in (None, False)
    ):
        return False
    expected_model = (
        "qwen3.5-ocr" if operation == "image.ocr" else "qwen3.7-flash"
    )
    if payload.get("model") != expected_model:
        return False
    if operation != "image.ocr" and payload.get("enable_thinking") is not False:
        return False
    if operation == "image.ocr" and (
        "enable_thinking" in payload or "response_format" in payload
    ):
        return False
    if "response_format" in payload and payload["response_format"] != {
        "type": "json_object"
    }:
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
        ):
            return False
        content = message["content"]
        if operation != "image.ocr" and not isinstance(content, str):
            return False
    if operation == "image.ocr":
        return len(media_hashes) == 1 and _ocr_messages_valid(messages)
    return not media_hashes


def _response_observation(
    response: httpx.Response,
    *,
    chat: bool = False,
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
        fields = (
            ("total_tokens",)
            if chat
            else ("total_tokens", "input_tokens", "prompt_tokens")
        )
        for field in fields:
            value = usage.get(field)
            if type(value) is int and 0 <= value <= _MAX_USAGE:
                observed = value
                break
    request_id = safe_identifier(
        payload.get("request_id") or response.headers.get("x-request-id")
    )
    return observed, request_id
