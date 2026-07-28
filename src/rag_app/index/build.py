"""从受控 DOCX 目录构建可写入 Qdrant 的完整分块集合。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rag_app.chunking import Chunker
from rag_app.clients.model_services import EmbeddingResult
from rag_app.clients.resilience import (
    ExternalRequestRejectedError,
    ExternalServiceUnavailableError,
)
from rag_app.contracts import (
    DocumentMetadata,
    Element,
    ElementKind,
    OcrState,
    Parser,
)
from rag_app.index.planner import DiscoveredSource
from rag_app.index.qdrant import IndexedChunk
from rag_app.ocr.models import OcrResponse
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.state import (
    MediaReference,
    OcrResult,
    SourceVersion,
    StateStore,
)

__all__ = [
    "DocxBuildConfig",
    "DocxBuildServices",
    "DocxChunkBuilder",
    "discover_docx_sources",
]

_HASH_BLOCK_BYTES = 1024 * 1024
_PENDING_OCR_ERROR = "GPU_OCR_PENDING_SELECTION"


class _EmbeddingClient(Protocol):
    """索引构建所需的最小 embedding 接口。"""

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        instruction: str,
    ) -> EmbeddingResult:
        """按输入顺序返回向量。

        Args:
            texts: 待向量化文本。
            instruction: 冻结的文档 embedding 指令。

        Returns:
            向量与调用审计。

        """


class _OcrClient(Protocol):
    """索引构建所需的最小 OCR 接口。"""

    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> OcrResponse:
        """按媒体和 revision 返回严格 OCR 结果。

        Args:
            media_bytes: 原始媒体字节。
            media_type: 已验证的媒体 MIME 类型。
            media_sha256: 原始媒体内容摘要。

        Returns:
            严格 OCR 响应。

        """


@dataclass(frozen=True, slots=True)
class DocxBuildConfig:
    """DOCX 构建的路径与冻结文本配置。"""

    input_root: Path
    ocr_revision: str
    embedding_instruction: str
    metadata_by_source: Mapping[str, DocumentMetadata]
    minimum_ocr_confidence: float = 0.80


@dataclass(frozen=True, slots=True)
class DocxBuildServices:
    """DOCX 构建所需的可替换服务集合。"""

    parser: Parser
    chunker: Chunker
    embedder: _EmbeddingClient
    sparse_encoder: QdrantBm25Encoder
    state: StateStore
    ocr_client: _OcrClient | None = None


class DocxChunkBuilder:
    """解析、登记待处理媒体、切块并完成 dense/sparse 编码。"""

    def __init__(
        self,
        *,
        config: DocxBuildConfig,
        services: DocxBuildServices,
    ) -> None:
        """保存构建依赖。

        Args:
            config: 输入路径、OCR revision 与 embedding 指令。
            services: 解析、切块、编码和状态持久化依赖。

        """
        self._input_root = config.input_root.resolve(strict=True)
        self._parser = services.parser
        self._chunker = services.chunker
        self._embedder = services.embedder
        self._sparse_encoder = services.sparse_encoder
        self._state = services.state
        self._ocr_client = services.ocr_client
        self._ocr_revision = config.ocr_revision
        self._metadata_by_source = dict(config.metadata_by_source)
        if not 0.0 <= config.minimum_ocr_confidence <= 1.0:
            raise ValueError("OCR 最低置信度必须位于 [0,1]。")
        self._minimum_ocr_confidence = config.minimum_ocr_confidence
        self._embedding_instruction = config.embedding_instruction

    def __call__(
        self,
        source_path: str,
        version: SourceVersion,
    ) -> tuple[IndexedChunk, ...]:
        """构建一个不可变来源版本。

        Args:
            source_path: 相对输入根目录的稳定展示路径。
            version: 已持久化的 staging 来源版本。

        Returns:
            完成 dense 与 sparse 编码的全部非 OCR 分块。

        Raises:
            ValueError: 路径越界、来源身份不一致或文件在构建中变化。

        """
        path = _safe_source_path(self._input_root, source_path)
        metadata = self._metadata_by_source.get(source_path)
        if metadata is None:
            raise ValueError("source_path 缺少已解析的 corpus policy 元数据。")
        before = _file_identity(path)
        if before[0] != version.content_sha256:
            raise ValueError("DOCX 内容摘要与冻结同步计划不一致。")
        elements = self._parser.parse(path, display_path=source_path)
        after = _file_identity(path)
        if before != after:
            raise ValueError("DOCX 在解析期间发生变化。")

        elements = self._process_images(elements, version)
        chunks = self._chunker.chunk(
            version.source_id,
            version.doc_version,
            elements,
            metadata=metadata,
        )
        embeddings = self._embedder.embed(
            tuple(chunk.embedding_text for chunk in chunks),
            instruction=self._embedding_instruction,
        )
        if len(embeddings.vectors) != len(chunks):
            raise ValueError("embedding 数量与 chunk 数量不一致。")
        return tuple(
            IndexedChunk(
                chunk=chunk,
                dense=list(vector),
                sparse=self._sparse_encoder.embed_document(
                    chunk.embedding_text
                ),
            )
            for chunk, vector in zip(
                chunks,
                embeddings.vectors,
                strict=True,
            )
        )

    def _process_images(
        self,
        elements: list[Element],
        version: SourceVersion,
    ) -> list[Element]:
        processed = []
        for element in elements:
            if element.kind != ElementKind.IMAGE:
                processed.append(element)
                continue
            if element.binary_data is None:
                raise ValueError("图片元素缺少原始媒体数据。")
            if element.media_type is None:
                raise ValueError("图片元素缺少媒体类型。")
            result = self._ocr_result(element)
            self._state.record_ocr_result(result)
            self._state.record_media_reference(
                MediaReference(
                    source_id=version.source_id,
                    doc_version=version.doc_version,
                    element_id=element.element_id,
                    media_sha256=element.content_sha256,
                    media_type=element.media_type,
                    media_name=element.media_name,
                    locator=element.locator.display(),
                    ocr_revision=self._ocr_revision,
                    state=result.state,
                    error_code=result.error_code,
                )
            )
            if result.text:
                processed.append(
                    element.model_copy(
                        update={
                            "text": result.text,
                            "ocr_state": OcrState(result.state),
                            "ocr_confidence": result.confidence,
                            "ocr_error": result.error_code,
                        }
                    )
                )
            else:
                processed.append(
                    element.model_copy(
                        update={
                            "ocr_state": OcrState(result.state),
                            "ocr_confidence": result.confidence,
                            "ocr_error": result.error_code,
                        }
                    )
                )
        return processed

    def _ocr_result(self, element: Element) -> OcrResult:
        cached = self._state.get_ocr_result(
            element.content_sha256,
            self._ocr_revision,
        )
        if cached is not None:
            return cached
        if self._ocr_client is None:
            return OcrResult(
                media_sha256=element.content_sha256,
                ocr_revision=self._ocr_revision,
                state=OcrState.PENDING.value,
                text=None,
                confidence=None,
                error_code=_PENDING_OCR_ERROR,
            )
        if element.binary_data is None or element.media_type is None:
            raise ValueError("图片元素缺少 OCR 所需媒体字段。")
        try:
            response = self._ocr_client.recognize(
                element.binary_data,
                media_type=element.media_type,
                media_sha256=element.content_sha256,
            )
        except (
            ExternalServiceUnavailableError,
            ExternalRequestRejectedError,
            ValueError,
        ) as error:
            return OcrResult(
                media_sha256=element.content_sha256,
                ocr_revision=self._ocr_revision,
                state=OcrState.FAILED.value,
                text=None,
                confidence=None,
                error_code=_ocr_error_code(error),
            )
        if not response.text.strip():
            return OcrResult(
                media_sha256=element.content_sha256,
                ocr_revision=self._ocr_revision,
                state=OcrState.FAILED.value,
                text=None,
                confidence=response.confidence,
                error_code="OCR_NO_TEXT",
            )
        state = (
            OcrState.SUCCEEDED
            if response.confidence >= self._minimum_ocr_confidence
            else OcrState.LOW_CONFIDENCE
        )
        return OcrResult(
            media_sha256=element.content_sha256,
            ocr_revision=self._ocr_revision,
            state=state.value,
            text=response.text,
            confidence=response.confidence,
            error_code=(
                None
                if state == OcrState.SUCCEEDED
                else "OCR_LOW_CONFIDENCE"
            ),
        )


def discover_docx_sources(input_root: Path) -> tuple[DiscoveredSource, ...]:
    """递归发现受控目录中的 DOCX，并计算稳定内容摘要。

    Args:
        input_root: 只读输入根目录。

    Returns:
        按 POSIX 相对路径排序的目录快照。

    Raises:
        ValueError: 根目录无效、文件是符号链接或解析后越界。

    """
    root = input_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("DOCX 输入根路径必须是目录。")
    discovered: list[DiscoveredSource] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if (
            not path.is_file()
            or path.suffix.lower() != ".docx"
            or "Zone.Identifier" in path.name
        ):
            continue
        if path.is_symlink():
            raise ValueError("DOCX 输入不能是符号链接。")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("DOCX 输入路径越出根目录。")
        relative = resolved.relative_to(root).as_posix()
        discovered.append(
            DiscoveredSource(
                source_path=relative,
                content_sha256=_sha256_file(resolved),
            )
        )
    return tuple(discovered)


def _safe_source_path(input_root: Path, source_path: str) -> Path:
    relative = Path(source_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source_path 必须是安全相对路径。")
    path = input_root / relative
    if path.is_symlink():
        raise ValueError("DOCX 输入不能是符号链接。")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(input_root):
        raise ValueError("source_path 越出输入根目录。")
    return resolved


def _ocr_error_code(error: Exception) -> str:
    if isinstance(error, ExternalServiceUnavailableError):
        return "OCR_SERVICE_UNAVAILABLE"
    if isinstance(error, ExternalRequestRejectedError):
        return "OCR_REQUEST_REJECTED"
    return "OCR_RESPONSE_INVALID"


def _file_identity(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return (_sha256_file(path), stat.st_size, stat.st_mtime_ns)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()
