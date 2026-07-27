"""组装只写索引的单 worker 运行时。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx
from qdrant_client import QdrantClient

from rag_app.chunking import (
    Chunker,
    ChunkerConfig,
    HuggingFaceTokenCounter,
)
from rag_app.clients.model_services import TeiEmbeddingClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import PipelineSpec
from rag_app.index.build import (
    DocxBuildConfig,
    DocxBuildServices,
    DocxChunkBuilder,
)
from rag_app.index.job_runner import (
    IndexJobRunner,
    JobRunnerConfig,
    JobRunnerServices,
)
from rag_app.index.worker import SyncChunkBuilder
from rag_app.manifest import ManifestRepository
from rag_app.ocr.client import OcrClient
from rag_app.parsers import DocxParser
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.runtime import load_pipeline
from rag_app.settings import (
    ConfigurationState,
    RetrievalSettings,
    RuntimeSettings,
)
from rag_app.state import StateStore

__all__ = [
    "WorkerRuntimeBundle",
    "build_worker_runtime",
    "require_indexable_configuration",
]


@dataclass(frozen=True, slots=True)
class WorkerRuntimeBundle:
    """单 worker 及其持有的网络资源。"""

    runner: IndexJobRunner
    control: StateStore
    qdrant: QdrantClient
    http_client: httpx.Client
    ocr_http_client: httpx.Client

    def close(self) -> None:
        """关闭本进程拥有的连接。"""
        self.http_client.close()
        self.ocr_http_client.close()
        self.qdrant.close()


def build_worker_runtime(settings: RuntimeSettings) -> WorkerRuntimeBundle:
    """从冻结配置组装索引 worker。

    Args:
        settings: 已完成环境校验的运行设置。

    Returns:
        可循环领取管理 API 任务的 worker 运行时。

    Raises:
        ValueError: 检索参数或必要模型 revision 尚未冻结。

    """
    pipeline = load_pipeline(settings.pipeline_path)
    retrieval = RetrievalSettings.load(settings.retrieval_path)
    require_indexable_configuration(pipeline, retrieval)
    qdrant = QdrantClient(
        url=settings.qdrant_url.rstrip("/"),
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=math.ceil(settings.embedding_timeout_seconds),
        prefer_grpc=False,
    )
    http_client = httpx.Client(
        timeout=httpx.Timeout(
            settings.embedding_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    ocr_http_client = httpx.Client(
        timeout=httpx.Timeout(
            settings.ocr_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    pool = ResilientHttpPool(
        settings.embedding_endpoint_urls(),
        client=http_client,
        policy=ResiliencePolicy(
            max_attempts=settings.max_attempts,
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            max_concurrency=settings.max_model_concurrency,
        ),
    )
    embedder = TeiEmbeddingClient(
        pool,
        dimension=pipeline.embedding_dimension,
        max_batch_size=settings.embedding_max_batch_size,
        max_batch_chars=settings.embedding_max_batch_chars,
        api_token=_secret(settings.embedding_api_token),
    )
    ocr_pool = ResilientHttpPool(
        settings.ocr_endpoint_urls(),
        client=ocr_http_client,
        policy=ResiliencePolicy(
            max_attempts=settings.max_attempts,
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            max_concurrency=settings.max_ocr_concurrency,
        ),
    )
    ocr_client = OcrClient(
        ocr_pool,
        revision=pipeline.ocr_revision,
        api_token=_secret(settings.ocr_api_token),
        max_input_bytes=settings.ocr_max_input_bytes,
    )
    chunker = Chunker(
        _chunker_config(pipeline),
        HuggingFaceTokenCounter(settings.embedding_tokenizer_path),
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    sparse = QdrantBm25Encoder(
        tokenizer=retrieval.bm25_tokenizer,
        language=retrieval.bm25_language,
    )
    control = StateStore(settings.state_database)
    control.initialize()
    manifests = ManifestRepository(settings.manifest_database)
    manifests.initialize()

    def build_factory(state: StateStore) -> SyncChunkBuilder:
        return DocxChunkBuilder(
            config=DocxBuildConfig(
                input_root=settings.input_root,
                ocr_revision=pipeline.ocr_revision,
                embedding_instruction="",
                minimum_ocr_confidence=retrieval.low_ocr_threshold,
            ),
            services=DocxBuildServices(
                parser=DocxParser(),
                chunker=chunker,
                embedder=embedder,
                sparse_encoder=sparse,
                state=state,
                ocr_client=ocr_client,
            ),
        )

    runner = IndexJobRunner(
        config=JobRunnerConfig(
            alias_name=settings.qdrant_alias,
            input_root=settings.input_root,
            index_state_dir=settings.index_state_dir,
        ),
        services=JobRunnerServices(
            control=control,
            manifests=manifests,
            qdrant=qdrant,
            pipeline=pipeline,
            build_chunks_factory=build_factory,
        ),
    )
    return WorkerRuntimeBundle(
        runner=runner,
        control=control,
        qdrant=qdrant,
        http_client=http_client,
        ocr_http_client=ocr_http_client,
    )


def require_indexable_configuration(
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
) -> None:
    """阻止临时检索参数或未核验模型 revision 写入生产索引。

    Args:
        pipeline: 待写入 manifest 的完整 pipeline。
        retrieval: 冻结集确定的检索配置。

    Raises:
        ValueError: 配置仍含 provisional、pending 或 unknown 标记。

    """
    if retrieval.status != ConfigurationState.FROZEN:
        raise ValueError("检索参数尚未由冻结集定标，拒绝索引。")
    required_revisions = (
        pipeline.chunker_revision,
        pipeline.ocr_revision,
        pipeline.embedding_revision,
        pipeline.reranker_revision,
        *(revision for _, revision in pipeline.llm_revisions),
    )
    if any(
        marker in revision.lower()
        for revision in required_revisions
        for marker in ("provisional", "pending", "unknown")
    ):
        raise ValueError("必要模型或 chunker revision 尚未核验，拒绝索引。")


def _chunker_config(pipeline: PipelineSpec) -> ChunkerConfig:
    parameters = dict(pipeline.chunker_parameters)
    required = {
        "target_tokens",
        "hard_max_tokens",
        "overlap_tokens",
    }
    if set(parameters) != required:
        raise ValueError("pipeline chunker_parameters 字段不完整。")
    try:
        return ChunkerConfig(
            target_tokens=int(parameters["target_tokens"]),
            hard_max_tokens=int(parameters["hard_max_tokens"]),
            overlap_tokens=int(parameters["overlap_tokens"]),
        )
    except ValueError as error:
        raise ValueError("pipeline 分块参数必须是十进制整数。") from error


def _secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if get_secret_value is None:
        return None
    secret = get_secret_value()
    return secret if isinstance(secret, str) and secret else None
