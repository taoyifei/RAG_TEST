"""把冻结配置组装为可运行的查询 API。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI
from qdrant_client import QdrantClient

from rag_app._build_revision import SOURCE_REVISION
from rag_app.api import ApiServices, create_app
from rag_app.chunking import HuggingFaceTokenCounter
from rag_app.clients.intent_classifier import IntentClassifier
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    RerankerClient,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import PipelineSpec
from rag_app.corpus_policy import CorpusPolicy
from rag_app.generation.answer import AnswerConfig, AnswerGenerator
from rag_app.generation.evidence import EvidenceAssembler, EvidenceConfig
from rag_app.generation.semantic_router import (
    LLM_CLASSIFIER_CONTRACT_REVISION,
    QUESTION_PROFILE_SCHEMA_REVISION,
    IntentRouterConfig,
    PrototypeWarmup,
    QuestionProfileCalibration,
    SemanticQuestionRouter,
    load_intent_router_config,
    load_question_profile_calibration,
)
from rag_app.health import (
    FrozenConfigurationProbe,
    HttpEndpointProbe,
    ManifestAliasProbe,
    QdrantServiceProbe,
    ReadinessService,
)
from rag_app.index import QdrantIndex
from rag_app.manifest import ManifestRepository
from rag_app.model_contracts import actual_prompt_revision
from rag_app.observability import StructuredAuditLogger
from rag_app.parsers.docx import DocxParser
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import QueryDependencies, QueryService
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.hybrid import (
    HybridRetrievalConfig,
    HybridRetrievalServices,
    HybridRetriever,
)
from rag_app.retrieval.neighbors import NeighborExpander
from rag_app.retrieval.rerank import RerankConfig, RerankStage
from rag_app.retrieval.rewrite import QueryRewriteConfig, QueryRewriter
from rag_app.retrieval.routing import KeywordRouteRule, KeywordSoftRouter
from rag_app.settings import RetrievalSettings, RunMode, RuntimeSettings
from rag_app.state.answer_cache import AnswerCache
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.intent_router_cache import (
    IntentRouterCache,
    PrototypeNamespace,
)
from rag_app.state.jobs import JobStore
from rag_app.strict_json import load_json_file
from rag_app.tracing.models import TraceIdentity, TraceMode
from rag_app.tracing.recorder import TraceRecorder
from rag_app.tracing.store import TraceStore

__all__ = [
    "RuntimeBundle",
    "build_runtime",
    "load_pipeline",
    "log_run_mode_startup",
    "require_release_revision",
]

_PIPELINE_CONFIG_FIELDS = frozenset(PipelineSpec.model_fields)
_PAYLOAD_SCHEMA_VERSION = 2
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(slots=True)
class RuntimeBundle:
    """应用及需要保持存活的网络客户端。"""

    app: FastAPI
    settings: RuntimeSettings
    qdrant: QdrantClient
    http_clients: tuple[httpx.Client, ...]
    readiness: ReadinessService
    query_executor: QueryExecutor
    trace_recorder: TraceRecorder
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """关闭本进程拥有的外部连接。

        Args:
            无参数；关闭当前运行时持有的资源。

        Returns:
            无返回值。

        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        with ExitStack() as close_stack:
            close_stack.callback(self.qdrant.close)
            for client in reversed(self.http_clients):
                close_stack.callback(client.close)
            close_stack.callback(self.readiness.close)
            close_stack.callback(self.trace_recorder.close)
            close_stack.callback(self.query_executor.close)


@dataclass(frozen=True, slots=True)
class _QueryParts:
    """查询编排所需的已构造对象。"""

    index: QdrantIndex
    embedding: TeiEmbeddingClient
    reranker: RerankerClient
    llm: BufferedLlmClient
    token_counter: HuggingFaceTokenCounter
    conversations: ConversationStore


def load_pipeline(path: Path) -> PipelineSpec:
    """从 UTF-8 JSON 加载并校验 pipeline。

    Args:
        path: pipeline 配置文件。

    Returns:
        不可变 PipelineSpec。

    Raises:
        ValueError: JSON 不是对象或缺少显式 pipeline 字段。

    """
    payload = load_json_file(path, label="pipeline")
    if not isinstance(payload, dict):
        raise ValueError("pipeline 配置必须是 JSON object。")
    missing = sorted(_PIPELINE_CONFIG_FIELDS - set(payload))
    if missing:
        raise ValueError(f"pipeline 配置缺少显式字段：{','.join(missing)}")
    return PipelineSpec.model_validate(payload)


