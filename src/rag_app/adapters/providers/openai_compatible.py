"""OpenAI-compatible Embedding、Reranker 与 Chat adapters。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Literal

from pydantic import Field, StrictInt

from rag_app.adapters.providers.aliyun_chat import (
    AliyunChatAdapter,
    ChatCompletion,
    ChatContent,
    ChatMessage,
    ChatResponseError,
    _call_usage,
    _ChatStreamAccumulator,
    _sse_data_events,
    decode_chat_content,
    message_token_estimate,
)
from rag_app.adapters.providers.batching import (
    BatchLimits,
    batch_texts,
    estimate_tokens,
)
from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
    invalid_response_error,
    provider_error,
)
from rag_app.adapters.providers.validation import (
    finite_score,
    ordered_vectors,
    usage_tokens,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    PolicyDenied,
    ProviderInputTooLarge,
    QueryCancelled,
    RagError,
)
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    ProviderCall,
    ProviderHealth,
    ProviderHealthStatus,
    RerankExecutionMode,
    RerankItem,
    RerankRequest,
    RerankResult,
)
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.retrieval import AnswerClaim, AnswerDraft
from rag_app.core.ports import CancellationPort
from rag_app.core.ports.generator import GenerationRequest

_PROVIDER_ID = "openai-compatible"
_CHAT_COMPLETIONS_PATH = "/chat/completions"
_STREAM_UNSUPPORTED = frozenset(
    {
        "HTTP_400",
        "HTTP_404",
        "HTTP_405",
        "HTTP_415",
        "HTTP_422",
        "INVALID_STREAM_CONTENT_TYPE",
        # 完整 JSON 解析器兼容 JSON 代码围栏；增量解析无法在看到前缀时
        # 安全发布 claim，因此在尚未发布任何 claim 时改走一次同步解析。
        "INVALID_STREAM_SCHEMA",
    }
)


class OpenAICompatibleEmbeddingConfig(FrozenModel):
    """自定义 Embedding 的有限、非敏感运行参数。"""

    slot_id: str
    model: str = Field(min_length=1, max_length=200)
    dimension: StrictInt = Field(gt=0, le=65536)
    request_policy_identity: str
    document_request_policy_identity: str
    query_request_policy_identity: str
    document_egress_allowed: bool = False
    query_egress_allowed: bool = False
    max_batch_items: StrictInt = Field(default=8, gt=0, le=1024)
    max_input_tokens: StrictInt = Field(default=32768, gt=0, le=1_000_000)
    adapter_revision: str = "2"
    normalization: Literal["l2-v1"] = "l2-v1"


class OpenAICompatibleRerankerConfig(FrozenModel):
    """TEI 或 Jina-compatible Reranker 的显式协议配置。"""

    model: str = Field(min_length=1, max_length=200)
    protocol: Literal["tei", "jina-compatible"]
    path: str = Field(min_length=1, max_length=300)
    egress_allowed: bool = False
    max_total_tokens: StrictInt = Field(default=32768, gt=0)
    max_candidates: StrictInt = Field(default=100, gt=0, le=1000)


class OpenAICompatibleChatConfig(FrozenModel):
    """显式选择 Chat Completions 的结构化输出协议。"""

    model: str = Field(min_length=1, max_length=200)
    egress_allowed: bool = False
    max_input_tokens: StrictInt = Field(default=6144, gt=0, le=131072)
    max_output_tokens: StrictInt = Field(default=1536, gt=0, le=16384)
    max_messages: StrictInt = Field(default=6, gt=0, le=32)
    prompt_version: str = Field(default="grounded-chat-v8", max_length=64)
    disable_thinking_supported: bool = False
    structured_output_mode: Literal[
        "none", "response_format", "structured_outputs", "guided_json"
    ] = "none"


class OpenAICompatibleEmbeddingAdapter:
    """使用标准 ``/embeddings`` 协议并严格恢复输入顺序。"""

    def __init__(
        self,
        config: OpenAICompatibleEmbeddingConfig,
        *,
        http_client: ProviderHttpClient,
        api_key_resolver: Callable[[], str],
    ) -> None:
        self._config = config
        self._http = http_client
        self._resolve_key = api_key_resolver
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="openai-compatible-embedding",
            version=f"{config.model}:{config.adapter_revision}",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(config.dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def config(self) -> OpenAICompatibleEmbeddingConfig:
        """返回不含凭据的冻结配置。"""
        return self._config

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回批量、角色与维度能力。"""
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """执行标准 Embedding 调用并校验数量、索引、维度和有限值。"""
        if request.slot_id != self._config.slot_id:
            raise ValueError("兼容 Embedding slot 不匹配。")
        allowed = (
            self._config.document_egress_allowed
            if request.role is EmbeddingRequestRole.DOCUMENT
            else self._config.query_egress_allowed
        )
        if not allowed:
            raise PolicyDenied(
                "兼容 Embedding 出网未启用。",
                stage="provider.openai_compatible.embedding",
            )
        batches = batch_texts(
            request.texts,
            BatchLimits(
                max_items=self._config.max_batch_items,
                max_input_tokens=self._config.max_input_tokens,
            ),
        )
        vectors: list[tuple[float, ...]] = []
        calls: list[ProviderCall] = []
        for batch in batches:
            try:
                response = self._http.request_json(
                    "POST",
                    "/embeddings",
                    payload={
                        "model": self._config.model,
                        "input": list(batch),
                        "encoding_format": "float",
                    },
                    headers=_headers(self._resolve_key()),
                    provider_id=_PROVIDER_ID,
                    operation=f"embedding.{request.role.value}",
                    model=self._config.model,
                    input_count=len(batch),
                    estimated_tokens=sum(
                        estimate_tokens(text) for text in batch
                    ),
                )
            except ProviderHttpError as failure:
                raise provider_error(
                    failure, stage="provider.openai_compatible.embedding"
                ) from None
            observed_tokens: int | None = None
            try:
                payload = _mapping(response.payload)
                _validate_optional_model(payload, self._config.model)
                observed_tokens = _optional_usage_tokens(payload)
                batch_vectors = ordered_vectors(
                    payload.get("data"),
                    expected_count=len(batch),
                    dimension=self._config.dimension,
                    index_field="index",
                    vector_field="embedding",
                )
            except (TypeError, ValueError):
                failed = self._http.complete_call(
                    response.call,
                    observed_tokens=observed_tokens,
                    failure_reason_code="INVALID_RESPONSE_CONTRACT",
                )
                raise invalid_response_error(
                    "INVALID_RESPONSE_CONTRACT",
                    failed,
                    stage="provider.openai_compatible.embedding",
                ) from None
            calls.append(
                self._http.complete_call(
                    response.call, observed_tokens=observed_tokens
                )
            )
            vectors.extend(batch_vectors)
        if len(vectors) != len(request.texts):
            raise AssertionError("兼容 Embedding 跨批数量必须保持不变。")
        policy = (
            self._config.document_request_policy_identity
            if request.role is EmbeddingRequestRole.DOCUMENT
            else self._config.query_request_policy_identity
        )
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=tuple(vectors),
            observed_dimension=self._config.dimension,
            request_policy_identity=policy,
            calls=tuple(calls),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """无鉴权也是有效配置，健康读取不主动发请求。"""
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            checked_network=False,
            reason_code="NOT_PROBED",
        )

    def close(self) -> None:
        """关闭底层连接池。"""
        self._http.close()


