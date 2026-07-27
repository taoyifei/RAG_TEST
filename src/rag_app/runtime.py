"""把冻结配置组装为可运行的查询 API。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from qdrant_client import QdrantClient

from rag_app.api import ApiServices, create_app
from rag_app.chunking import HuggingFaceTokenCounter
from rag_app.clients.llm import BufferedLlmClient
from rag_app.clients.model_services import RerankerClient, TeiEmbeddingClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import PipelineSpec
from rag_app.generation.answer import AnswerConfig, AnswerGenerator
from rag_app.generation.evidence import EvidenceAssembler, EvidenceConfig
from rag_app.health import (
    FrozenConfigurationProbe,
    HttpEndpointProbe,
    ManifestAliasProbe,
    QdrantServiceProbe,
    ReadinessService,
)
from rag_app.index import QdrantIndex
from rag_app.manifest import ManifestRepository
from rag_app.observability import StructuredAuditLogger
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
from rag_app.settings import RetrievalSettings, RuntimeSettings
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore

__all__ = ["RuntimeBundle", "build_runtime", "load_pipeline"]


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    """应用及需要保持存活的网络客户端。"""

    app: FastAPI
    settings: RuntimeSettings
    qdrant: QdrantClient
    http_clients: tuple[httpx.Client, ...]

    def close(self) -> None:
        """关闭本进程拥有的外部连接。"""
        for client in self.http_clients:
            client.close()
        self.qdrant.close()


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

    """
    return PipelineSpec.model_validate_json(path.read_text(encoding="utf-8"))


def build_runtime(settings: RuntimeSettings) -> RuntimeBundle:
    """组装查询链、状态库与严格 readiness。

    Args:
        settings: 已验证且密钥被遮蔽的环境设置。

    Returns:
        FastAPI 应用与网络资源。

    Raises:
        ValueError: 活动 alias/manifest 存在但与配置不一致。

    """
    pipeline = load_pipeline(settings.pipeline_path)
    retrieval = RetrievalSettings.load(settings.retrieval_path)
    fingerprint = pipeline.fingerprint()
    qdrant = QdrantClient(
        url=settings.qdrant_url.rstrip("/"),
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=math.ceil(settings.embedding_timeout_seconds),
        prefer_grpc=False,
    )
    index = QdrantIndex(
        qdrant,
        collection_name=settings.qdrant_alias,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=fingerprint,
    )
    manifests = ManifestRepository(settings.manifest_database)
    manifests.initialize()
    _reject_incompatible_active_index(
        index=index,
        alias_name=settings.qdrant_alias,
        manifests=manifests,
        pipeline_fingerprint=fingerprint,
    )

    clients = _build_http_clients(settings)
    embedding_pool = _pool(
        settings.embedding_endpoint_urls(),
        clients[0],
        settings,
    )
    reranker_pool = _pool(
        settings.reranker_endpoint_urls(),
        clients[1],
        settings,
    )
    llm_pool = _pool(settings.llm_endpoint_urls(), clients[2], settings)
    embedding = TeiEmbeddingClient(
        embedding_pool,
        dimension=pipeline.embedding_dimension,
        max_batch_size=settings.embedding_max_batch_size,
        max_batch_chars=settings.embedding_max_batch_chars,
        api_token=_secret(settings.embedding_api_token),
    )
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
    jobs = JobStore(settings.state_database)
    jobs.initialize()
    feedback = FeedbackStore(settings.state_database)
    feedback.initialize()
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
    )
    readiness = ReadinessService(
        (
            FrozenConfigurationProbe(retrieval),
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
                expected_model=settings.embedding_model,
            ),
            HttpEndpointProbe(
                name="reranker",
                endpoints=settings.reranker_endpoint_urls(),
                client=clients[4],
                minimum_healthy=1,
                expected_model=settings.reranker_model,
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
    logger = StructuredAuditLogger(
        logging.getLogger("rag_app.audit"),
        fingerprint,
    )
    app = create_app(
        ApiServices(
            readiness=readiness,
            query_token=settings.query_token.get_secret_value(),
            admin_token=settings.admin_token.get_secret_value(),
            query=query,
            conversations=conversations,
            jobs=jobs,
            feedback=feedback,
            pipeline_fingerprint=fingerprint,
            frontend_dir=settings.frontend_dir,
            audit=logger,
        )
    )
    return RuntimeBundle(
        app=app,
        settings=settings,
        qdrant=qdrant,
        http_clients=clients,
    )


def _build_query_service(
    *,
    retrieval: RetrievalSettings,
    parts: _QueryParts,
) -> QueryService:
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
                allowed_authority_levels=(
                    retrieval.allowed_authority_levels
                ),
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
        )
    )


def _build_http_clients(
    settings: RuntimeSettings,
) -> tuple[httpx.Client, ...]:
    pairs = (
        (settings.embedding_timeout_seconds, settings.embedding_api_token),
        (settings.reranker_timeout_seconds, settings.reranker_api_token),
        (settings.llm_timeout_seconds, settings.llm_api_token),
    )
    request_clients = tuple(
        _http_client(settings, timeout, token)
        for timeout, token in pairs
    )
    health_clients = tuple(
        _http_client(settings, min(timeout, 5.0), token)
        for timeout, token in pairs
    )
    return (*request_clients, *health_clients)


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
) -> ResilientHttpPool:
    return ResilientHttpPool(
        endpoints,
        client=client,
        policy=ResiliencePolicy(
            max_attempts=settings.max_attempts,
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            max_concurrency=settings.max_model_concurrency,
        ),
    )


def _secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if get_secret_value is None:
        return None
    secret = get_secret_value()
    return secret if isinstance(secret, str) and secret else None


def _reject_incompatible_active_index(
    *,
    index: QdrantIndex,
    alias_name: str,
    manifests: ManifestRepository,
    pipeline_fingerprint: str,
) -> None:
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