def require_release_revision(settings: RuntimeSettings) -> None:
    """在创建任何外部资源前绑定安装 wheel 与 release 身份。

    Args:
        settings: 含正式 release revision 的已验证运行设置。

    Returns:
        安装 wheel 与 release revision 完全一致时返回。

    Raises:
        ValueError: 安装 wheel revision 缺失、为开发占位或与 release 不同。

    """
    if _FULL_GIT_SHA.fullmatch(SOURCE_REVISION) is None:
        raise ValueError("安装 wheel SOURCE_REVISION 缺失或不是正式 Git SHA。")
    if settings.release_revision != SOURCE_REVISION:
        raise ValueError(
            "安装 wheel SOURCE_REVISION 与 release revision 不一致。"
        )


def log_run_mode_startup(run_mode: RunMode, *, component: str) -> None:
    """为显式 demo 进程输出稳定启动标记。

    Args:
        run_mode: 当前运行模式。
        component: 启动中的容器角色。

    Returns:
        无返回值；production 模式不写额外日志。

    """
    if run_mode is RunMode.DEMO:
        logging.getLogger("rag_app.run_mode").warning(
            "DEMO_MODE_ACTIVE component=%s",
            component,
        )


def build_runtime(settings: RuntimeSettings) -> RuntimeBundle:
    """组装查询链、状态库与严格 readiness。

    Args:
        settings: 已验证且密钥被遮蔽的环境设置。

    Returns:
        FastAPI 应用与网络资源。

    Raises:
        ValueError: 活动 alias/manifest 存在但与配置不一致。

    """
    require_release_revision(settings)
    log_run_mode_startup(settings.run_mode, component="rag-app")
    pipeline = load_pipeline(settings.pipeline_path)
    retrieval = RetrievalSettings.load(settings.retrieval_path)
    intent_router = load_intent_router_config(settings.intent_router_path)
    intent_calibration = load_question_profile_calibration(
        settings.intent_router_calibration_path
    )
    _validate_runtime_contract(settings, pipeline, retrieval)
    with ExitStack() as rollback:
        bundle = _assemble_runtime(
            settings,
            pipeline,
            retrieval,
            intent_router,
            intent_calibration,
            rollback,
        )
        rollback.pop_all()
        return bundle