class OpenAICompatibleRerankerAdapter:
    """按显式 TEI 或 Jina-compatible 协议完整评分候选。"""

    def __init__(
        self,
        config: OpenAICompatibleRerankerConfig,
        *,
        http_client: ProviderHttpClient,
        api_key_resolver: Callable[[], str],
    ) -> None:
        self._config = config
        self._http = http_client
        self._resolve_key = api_key_resolver
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.RERANKER,
            name="openai-compatible-reranker",
            version=f"{config.protocol}:{config.model}",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True, permits_network=True
            ),
        )

    @property
    def config(self) -> OpenAICompatibleRerankerConfig:
        """返回不含凭据的冻结配置。"""
        return self._config

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回远程完整候选评分能力。"""
        return self.descriptor.capabilities

    def rerank(self, request: RerankRequest) -> RerankResult:
        """发送全部候选，并拒绝缺失、重复、越界或非有限分数。"""
        if not self._config.egress_allowed:
            raise PolicyDenied(
                "兼容 Reranker 出网未启用。",
                stage="provider.openai_compatible.reranker",
            )
        if len(request.candidates) > self._config.max_candidates:
            raise ProviderInputTooLarge(
                "兼容 Reranker 候选数超过本地上限。",
                stage="provider.openai_compatible.reranker",
            )
        documents = tuple(text for _, text in request.candidates)
        estimated_tokens = estimate_tokens(request.query) + sum(
            estimate_tokens(document) for document in documents
        )
        if estimated_tokens > self._config.max_total_tokens:
            raise ProviderInputTooLarge(
                "兼容 Reranker 输入超过本地 Token 上限。",
                stage="provider.openai_compatible.reranker",
            )
        payload: dict[str, object] = {
            "query": request.query,
            "documents": list(documents),
        }
        if self._config.protocol == "tei":
            payload.update(
                {"texts": payload.pop("documents"), "truncate": False}
            )
        else:
            payload.update(
                {
                    "model": self._config.model,
                    "return_documents": False,
                    "top_n": len(documents),
                }
            )
        try:
            response = self._http.request_json(
                "POST",
                self._config.path,
                payload=payload,
                headers=_headers(self._resolve_key()),
                provider_id=_PROVIDER_ID,
                operation="reranking",
                model=self._config.model,
                input_count=len(documents),
                estimated_tokens=estimated_tokens,
            )
        except ProviderHttpError as failure:
            raise provider_error(
                failure, stage="provider.openai_compatible.reranker"
            ) from None
        observed_tokens: int | None = None
        try:
            response_payload = _mapping(response.payload)
            _validate_optional_model(response_payload, self._config.model)
            observed_tokens = _optional_usage_tokens(response_payload)
            items = _rerank_items(response_payload, request)
        except (TypeError, ValueError):
            failed = self._http.complete_call(
                response.call,
                observed_tokens=observed_tokens,
                failure_reason_code="INVALID_RESPONSE_CONTRACT",
            )
            raise invalid_response_error(
                "INVALID_RESPONSE_CONTRACT",
                failed,
                stage="provider.openai_compatible.reranker",
            ) from None
        call = self._http.complete_call(
            response.call, observed_tokens=observed_tokens
        )
        return RerankResult(
            mode=RerankExecutionMode.PROVIDER,
            items=items,
            calls=(call,),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """健康读取不主动消耗兼容服务。"""
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            checked_network=False,
            reason_code="NOT_PROBED",
        )

    def close(self) -> None:
        """关闭底层连接池。"""
        self._http.close()


class OpenAICompatibleChatAdapter(AliyunChatAdapter):
    """复用同一事实闭合逻辑，但只发送标准 Chat Completions 字段。"""

    def __init__(
        self,
        config: OpenAICompatibleChatConfig,
        *,
        http_client: ProviderHttpClient,
        api_key_resolver: Callable[[], str],
    ) -> None:
        super().__init__(
            config,  # type: ignore[arg-type]
            http_client=http_client,
            api_key_resolver=api_key_resolver,
        )
        self._compatible_config = config
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.GENERATOR,
            name="openai-compatible-grounded-chat",
            version=f"1:{config.model}:{config.prompt_version}",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                permits_network=True,
                roles=("generation", "query.interpret", "query.rewrite"),
            ),
        )

    def _generation_stage(self) -> str:
        """把共享事实闭合失败归入兼容 Provider 阶段。"""
        return "provider.openai_compatible.generation"

    def complete(  # noqa: PLR0913
        self,
        messages: tuple[ChatMessage, ...],
        *,
        operation: Literal[
            "generation", "query.interpret", "query.rewrite"
        ] = "generation",
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
        json_schema: Mapping[str, object] | None = None,
        schema_revision: str | None = None,
    ) -> ChatCompletion:
        """执行一次标准同步 Chat Completions 请求。"""
        if not self._compatible_config.egress_allowed:
            raise PolicyDenied(
                "兼容 Chat 出网未启用。",
                stage="provider.openai_compatible.chat",
            )
        if operation not in {"generation", "query.interpret", "query.rewrite"}:
            raise ValueError("Chat 用途不在允许范围。")
        return self.request_payload(
            openai_compatible_chat_payload(
                messages,
                self._compatible_config,
                max_output_tokens=max_output_tokens,
                disable_thinking=operation == "query.interpret",
                json_schema=json_schema,
                schema_revision=schema_revision,
            ),
            operation=operation,
            input_count=len(messages),
            estimated_tokens=message_token_estimate(messages),
            timeout_seconds=timeout_seconds,
        )

    def complete_stream(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        on_delta: Callable[[str], None],
        cancellation: CancellationPort,
    ) -> ChatCompletion:
        """消费标准 SSE；usage 缺失保持未知。"""
        if not self._compatible_config.egress_allowed:
            raise PolicyDenied(
                "兼容 Chat 出网未启用。",
                stage="provider.openai_compatible.chat",
            )
        payload = openai_compatible_chat_payload(
            messages, self._compatible_config, stream=True
        )

        def consume(chunks: Iterator[bytes]) -> ChatContent:
            accumulator = _ChatStreamAccumulator(
                expected_model=self._compatible_config.model,
                on_delta=on_delta,
            )
            for data in _sse_data_events(chunks):
                if cancellation.is_cancelled():
                    raise QueryCancelled("PROVIDER_STREAM_CANCELLED")
                accumulator.consume(data)
            return accumulator.complete()

        try:
            response = self._http.request_stream(
                "POST",
                _CHAT_COMPLETIONS_PATH,
                payload=payload,
                headers=_headers(self._resolve_key()),
                provider_id=_PROVIDER_ID,
                operation="generation",
                model=self._compatible_config.model,
                input_count=len(messages),
                estimated_tokens=message_token_estimate(messages),
                consumer=consume,
                cancellation=cancellation,
            )
        except ProviderHttpError as failure:
            raise provider_error(
                failure, stage="provider.openai_compatible.generation"
            ) from None
        content = response.value
        call = self._http.complete_call(
            _call_usage(response.call, content.usage),
            observed_tokens=content.usage.total_tokens or None,
        )
        return ChatCompletion(**content.model_dump(), call=call)

    def request_payload(
        self,
        payload: Mapping[str, object],
        *,
        operation: Literal[
            "generation", "query.interpret", "query.rewrite", "image.ocr"
        ],
        input_count: int,
        estimated_tokens: int,
        timeout_seconds: float | None = None,
    ) -> ChatCompletion:
        """发送标准同步请求并校验可选 model、finish 和 usage。"""
        try:
            response = self._http.request_json(
                "POST",
                _CHAT_COMPLETIONS_PATH,
                payload=dict(payload),
                headers=_headers(self._resolve_key()),
                provider_id=_PROVIDER_ID,
                operation=operation,
                model=self._compatible_config.model,
                input_count=input_count,
                estimated_tokens=estimated_tokens,
                timeout_seconds=timeout_seconds,
            )
        except ProviderHttpError as failure:
            raise provider_error(
                failure, stage=f"provider.openai_compatible.{operation}"
            ) from None
        try:
            content = decode_chat_content(
                response.payload, expected_model=self._compatible_config.model
            )
        except ChatResponseError as error:
            call = self._http.complete_call(
                _call_usage(response.call, error.usage),
                observed_tokens=error.usage.total_tokens or None,
                failure_reason_code=error.reason_code,
            )
            raise invalid_response_error(
                error.reason_code,
                call,
                stage=f"provider.openai_compatible.{operation}",
            ) from None
        call = self._http.complete_call(
            _call_usage(response.call, content.usage),
            observed_tokens=content.usage.total_tokens or None,
        )
        return ChatCompletion(**content.model_dump(), call=call)

    def generate_stream(
        self,
        request: GenerationRequest,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: CancellationPort,
    ) -> AnswerDraft:
        """上游明确不支持 SSE 时退回一次同步最终结果，不伪造增量。"""
        emitted = False

        def observe(claim: AnswerClaim) -> None:
            nonlocal emitted
            emitted = True
            on_claim(claim)

        try:
            return super().generate_stream(
                request, on_claim=observe, cancellation=cancellation
            )
        except RagError as stream_error:
            reason = dict(stream_error.details).get("reason_code")
            if emitted or reason not in _STREAM_UNSUPPORTED:
                raise
            stream_calls = _error_calls(stream_error)
            try:
                draft = self.generate(request)
            except RagError as final_error:
                final_error.provider_calls = (
                    *stream_calls,
                    *_error_calls(final_error),
                )
                raise
            return draft.model_copy(
                update={
                    "provider_calls": (*stream_calls, *draft.provider_calls)
                }
            )


def openai_compatible_chat_payload(  # noqa: PLR0913
    messages: tuple[ChatMessage, ...],
    config: OpenAICompatibleChatConfig,
    *,
    max_output_tokens: int | None = None,
    stream: bool = False,
    disable_thinking: bool = False,
    json_schema: Mapping[str, object] | None = None,
    schema_revision: str | None = None,
) -> dict[str, object]:
    """按固定配置构造单次 Chat 请求，不在失败时轮询协议。"""
    limit = (
        config.max_output_tokens
        if max_output_tokens is None
        else max_output_tokens
    )
    if type(limit) is not int or not 0 < limit <= config.max_output_tokens:
        raise ValueError("输出上限必须位于已配置范围内。")
    if not messages or any(not item.content.strip() for item in messages):
        raise ValueError("Chat 消息不能为空。")
    if (
        len(messages) > config.max_messages
        or message_token_estimate(messages) > config.max_input_tokens
    ):
        raise ProviderInputTooLarge(
            "兼容 Chat 输入超过本地上限。",
            stage="provider.openai_compatible.chat",
        )
    payload: dict[str, object] = {
        "model": config.model,
        "messages": [message.model_dump() for message in messages],
        "temperature": 0,
        "max_tokens": limit,
        "stream": stream,
    }
    if disable_thinking and config.disable_thinking_supported:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if json_schema is not None:
        if not schema_revision:
            raise ValueError("结构化 Schema 必须具有明确 revision。")
        mode = config.structured_output_mode
        if mode == "response_format":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_revision,
                    "schema": dict(json_schema),
                    "strict": True,
                },
            }
        elif mode == "structured_outputs":
            payload["structured_outputs"] = {"json": dict(json_schema)}
        elif mode == "guided_json":
            payload["guided_json"] = dict(json_schema)
    return payload


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("Provider response 必须是 object。")
    return value


def _validate_optional_model(
    payload: Mapping[str, object], expected: str
) -> None:
    observed = payload.get("model")
    if observed is not None and observed != expected:
        raise ValueError("Provider response model 不匹配。")


def _optional_usage_tokens(payload: Mapping[str, object]) -> int | None:
    return None if payload.get("usage") is None else usage_tokens(payload)


def _rerank_items(
    payload: Mapping[str, object], request: RerankRequest
) -> tuple[RerankItem, ...]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Reranker results 必须是 list。")
    scores: dict[int, float] = {}
    for result in results:
        item = _mapping(result)
        index = item.get("index")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index in scores
            or not 0 <= index < len(request.candidates)
        ):
            raise ValueError("Reranker index 重复或越界。")
        key = (
            "score"
            if "score" in item
            else "relevance_score"
            if "relevance_score" in item
            else None
        )
        if key is None:
            raise ValueError("Reranker score 缺失。")
        scores[index] = finite_score(item[key])
    if set(scores) != set(range(len(request.candidates))):
        raise ValueError("Reranker 没有完整返回全部候选。")
    ranked = [
        RerankItem(
            candidate_id=request.candidates[index][0], score=scores[index]
        )
        for index in range(len(request.candidates))
    ]
    ranked.sort(key=lambda item: -item.score)
    return tuple(ranked[: request.limit])


def _error_calls(error: RagError) -> tuple[ProviderCall, ...]:
    if error.provider_calls:
        return error.provider_calls
    return () if error.provider_call is None else (error.provider_call,)


__all__ = [
    "OpenAICompatibleChatAdapter",
    "OpenAICompatibleChatConfig",
    "OpenAICompatibleEmbeddingAdapter",
    "OpenAICompatibleEmbeddingConfig",
    "OpenAICompatibleRerankerAdapter",
    "OpenAICompatibleRerankerConfig",
    "openai_compatible_chat_payload",
]
