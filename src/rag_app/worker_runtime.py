"""组装只写索引的单 worker 运行时。"""

from __future__ import annotations

import hashlib
import math
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

from rag_app.chunking import (
    Chunker,
    ChunkerConfig,
    HuggingFaceTokenCounter,
)
from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import DocumentMetadata, PipelineSpec
from rag_app.corpus_policy import CorpusPolicy
from rag_app.index.build import (
    DocxBuildConfig,
    DocxBuildServices,
    DocxChunkBuilder,
    discover_docx_sources,
)
from rag_app.index.job_runner import (
    IndexJobRunner,
    JobRunnerConfig,
    JobRunnerServices,
)
from rag_app.index.qdrant import QdrantIndex
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


@dataclass(slots=True)
class WorkerRuntimeBundle:
    """单 worker 及其持有的网络资源。"""

    runner: IndexJobRunner
    control: StateStore
    qdrant: QdrantClient
    http_client: httpx.Client
    ocr_http_client: httpx.Client
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """关闭本进程拥有的连接。

        Args:
            无参数；关闭当前 worker 持有的资源。

        Returns:
            无返回值。

        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        with ExitStack() as close_stack:
            close_stack.callback(self.qdrant.close)
            close_stack.callback(self.http_client.close)
            close_stack.callback(self.ocr_http_client.close)


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
    metadata_by_source = _validate_worker_contract(
        settings,
        pipeline,
        retrieval,
    )
    with ExitStack() as rollback:
        bundle = _assemble_worker_runtime(
            settings,
            pipeline,
            metadata_by_source,
            rollback,
        )
        rollback.pop_all()
        return bundle


def _assemble_worker_runtime(
    settings: RuntimeSettings,
    pipeline: PipelineSpec,
    metadata_by_source: dict[str, DocumentMetadata],
    rollback: ExitStack,
) -> WorkerRuntimeBundle:
    qdrant = QdrantClient(
        url=settings.qdrant_url.rstrip("/"),
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=math.ceil(settings.embedding_timeout_seconds),
        prefer_grpc=False,
    )
    rollback.callback(qdrant.close)
    manifests = ManifestRepository(settings.manifest_database)
    manifests.initialize()
    _reject_incompatible_active_collection(
        qdrant=qdrant,
        manifests=manifests,
        alias_name=settings.qdrant_alias,
        pipeline=pipeline,
    )
    http_client = httpx.Client(
        timeout=httpx.Timeout(
            settings.embedding_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    rollback.callback(http_client.close)
    ocr_http_client = httpx.Client(
        timeout=httpx.Timeout(
            settings.ocr_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    rollback.callback(ocr_http_client.close)
    pool = ResilientHttpPool(
        settings.embedding_endpoint_urls(),
        client=http_client,
        policy=ResiliencePolicy(
            max_attempts=settings.max_attempts,
            failure_threshold=settings.failure_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            max_concurrency=settings.max_embedding_concurrency,
        ),
    )
    embedder = TeiEmbeddingClient(
        pool,
        config=EmbeddingClientConfig(
            model=settings.embedding_model,
            dimension=pipeline.embedding_dimension,
            max_batch_size=settings.embedding_max_batch_size,
            max_batch_chars=settings.embedding_max_batch_chars,
        ),
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
        tokenizer=pipeline.sparse_tokenizer,
        language=pipeline.sparse_language,
    )
    control = StateStore(settings.state_database)
    control.initialize()

    def build_factory(state: StateStore) -> SyncChunkBuilder:
        """为一次任务创建绑定状态库的 DOCX 构建器。

        Args:
            state: 当前任务使用的状态库。

        Returns:
            配置完整的同步 chunk 构建器。

        """
        return DocxChunkBuilder(
            config=DocxBuildConfig(
                input_root=settings.input_root,
                ocr_revision=pipeline.ocr_revision,
                embedding_instruction=(
                    pipeline.document_embedding_instruction
                ),
                metadata_by_source=metadata_by_source,
                minimum_ocr_confidence=(
                    pipeline.ocr_minimum_confidence
                ),
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


def _reject_incompatible_active_collection(
    *,
    qdrant: QdrantClient,
    manifests: ManifestRepository,
    alias_name: str,
    pipeline: PipelineSpec,
) -> None:
    """在构造模型客户端前拒绝旧 payload schema 的活动索引。

    Args:
        qdrant: 已配置鉴权的 Qdrant 客户端。
        manifests: worker 使用的 manifest 仓库。
        alias_name: 当前活动索引别名。
        pipeline: 本进程冻结的 pipeline。

    Returns:
        无活动 manifest 或全部契约兼容时返回。

    Raises:
        ValueError: alias、manifest、pipeline 或 payload schema 不兼容。

    """
    active = manifests.get_active()
    if active is None:
        return
    fingerprint = pipeline.fingerprint()
    index = QdrantIndex(
        qdrant,
        collection_name=active.manifest.collection_name,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=fingerprint,
    )
    target = index.alias_target(alias_name)
    if target != active.manifest.collection_name:
        raise ValueError("worker 活动 alias 与 manifest collection 不一致。")
    manifests.require_compatible(
        collection_name=target,
        pipeline_fingerprint=fingerprint,
    )
    index.require_compatible_collection(target)


def require_indexable_configuration(
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
) -> None:
    """阻止临时检索参数或未核验模型 revision 写入生产索引。

    Args:
        pipeline: 待写入 manifest 的完整 pipeline。
        retrieval: 冻结集确定的检索配置。

    Returns:
        无返回值；校验通过即允许继续构建索引。

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


def _validate_worker_contract(
    settings: RuntimeSettings,
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
) -> dict[str, DocumentMetadata]:
    _require_file_sha256(
        settings.embedding_tokenizer_path,
        pipeline.embedding_tokenizer_sha256,
        "embedding tokenizer",
    )
    if settings.embedding_model != pipeline.embedding_model:
        raise ValueError("embedding model ID 与 pipeline 不一致。")
    if DocxParser.version != pipeline.parser_revision:
        raise ValueError("parser revision 与实际 DocxParser 不一致。")
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
    if retrieval.low_ocr_threshold != pipeline.ocr_minimum_confidence:
        raise ValueError("OCR minimum confidence 与 pipeline 不一致。")
    corpus_policy = CorpusPolicy.load(settings.corpus_policy_path)
    if corpus_policy.semantic_sha256() != pipeline.corpus_policy_sha256:
        raise ValueError("corpus policy SHA256 与 pipeline 不一致。")
    discovered = discover_docx_sources(settings.input_root)
    return corpus_policy.resolve(
        input_root=settings.input_root,
        discovered_paths=tuple(
            source.source_path for source in discovered
        ),
    )


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