def _assemble_runtime(  # noqa: PLR0913, PLR0917
    settings: RuntimeSettings,
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
    intent_router: IntentRouterConfig,
    intent_calibration: QuestionProfileCalibration,
    rollback: ExitStack,
) -> RuntimeBundle:
    """组装查询进程资源，并把未交付资源注册到回滚栈。

    该流程初始化状态存储和追踪存储，绑定活动索引身份，构造查询链与
    readiness 探针，并在返回前启动后台探测。

    Args:
        settings: 已完成环境校验的运行设置。
        pipeline: 当前索引必须匹配的冻结 pipeline。
        retrieval: 当前服务使用的冻结检索参数。
        intent_router: 受控的 shadow 语义路由配置。
        intent_calibration: 当前路由的真实或未验证校准状态。
        rollback: 在组装失败时按逆序关闭已创建资源的退出栈。

    Returns:
        持有应用、网络客户端、探针和执行器的运行时 bundle。

    Raises:
        ValueError: 活动索引身份或 manifest 与冻结 pipeline 不兼容。

    """
    fingerprint = pipeline.fingerprint()
    serving_fingerprint = retrieval.serving_fingerprint(
        pipeline,
        question_profile_identity={
            "intent_router_sha256": intent_router.canonical_sha256,
            "calibration_sha256": intent_calibration.canonical_sha256,
            "router_revision": intent_router.router_revision,
            "active_mode": intent_router.mode.value,
            "question_profile_schema_revision": (
                QUESTION_PROFILE_SCHEMA_REVISION
            ),
            "llm_classifier_contract_revision": (
                LLM_CLASSIFIER_CONTRACT_REVISION
            ),
        },
    )
    qdrant = QdrantClient(
        url=settings.qdrant_url.rstrip("/"),
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=math.ceil(settings.embedding_timeout_seconds),
        prefer_grpc=False,
    )
    rollback.callback(qdrant.close)
    index = QdrantIndex(
        qdrant,
        collection_name=settings.qdrant_alias,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=fingerprint,
        index_revision=pipeline.index_revision,
    )
    manifests = ManifestRepository(settings.manifest_database)
    manifests.initialize()
    _reject_incompatible_active_index(
        index=index,
        alias_name=settings.qdrant_alias,
        manifests=manifests,
        pipeline_fingerprint=fingerprint,
    )

    clients = _build_http_clients(settings, rollback)
    embedding_pool = _pool(
        settings.embedding_endpoint_urls(),
        clients[0],
        settings,
        max_concurrency=settings.max_embedding_concurrency,
    )
    reranker_pool = _pool(
        settings.reranker_endpoint_urls(),
        clients[1],
        settings,
        max_concurrency=settings.max_reranker_concurrency,
    )
    llm_pool = _pool(
        settings.llm_endpoint_urls(),
        clients[2],
        settings,
        max_concurrency=settings.max_llm_concurrency,
        max_attempts=min(settings.max_attempts, 2),
    )
    embedding = TeiEmbeddingClient(
        embedding_pool,
        config=EmbeddingClientConfig(
            model=settings.embedding_model,
            dimension=pipeline.embedding_dimension,
            max_batch_size=settings.embedding_max_batch_size,
            max_batch_chars=settings.embedding_max_batch_chars,
        ),
        api_token=_secret(settings.embedding_api_token),
    )
    prototype_namespace = PrototypeNamespace(
        config_sha256=intent_router.canonical_sha256,
        embedding_model=pipeline.embedding_model,
        embedding_revision=pipeline.embedding_revision,
        tokenizer_sha256=pipeline.embedding_tokenizer_sha256,
        dimension=pipeline.embedding_dimension,
        expected_example_count=intent_router.example_count,
    )
    semantic_router = SemanticQuestionRouter(
        config=intent_router,
        calibration=intent_calibration,
        namespace=prototype_namespace,
    )
    intent_cache = IntentRouterCache(
        settings.state_database.with_name("intent-router.sqlite3")
    )
    intent_cache.initialize()
    intent_warmup = PrototypeWarmup(
        cache=intent_cache,
        router=semantic_router,
        embedding=embedding,
        instruction=retrieval.query_instruction,
    )
    intent_warmup.start()
    reranker = RerankerClient(
        reranker_pool,
        api_token=_secret(settings.reranker_api_token),
    )
    llm = BufferedLlmClient(
        llm_pool,
        model=settings.llm_model,
        max_context_tokens=settings.llm_max_context_tokens,
        api_token=_secret(settings.llm_api_token),
    )
    token_counter = HuggingFaceTokenCounter(settings.llm_tokenizer_path)
    conversations = ConversationStore(
        settings.state_database,
        ttl_seconds=retrieval.conversation_ttl_seconds,
        max_rounds=retrieval.max_history_turns,
    )
    conversations.initialize()
    answer_cache = AnswerCache(
        settings.state_database.with_name("answer-cache.sqlite3")
    )
    answer_cache.initialize()
    jobs = JobStore(settings.state_database)
    jobs.initialize()
    feedback = FeedbackStore(settings.state_database)
    feedback.initialize()
    logger = StructuredAuditLogger(
        logging.getLogger("rag_app.audit"),
        fingerprint,
        serving_fingerprint,
    )
    trace_store = TraceStore(settings.trace_database)
    trace_store.initialize()
    trace_recorder = TraceRecorder(
        trace_store,
        audit_failure=lambda trace_id, code: logger.trace_failure(
            trace_id,
            code.value,
        ),
    )
    rollback.callback(trace_recorder.close)
    query = _build_query_service(
        retrieval=retrieval,
        parts=_QueryParts(
            index=index,
            embedding=embedding,
            reranker=reranker,
            llm=llm,
            token_counter=token_counter,
            conversations=conversations,
        ),
        trace_recorder=trace_recorder,
        trace_identity=lambda: _trace_identity(
            manifests,
            pipeline_fingerprint=fingerprint,
            serving_fingerprint=serving_fingerprint,
            release_revision=settings.release_revision,
        ),
        default_trace_mode=settings.trace_mode,
        answer_cache=answer_cache,
        access_mode=settings.access_mode.value,
        question_profile_router=semantic_router,
        intent_classifier_max_output_tokens=(
            intent_router.llm_fallback_max_output_tokens
        ),
    )
    readiness = ReadinessService(
        (
            FrozenConfigurationProbe(
                retrieval,
                allow_provisional=settings.run_mode is RunMode.DEMO,
            ),
            QdrantServiceProbe(qdrant),
            ManifestAliasProbe(
                index=index,
                alias_name=settings.qdrant_alias,
                manifests=manifests,
                pipeline_fingerprint=fingerprint,
            ),
            HttpEndpointProbe(
                name="embedding",
                endpoints=settings.embedding_endpoint_urls(),
                client=clients[3],
                minimum_healthy=1,
                expected_model=None,
            ),
            HttpEndpointProbe(
                name="reranker",
                endpoints=settings.reranker_endpoint_urls(),
                client=clients[4],
                minimum_healthy=1,
                expected_model=None,
            ),
            HttpEndpointProbe(
                name="llm",
                endpoints=settings.llm_endpoint_urls(),
                client=clients[5],
                minimum_healthy=1,
                expected_model=settings.llm_model,
            ),
        )
    )
    rollback.callback(readiness.close)
    query_executor = QueryExecutor()
    rollback.callback(query_executor.close)
    app = create_app(
        ApiServices(
            readiness=readiness,
            query_token=settings.query_token.get_secret_value(),
            admin_token=settings.admin_token.get_secret_value(),
            run_mode=settings.run_mode,
            query=query,
            query_executor=query_executor,
            conversations=conversations,
            jobs=jobs,
            feedback=feedback,
            pipeline_fingerprint=fingerprint,
            frontend_dir=settings.frontend_dir,
            audit=logger,
            trace_store=trace_store,
            trace_recorder=trace_recorder,
        )
    )
    readiness.start()
    return RuntimeBundle(
        app=app,
        settings=settings,
        qdrant=qdrant,
        http_clients=clients,
        readiness=readiness,
        query_executor=query_executor,
        trace_recorder=trace_recorder,
    )


