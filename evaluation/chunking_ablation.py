"""对 legacy 与 section-pack-v2 候选执行隔离的结构和检索消融。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(
        0,
        str(Path(__file__).resolve().parents[1] / "src"),
    )

from evaluation.chunking_experiment import summarize_token_lengths
from evaluation.dataset import EvaluationCase, load_dataset, load_tuning_cases
from evaluation.legacy_chunking import (
    LegacyElementChunk,
    legacy_element_chunks,
)
from rag_app.chunking import (
    Chunker,
    ChunkerConfig,
    HuggingFaceTokenCounter,
    TokenCounter,
)
from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    RerankerClient,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import (
    Chunk,
    ChunkRole,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
    PipelineSpec,
    allocate_source_id,
    content_doc_version,
    validate_chunk_source_spans,
)
from rag_app.corpus_policy import CorpusPolicy
from rag_app.freeze_evidence import (
    FreezeCandidateConfig,
    ModelFleetIdentity,
    RetrievalModelEndpoints,
    build_candidate_pipeline,
    canonical_tuning_digest,
    verify_model_fleet,
)
from rag_app.index.build import OcrElementProcessor
from rag_app.index.qdrant import QdrantIndex
from rag_app.ocr.client import OcrClient
from rag_app.parsers.docx import DocxParser
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.fusion import FusedHit, reciprocal_rank_fusion
from rag_app.retrieval.rerank import RerankConfig, RerankStage
from rag_app.runtime import load_pipeline
from rag_app.settings import ConfigurationState, RetrievalSettings
from rag_app.state import SourceVersion, StateStore, VersionState
from scripts.freeze_corpus_manifest import CorpusManifest, verify_corpus

__all__ = [
    "DEFAULT_SECTION_CANDIDATES",
    "LEGACY_CANDIDATE",
    "AblationCandidate",
    "AblationInputFiles",
    "CalibrationEvidenceFiles",
    "RetrievalEnvironment",
    "RetrievalEvidenceFiles",
    "load_tuning_cases_only",
    "parse_candidate",
    "run_retrieval_ablation",
    "run_structural_ablation",
    "summarize_section_candidate",
]

_RETRIEVAL_CATEGORIES = ("cross_chunk", "table", "numeric")
_CANDIDATE_PART_COUNT = 3
_SMALL_CHUNK_32 = 32
_SMALL_CHUNK_64 = 64
_SHA256_HEX_LENGTH = 64
_OCR_MAX_INPUT_BYTES = 10 * 1024 * 1024
_OCR_CALIBRATION_STATES = (
    OcrState.SUCCEEDED,
    OcrState.LOW_CONFIDENCE,
    OcrState.FAILED,
    OcrState.PENDING,
)
_LEGACY_CONFIG = ChunkerConfig(
    target_tokens=384,
    hard_max_tokens=512,
    overlap_tokens=64,
)


@dataclass(frozen=True, slots=True)
class AblationCandidate:
    """一个不代表生产结论的确定性 chunk 参数候选。"""

    label: str
    strategy: str
    target_tokens: int
    hard_max_tokens: int
    overlap_tokens: int

    def chunker_config(self) -> ChunkerConfig:
        """生成候选使用的 ChunkerConfig。

        Args:
            无参数。

        Returns:
            与当前候选三个整数完全一致的配置。

        """
        return ChunkerConfig(
            target_tokens=self.target_tokens,
            hard_max_tokens=self.hard_max_tokens,
            overlap_tokens=self.overlap_tokens,
        )


LEGACY_CANDIDATE = AblationCandidate(
    label="legacy-element-384-512-64",
    strategy="legacy_element",
    target_tokens=384,
    hard_max_tokens=512,
    overlap_tokens=64,
)
DEFAULT_SECTION_CANDIDATES = (
    AblationCandidate(
        label="section-pack-v2-256-512-32",
        strategy="section_pack_v2",
        target_tokens=256,
        hard_max_tokens=512,
        overlap_tokens=32,
    ),
    AblationCandidate(
        label="section-pack-v2-320-512-48",
        strategy="section_pack_v2",
        target_tokens=320,
        hard_max_tokens=512,
        overlap_tokens=48,
    ),
    AblationCandidate(
        label="section-pack-v2-384-512-64",
        strategy="section_pack_v2",
        target_tokens=384,
        hard_max_tokens=512,
        overlap_tokens=64,
    ),
)


@dataclass(frozen=True, slots=True)
class RetrievalEnvironment:
    """真实 embedding、reranker、OCR 与临时 Qdrant 的连接参数。"""

    qdrant_url: str
    qdrant_api_key: str
    embedding_endpoints: tuple[str, ...]
    reranker_endpoints: tuple[str, ...]
    ocr_endpoints: tuple[str, ...]
    embedding_api_token: str | None
    reranker_api_token: str | None
    ocr_api_token: str | None
    document_paths: dict[str, str]
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        """拒绝缺失服务或无界超时。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            ValueError: 真实检索消融连接配置不完整。

        """
        if (
            not self.qdrant_url
            or not self.qdrant_api_key
            or not self.embedding_endpoints
            or not self.reranker_endpoints
            or not self.ocr_endpoints
            or not self.document_paths
            or self.timeout_seconds <= 0
        ):
            raise ValueError("retrieval 消融的真实 OCR/模型配置不完整。")


@dataclass(frozen=True, slots=True)
class AblationInputFiles:
    """结构与检索消融共享的四个 operator 输入。"""

    input_directory: Path
    tokenizer_path: Path
    pipeline_path: Path
    corpus_policy_path: Path


@dataclass(frozen=True, slots=True)
class CalibrationEvidenceFiles:
    """结构消融必须绑定的源码与 corpus 证据。"""

    calibration_source_revision: str
    corpus_manifest_path: Path


@dataclass(frozen=True, slots=True)
class RetrievalEvidenceFiles(CalibrationEvidenceFiles):
    """真实检索额外绑定的配置、数据集和模型证据。"""

    retrieval_path: Path
    dataset_path: Path
    fleet_report_path: Path
    model_contract_directory: Path


@dataclass(frozen=True, slots=True)
class _ParsedDocument:
    source_id: str
    source_path: str
    doc_version: str
    elements: tuple[Element, ...]
    metadata: DocumentMetadata


@dataclass(frozen=True, slots=True)
class _RetrievalChunk:
    chunk_id: str
    source_id: str
    source_path: str
    doc_version: str
    text: str
    embedding_text: str
    locators: tuple[Locator, ...]
    metadata: DocumentMetadata


@dataclass(frozen=True, slots=True)
class _RetrievalServices:
    pipeline: PipelineSpec
    counter: TokenCounter
    documents: tuple[_ParsedDocument, ...]
    cases: tuple[EvaluationCase, ...]
    retrieval: RetrievalSettings
    qdrant: QdrantClient
    embedding: TeiEmbeddingClient
    reranker: RerankerClient
    document_paths: dict[str, str]
    fleet: ModelFleetIdentity


@dataclass(frozen=True, slots=True)
class _IndexingServices:
    index: QdrantIndex
    qdrant: QdrantClient
    embedding: TeiEmbeddingClient
    sparse: QdrantBm25Encoder
    document_instruction: str


@dataclass(frozen=True, slots=True)
class _QueryServices:
    index: QdrantIndex
    embedding: TeiEmbeddingClient
    reranker: RerankerClient
    sparse: QdrantBm25Encoder
    retrieval: RetrievalSettings
    document_paths: dict[str, str]


def parse_candidate(raw: str) -> AblationCandidate:
    """解析 `target/hard/overlap` section-pack-v2 候选。

    Args:
        raw: 三个十进制整数，以 `/` 分隔。

    Returns:
        已通过 ChunkerConfig 约束的候选。

    Raises:
        ValueError: 格式或数值边界无效。

    """
    parts = raw.split("/")
    if len(parts) != _CANDIDATE_PART_COUNT:
        raise ValueError("candidate 必须为 target/hard/overlap。")
    try:
        target, hard, overlap = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError("candidate 必须只含十进制整数。") from error
    ChunkerConfig(
        target_tokens=target,
        hard_max_tokens=hard,
        overlap_tokens=overlap,
    )
    return AblationCandidate(
        label=f"section-pack-v2-{target}-{hard}-{overlap}",
        strategy="section_pack_v2",
        target_tokens=target,
        hard_max_tokens=hard,
        overlap_tokens=overlap,
    )


def load_tuning_cases_only(path: Path) -> tuple[EvaluationCase, ...]:
    """只通过冻结集 tuning 入口取得可用于调参的标签。

    Args:
        path: 人工冻结集 JSON。

    Returns:
        全部且仅含 tuning split 的题目。

    Raises:
        ValueError: loader 返回了任何 holdout 或未知 split。

    """
    cases = load_tuning_cases(path)
    if any(case.split != "tuning" for case in cases):
        raise ValueError("tuning 消融拒绝加载 holdout expected 标签。")
    return cases


def run_structural_ablation(
    inputs: AblationInputFiles,
    evidence: CalibrationEvidenceFiles,
    *,
    candidates: tuple[AblationCandidate, ...] = (
        DEFAULT_SECTION_CANDIDATES
    ),
) -> dict[str, object]:
    """在真实只读 DOCX 上生成不含私有内容的结构消融。

    Args:
        inputs: DOCX、tokenizer、pipeline 与 corpus policy 路径。
        evidence: 本次源码 revision 与 canonical corpus 清单。
        candidates: 至少一个 section-pack-v2 参数候选。

    Returns:
        legacy 与全部 v2 候选的聚合结构报告。

    Raises:
        ValueError: 配置、资产或候选不兼容。

    """
    corpus = verify_corpus(
        docs_root=inputs.input_directory,
        manifest_path=evidence.corpus_manifest_path,
    )
    pipeline, counter, documents = _load_documents(
        inputs.input_directory,
        inputs.tokenizer_path,
        inputs.pipeline_path,
        inputs.corpus_policy_path,
    )
    _require_section_candidates(candidates)
    reports: list[dict[str, object]] = []
    legacy_documents = tuple(
        (
            document.elements,
            legacy_element_chunks(
                document.elements,
                counter,
                _LEGACY_CONFIG,
            ),
        )
        for document in documents
    )
    reports.append(
        _candidate_payload(
            LEGACY_CANDIDATE,
            _summarize_legacy_candidate(
                legacy_documents,
                counter,
                _LEGACY_CONFIG,
            ),
        )
    )
    for candidate in candidates:
        config = candidate.chunker_config()
        fingerprint = _candidate_fingerprint(pipeline, candidate)
        chunker = Chunker(
            config,
            counter,
            pipeline_fingerprint=fingerprint,
        )
        section_documents = tuple(
            (
                document.elements,
                chunker.chunk(
                    source_id=document.source_id,
                    doc_version=document.doc_version,
                    elements=list(document.elements),
                    metadata=document.metadata,
                ),
            )
            for document in documents
        )
        reports.append(
            _candidate_payload(
                candidate,
                summarize_section_candidate(
                    section_documents,
                    counter,
                    config,
                ),
            )
        )
    return {
        "mode": "structural",
        "status": "provisional_no_parameter_selection",
        "identity": _calibration_identity(
            calibration_source_revision=(
                evidence.calibration_source_revision
            ),
            pipeline_path=inputs.pipeline_path,
            pipeline=pipeline,
            corpus_manifest_path=evidence.corpus_manifest_path,
            corpus=corpus,
        ),
        "documents": len(documents),
        "parser_counts": _parser_counts(documents),
        "candidates": reports,
    }


def summarize_section_candidate(
    documents: Sequence[
        tuple[Sequence[Element], Sequence[Chunk]]
    ],
    token_counter: TokenCounter,
    config: ChunkerConfig,
) -> dict[str, object]:
    """汇总由真实 Chunk/source span 产生的 section 候选。

    Args:
        documents: 每篇文档的原始元素与 production Chunker 结果。
        token_counter: 与候选相同的 tokenizer。
        config: 当前候选硬上限。

    Returns:
        可验证覆盖、边界、重复和 token 分布的聚合报告。

    """
    chunks = tuple(chunk for _, items in documents for chunk in items)
    elements = tuple(element for items, _ in documents for element in items)
    expected = _expected_elements(elements)
    expected_ids = {element.element_id for element in expected}
    represented_ids = {
        span.element_id for chunk in chunks for span in chunk.source_spans
    }
    text_lengths = [token_counter.count(chunk.text) for chunk in chunks]
    embedding_lengths = [
        token_counter.count(chunk.embedding_text) for chunk in chunks
    ]
    ordinary_source_chars = sum(
        len(element.text)
        for element in expected
        if element.kind == ElementKind.PARAGRAPH
    )
    ordinary_span_chars = sum(
        span.end_char - span.start_char
        for chunk in chunks
        if chunk.chunk_role == ChunkRole.TEXT
        for span in chunk.source_spans
    )
    source_chars = sum(len(element.text) for element in expected)
    span_chars = sum(
        span.end_char - span.start_char
        for chunk in chunks
        for span in chunk.source_spans
    )
    return {
        "chunks": len(chunks),
        "chunks_by_role": dict(
            sorted(Counter(chunk.chunk_role.value for chunk in chunks).items())
        ),
        "text_token_lengths": _token_summary(text_lengths),
        "embedding_token_lengths": _token_summary(embedding_lengths),
        **_small_chunk_counts(text_lengths),
        "standalone_heading_chunks": sum(
            chunk.element_kind == ElementKind.HEADING for chunk in chunks
        ),
        "cross_section_chunks": _cross_section_chunks(chunks),
        "cross_neighbor_group_links": _cross_group_links(chunks),
        "hard_max_violations": sum(
            length > config.hard_max_tokens for length in text_lengths
        ),
        "uncovered_source_elements": len(expected_ids - represented_ids),
        "source_coverage_ratio": _ratio(
            len(expected_ids & represented_ids),
            len(expected_ids),
        ),
        "duplicated_source_characters": max(0, span_chars - source_chars),
        "duplicate_source_character_ratio": _ratio(
            max(0, span_chars - source_chars),
            source_chars,
        ),
        "ordinary_duplicated_source_characters": max(
            0,
            ordinary_span_chars - ordinary_source_chars,
        ),
        "ordinary_duplicate_source_character_ratio": _ratio(
            max(0, ordinary_span_chars - ordinary_source_chars),
            ordinary_source_chars,
        ),
        "table_row_split_violations": _table_row_split_violations(
            chunks,
            {element.element_id: element for element in elements},
            token_counter,
            config.hard_max_tokens,
        ),
        "blank_chunks": sum(not chunk.text.strip() for chunk in chunks),
        "duplicate_chunk_ids": len(chunks)
        - len({chunk.chunk_id for chunk in chunks}),
        "ambiguous_quote_locator_cases": _ambiguous_quote_cases(chunks),
        "quote_locator_contract_violations": (
            _quote_locator_contract_violations(chunks)
        ),
        **_numbering_counts(elements),
    }


def run_retrieval_ablation(
    inputs: AblationInputFiles,
    evidence: RetrievalEvidenceFiles,
    environment: RetrievalEnvironment,
    *,
    candidates: tuple[AblationCandidate, ...] = (
        DEFAULT_SECTION_CANDIDATES
    ),
) -> dict[str, object]:
    """用真实模型和候选独立临时 collection 运行 tuning-only 检索。

    Args:
        inputs: DOCX、tokenizer、pipeline、corpus policy 路径。
        evidence: retrieval、dataset、corpus 与同次 fleet 证据路径。
        environment: 真实 embedding、reranker、OCR 和 Qdrant 服务。
        candidates: section-pack-v2 候选。

    Returns:
        不含问题、原文、quote 或文件名的 tuning 聚合指标。

    Raises:
        ValueError: 配置、tuning 隔离或模型响应不满足契约。

    """
    corpus = verify_corpus(
        docs_root=inputs.input_directory,
        manifest_path=evidence.corpus_manifest_path,
    )
    pipeline, counter, documents = _load_documents(
        inputs.input_directory,
        inputs.tokenizer_path,
        inputs.pipeline_path,
        inputs.corpus_policy_path,
    )
    retrieval = RetrievalSettings.load(evidence.retrieval_path)
    if (
        retrieval.status != ConfigurationState.PROVISIONAL
        or retrieval.freeze_decision_sha256 is not None
    ):
        raise ValueError("retrieval 消融必须使用 provisional 配置。")
    dataset = load_dataset(evidence.dataset_path)
    tuning_cases = load_tuning_cases_only(evidence.dataset_path)
    if environment.document_paths != dataset.documents:
        raise ValueError("document map 必须与 calibration dataset 完全一致。")
    _require_document_map(tuning_cases, environment.document_paths)
    _require_section_candidates(candidates)
    fleet = verify_model_fleet(
        evidence.fleet_report_path,
        evidence.model_contract_directory,
        pipeline=pipeline,
        calibration_source_revision=evidence.calibration_source_revision,
        retrieval_endpoints=RetrievalModelEndpoints(
            embedding=environment.embedding_endpoints,
            reranker=environment.reranker_endpoints,
        ),
    )
    qdrant = QdrantClient(
        url=environment.qdrant_url,
        api_key=environment.qdrant_api_key,
        timeout=math.ceil(environment.timeout_seconds),
        check_compatibility=False,
    )
    embedding_http = httpx.Client(
        timeout=environment.timeout_seconds,
        trust_env=False,
        follow_redirects=False,
    )
    reranker_http = httpx.Client(
        timeout=environment.timeout_seconds,
        trust_env=False,
        follow_redirects=False,
    )
    ocr_http = httpx.Client(
        timeout=environment.timeout_seconds,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        embedding = TeiEmbeddingClient(
            _pool(environment.embedding_endpoints, embedding_http),
            config=EmbeddingClientConfig(
                model=pipeline.embedding_model,
                dimension=pipeline.embedding_dimension,
                max_batch_size=32,
                max_batch_chars=100_000,
            ),
            api_token=environment.embedding_api_token,
        )
        reranker = RerankerClient(
            _pool(environment.reranker_endpoints, reranker_http),
            api_token=environment.reranker_api_token,
        )
        ocr = OcrClient(
            _pool(environment.ocr_endpoints, ocr_http),
            revision=pipeline.ocr_revision,
            api_token=environment.ocr_api_token,
            max_input_bytes=_OCR_MAX_INPUT_BYTES,
        )
        with tempfile.TemporaryDirectory(
            prefix="rag-ablation-ocr-state-"
        ) as state_dir:
            state = StateStore(Path(state_dir) / "state.sqlite3")
            state.initialize()
            documents, ocr_states = _enrich_documents_with_ocr(
                documents,
                tuning_cases,
                environment.document_paths,
                processor=OcrElementProcessor(
                    state=state,
                    ocr_client=ocr,
                    ocr_revision=pipeline.ocr_revision,
                    minimum_confidence=pipeline.ocr_minimum_confidence,
                ),
                pipeline_fingerprint=pipeline.fingerprint(),
            )
        services = _RetrievalServices(
            pipeline=pipeline,
            counter=counter,
            documents=documents,
            cases=tuning_cases,
            retrieval=retrieval,
            qdrant=qdrant,
            embedding=embedding,
            reranker=reranker,
            document_paths=environment.document_paths,
            fleet=fleet,
        )
        reports = [
            _run_retrieval_candidate(candidate, services)
            for candidate in (LEGACY_CANDIDATE, *candidates)
        ]
    finally:
        ocr_http.close()
        reranker_http.close()
        embedding_http.close()
        qdrant.close()
    return {
        "mode": "retrieval",
        "split": "tuning",
        "status": "real_model_results_provisional",
        "identity": {
            **_calibration_identity(
                calibration_source_revision=(
                    evidence.calibration_source_revision
                ),
                pipeline_path=inputs.pipeline_path,
                pipeline=pipeline,
                corpus_manifest_path=evidence.corpus_manifest_path,
                corpus=corpus,
            ),
            "retrieval_file_sha256": _prefixed_file_sha256(
                evidence.retrieval_path
            ),
            "retrieval_serving_fingerprint": (
                retrieval.serving_fingerprint(pipeline)
            ),
            "evaluation_dataset_sha256": _prefixed_file_sha256(
                evidence.dataset_path
            ),
            "tuning_digest": canonical_tuning_digest(
                dataset.documents,
                tuple(
                    case.model_dump(mode="json") for case in dataset.cases
                ),
            ),
            "fleet": fleet.model_dump(mode="json"),
        },
        "cases": len(tuning_cases),
        "ocr_calibrated": True,
        "ocr_states": ocr_states,
        "candidates": reports,
    }


def _enrich_documents_with_ocr(
    documents: tuple[_ParsedDocument, ...],
    cases: tuple[EvaluationCase, ...],
    document_paths: dict[str, str],
    *,
    processor: OcrElementProcessor,
    pipeline_fingerprint: str,
) -> tuple[tuple[_ParsedDocument, ...], dict[str, int]]:
    """在候选建库前只执行一次生产一致的图片 OCR。

    Args:
        documents: 解析器产生的全部只读文档。
        cases: 仅含 tuning split 的校准题。
        document_paths: 冻结题文档键到相对路径的映射。
        processor: 与生产 builder 共用的 OCR 状态处理器。
        pipeline_fingerprint: 校准输入 pipeline 的稳定指纹。

    Returns:
        已补充 OCR 文本的文档和按图片出现次数统计的状态计数。

    Raises:
        ValueError: 出现 pending 图片，或 tuning OCR locator 缺失或失败。

    """
    enriched = tuple(
        _ParsedDocument(
            source_id=document.source_id,
            source_path=document.source_path,
            doc_version=document.doc_version,
            elements=tuple(
                processor.process(
                    list(document.elements),
                    _calibration_source_version(
                        document,
                        pipeline_fingerprint,
                    ),
                )
            ),
            metadata=document.metadata,
        )
        for document in documents
    )
    counts = _ocr_state_counts(enriched)
    if counts[OcrState.PENDING.value] > 0:
        raise ValueError("OCR 校准仍含 pending 图片，拒绝候选建库。")
    _require_tuning_ocr_evidence(enriched, cases, document_paths)
    return enriched, counts


def _calibration_source_version(
    document: _ParsedDocument,
    pipeline_fingerprint: str,
) -> SourceVersion:
    content_sha256 = document.doc_version.removeprefix("sha256:")
    if len(content_sha256) != _SHA256_HEX_LENGTH:
        raise ValueError("校准文档 doc_version 不是 SHA256。")
    return SourceVersion(
        source_id=document.source_id,
        doc_version=document.doc_version,
        content_sha256=content_sha256,
        source_path=document.source_path,
        pipeline_fingerprint=pipeline_fingerprint,
        state=VersionState.STAGING,
        job_id="calibration-ocr",
        chunk_count=None,
        error_code=None,
    )


def _ocr_state_counts(
    documents: tuple[_ParsedDocument, ...],
) -> dict[str, int]:
    counts = {state.value: 0 for state in _OCR_CALIBRATION_STATES}
    for document in documents:
        for element in document.elements:
            if element.kind != ElementKind.IMAGE:
                continue
            state = element.ocr_state
            if state not in _OCR_CALIBRATION_STATES:
                raise ValueError("OCR 校准图片缺少终态或 pending 状态。")
            counts[state.value] += 1
    return counts


def _require_tuning_ocr_evidence(
    documents: tuple[_ParsedDocument, ...],
    cases: tuple[EvaluationCase, ...],
    document_paths: dict[str, str],
) -> None:
    images = tuple(
        (document.source_path, element)
        for document in documents
        for element in document.elements
        if element.kind == ElementKind.IMAGE
    )
    for case in cases:
        if "ocr" not in case.categories:
            continue
        for label in case.expected.evidence:
            if label.quote is not None:
                continue
            expected_path = document_paths.get(label.document)
            if expected_path is None:
                raise ValueError("tuning OCR 文档键不在 document map。")
            matches = tuple(
                element
                for source_path, element in images
                if source_path == expected_path
                and label.locator_contains in element.locator.display()
            )
            if not matches:
                raise ValueError("tuning OCR locator 未命中校准图片。")
            if any(
                element.ocr_state in {OcrState.FAILED, OcrState.PENDING}
                for element in matches
            ):
                raise ValueError("tuning OCR locator 对应图片识别失败。")


def _run_retrieval_candidate(
    candidate: AblationCandidate,
    services: _RetrievalServices,
) -> dict[str, object]:
    candidate_pipeline = build_candidate_pipeline(
        services.pipeline,
        FreezeCandidateConfig(
            target_tokens=candidate.target_tokens,
            hard_max_tokens=candidate.hard_max_tokens,
            overlap_tokens=candidate.overlap_tokens,
        ),
        strategy=candidate.strategy,
        fleet=services.fleet,
    )
    with _temporary_candidate_index(
        services.qdrant,
        candidate_pipeline,
        candidate,
        pipeline_fingerprint=candidate_pipeline.index_fingerprint(),
    ) as index:
        chunks = _retrieval_chunks(
            candidate,
            pipeline=candidate_pipeline,
            counter=services.counter,
            documents=services.documents,
        )
        _index_retrieval_chunks(
            chunks,
            _IndexingServices(
                index=index,
                qdrant=services.qdrant,
                embedding=services.embedding,
                sparse=QdrantBm25Encoder(
                    tokenizer=candidate_pipeline.sparse_tokenizer,
                    language=candidate_pipeline.sparse_language,
                ),
                document_instruction=(
                    candidate_pipeline.document_embedding_instruction
                ),
            ),
        )
        metrics = _retrieval_metrics(
            services.cases,
            _QueryServices(
                index=index,
                embedding=services.embedding,
                reranker=services.reranker,
                sparse=QdrantBm25Encoder(
                    tokenizer=candidate_pipeline.sparse_tokenizer,
                    language=candidate_pipeline.sparse_language,
                ),
                retrieval=services.retrieval,
                document_paths=services.document_paths,
            ),
        )
    payload = _candidate_payload(candidate, metrics)
    payload.update(
        {
            "index_fingerprint": candidate_pipeline.index_fingerprint(),
            "serving_fingerprint": services.retrieval.serving_fingerprint(
                candidate_pipeline
            ),
        }
    )
    return payload


@contextmanager
def _temporary_candidate_index(
    qdrant: QdrantClient,
    pipeline: PipelineSpec,
    candidate: AblationCandidate,
    *,
    pipeline_fingerprint: str | None = None,
) -> Iterator[QdrantIndex]:
    """创建且只清理本次校准使用的随机 collection。"""
    collection = f"rag-ablation-{uuid.uuid4().hex}"
    index = QdrantIndex(
        qdrant,
        collection_name=collection,
        dense_dimension=pipeline.embedding_dimension,
        pipeline_fingerprint=(
            pipeline_fingerprint
            if pipeline_fingerprint is not None
            else _candidate_fingerprint(pipeline, candidate)
        ),
    )
    if qdrant.collection_exists(collection):
        raise ValueError("随机校准 collection 已存在，拒绝接管。")
    try:
        index.create_collection()
        yield index
    finally:
        if qdrant.collection_exists(collection):
            qdrant.delete_collection(collection)


def _retrieval_chunks(
    candidate: AblationCandidate,
    *,
    pipeline: PipelineSpec,
    counter: TokenCounter,
    documents: tuple[_ParsedDocument, ...],
) -> tuple[_RetrievalChunk, ...]:
    result: list[_RetrievalChunk] = []
    if candidate.strategy == "legacy_element":
        for document in documents:
            legacy = legacy_element_chunks(
                document.elements,
                counter,
                candidate.chunker_config(),
            )
            for index, legacy_chunk in enumerate(legacy, start=1):
                result.append(
                    _legacy_retrieval_chunk(
                        document,
                        legacy_chunk,
                        index,
                        candidate,
                    )
                )
        return tuple(result)
    chunker = Chunker(
        candidate.chunker_config(),
        counter,
        pipeline_fingerprint=pipeline.index_fingerprint(),
    )
    for document in documents:
        section_chunks = chunker.chunk(
            source_id=document.source_id,
            doc_version=document.doc_version,
            elements=list(document.elements),
            metadata=document.metadata,
        )
        result.extend(
                _RetrievalChunk(
                    chunk_id=section_chunk.chunk_id,
                    source_id=section_chunk.source_id,
                    source_path=document.source_path,
                    doc_version=section_chunk.doc_version,
                text=section_chunk.text,
                embedding_text=section_chunk.embedding_text,
                locators=section_chunk.locators,
                metadata=document.metadata,
            )
            for section_chunk in section_chunks
        )
    return tuple(result)


def _legacy_retrieval_chunk(
    document: _ParsedDocument,
    chunk: LegacyElementChunk,
    index: int,
    candidate: AblationCandidate,
) -> _RetrievalChunk:
    canonical = json.dumps(
        {
            "candidate": candidate.label,
            "source_id": document.source_id,
            "element_id": chunk.element_id,
            "index": index,
            "locator": chunk.locator.logical_key(),
            "text": chunk.text,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return _RetrievalChunk(
        chunk_id=f"chunk_{digest[:32]}",
        source_id=document.source_id,
        source_path=document.source_path,
        doc_version=document.doc_version,
        text=chunk.text,
        embedding_text=chunk.embedding_text,
        locators=(chunk.locator,),
        metadata=document.metadata,
    )


def _index_retrieval_chunks(
    chunks: tuple[_RetrievalChunk, ...],
    services: _IndexingServices,
) -> None:
    vectors = services.embedding.embed(
        tuple(chunk.embedding_text for chunk in chunks),
        instruction=services.document_instruction,
    ).vectors
    points = [
        models.PointStruct(
            id=uuid.uuid5(uuid.NAMESPACE_URL, chunk.chunk_id),
            vector={
                "dense": list(vector),
                "bm25": services.sparse.embed_document(
                    chunk.embedding_text
                ),
            },
            payload=_retrieval_payload(chunk),
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    for start in range(0, len(points), 128):
        services.qdrant.upsert(
            collection_name=services.index.collection_name,
            points=points[start : start + 128],
            wait=True,
        )


def _retrieval_payload(chunk: _RetrievalChunk) -> dict[str, object]:
    payload: dict[str, object] = {
        "chunk_id": chunk.chunk_id,
        "source_id": chunk.source_id,
        "source_path": chunk.source_path,
        "doc_version": chunk.doc_version,
        "version_state": "active",
        "text": chunk.text,
        "embedding_text": chunk.embedding_text,
        "locators": [
            locator.model_dump(mode="json") for locator in chunk.locators
        ],
        "document_status": chunk.metadata.document_status,
        "authority_level": chunk.metadata.authority_level,
    }
    if chunk.metadata.effective_from is not None:
        payload["effective_from"] = (
            chunk.metadata.effective_from.isoformat()
        )
    if chunk.metadata.effective_to is not None:
        payload["effective_to"] = chunk.metadata.effective_to.isoformat()
    return payload


def _retrieval_metrics(
    cases: tuple[EvaluationCase, ...],
    services: _QueryServices,
) -> dict[str, object]:
    metadata_filter = MetadataPolicy(
        allowed_statuses=services.retrieval.allowed_statuses,
        allowed_authority_levels=(
            services.retrieval.allowed_authority_levels
        ),
    ).to_qdrant_filter(as_of=datetime.now(UTC), source_ids=())
    rerank_stage = RerankStage(
        services.reranker,
        RerankConfig(
            candidate_limit=services.retrieval.candidate_limit,
            final_limit=max(5, services.retrieval.final_limit),
            max_final_limit=max(
                5,
                services.retrieval.max_final_limit,
            ),
        ),
    )
    results: list[
        tuple[
            EvaluationCase,
            tuple[FusedHit, ...],
            tuple[FusedHit, ...],
        ]
    ] = []
    for case in cases:
        vector = services.embedding.embed(
            (case.question,),
            instruction=services.retrieval.query_instruction,
        ).vectors[0]
        fused = reciprocal_rank_fusion(
            {
                "dense": services.index.query_dense(
                    list(vector),
                    limit=services.retrieval.dense_limit,
                    additional_filter=metadata_filter,
                ),
                "bm25": services.index.query_sparse(
                    services.sparse.embed_query(case.question),
                    limit=services.retrieval.bm25_limit,
                    additional_filter=metadata_filter,
                ),
            },
            rank_constant=services.retrieval.rrf_rank_constant,
            limit=services.retrieval.candidate_limit,
        )
        reranked = tuple(
            item.hit
            for item in rerank_stage.rerank(case.question, fused).hits
        )
        results.append((case, fused, reranked))
    return {
        "overall": _metric_slice(results, services.document_paths),
        "categories": {
            category: _metric_slice(
                [
                    result
                    for result in results
                    if category in result[0].categories
                ],
                services.document_paths,
            )
            for category in _RETRIEVAL_CATEGORIES
        },
    }


def _metric_slice(
    results: Sequence[
        tuple[EvaluationCase, tuple[FusedHit, ...], tuple[FusedHit, ...]]
    ],
    document_paths: dict[str, str],
) -> dict[str, float | int]:
    answerable = [
        result for result in results if result[0].expected.evidence
    ]
    return {
        "cases": len(results),
        "answerable_cases": len(answerable),
        "recall_at_5": _mean(
            [
                _case_recall(case, hits[:5], document_paths)
                for case, hits, _ in answerable
            ]
        ),
        "recall_at_10": _mean(
            [
                _case_recall(case, hits[:10], document_paths)
                for case, hits, _ in answerable
            ]
        ),
        "recall_at_20": _mean(
            [
                _case_recall(case, hits[:20], document_paths)
                for case, hits, _ in answerable
            ]
        ),
        "mrr": _mean(
            [
                _case_mrr(case, hits, document_paths)
                for case, hits, _ in answerable
            ]
        ),
        "rerank_recall_at_5": _mean(
            [
                _case_recall(case, reranked[:5], document_paths)
                for case, _, reranked in answerable
            ]
        ),
    }


def _case_recall(
    case: EvaluationCase,
    hits: Sequence[FusedHit],
    document_paths: dict[str, str],
) -> float:
    labels = case.expected.evidence
    matched = sum(
        any(
            _hit_matches_label(hit, label, document_paths)
            for hit in hits
        )
        for label in labels
    )
    return _ratio(matched, len(labels))


def _case_mrr(
    case: EvaluationCase,
    hits: Sequence[FusedHit],
    document_paths: dict[str, str],
) -> float:
    for rank, hit in enumerate(hits, start=1):
        if any(
            _hit_matches_label(hit, label, document_paths)
            for label in case.expected.evidence
        ):
            return 1.0 / rank
    return 0.0


def _hit_matches_label(
    hit: FusedHit,
    label: object,
    document_paths: dict[str, str],
) -> bool:
    document_key = getattr(label, "document", None)
    locator_contains = getattr(label, "locator_contains", None)
    quote = getattr(label, "quote", None)
    if (
        not isinstance(document_key, str)
        or not isinstance(locator_contains, str)
    ):
        return False
    expected_path = document_paths.get(document_key)
    if expected_path is None:
        return False
    raw_locators = hit.payload.get("locators")
    source_path = hit.payload.get("source_path")
    text = hit.payload.get("text")
    if (
        not isinstance(raw_locators, list)
        or not isinstance(source_path, str)
        or not isinstance(text, str)
    ):
        return False
    locators = tuple(Locator.model_validate(item) for item in raw_locators)
    return (
        (
            source_path == expected_path
            or source_path.endswith(expected_path)
        )
        and any(
            locator_contains in locator.display()
            for locator in locators
        )
        and (
            quote is None
            or (isinstance(quote, str) and quote in text)
        )
    )


def _load_documents(
    input_directory: Path,
    tokenizer_path: Path,
    pipeline_path: Path,
    corpus_policy_path: Path,
) -> tuple[
    PipelineSpec,
    HuggingFaceTokenCounter,
    tuple[_ParsedDocument, ...],
]:
    pipeline = load_pipeline(pipeline_path)
    policy = CorpusPolicy.load(corpus_policy_path)
    if _sha256_file(tokenizer_path) != pipeline.embedding_tokenizer_sha256:
        raise ValueError("embedding tokenizer SHA256 与 pipeline 不一致。")
    if policy.semantic_sha256() != pipeline.corpus_policy_sha256:
        raise ValueError("corpus policy SHA256 与 pipeline 不一致。")
    if pipeline.parser_revision != DocxParser.version:
        raise ValueError("parser revision 与 DocxParser 不一致。")
    paths = sorted(
        path
        for path in input_directory.rglob("*.docx")
        if "Zone.Identifier" not in path.name
    )
    relative_paths = tuple(
        path.relative_to(input_directory).as_posix() for path in paths
    )
    metadata = policy.resolve(
        input_root=input_directory,
        discovered_paths=relative_paths,
    )
    parser = DocxParser()
    documents = []
    for path, relative_path in zip(paths, relative_paths, strict=True):
        content_sha256 = _sha256_file(path)
        documents.append(
            _ParsedDocument(
                source_id=allocate_source_id(
                    relative_path,
                    content_sha256,
                ),
                source_path=relative_path,
                doc_version=content_doc_version(content_sha256),
                elements=tuple(
                    parser.parse(path, display_path=relative_path)
                ),
                metadata=metadata[relative_path],
            )
        )
    return pipeline, HuggingFaceTokenCounter(tokenizer_path), tuple(documents)


def _summarize_legacy_candidate(
    documents: Sequence[
        tuple[Sequence[Element], Sequence[LegacyElementChunk]]
    ],
    token_counter: TokenCounter,
    config: ChunkerConfig,
) -> dict[str, object]:
    chunks = tuple(chunk for _, items in documents for chunk in items)
    elements = tuple(element for items, _ in documents for element in items)
    expected = _expected_elements(elements)
    expected_ids = {element.element_id for element in expected}
    represented_ids = {
        chunk.element_id
        for chunk in chunks
        if chunk.element_kind != ElementKind.HEADING
    }
    text_lengths = [token_counter.count(chunk.text) for chunk in chunks]
    embedding_lengths = [
        token_counter.count(chunk.embedding_text) for chunk in chunks
    ]
    source_chars = sum(len(element.text) for element in expected)
    chunk_chars = sum(
        len(chunk.text)
        for chunk in chunks
        if chunk.element_kind != ElementKind.HEADING
    )
    paragraph_chars = sum(
        len(element.text)
        for element in expected
        if element.kind == ElementKind.PARAGRAPH
    )
    paragraph_chunk_chars = sum(
        len(chunk.text)
        for chunk in chunks
        if chunk.element_kind == ElementKind.PARAGRAPH
    )
    identifiers = [
        hashlib.sha256(
            f"{chunk.element_id}\0{index}\0{chunk.text}".encode()
        ).hexdigest()
        for index, chunk in enumerate(chunks)
    ]
    return {
        "chunks": len(chunks),
        "chunks_by_role": dict(
            sorted(
                Counter(
                    _legacy_role(chunk.element_kind) for chunk in chunks
                ).items()
            )
        ),
        "text_token_lengths": _token_summary(text_lengths),
        "embedding_token_lengths": _token_summary(embedding_lengths),
        **_small_chunk_counts(text_lengths),
        "standalone_heading_chunks": sum(
            chunk.element_kind == ElementKind.HEADING for chunk in chunks
        ),
        "cross_section_chunks": 0,
        "cross_neighbor_group_links": 0,
        "hard_max_violations": sum(
            length > config.hard_max_tokens for length in text_lengths
        ),
        "uncovered_source_elements": len(expected_ids - represented_ids),
        "source_coverage_ratio": _ratio(
            len(expected_ids & represented_ids),
            len(expected_ids),
        ),
        "duplicated_source_characters": max(0, chunk_chars - source_chars),
        "duplicate_source_character_ratio": _ratio(
            max(0, chunk_chars - source_chars),
            source_chars,
        ),
        "ordinary_duplicated_source_characters": max(
            0,
            paragraph_chunk_chars - paragraph_chars,
        ),
        "ordinary_duplicate_source_character_ratio": _ratio(
            max(0, paragraph_chunk_chars - paragraph_chars),
            paragraph_chars,
        ),
        "table_row_split_violations": _legacy_table_violations(
            documents,
            token_counter,
            config.hard_max_tokens,
        ),
        "blank_chunks": sum(not chunk.text.strip() for chunk in chunks),
        "duplicate_chunk_ids": len(identifiers) - len(set(identifiers)),
        "ambiguous_quote_locator_cases": 0,
        "quote_locator_contract_violations": 0,
        **_numbering_counts(elements),
    }


def _parser_counts(
    documents: tuple[_ParsedDocument, ...],
) -> dict[str, int]:
    elements = tuple(
        element for document in documents for element in document.elements
    )
    kinds = Counter(element.kind for element in elements)
    return {
        "documents": len(documents),
        "headings": kinds[ElementKind.HEADING],
        "paragraphs": kinds[ElementKind.PARAGRAPH],
        "tables": kinds[ElementKind.TABLE],
        "images": kinds[ElementKind.IMAGE],
        "unique_media": len(
            {
                element.content_sha256
                for element in elements
                if element.kind == ElementKind.IMAGE
            }
        ),
        "blank_elements": sum(
            element.kind != ElementKind.IMAGE and not element.text.strip()
            for element in elements
        ),
    }


def _cross_section_chunks(chunks: Sequence[Chunk]) -> int:
    return sum(
        len(
            {
                (
                    span.locator.heading_path,
                    span.locator.heading_index,
                )
                for span in chunk.source_spans
            }
        )
        > 1
        for chunk in chunks
    )


def _cross_group_links(chunks: Sequence[Chunk]) -> int:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    violations = 0
    for chunk in chunks:
        for linked_id in (
            chunk.previous_chunk_id,
            chunk.next_chunk_id,
        ):
            if linked_id is None:
                continue
            linked = by_id.get(linked_id)
            if (
                linked is None
                or linked.neighbor_group_id != chunk.neighbor_group_id
            ):
                violations += 1
    return violations


def _table_row_split_violations(
    chunks: Sequence[Chunk],
    elements: dict[str, Element],
    token_counter: TokenCounter,
    hard_max_tokens: int,
) -> int:
    violations: set[tuple[str, int, int]] = set()
    for chunk in chunks:
        if chunk.chunk_role != ChunkRole.TABLE:
            continue
        for span in chunk.source_spans:
            element = elements.get(span.element_id)
            if element is None or element.kind != ElementKind.TABLE:
                violations.add(
                    (
                        span.element_id,
                        span.source_start_char,
                        span.source_end_char,
                    )
                )
                continue
            if _span_splits_normal_row(
                element.text,
                span.source_start_char,
                span.source_end_char,
                token_counter,
                hard_max_tokens,
            ):
                violations.add(
                    (
                        span.element_id,
                        span.source_start_char,
                        span.source_end_char,
                    )
                )
    return len(violations)


def _span_splits_normal_row(
    text: str,
    start: int,
    end: int,
    token_counter: TokenCounter,
    hard_max_tokens: int,
) -> bool:
    cursor = 0
    for row in text.splitlines(keepends=True):
        row_text = row.rstrip("\r\n")
        row_start = cursor
        row_end = cursor + len(row_text)
        cursor += len(row)
        overlaps = start < row_end and end > row_start
        if (
            overlaps
            and (start > row_start or end < row_end)
            and token_counter.count(row_text) <= hard_max_tokens
        ):
            return True
    return False


def _legacy_table_violations(
    documents: Sequence[
        tuple[Sequence[Element], Sequence[LegacyElementChunk]]
    ],
    token_counter: TokenCounter,
    hard_max_tokens: int,
) -> int:
    violations = 0
    for elements, chunks in documents:
        by_element: dict[str, list[str]] = defaultdict(list)
        for chunk in chunks:
            if chunk.element_kind == ElementKind.TABLE:
                by_element[chunk.element_id].append(chunk.text)
        for element in elements:
            if element.kind != ElementKind.TABLE:
                continue
            for row in element.text.splitlines():
                if (
                    token_counter.count(row) <= hard_max_tokens
                    and not any(
                        row in text
                        for text in by_element.get(element.element_id, [])
                    )
                ):
                    violations += 1
    return violations


def _ambiguous_quote_cases(chunks: Sequence[Chunk]) -> int:
    cases = 0
    for chunk in chunks:
        by_text: dict[str, set[str]] = defaultdict(set)
        for span in chunk.source_spans:
            span_text = chunk.text[span.start_char : span.end_char]
            by_text[span_text].add(span.locator.logical_key())
        cases += sum(
            len(locators) > 1 and bool(text)
            for text, locators in by_text.items()
        )
    return cases


def _quote_locator_contract_violations(chunks: Sequence[Chunk]) -> int:
    violations = 0
    for chunk in chunks:
        try:
            validate_chunk_source_spans(
                chunk.text,
                chunk.locators,
                chunk.source_spans,
            )
        except ValueError:
            violations += 1
    return violations


def _expected_elements(elements: Sequence[Element]) -> tuple[Element, ...]:
    return tuple(
        element
        for element in elements
        if element.kind != ElementKind.HEADING
        and bool(element.text.strip())
        and (
            element.kind != ElementKind.IMAGE
            or element.ocr_state
            in {OcrState.SUCCEEDED, OcrState.LOW_CONFIDENCE}
        )
    )


def _numbering_counts(elements: Sequence[Element]) -> dict[str, int]:
    detected = sum(
        element.kind == ElementKind.PARAGRAPH
        and element.list_level is not None
        for element in elements
    )
    return {
        "automatic_numbering_paragraphs_detected": detected,
        "automatic_numbering_markers_not_represented_in_text": detected,
    }


def _legacy_role(kind: ElementKind) -> str:
    return {
        ElementKind.HEADING: "heading",
        ElementKind.PARAGRAPH: ChunkRole.TEXT.value,
        ElementKind.TABLE: ChunkRole.TABLE.value,
        ElementKind.IMAGE: ChunkRole.OCR.value,
    }[kind]


def _token_summary(lengths: list[int]) -> dict[str, int]:
    if not lengths:
        return {
            "count": 0,
            "minimum": 0,
            "p50": 0,
            "p90": 0,
            "p95": 0,
            "maximum": 0,
        }
    return summarize_token_lengths(lengths)


def _small_chunk_counts(lengths: list[int]) -> dict[str, int | float]:
    below_32 = sum(length < _SMALL_CHUNK_32 for length in lengths)
    below_64 = sum(length < _SMALL_CHUNK_64 for length in lengths)
    return {
        "chunks_below_32_tokens": below_32,
        "chunks_below_32_ratio": _ratio(below_32, len(lengths)),
        "chunks_below_64_tokens": below_64,
        "chunks_below_64_ratio": _ratio(below_64, len(lengths)),
    }


def _candidate_payload(
    candidate: AblationCandidate,
    report: dict[str, object],
) -> dict[str, object]:
    return {
        "candidate": candidate.label,
        "strategy": candidate.strategy,
        "config": {
            "target_tokens": candidate.target_tokens,
            "hard_max_tokens": candidate.hard_max_tokens,
            "overlap_tokens": candidate.overlap_tokens,
        },
        "report": report,
    }


def _candidate_fingerprint(
    pipeline: PipelineSpec,
    candidate: AblationCandidate,
) -> str:
    payload = {
        "base_pipeline": pipeline.model_dump(mode="json"),
        "candidate": {
            "strategy": candidate.strategy,
            "target_tokens": candidate.target_tokens,
            "hard_max_tokens": candidate.hard_max_tokens,
            "overlap_tokens": candidate.overlap_tokens,
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _require_section_candidates(
    candidates: tuple[AblationCandidate, ...],
) -> None:
    if not candidates or any(
        candidate.strategy != "section_pack_v2"
        for candidate in candidates
    ):
        raise ValueError("至少提供一个 section-pack-v2 候选。")
    if len({candidate.label for candidate in candidates}) != len(candidates):
        raise ValueError("候选 label 不得重复。")


def _require_document_map(
    cases: tuple[EvaluationCase, ...],
    document_paths: dict[str, str],
) -> None:
    required = {
        label.document
        for case in cases
        for label in case.expected.evidence
    }
    if required - set(document_paths):
        raise ValueError("document map 缺少 tuning 证据文档键。")
    if any(not key or not value for key, value in document_paths.items()):
        raise ValueError("document map 的键和值必须非空。")


def _pool(
    endpoints: tuple[str, ...],
    client: httpx.Client,
) -> ResilientHttpPool:
    return ResilientHttpPool(
        endpoints,
        client=client,
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=2,
            cooldown_seconds=30,
            max_concurrency=1,
        ),
    )


def _mean(values: Sequence[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _prefixed_file_sha256(path: Path) -> str:
    """计算普通证据文件的带算法前缀摘要。"""
    if not path.is_file() or path.is_symlink():
        raise ValueError("calibration 证据必须是普通文件。")
    return f"sha256:{_sha256_file(path)}"


def _calibration_identity(
    *,
    calibration_source_revision: str,
    pipeline_path: Path,
    pipeline: PipelineSpec,
    corpus_manifest_path: Path,
    corpus: CorpusManifest,
) -> dict[str, str]:
    """构造结构与检索报告共享的不可泄密身份。

    Args:
        calibration_source_revision: 本次执行的 Git revision。
        pipeline_path: 本次加载的 operator pipeline 文件。
        pipeline: 已完成严格校验的 pipeline。
        corpus_manifest_path: 已核验 exact DOCX set 的清单文件。
        corpus: 已核验的 canonical corpus 清单。

    Returns:
        不含文件名、原文、endpoint 或密钥的身份字段。

    Raises:
        ValueError: calibration revision 不是 40 位小写十六进制。

    """
    if re.fullmatch(r"[0-9a-f]{40}", calibration_source_revision) is None:
        raise ValueError("calibration source revision 必须是 40 位 Git SHA。")
    return {
        "schema_version": "1",
        "calibration_source_revision": calibration_source_revision,
        "pipeline_file_sha256": _prefixed_file_sha256(pipeline_path),
        "pipeline_index_fingerprint": pipeline.index_fingerprint(),
        "corpus_id": corpus.corpus_id,
        "corpus_digest": corpus.corpus_digest,
        "corpus_manifest_sha256": _prefixed_file_sha256(
            corpus_manifest_path
        ),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_directory", type=Path)
    parser.add_argument(
        "--mode",
        choices=("structural", "retrieval"),
        required=True,
    )
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--corpus-policy", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--calibration-source-revision", required=True)
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--retrieval-config", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--fleet-report", type=Path)
    parser.add_argument("--model-contract-directory", type=Path)
    parser.add_argument("--qdrant-url")
    parser.add_argument("--embedding-endpoint", action="append", default=[])
    parser.add_argument("--reranker-endpoint", action="append", default=[])
    parser.add_argument("--ocr-endpoint", action="append", default=[])
    parser.add_argument(
        "--qdrant-api-key-env",
        default="RAG_QDRANT_API_KEY",
    )
    parser.add_argument(
        "--embedding-api-token-env",
        default="RAG_EMBEDDING_API_TOKEN",
    )
    parser.add_argument(
        "--reranker-api-token-env",
        default="RAG_RERANKER_API_TOKEN",
    )
    parser.add_argument(
        "--ocr-api-token-env",
        default="RAG_OCR_API_TOKEN",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    """执行 structural 或真实 tuning-only retrieval 消融。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    arguments = _arguments()
    candidates = tuple(
        parse_candidate(raw) for raw in arguments.candidate
    ) or DEFAULT_SECTION_CANDIDATES
    inputs = AblationInputFiles(
        input_directory=arguments.input_directory,
        tokenizer_path=arguments.tokenizer,
        pipeline_path=arguments.pipeline,
        corpus_policy_path=arguments.corpus_policy,
    )
    if arguments.mode == "structural":
        result = run_structural_ablation(
            inputs,
            CalibrationEvidenceFiles(
                calibration_source_revision=(
                    arguments.calibration_source_revision
                ),
                corpus_manifest_path=arguments.corpus_manifest,
            ),
            candidates=candidates,
        )
    else:
        required = {
            "--retrieval-config": arguments.retrieval_config,
            "--dataset": arguments.dataset,
            "--fleet-report": arguments.fleet_report,
            "--model-contract-directory": (
                arguments.model_contract_directory
            ),
            "--qdrant-url": arguments.qdrant_url,
            "--embedding-endpoint": arguments.embedding_endpoint,
            "--reranker-endpoint": arguments.reranker_endpoint,
            "--ocr-endpoint": arguments.ocr_endpoint,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "retrieval mode 缺少参数：" + ",".join(sorted(missing))
            )
        qdrant_key = os.environ.get(arguments.qdrant_api_key_env)
        if not qdrant_key:
            raise ValueError("retrieval mode 缺少 Qdrant API key 环境变量。")
        result = run_retrieval_ablation(
            inputs,
            RetrievalEvidenceFiles(
                calibration_source_revision=(
                    arguments.calibration_source_revision
                ),
                corpus_manifest_path=arguments.corpus_manifest,
                retrieval_path=arguments.retrieval_config,
                dataset_path=arguments.dataset,
                fleet_report_path=arguments.fleet_report,
                model_contract_directory=(
                    arguments.model_contract_directory
                ),
            ),
            RetrievalEnvironment(
                qdrant_url=arguments.qdrant_url,
                qdrant_api_key=qdrant_key,
                embedding_endpoints=tuple(arguments.embedding_endpoint),
                reranker_endpoints=tuple(arguments.reranker_endpoint),
                ocr_endpoints=tuple(arguments.ocr_endpoint),
                embedding_api_token=os.environ.get(
                    arguments.embedding_api_token_env
                ),
                reranker_api_token=os.environ.get(
                    arguments.reranker_api_token_env
                ),
                ocr_api_token=os.environ.get(
                    arguments.ocr_api_token_env
                ),
                document_paths=dict(
                    load_dataset(arguments.dataset).documents
                ),
            ),
            candidates=candidates,
        )
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if arguments.output is None:
        print(rendered)
    else:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
