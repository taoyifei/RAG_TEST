"""DOCX 与旧版二进制 DOC 的统一安全 Parser adapter。"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import InvalidDocument
from rag_app.core.identifiers import document_version_id, node_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentSource,
    DocumentVersionRef,
    NodeKind,
    ParseContext,
    ParsedArtifact,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    SourceAnchor,
    StoryKind,
    text_payload,
)
from rag_app.core.policies import ParsingPolicy

_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"
_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ANTIWORD_EXECUTABLE = "/usr/bin/antiword"
_PRLIMIT_EXECUTABLE = "/usr/bin/prlimit"
_ANTIWORD_PACKAGE_VERSION = "0.37-17"
_MAX_PROCESS_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
_PART_URI = "/legacy-word/document.txt"

DocTextExtractor = Callable[[bytes, ParsingPolicy], str]


class WordDocumentV1Parser:
    """按真实格式路由 DOCX 与旧二进制 DOC。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="word-document-v1",
        version=(f"1.0.0+docx-4.0.0.antiword-{_ANTIWORD_PACKAGE_VERSION}"),
        mode=ProviderMode.LOCAL,
        capabilities=ComponentCapabilities(
            formats=(_DOCX_MEDIA_TYPE, _DOC_MEDIA_TYPE)
        ),
    )
    parser_capabilities = ParserCapabilities(
        supported_extensions=(".docx", ".doc"),
        supported_media_types=(_DOCX_MEDIA_TYPE, _DOC_MEDIA_TYPE),
        supports_tables="partial",
        supports_images="partial",
        supports_numbering="partial",
        supports_headers_footers="partial",
        supports_footnotes="partial",
        supports_revisions="partial",
        supports_comments="partial",
        supports_text_boxes="partial",
    )

    def __init__(
        self,
        *,
        docx_parser: DocxOoxmlV4Parser | None = None,
        doc_extractor: DocTextExtractor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建统一 Word parser。

        Args:
            docx_parser: 可注入的现有 OOXML parser。
            doc_extractor: 可注入的旧 DOC 文本提取器。
            clock: 解析报告使用的单调时钟。

        Returns:
            无返回值。

        """
        self._docx_parser = docx_parser or DocxOoxmlV4Parser(clock=clock)
        self._doc_extractor = doc_extractor or _extract_doc_text
        self._clock = clock

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """按扩展名、媒体类型和文件签名解析 Word 文档。

        Args:
            source: 受控文档字节与格式元数据。
            policy: 不可放宽资源边界的解析策略。
            context: 不进入策略指纹的逻辑文档身份。

        Returns:
            Document IR 与同一解析报告。

        Raises:
            InvalidDocument: 格式合同不匹配或解析失败。

        """
        extension = source.extension.casefold()
        if extension == ".docx":
            _require_media_type(
                source.media_type,
                allowed=frozenset({_DOCX_MEDIA_TYPE, _OCTET_STREAM_MEDIA_TYPE}),
            )
            return self._docx_parser.parse(source, policy, context)
        if extension == ".doc":
            _require_media_type(
                source.media_type,
                allowed=frozenset({_DOC_MEDIA_TYPE, _OCTET_STREAM_MEDIA_TYPE}),
            )
            return self._parse_doc(source, policy, context)
        raise InvalidDocument(
            "Word parser 仅接受 .doc 或 .docx 扩展名。",
            stage="word-document-v1.input",
        )

    def close(self) -> None:
        """关闭内部 parser 资源。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self._docx_parser.close()

    def _parse_doc(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        if len(source.content) > policy.max_file_bytes:
            raise InvalidDocument(
                "DOC 文件大小超过 ParsingPolicy 限制。",
                stage="word-document-v1.resource",
            )
        if not source.content.startswith(_OLE_COMPOUND_FILE_MAGIC):
            raise InvalidDocument(
                "DOC 文件签名无效。",
                stage="word-document-v1.input",
            )
        started_at = self._clock()
        extracted = _normalize_extracted_text(
            self._doc_extractor(source.content, policy)
        )
        paragraphs = _paragraphs(extracted)
        if not paragraphs:
            raise InvalidDocument(
                "DOC 未提取到可索引文本。",
                stage="word-document-v1.content",
            )

        document = context.document
        content_sha256 = hashlib.sha256(source.content).hexdigest()
        version_id = document_version_id(
            document.document_id,
            content_sha256,
        )
        nodes = tuple(
            _paragraph_node(version_id, order, text)
            for order, text in enumerate(paragraphs)
        )
        issue = ParseIssue(
            code="LEGACY_DOC_FLATTENED_TEXT",
            severity="warning",
            action="flattened_to_paragraphs",
            safe_message=(
                "旧版 DOC 以纯文本段落解析；表格、图片和复杂结构不保留。"
            ),
        )
        report = ParseReport(
            parser_id=self.descriptor.name,
            parser_version=self.descriptor.version,
            node_count=len(nodes),
            visible_text_nodes=len(nodes),
            represented_visible_text_nodes=len(nodes),
            part_count=1,
            story_counts=((StoryKind.BODY.value, len(nodes)),),
            issues=(issue,),
            elapsed_seconds=self._clock() - started_at,
            warnings=(issue.code,),
        )
        artifact_id = f"sha256:{content_sha256}"
        document_ir = DocumentIR(
            source=DocumentSource(
                document_id=document.document_id,
                document_version_id=version_id,
                display_name=source.display_name,
                media_type=_DOC_MEDIA_TYPE,
                extension=".doc",
                content_sha256=content_sha256,
                size_bytes=len(source.content),
                blob_ref=artifact_id,
            ),
            document=document.model_copy(
                update={"display_name": source.display_name}
            ),
            version=DocumentVersionRef(
                document_id=document.document_id,
                document_version_id=version_id,
                content_sha256=content_sha256,
            ),
            root_node_ids=tuple(node.node_id for node in nodes),
            nodes=nodes,
            parse_report=report,
            metadata=(
                ("converter", f"antiword-{_ANTIWORD_PACKAGE_VERSION}"),
                ("parsing_policy_id", policy.policy_id),
                ("source_representation", "flattened-text"),
            ),
        )
        return ParseResult(
            document_ir=document_ir,
            report=report,
            artifacts=(
                ParsedArtifact(
                    artifact_id=artifact_id,
                    content_sha256=content_sha256,
                    media_type=_DOC_MEDIA_TYPE,
                    content=source.content,
                    role="source_document",
                ),
            ),
        )


def _require_media_type(
    media_type: str,
    *,
    allowed: frozenset[str],
) -> None:
    if media_type.casefold() not in allowed:
        raise InvalidDocument(
            "Word 文档的扩展名与 Content-Type 不匹配。",
            stage="word-document-v1.input",
        )


def _extract_doc_text(content: bytes, policy: ParsingPolicy) -> str:
    executable = Path(_ANTIWORD_EXECUTABLE)
    limiter = Path(_PRLIMIT_EXECUTABLE)
    if not executable.is_file() or not limiter.is_file():
        raise InvalidDocument(
            "DOC 解析组件不可用。",
            stage="word-document-v1.runtime",
        )
    output_limit = min(
        policy.max_uncompressed_bytes,
        policy.max_entry_bytes,
    )
    with tempfile.TemporaryDirectory(prefix="rag-doc-") as temporary:
        temporary_path = Path(temporary)
        source_path = temporary_path / "source.doc"
        output_path = temporary_path / "extracted.txt"
        source_path.write_bytes(content)
        source_path.chmod(0o400)
        command = (
            str(limiter),
            f"--fsize={output_limit}:{output_limit}",
            (
                "--as="
                f"{_MAX_PROCESS_ADDRESS_SPACE_BYTES}:"
                f"{_MAX_PROCESS_ADDRESS_SPACE_BYTES}"
            ),
            "--",
            str(executable),
            "-m",
            "UTF-8.txt",
            "-w",
            "0",
            str(source_path),
        )
        try:
            with output_path.open("wb") as output:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=temporary_path,
                    env={
                        "HOME": str(temporary_path),
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": "/usr/bin:/bin",
                        "TMPDIR": str(temporary_path),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                try:
                    return_code = process.wait(
                        timeout=policy.parse_timeout_seconds
                    )
                except subprocess.TimeoutExpired as error:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise InvalidDocument(
                        "DOC 解析超过时间上限。",
                        stage="word-document-v1.timeout",
                    ) from error
        except OSError as error:
            raise InvalidDocument(
                "DOC 解析组件无法启动。",
                stage="word-document-v1.runtime",
            ) from error
        if return_code != 0:
            raise InvalidDocument(
                "DOC 解析失败或输出超过资源上限。",
                stage="word-document-v1.convert",
            )
        if output_path.stat().st_size > output_limit:
            raise InvalidDocument(
                "DOC 解析输出超过资源上限。",
                stage="word-document-v1.resource",
            )
        try:
            return output_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise InvalidDocument(
                "DOC 解析输出不是有效 UTF-8。",
                stage="word-document-v1.convert",
            ) from error


def _normalize_extracted_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise InvalidDocument(
            "DOC 解析输出包含无效控制字符。",
            stage="word-document-v1.convert",
        )
    return normalized


def _paragraphs(value: str) -> tuple[str, ...]:
    paragraphs: list[str] = []
    for raw_line in value.splitlines(keepends=True):
        line = raw_line.rstrip("\n\f\v")
        text = line.strip()
        if text:
            paragraphs.append(text)
    return tuple(paragraphs)


def _paragraph_node(
    version_id: str,
    order: int,
    text: str,
) -> DocumentNode:
    payload = text_payload(text)
    structural_path = ("body", f"paragraph:{order}")
    return DocumentNode(
        node_id=node_id(
            version_id,
            _PART_URI,
            structural_path,
            NodeKind.PARAGRAPH.value,
            payload.semantic_sha256,
        ),
        kind=NodeKind.PARAGRAPH,
        order=order,
        anchor=SourceAnchor(
            part_uri=_PART_URI,
            story_kind=StoryKind.BODY,
            structural_path=structural_path,
            ordinal=order,
            paragraph_index=order,
            source_start_char=0,
            source_end_char=len(text),
        ),
        text_payload=payload,
    )


__all__ = ["WordDocumentV1Parser"]