def _build_query_service(  # noqa: PLR0913
    *,
    retrieval: RetrievalSettings,
    parts: _QueryParts,
    trace_recorder: TraceRecorder,
    trace_identity: Callable[[], TraceIdentity],
    default_trace_mode: TraceMode,
    answer_cache: AnswerCache,
    access_mode: str,
    question_profile_router: SemanticQuestionRouter,
    intent_classifier_max_output_tokens: int,
) -> QueryService:
    """把冻结检索参数绑定为一条完整且可追踪的查询链。

    Args:
        retrieval: 控制改写、召回、重排和证据预算的冻结参数。
        parts: 已创建的索引、模型客户端、计数器和会话存储。
        trace_recorder: 持久化查询追踪的记录器。
        trace_identity: 在请求时绑定活动索引身份的工厂。
        default_trace_mode: 普通查询默认使用的追踪模式。
        answer_cache: 与活动索引和回答协议绑定的精确缓存。
        access_mode: 当前查询访问范围模式。
        question_profile_router: 复用检索向量的回答组织路由器。
        intent_classifier_max_output_tokens: 关闭式 classifier 的输出上限。

    Returns:
        依赖和阶段配置均已固定的查询服务。

    """
    bm25 = QdrantBm25Encoder(
        tokenizer=retrieval.bm25_tokenizer,
        language=retrieval.bm25_language,
    )
    rewriter = QueryRewriter(
        parts.llm,
        parts.token_counter,
        QueryRewriteConfig(
            max_history_turns=retrieval.max_history_turns,
            history_token_budget=retrieval.history_token_budget,
            max_question_tokens=retrieval.max_question_tokens,
            max_output_tokens=retrieval.rewrite_output_tokens,
        ),
    )
    retriever = HybridRetriever(
        HybridRetrievalServices(
            index=parts.index,
            embedding=parts.embedding,
            bm25=bm25,
            metadata_policy=MetadataPolicy(
                allowed_statuses=retrieval.allowed_statuses,
                allowed_authority_levels=(retrieval.allowed_authority_levels),
            ),
            router=KeywordSoftRouter(
                tuple(
                    KeywordRouteRule(
                        route_id=route.route_id,
                        keywords=route.keywords,
                        source_ids=route.source_ids,
                    )
                    for route in retrieval.soft_routes
                ),
                minimum_confidence=retrieval.soft_route_min_confidence,
            ),
        ),
        HybridRetrievalConfig(
            dense_limit=retrieval.dense_limit,
            bm25_limit=retrieval.bm25_limit,
            rrf_rank_constant=retrieval.rrf_rank_constant,
            candidate_limit=retrieval.candidate_limit,
            query_instruction=retrieval.query_instruction,
        ),
    )
    rerank_stage = RerankStage(
        parts.reranker,
        RerankConfig(
            candidate_limit=retrieval.candidate_limit,
            final_limit=retrieval.final_limit,
            max_final_limit=retrieval.max_final_limit,
        ),
    )
    assembler = EvidenceAssembler(
        parts.token_counter,
        EvidenceConfig(
            max_evidence_tokens=retrieval.max_evidence_tokens,
            max_items=retrieval.max_final_limit,
            low_ocr_threshold=retrieval.low_ocr_threshold,
        ),
    )
    answerer = AnswerGenerator(
        parts.llm,
        AnswerConfig(
            max_output_tokens=retrieval.answer_output_tokens,
            max_repair_tokens=retrieval.repair_output_tokens,
        ),
    )
    intent_classifier = IntentClassifier(
        parts.llm,
        max_output_tokens=intent_classifier_max_output_tokens,
    )
    return QueryService(
        dependencies=QueryDependencies(
            conversations=parts.conversations,
            rewriter=rewriter,
            retriever=retriever,
            reranker=rerank_stage,
            neighbors=NeighborExpander(
                parts.index,
                max_items=retrieval.max_final_limit,
            ),
            assembler=assembler,
            answerer=answerer,
            question_profile_router=question_profile_router,
            intent_classifier=intent_classifier,
        ),
        trace_recorder=trace_recorder,
        trace_identity=trace_identity,
        default_trace_mode=default_trace_mode,
        answer_cache=answer_cache,
        access_mode=access_mode,
    )


