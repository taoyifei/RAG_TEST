"""OpenAI-compatible Embedding、Reranker 与 Chat adapters。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Literal
from uuid import uuid4

from pydantic import Field, StrictInt, model_validator

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
from rag_app.adapters.providers.generation_packet import (
    complete_generation_transport,
    observe_generation_transport,
)
from rag_app.adapters.providers.grounded_wire import GroundedWirePayload
from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
    invalid_response_error,
    provider_error,
)
from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
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
from rag_app.core.identifiers import canonical_json, canonical_sha256
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
from rag_app.core.models.generation_packet import PreparedGenerationPacket
from rag_app.core.models.query_plan import GROUNDED_CLAIM_SCHEMA_REVISION
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
    prompt_version: str = Field(default="grounded-chat-v9", max_length=64)
    disable_thinking_supported: bool = False
    disable_thinking: bool = False
    structured_output_mode: Literal[
        "none", "response_format", "structured_outputs", "guided_json"
    ] = "none"
    structured_output_profile: StructuredOutputCapabilityProfile | None = None

    @model_validator(mode="after")
    def _validate_thinking_strategy(self) -> OpenAICompatibleChatConfig:
        if self.disable_thinking and not self.disable_thinking_supported:
            raise ValueError("关闭 thinking 前必须确认 Provider 支持该参数。")
        profile = self.structured_output_profile
        if profile is not None and (
            self.structured_output_mode == "none"
            or profile.mode != self.structured_output_mode
            or profile.model != self.model
        ):
            raise ValueError("结构化输出能力合同与模型或固定模式不一致。")
        return self


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

    @property
    def compatible_config(self) -> OpenAICompatibleChatConfig:
        """返回兼容协议专属配置，避免沿基类类型读取不存在的能力字段。

        Args:
            无参数；读取当前适配器的实际配置。

        Returns:
            实际初始化本 adapter 的不可变兼容 Provider 配置。

        """
        return self._compatible_config

    def _generation_stage(self) -> str:
        """把共享事实闭合失败归入兼容 Provider 阶段。"""
        return "provider.openai_compatible.generation"

    def _complete_natural(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
    ) -> ChatCompletion:
        """按已探测的唯一 Schema 协议执行自然 Claim 生成。"""
        return self.complete(
            messages,
            max_output_tokens=max_output_tokens,
            json_schema=GroundedWirePayload.model_json_schema(),
            schema_revision=GROUNDED_CLAIM_SCHEMA_REVISION,
        )

    def _natural_schema_tokens(self) -> int:
        """计入消息外发送的输出 Schema 输入开销。"""
        if self._compatible_config.structured_output_mode == "none":
            return 0
        return _schema_payload_tokens(
            _structured_schema_fields(
                GroundedWirePayload.model_json_schema(),
                mode=self._compatible_config.structured_output_mode,
                revision=GROUNDED_CLAIM_SCHEMA_REVISION,
            )
        )

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
        request_label: str | None = None,
    ) -> ChatCompletion:
        """执行一次标准同步 Chat Completions 请求。"""
        if not self._compatible_config.egress_allowed:
            raise PolicyDenied(
                "兼容 Chat 出网未启用。",
                stage="provider.openai_compatible.chat",
            )
        if operation not in {"generation", "query.interpret", "query.rewrite"}:
            raise ValueError("Chat 用途不在允许范围。")
        payload = openai_compatible_chat_payload(
            messages,
            self._compatible_config,
            max_output_tokens=max_output_tokens,
            disable_thinking=self._compatible_config.disable_thinking,
            json_schema=json_schema,
            schema_revision=schema_revision,
        )
        profile = self._compatible_config.structured_output_profile
        if (
            json_schema is not None
            and schema_revision is not None
            and profile is not None
        ):
            profile.assert_schema(schema_revision, json_schema)
        estimated_tokens = message_token_estimate(messages) + (
            _schema_payload_tokens(payload)
        )
        output_budget = (
            self._compatible_config.max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        request_diagnostics: dict[str, object] = {
            "purpose": operation,
            "output_budget": output_budget,
            "preflight_estimated_tokens": estimated_tokens,
        }
        if json_schema is not None and schema_revision is not None:
            request_diagnostics.update(
                {
                    "schema_family": schema_revision.split("-v", 1)[0],
                    "schema_revision": schema_revision,
                    "schema_sha256": canonical_sha256(json_schema),
                }
            )
        if profile is not None:
            request_diagnostics.update(
                {
                    "grammar_backend_fingerprint": (
                        profile.grammar_backend_fingerprint
                    ),
                    "capability_profile_sha256": profile.profile_sha256,
                }
            )
        if request_label is not None:
            request_diagnostics["request_label"] = request_label
        return self.request_payload(
            payload,
            operation=operation,
            input_count=len(messages),
            estimated_tokens=estimated_tokens,
            timeout_seconds=timeout_seconds,
            request_diagnostics=request_diagnostics,
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
            messages,
            self._compatible_config,
            stream=True,
            disable_thinking=self._compatible_config.disable_thinking,
        )
        observe_generation_transport(payload)

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
            if failure.reason_code in _STREAM_UNSUPPORTED:
                # HTTP/SSE协议明确拒绝证明本次消息已到达服务端。
                complete_generation_transport(None)
            raise provider_error(
                failure, stage="provider.openai_compatible.generation"
            ) from None
        content = response.value
        complete_generation_transport(content.usage.prompt_tokens)
        call = self._http.complete_call(
            _call_usage(response.call, content.usage),
            observed_tokens=content.usage.total_tokens or None,
        )
        return ChatCompletion(**content.model_dump(), call=call)

    def request_payload(  # noqa: PLR0913
        self,
        payload: Mapping[str, object],
        *,
        operation: Literal[
            "generation", "query.interpret", "query.rewrite", "image.ocr"
        ],
        input_count: int,
        estimated_tokens: int,
        timeout_seconds: float | None = None,
        request_diagnostics: Mapping[str, object] | None = None,
    ) -> ChatCompletion:
        """发送标准同步请求并校验可选 model、finish 和 usage。"""
        observe_generation_transport(payload)
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
                request_diagnostics=request_diagnostics,
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
        complete_generation_transport(content.usage.prompt_tokens)
        return ChatCompletion(**content.model_dump(), call=call)

    def generate_stream(
        self,
        request: GenerationRequest,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: CancellationPort,
    ) -> AnswerDraft:
        """上游明确不支持 SSE 时退回一次同步最终结果，不伪造增量。"""
        if request.query_plan is not None:
            return super().generate_stream(
                request, on_claim=on_claim, cancellation=cancellation
            )
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
            stream_packet = getattr(
                stream_error, "_prepared_generation_packet", None
            )
            previous_packets = (
                (stream_packet,)
                if isinstance(stream_packet, PreparedGenerationPacket)
                else ()
            )
            retry_request = request.model_copy(
                update={"attempt_id": uuid4().hex}
            )
            try:
                draft = self.generate(retry_request)
            except RagError as final_error:
                final_error.provider_calls = (
                    *stream_calls,
                    *_error_calls(final_error),
                )
                vars(final_error)["_previous_prepared_generation_packets"] = (
                    previous_packets
                )
                raise
            return draft.model_copy(
                update={
                    "provider_calls": (*stream_calls, *draft.provider_calls),
                    "previous_prepared_packets": (
                        *previous_packets,
                        *draft.previous_prepared_packets,
                    ),
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
        payload.update(
            _structured_schema_fields(
                json_schema,
                mode=config.structured_output_mode,
                revision=schema_revision,
            )
        )
    if (
        message_token_estimate(messages) + _schema_payload_tokens(payload)
        > config.max_input_tokens
    ):
        raise ProviderInputTooLarge(
            "完整消息与输出 Schema 超过生成输入预算。",
            stage="provider.openai_compatible.chat",
            code="GENERATION_INPUT_BUDGET_EXCEEDED",
        )
    return payload


def _schema_payload_tokens(payload: Mapping[str, object]) -> int:
    """按实际唯一协议计算消息之外 Schema 的估计开销。"""
    fields = {
        key: payload[key]
        for key in ("response_format", "structured_outputs", "guided_json")
        if key in payload
    }
    return estimate_tokens(canonical_json(fields)) if fields else 0


def _structured_schema_fields(
    schema: Mapping[str, object], *, mode: str, revision: str
) -> dict[str, object]:
    """预算估算与实际 HTTP 使用同一个 Schema 序列化合同。"""
    if mode == "response_format":
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": revision,
                    "schema": dict(schema),
                    "strict": True,
                },
            }
        }
    if mode == "structured_outputs":
        return {"structured_outputs": {"json": dict(schema)}}
    if mode == "guided_json":
        return {"guided_json": dict(schema)}
    return {}


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
