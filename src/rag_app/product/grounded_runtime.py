"""将已有模型连接与知识库资料范围绑定到生成和一次改写。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from threading import RLock
from time import perf_counter
from typing import TypeVar

from pydantic import (
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from rag_app.adapters.providers.aliyun_chat import (
    AliyunChatAdapter,
    AliyunChatConfig,
    ChatMessage,
)
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.adapters.providers.budget_transport import (
    provider_budget_scope,
    provider_data_scope,
)
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.application.retrieval.adaptive import (
    AdaptivePlanOutcome,
    ReasoningEffort,
)
from rag_app.application.retrieval.context_resolution import (
    QueryInputSpan,
    build_input_spans,
    resolve_root_query,
)
from rag_app.application.retrieval.minimal_plan import (
    MinimalPlanPayload,
    MinimalPlanValidationError,
    build_query_atoms,
    planner_json_schema,
)
from rag_app.application.retrieval.rewrite_constraints import (
    interpretation_constraint_reason,
    rewrite_constraint_reason,
)
from rag_app.core.capabilities import ComponentCapabilities, ComponentDescriptor
from rag_app.core.errors import (
    PolicyDenied,
    ProviderQuotaExhausted,
    QueryCancelled,
    RagError,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    ProviderCall,
    ProviderHealth,
    QueryAnalysis,
    QuerySemantics,
    QueryVariant,
    RequestedAnswerType,
    SearchRequest,
)
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import QUERY_PLAN_SCHEMA_REVISION
from rag_app.core.ports import CancellationPort, GenerationRequest
from rag_app.core.ports.query_interpret import InterpretOutcome
from rag_app.core.ports.query_rewrite import RewriteOutcome
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.provider_runtime import ProviderRuntimeRegistry
from rag_app.product.structured_json import extract_json_object

_REWRITE_SIGNAL = re.compile(
    r"这个|那个|它|其中|上述|前者|后者|具体干什么|干啥|干什么|咋|怎么说|说白了|那怎么办"
)
_CONTEXT_REFERENCE = re.compile(
    r"这个|那个|它|其中|上述|前者|后者|刚才提到的|前面提到的"
)
_MAX_REWRITE_CHARS = 512
_MAX_INTERPRET_FIELD_CHARS = 512
_MAX_GROUNDED_INPUT_TOKENS = 6_144
# 内网演示模型的上下文窗口同时容纳输入与输出。回答采用结构化、逐条的
# claim，固定为输出保留 1536 token；输入预算限制在 6144 token，为 8K
# 上下文窗口留出固定余量。证据候选由 Provider adapter 按重排顺序裁剪，
# 不改变引用校验所能看到的完整有界证据包。
_MAX_GROUNDED_OUTPUT_TOKENS = 1536
_RotationResult = TypeVar("_RotationResult")
_LOW_CONFIDENCE_RULE_REASONS = frozenset(
    {
        "AMBIGUOUS_ACTION_QUESTION_SYNTAX",
        "SHARED_QUERY_SEMANTICS_V2",
    }
)
_INTERPRET_CANONICAL_RELATIONS = {
    RequestedAnswerType.DEFINITION: ("定义", ("定义", "释义", "含义")),
    RequestedAnswerType.PURPOSE: ("目的", ("目的", "目标", "作用", "用途")),
    RequestedAnswerType.DUTIES: ("职责", ("职责", "工作内容", "职能")),
    RequestedAnswerType.RESPONSIBLE_PARTY: (
        "责任角色",
        ("责任角色", "责任人", "负责人", "牵头人", "主责角色"),
    ),
    RequestedAnswerType.ENUMERATION: (
        "组成",
        ("组成", "主要阶段", "阶段", "分类", "类型", "种类", "项目"),
    ),
    RequestedAnswerType.COUNT: (
        "组成",
        ("组成", "主要阶段", "阶段", "分类", "类型", "种类", "项目"),
    ),
    RequestedAnswerType.ORDINAL_ITEM: (
        "组成",
        ("组成", "主要阶段", "阶段", "步骤", "流程", "项目"),
    ),
    RequestedAnswerType.PROCEDURE: (
        "流程",
        ("流程", "步骤", "程序", "顺序", "阶段"),
    ),
    RequestedAnswerType.SECTION_SUMMARY: (
        "章节内容",
        ("章节内容", "管理要求", "工作要求", "规定", "要求"),
    ),
    RequestedAnswerType.FACT: ("事实关系", ("事实关系",)),
}


class _InterpretPayload(FrozenModel):
    """Provider 返回的严格、完整问题解释 JSON。"""

    standalone_query: str = Field(min_length=1, max_length=_MAX_REWRITE_CHARS)
    target: str = Field(min_length=1, max_length=_MAX_INTERPRET_FIELD_CHARS)
    relation: str = Field(min_length=1, max_length=160)
    answer_type: RequestedAnswerType
    expected_count: StrictInt | None = Field(default=None, ge=1)
    ordinal: StrictInt | None = Field(default=None, ge=1)
    source_qualifier: str | None = Field(
        default=None, max_length=_MAX_INTERPRET_FIELD_CHARS
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> _InterpretPayload:
        if self.answer_type in {
            RequestedAnswerType.UNKNOWN,
        }:
            raise ValueError("解释结果不能保持 UNKNOWN。")
        if self.ordinal is not None and self.answer_type is not (
            RequestedAnswerType.ORDINAL_ITEM
        ):
            raise ValueError("只有序号问题可以返回 ordinal。")
        if self.answer_type is RequestedAnswerType.ORDINAL_ITEM and (
            self.ordinal is None
        ):
            raise ValueError("序号问题必须返回 ordinal。")
        if self.expected_count is not None and self.answer_type not in {
            RequestedAnswerType.ENUMERATION,
            RequestedAnswerType.COUNT,
        }:
            raise ValueError("只有列举或计数问题可以返回 expected_count。")
        return self


_AdaptivePlanPayload = MinimalPlanPayload


class ProductGroundedModel:
    """一个知识库的模型引用，每次发送都重新核对来源与持久授权。"""

    def __init__(
        self,
        settings: KnowledgeBaseModelSettings,
        knowledge_base_id: str,
        connections: SqliteConnectionFactory,
        providers: ProviderRuntimeRegistry,
    ) -> None:
        self.settings = settings
        self.knowledge_base_id = knowledge_base_id
        self.connections = connections
        self.providers = providers
        with connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM knowledge_bases "
                "WHERE knowledge_base_id = ? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            raise PolicyDenied("知识库不可用。", stage="generation.scope")
        self.project_id = str(row[0])
        if (
            not settings.generation_connection_id
            or not settings.generation_model
        ):
            raise ValueError("回答模型尚未配置。")
        self._campaign_required = providers.requires_campaign(
            settings.generation_connection_id
        )
        self.adapters = tuple(
            providers.chat_adapter(
                settings.generation_connection_id,
                model=model,
                config=self._chat_config(model),
            )
            for model in settings.generation_models
        )
        self.adapter = self.adapters[0]
        self._quota_exhausted_models: set[str] = set()
        self._rotation_lock = RLock()

    @property
    def descriptor(self) -> ComponentDescriptor:
        """保留真实 Provider 组件身份。

        Args:
            无参数；读取当前配置。

        Returns:
            真实模型适配器描述符。

        """
        return self.adapter.descriptor

    @property
    def capabilities(self) -> ComponentCapabilities:
        """保留真实生成能力。

        Args:
            无参数；读取当前适配器。

        Returns:
            当前适配器声明的能力。

        """
        return self.adapter.capabilities

    def health(self, *, network: bool = False) -> ProviderHealth:
        """健康读取默认不出网。

        Args:
            network: 是否明确允许实际探测。

        Returns:
            不含凭据的健康状态。

        """
        return self._rotation_candidates()[0].health(network=network)

    @contextmanager
    def _scope(
        self, operation: str, source_hashes: tuple[str, ...] = ()
    ) -> Iterator[None]:
        if not self._campaign_required:
            yield
            return
        campaign_id = self.settings.budget_campaign_id
        if campaign_id is None:
            raise PolicyDenied(
                "当前知识库尚未绑定资料出网授权。",
                stage="generation.authorization",
                code="DATA_EGRESS_NOT_AUTHORIZED",
            )
        ledger = ProviderBudgetLedger(
            self.connections.database_path.parent / "provider-budget.sqlite3"
        )
        campaign = ledger.campaign(campaign_id)
        with (
            provider_budget_scope(
                ledger,
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                scope=campaign.scope,
                step_id=operation,
            ),
            provider_data_scope(
                project_id=self.project_id,
                knowledge_base_id=self.knowledge_base_id,
                source_hashes=source_hashes,
            ),
        ):
            yield

    def _chat_config(
        self, model: str
    ) -> AliyunChatConfig | OpenAICompatibleChatConfig:
        """按连接协议创建模型配置，不用内置模型形状限制自定义 ID。"""
        if not self._campaign_required:
            return OpenAICompatibleChatConfig(
                model=model,
                egress_allowed=True,
                max_input_tokens=_MAX_GROUNDED_INPUT_TOKENS,
                max_output_tokens=_MAX_GROUNDED_OUTPUT_TOKENS,
                disable_thinking_supported=(
                    self.settings.disable_thinking_supported
                ),
                structured_output_mode=self.settings.structured_output_mode,
            )
        return AliyunChatConfig(
            model=model,
            egress_allowed=True,
            max_input_tokens=_MAX_GROUNDED_INPUT_TOKENS,
            max_output_tokens=_MAX_GROUNDED_OUTPUT_TOKENS,
            json_mode="json_object",
        )

    def _rotation_candidates(
        self,
    ) -> tuple[AliyunChatAdapter | OpenAICompatibleChatAdapter, ...]:
        """优先跳过本进程已经确认额度耗尽的模型。"""
        with self._rotation_lock:
            available = tuple(
                adapter
                for adapter in self.adapters
                if adapter.config.model not in self._quota_exhausted_models
            )
        # 全部曾失败时重新探测完整链，允许额度变化后自行恢复。
        return available or self.adapters

    def _call_with_rotation(
        self,
        action: Callable[
            [AliyunChatAdapter | OpenAICompatibleChatAdapter], _RotationResult
        ],
        *,
        can_rotate: Callable[[], bool] | None = None,
    ) -> tuple[_RotationResult, tuple[ProviderCall, ...]]:
        """只在单模型免费额度耗尽且尚可安全切换时尝试下一模型。"""
        failed_calls: list[ProviderCall] = []
        candidates = self._rotation_candidates()
        for index, adapter in enumerate(candidates):
            try:
                return action(adapter), tuple(failed_calls)
            except ProviderQuotaExhausted as error:
                current_calls = _error_provider_calls(error)
                with self._rotation_lock:
                    self._quota_exhausted_models.add(adapter.config.model)
                if index + 1 < len(candidates) and (
                    can_rotate is None or can_rotate()
                ):
                    failed_calls.extend(current_calls)
                    continue
                error.provider_calls = (*failed_calls, *current_calls)
                raise
            except RagError as error:
                error.provider_calls = (
                    *failed_calls,
                    *_error_provider_calls(error),
                )
                raise
            except QueryCancelled as error:
                error.provider_calls = (*failed_calls, *error.provider_calls)
                raise
        raise AssertionError("回答模型轮换链不能为空。")

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """只授权本次有限证据所对应的现存文档，不读取整份正文。

        Args:
            request: 原问题和有界证据包。

        Returns:
            尚待应用层验证的生成草稿。

        """
        hashes = self._source_hashes(request)
        with self._scope("generation", hashes):
            draft, failed_calls = self._call_with_rotation(
                lambda adapter: adapter.generate(request),
                can_rotate=lambda: request.query_plan is None,
            )
        return draft.model_copy(
            update={
                "provider_calls": (*failed_calls, *draft.provider_calls),
            }
        )

    def generate_stream(
        self,
        request: GenerationRequest,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: CancellationPort,
    ) -> AnswerDraft:
        """在同一来源授权内消费 Provider SSE 并转发完整 claim。

        Args:
            request: 与同步生成完全相同的有限证据请求。
            on_claim: 接收已通过 adapter 来源形状检查的 claim。
            cancellation: 断连时关闭当前上游响应的令牌。

        Returns:
            完整 AnswerDraft，仍须由 Application 执行业务事实校验。

        """
        hashes = self._source_hashes(request)
        emitted_count = 0

        def _on_claim(claim: AnswerClaim) -> None:
            nonlocal emitted_count
            emitted_count += 1
            on_claim(claim)

        with self._scope("generation", hashes):
            draft, failed_calls = self._call_with_rotation(
                lambda adapter: adapter.generate_stream(
                    request,
                    on_claim=_on_claim,
                    cancellation=cancellation,
                ),
                can_rotate=lambda: (
                    request.query_plan is None and emitted_count == 0
                ),
            )
        return draft.model_copy(
            update={
                "provider_calls": (*failed_calls, *draft.provider_calls),
            }
        )

    def _source_hashes(self, request: GenerationRequest) -> tuple[str, ...]:
        """重新核对本次证据仍属于当前活动知识库版本。"""
        hashes: set[str] = set()
        with self.connections.transaction() as connection:
            for item in request.evidence:
                row = connection.execute(
                    "SELECT v.content_sha256 FROM document_versions v "
                    "JOIN documents d ON d.document_id=v.document_id "
                    "WHERE v.document_version_id=? AND d.document_id=? "
                    "AND d.project_id=? AND d.knowledge_base_id=? "
                    "AND d.deleted_at IS NULL AND d.status='active'",
                    (
                        item.document_version_id,
                        item.document_id,
                        self.project_id,
                        self.knowledge_base_id,
                    ),
                ).fetchone()
                if row is None:
                    raise PolicyDenied(
                        "生成来源已不可用。",
                        stage="generation.scope",
                        code="GENERATION_SOURCE_UNAVAILABLE",
                    )
                hashes.add(str(row[0]))
        return tuple(sorted(hashes))

    def plan_adaptive(
        self,
        request: SearchRequest,
        analysis: QueryAnalysis,
        effort: ReasoningEffort,
    ) -> AdaptivePlanOutcome:
        """在同一模型连接上至多发一次最小结构化规划请求。"""
        if effort is ReasoningEffort.DIRECT:
            return AdaptivePlanOutcome()
        if len(request.text) > _MAX_REWRITE_CHARS:
            return AdaptivePlanOutcome(reason_code="ADAPTIVE_PLAN_INPUT_LIMIT")
        spans = build_input_spans(request)
        root = resolve_root_query(request, spans)
        if root.mode == "CLARIFY":
            return AdaptivePlanOutcome(
                standalone_query=root.resolved_query,
                intent="CLARIFICATION",
                needs_clarification=True,
                clarification_question="请明确您所指的对象和要查询的事项。",
                reason_code="PLANNER_CONTEXT_UNRESOLVED",
                failure_category="PLANNER_CONTEXT_UNRESOLVED",
            )
        try:
            schema = planner_json_schema(spans)
        except MinimalPlanValidationError as error:
            return AdaptivePlanOutcome(
                reason_code=error.code,
                failure_category=error.code,
            )
        mode = (
            self.adapter.config.structured_output_mode
            if isinstance(self.adapter, OpenAICompatibleChatAdapter)
            else "none"
        )
        outcome = self._plan_adaptive_once(
            request,
            analysis,
            spans=spans,
            root_query=root.resolved_query,
            schema=schema,
            mode=mode,
        )
        return replace(
            outcome,
            structured_output_mode=mode,
            schema_revision=QUERY_PLAN_SCHEMA_REVISION,
            schema_sha256=canonical_sha256(schema),
        )

    def _plan_adaptive_once(  # noqa: PLR0913
        self,
        request: SearchRequest,
        analysis: QueryAnalysis,
        *,
        spans: tuple[QueryInputSpan, ...],
        root_query: str,
        schema: dict[str, object],
        mode: str,
    ) -> AdaptivePlanOutcome:
        """只解释原问；服务端重建所有硬约束与来源范围。"""
        messages = (
            ChatMessage(
                role="system",
                content=(
                    "只选择给定 Span ID，将用户问题拆成至多四个可检索事实原子。"
                    "不得回答问题、创造 Span、补充条件或引用文档。"
                    "每个独立问句的 Clause ID 至少出现在一个原子 f 中；"
                    "修饰 Clause 可与问句共用，多个原子也可共用 f。"
                    "r 必须引用当前问句的 Relation ID。"
                    "受信上下文已由服务端消歧；输出键 i=意图、a=原子。"
                    "原子键 f=Clause ID 列表、t=Target ID、"
                    "r=当前 Relation ID、s=回答形状。"
                    "只输出符合 JSON Schema 的对象。"
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "current_question": request.text,
                        "resolved_root": root_query,
                        "spans": [
                            {
                                "id": span.span_id,
                                "kind": span.kind.value,
                                "turn": span.turn,
                                "text": span.text,
                            }
                            for span in spans
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        calls: tuple[ProviderCall, ...] = ()
        started = perf_counter()
        timeout = self.settings.planner_transport_timeout_seconds
        token_limit = self.settings.planner_max_output_tokens
        try:
            schema_args = (
                {
                    "json_schema": schema,
                    "schema_revision": QUERY_PLAN_SCHEMA_REVISION,
                }
                if isinstance(self.adapter, OpenAICompatibleChatAdapter)
                else {}
            )
            with self._scope("query.interpret"):
                completion = self.adapter.complete(
                    messages,
                    operation="query.interpret",
                    max_output_tokens=token_limit,
                    timeout_seconds=timeout,
                    **schema_args,
                )
            calls = (completion.call,)
            usage = getattr(completion, "usage", None)
            telemetry = {
                "planner_latency_ms": round((perf_counter() - started) * 1000),
                "planner_input_tokens": getattr(usage, "prompt_tokens", None),
                "planner_output_tokens": getattr(
                    usage, "completion_tokens", None
                ),
                "planner_finish_reason": getattr(
                    completion, "finish_reason", None
                ),
                "planner_transport_timeout_ms": round(timeout * 1000),
            }
            try:
                payload_data = (
                    json.loads(completion.content)
                    if mode != "none"
                    else extract_json_object(completion.content)
                )
            except (json.JSONDecodeError, ValueError):
                return AdaptivePlanOutcome(
                    calls=calls,
                    reason_code="PLANNER_INVALID_JSON",
                    attempted=True,
                    schema_fallback_detail="INVALID_JSON",
                    failure_category="PLANNER_INVALID_JSON",
                    **telemetry,
                )
            try:
                payload = _AdaptivePlanPayload.model_validate(payload_data)
            except ValidationError as error:
                first = error.errors(include_input=False)[0]
                location = ".".join(str(part) for part in first["loc"])
                shape = ""
                if isinstance(payload_data, dict):
                    raw_atoms = payload_data.get(
                        "a", payload_data.get("atoms")
                    )
                    raw_intent = payload_data.get(
                        "i", payload_data.get("intent")
                    )
                    intent_kind = (
                        raw_intent
                        if raw_intent
                        in {"SINGLE", "COMPOUND", "FOLLOW_UP", "CLARIFICATION"}
                        else "OTHER"
                    )
                    atom_count = (
                        str(len(raw_atoms))
                        if isinstance(raw_atoms, list)
                        else "INVALID"
                    )
                    reason_kind = (
                        "SET"
                        if payload_data.get(
                            "c", payload_data.get("clarification_reason")
                        )
                        is not None
                        else "NONE"
                    )
                    shape = (
                        f":intent={intent_kind}:atoms={atom_count}"
                        f":reason={reason_kind}"
                    )
                return AdaptivePlanOutcome(
                    calls=calls,
                    reason_code="PLANNER_INVALID_SCHEMA",
                    attempted=True,
                    schema_fallback_detail=(
                        f"INVALID_SCHEMA:{location}:{first['type']}{shape}"
                    ),
                    failure_category="PLANNER_INVALID_SCHEMA",
                    **telemetry,
                )
            try:
                atoms = build_query_atoms(payload, spans, analysis)
            except MinimalPlanValidationError as error:
                return AdaptivePlanOutcome(
                    calls=calls,
                    reason_code=error.code,
                    attempted=True,
                    schema_fallback_detail=error.code,
                    failure_category=error.code,
                    **telemetry,
                )
            return AdaptivePlanOutcome(
                standalone_query=root_query,
                intent=payload.intent,
                needs_clarification=False,
                clarification_question=None,
                atoms=atoms,
                route_hints=(),
                calls=calls,
                reason_code="ADAPTIVE_PLAN_APPLIED",
                attempted=True,
                **telemetry,
            )
        except RagError as error:
            provider_reason = str(
                dict(error.details).get("reason_code", error.code)
            )
            category = (
                "PLANNER_PROVIDER_TIMEOUT"
                if "TIMEOUT" in provider_reason
                else "PLANNER_OUTPUT_TRUNCATED"
                if provider_reason == "CHAT_OUTPUT_TRUNCATED"
                else "PLANNER_PROVIDER_UNAVAILABLE"
            )
            return AdaptivePlanOutcome(
                calls=_error_provider_calls(error),
                reason_code=category,
                attempted=True,
                schema_fallback_detail=provider_reason,
                failure_category=category,
                planner_latency_ms=round((perf_counter() - started) * 1000),
                planner_transport_timeout_ms=round(timeout * 1000),
            )
        except (ValueError, TypeError):
            return AdaptivePlanOutcome(
                calls=calls,
                reason_code="PLANNER_INTERNAL_VALIDATION",
                attempted=True,
                schema_fallback_detail="ATOM_CONSTRUCTION_INVALID",
                failure_category="PLANNER_INTERNAL_VALIDATION",
                planner_latency_ms=round((perf_counter() - started) * 1000),
                planner_transport_timeout_ms=round(timeout * 1000),
            )

    def interpret(  # noqa: PLR0911
        self, request: SearchRequest, analysis: QueryAnalysis
    ) -> InterpretOutcome:
        """规则低置信时至多调用一次严格结构化问题解释。

        Args:
            request: 原问题、鉴权范围与有限会话上下文。
            analysis: 原问题的权威确定性分析。

        Returns:
            通过原问题硬约束校验的共享语义和实际调用审计。

        """
        fallback_semantics = (
            analysis.semantics.source == "ORIGINAL_FALLBACK"
            and analysis.semantics.answer_type
            in {RequestedAnswerType.UNKNOWN, RequestedAnswerType.FACT}
        )
        low_confidence_rule = bool(
            _LOW_CONFIDENCE_RULE_REASONS & set(analysis.semantics.reason_codes)
        )
        if not self.settings.rewrite_enabled or not (
            fallback_semantics or low_confidence_rule
        ):
            return InterpretOutcome()
        if len(request.text) > _MAX_REWRITE_CHARS:
            return InterpretOutcome(
                reason_code="INTERPRET_INPUT_LIMIT", attempted=True
            )
        messages = (
            ChatMessage(
                role="system",
                content=(
                    "你只解释资料检索问题的意图，不能回答问题。"
                    "只输出一个 JSON 对象，必须包含 standalone_query、target、"
                    "relation、answer_type，不得添加字段。expected_count、ordinal、"
                    "source_qualifier 没有值时可以省略或设为 null。"
                    "answer_type 只能是 FACT、DEFINITION、PURPOSE、DUTIES、"
                    "RESPONSIBLE_PARTY、ENUMERATION、COUNT、ORDINAL_ITEM、"
                    "PROCEDURE 或 SECTION_SUMMARY。原对象、编号、日期、数字、"
                    "单位、"
                    "否定、数量、序号、来源范围和引号原词必须保留；不能增加实体、"
                    "改变范围或反转语义。上下文只用于解析指代，不能作为答案证据。"
                    "无法可靠解释时保持 standalone_query 原样，并使用 FACT。"
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "question": request.text,
                        "context": [
                            value[:300]
                            for value in request.conversation_context[-2:]
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        calls: tuple[ProviderCall, ...] = ()
        try:
            with self._scope("query.interpret"):
                result, failed_calls = self._call_with_rotation(
                    lambda adapter: adapter.complete(
                        messages,
                        operation="query.interpret",
                        max_output_tokens=384,
                    )
                )
            calls = (*failed_calls, result.call)
            payload = _InterpretPayload.model_validate(
                json.loads(result.content)
            )
            relation_spec = _INTERPRET_CANONICAL_RELATIONS.get(
                payload.answer_type
            )
            permitted_topic_terms = (
                payload.target,
                payload.source_qualifier or "",
                *(relation_spec[1] if relation_spec is not None else ()),
            )
            reason = interpretation_constraint_reason(
                request,
                payload.standalone_query,
                permitted_topic_terms=permitted_topic_terms,
            ) or _interpretation_semantics_reason(request, analysis, payload)
            if reason is not None:
                return InterpretOutcome(
                    calls=calls,
                    reason_code=reason,
                    attempted=True,
                )
            semantics = _interpreted_semantics(payload)
            return InterpretOutcome(
                standalone_query=payload.standalone_query,
                semantics=semantics,
                calls=calls,
                reason_code="INTERPRET_APPLIED",
                attempted=True,
            )
        except RagError as error:
            calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            return InterpretOutcome(
                calls=calls, reason_code=error.code, attempted=True
            )
        except json.JSONDecodeError:
            return InterpretOutcome(
                calls=calls,
                reason_code="INTERPRET_JSON_INVALID",
                attempted=True,
            )
        except ValidationError:
            return InterpretOutcome(
                calls=calls,
                reason_code="INTERPRET_SCHEMA_INVALID",
                attempted=True,
            )
        except (TypeError, ValueError, KeyError):
            return InterpretOutcome(
                calls=calls,
                reason_code="INTERPRET_SCHEMA_INVALID",
                attempted=True,
            )

    def rewrite(  # noqa: PLR0911
        self, request: SearchRequest, *, recall_insufficient: bool = False
    ) -> RewriteOutcome:
        """仅口语/指代或首轮不足时调用一次，非法变更直接丢弃。

        Args:
            request: 原问题和已鉴权会话范围。
            recall_insufficient: 是否由首轮召回不足触发。

        Returns:
            可选补充检索变体和实际调用审计。

        """
        if not self.settings.rewrite_enabled or (
            not recall_insufficient and not _REWRITE_SIGNAL.search(request.text)
        ):
            return RewriteOutcome()
        if len(request.text) > _MAX_REWRITE_CHARS:
            return RewriteOutcome(
                reason_code="REWRITE_INPUT_LIMIT", attempted=True
            )
        messages = (
            ChatMessage(
                role="system",
                content=(
                    '将用户问题改写为适合资料检索的一个独立问题。只输出JSON对象{"query":"问题"}。'
                    "原对象、编号、日期、数字、否定和范围必须保留，不添加事实、不回答问题。"
                    "保留原问题的业务词项和限制词，仅调整问句语法或词序；不要用别的对象或业务同义词替换。"
                    "上下文仅用于消歧，不能当事实证据。不能确定时原样返回。"
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "question": request.text,
                        "context": [
                            value[:300]
                            for value in request.conversation_context[-2:]
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        calls: tuple[ProviderCall, ...] = ()
        try:
            with self._scope("query.rewrite"):
                result, failed_calls = self._call_with_rotation(
                    lambda adapter: adapter.complete(
                        messages,
                        operation="query.rewrite",
                        max_output_tokens=256,
                    )
                )
            calls = (*failed_calls, result.call)
            payload = json.loads(result.content)
            text = payload.get("query") if isinstance(payload, dict) else None
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= _MAX_REWRITE_CHARS
            ):
                return RewriteOutcome(
                    calls=calls,
                    reason_code="REWRITE_INVALID",
                    attempted=True,
                )
            constraint_reason = rewrite_constraint_reason(request, text)
            if constraint_reason is not None:
                return RewriteOutcome(
                    calls=calls,
                    reason_code=constraint_reason,
                    attempted=True,
                )
            variant = (
                None
                if text == request.text
                else QueryVariant(
                    text=text,
                    kind="rewrite",
                    identity=canonical_sha256(
                        {"query": text, "policy": "bounded-rewrite-v3"}
                    ),
                )
            )
            return RewriteOutcome(
                variant,
                calls,
                "REWRITE_APPLIED" if variant else "REWRITE_UNCHANGED",
                True,
            )
        except RagError as error:
            calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            return RewriteOutcome(
                calls=calls, reason_code=error.code, attempted=True
            )
        except (ValueError, KeyError):
            return RewriteOutcome(
                calls=calls,
                reason_code="REWRITE_INVALID",
                attempted=True,
            )

    def close(self) -> None:
        """释放当前 adapter 的 HTTP 连接池。

        Args:
            无参数；关闭当前适配器。

        Returns:
            关闭成功时无返回值。

        """
        for adapter in self.adapters:
            adapter.close()


def _error_provider_calls(error: RagError) -> tuple[ProviderCall, ...]:
    """提取当前异常实际携带的脱敏调用，避免轮换后丢失审计。"""
    if error.provider_calls:
        return error.provider_calls
    return () if error.provider_call is None else (error.provider_call,)


def _interpretation_semantics_reason(  # noqa: PLR0911
    request: SearchRequest,
    analysis: QueryAnalysis,
    payload: _InterpretPayload,
) -> str | None:
    """验证模型字段来自原问题/上下文且不覆盖硬数量语义。"""
    if (
        payload.expected_count != analysis.semantics.expected_count
        or payload.ordinal != analysis.semantics.ordinal
    ):
        return "INTERPRET_CONSTRAINT_CHANGED"
    available = "\n".join(
        (
            request.text,
            *(
                request.conversation_context[-2:]
                if _CONTEXT_REFERENCE.search(request.text)
                else ()
            ),
        )
    )
    if not _surface_contains(
        available, payload.target
    ) or not _surface_contains(payload.standalone_query, payload.target):
        return "INTERPRET_SCOPE_CHANGED"
    original_source = analysis.semantics.source_qualifier
    if payload.source_qualifier != original_source:
        return "INTERPRET_SCOPE_CHANGED"
    if payload.source_qualifier is not None and not _surface_contains(
        available, payload.source_qualifier
    ):
        return "INTERPRET_SCOPE_CHANGED"
    if payload.source_qualifier is not None and not _surface_contains(
        payload.standalone_query, payload.source_qualifier
    ):
        return "INTERPRET_SCOPE_CHANGED"
    relation_spec = _INTERPRET_CANONICAL_RELATIONS.get(payload.answer_type)
    if (
        relation_spec is None
        or (
            payload.answer_type is RequestedAnswerType.FACT
            and payload.relation != "事实关系"
            and not _surface_contains(available, payload.relation)
        )
        or (
            payload.answer_type is not RequestedAnswerType.FACT
            and payload.relation not in relation_spec[1]
            and not _surface_contains(available, payload.relation)
        )
    ):
        return "INTERPRET_RELATION_INVALID"
    return None


def _interpreted_semantics(payload: _InterpretPayload) -> QuerySemantics:
    """把严格响应映射为内部 canonical relation。"""
    canonical_relation = (
        payload.relation
        if payload.answer_type is RequestedAnswerType.FACT
        else _INTERPRET_CANONICAL_RELATIONS[payload.answer_type][0]
    )
    return QuerySemantics(
        target=payload.target.strip(),
        source_qualifier=(
            None
            if payload.source_qualifier is None
            else payload.source_qualifier.strip()
        ),
        relation=canonical_relation,
        answer_type=payload.answer_type,
        expected_count=payload.expected_count,
        ordinal=payload.ordinal,
        source="LLM_INTERPRET",
        reason_codes=("STRUCTURED_QUERY_INTERPRET",),
    )


def _surface_contains(container: str, value: str) -> bool:
    """只忽略 Unicode 与空白差异，不做业务同义词扩展。"""
    normalized_container = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", container).casefold()
    )
    normalized_value = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", value).casefold()
    )
    return bool(normalized_value and normalized_value in normalized_container)