def _build_http_clients(
    settings: RuntimeSettings,
    rollback: ExitStack,
) -> tuple[httpx.Client, ...]:
    """分别创建模型请求和健康探测客户端并注册关闭回调。

    返回顺序固定为 embedding、reranker、LLM 的请求客户端，随后是
    相同服务顺序的短超时健康探测客户端。

    Args:
        settings: 提供各服务超时、鉴权和连接配置的运行设置。
        rollback: 在组装失败时关闭已创建客户端的退出栈。

    Returns:
        顺序稳定且彼此隔离的六个 HTTP 客户端。

    """
    pairs = (
        (settings.embedding_timeout_seconds, settings.embedding_api_token),
        (settings.reranker_timeout_seconds, settings.reranker_api_token),
        (settings.llm_timeout_seconds, settings.llm_api_token),
    )
    request_clients = tuple(
        _registered_http_client(
            settings,
            timeout,
            token,
            rollback,
        )
        for timeout, token in pairs
    )
    health_clients = tuple(
        _registered_http_client(
            settings,
            min(timeout, 5.0),
            token,
            rollback,
        )
        for timeout, token in pairs
    )
    return (*request_clients, *health_clients)


def _registered_http_client(
    settings: RuntimeSettings,
    timeout: float,
    token: object,
    rollback: ExitStack,
) -> httpx.Client:
    client = _http_client(settings, timeout, token)
    rollback.callback(client.close)
    return client


def _http_client(
    settings: RuntimeSettings,
    timeout: float,
    token: object,
) -> httpx.Client:
    headers: dict[str, str] = {}
    secret = _secret(token)
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    return httpx.Client(
        timeout=httpx.Timeout(
            timeout,
            connect=settings.http_connect_timeout_seconds,
        ),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
    )


def _pool(
    endpoints: tuple[str, ...],
    client: httpx.Client,
    settings: RuntimeSettings,
    *,
    max_concurrency: int,
    max_attempts: int | None = None,
) -> ResilientHttpPool:
    return ResilientHttpPool(
        endpoints,
        client=client,
        policy=ResiliencePolicy(
            max_attempts=(
                settings.max_attempts if max_attempts is None else max_attempts
            ),
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            max_concurrency=max_concurrency,
        ),
    )


def _secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if get_secret_value is None:
        return None
    secret = get_secret_value()
    return secret if isinstance(secret, str) and secret else None


def _trace_identity(
    manifests: ManifestRepository,
    *,
    pipeline_fingerprint: str,
    serving_fingerprint: str,
    release_revision: str,
) -> TraceIdentity:
    """把查询追踪绑定到当前活动索引和服务版本。

    Args:
        manifests: 提供当前活动 index manifest 的仓库。
        pipeline_fingerprint: 索引构建 pipeline 的稳定指纹。
        serving_fingerprint: 当前查询参数和 pipeline 的联合指纹。
        release_revision: 当前部署制品的版本标识。

    Returns:
        可证明查询所用索引、schema 和服务版本的追踪身份。

    Raises:
        ValueError: 当前没有可绑定的活动 index manifest。

    """
    active = manifests.get_active()
    if active is None:
        raise ValueError("Trace 无法绑定活动 index manifest。")
    return TraceIdentity(
        pipeline_fingerprint=pipeline_fingerprint,
        serving_fingerprint=serving_fingerprint,
        release_revision=release_revision,
        active_collection=active.manifest.collection_name,
        index_manifest_sha256=active.manifest_sha256,
        payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
    )


def _validate_runtime_contract(
    settings: RuntimeSettings,
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
) -> None:
    """在创建网络资源前验证查询进程的冻结契约。

    校验范围涵盖解析器和 prompt revision、tokenizer 内容、corpus
    policy、模型标识、BM25 契约及 OCR 阈值。

    Args:
        settings: 提供本地文件和实际模型标识的运行设置。
        pipeline: 索引构建时冻结的 pipeline。
        retrieval: 查询服务使用的冻结检索参数。

    Returns:
        无返回值；全部契约一致时允许继续组装运行时。

    Raises:
        ValueError: 任一文件摘要、模型、revision 或检索契约不一致。

    """
    if DocxParser.version != pipeline.parser_revision:
        raise ValueError("parser revision 与实际 DocxParser 不一致。")
    _require_file_sha256(
        settings.embedding_tokenizer_path,
        pipeline.embedding_tokenizer_sha256,
        "embedding tokenizer",
    )
    _require_file_sha256(
        settings.llm_tokenizer_path,
        pipeline.llm_tokenizer_sha256,
        "LLM tokenizer",
    )
    corpus_policy = CorpusPolicy.load(settings.corpus_policy_path)
    if corpus_policy.semantic_sha256() != pipeline.corpus_policy_sha256:
        raise ValueError("corpus policy SHA256 与 pipeline 不一致。")
    expected_models = (
        (
            "embedding",
            settings.embedding_model,
            pipeline.embedding_model,
        ),
        (
            "reranker",
            settings.reranker_model,
            pipeline.reranker_model,
        ),
        ("LLM", settings.llm_model, pipeline.llm_model),
    )
    for name, configured, expected in expected_models:
        if configured != expected:
            raise ValueError(f"{name} model ID 与 pipeline 不一致。")
    _require_sparse_contract(pipeline, retrieval)
    if retrieval.low_ocr_threshold != pipeline.ocr_minimum_confidence:
        raise ValueError("OCR minimum confidence 与 pipeline 不一致。")
    if pipeline.prompt_revision != actual_prompt_revision():
        raise ValueError("prompt revision 与实际实现不一致。")


def _require_sparse_contract(
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
) -> None:
    if (
        retrieval.bm25_tokenizer != pipeline.sparse_tokenizer
        or retrieval.bm25_language != pipeline.sparse_language
    ):
        raise ValueError("BM25 tokenizer/language 与 pipeline 不一致。")
    encoder = QdrantBm25Encoder(
        tokenizer=pipeline.sparse_tokenizer,
        language=pipeline.sparse_language,
    )
    if encoder.revision() != pipeline.sparse_revision:
        raise ValueError("BM25 revision 与实际实现不一致。")


def _require_file_sha256(
    path: Path,
    expected_sha256: str,
    label: str,
) -> None:
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"{label} 文件不可读。") from error
    if actual != expected_sha256:
        raise ValueError(f"{label} SHA256 与 pipeline 不一致。")


def _reject_incompatible_active_index(
    *,
    index: QdrantIndex,
    alias_name: str,
    manifests: ManifestRepository,
    pipeline_fingerprint: str,
) -> None:
    """要求活动 alias 和 manifest 同时存在并匹配当前 pipeline。

    Args:
        index: 用于解析 Qdrant alias 的索引访问器。
        alias_name: 查询进程读取的活动索引别名。
        manifests: 提供当前活动 manifest 的仓库。
        pipeline_fingerprint: 当前冻结 pipeline 的稳定指纹。

    Returns:
        无活动索引，或 alias 与 manifest 完全兼容时返回。

    Raises:
        ValueError: alias 与 manifest 缺失状态或冻结身份不一致。

    """
    target = index.alias_target(alias_name)
    active = manifests.get_active()
    if target is None and active is None:
        return
    if target is None or active is None:
        raise ValueError("活动 alias 与 manifest 必须同时存在。")
    manifests.require_compatible(
        collection_name=target,
        pipeline_fingerprint=pipeline_fingerprint,
    )
