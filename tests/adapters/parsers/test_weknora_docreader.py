"""固定 docreader gRPC 协议与解析产物来源的定向回归。"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import grpc  # type: ignore[import-untyped]
import pytest

from rag_app.adapters.parsers.document_router import WeKnoraDocumentRouter
from rag_app.adapters.parsers.docx.parser import DocxOoxmlV4Parser
from rag_app.adapters.parsers.weknora_docreader import WeKnoraDocreaderClient
from rag_app.adapters.parsers.weknora_proto import (
    docreader_pb2,
    docreader_pb2_grpc,
)
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import (
    DocumentRef,
    ParseContext,
    ParseSource,
    validate_document_ir,
)
from rag_app.core.policies import ParsingPolicy


class _Servicer(docreader_pb2_grpc.DocReaderServicer):
    """仅模拟固定协议帧，不替代真实格式解析验收。"""

    def __init__(self) -> None:
        self.last_request: docreader_pb2.ReadRequest | None = None

    def ReadStream(  # noqa: N802
        self,
        request: docreader_pb2.ReadRequest,
        context: object,
    ) -> Iterator[docreader_pb2.ReadStreamResponse]:
        del context
        self.last_request = request
        yield docreader_pb2.ReadStreamResponse(
            meta=docreader_pb2.ReadStreamMeta(
                markdown_content="# 解析标题\n\n实际内容",
                image_count=1,
            )
        )
        yield docreader_pb2.ReadStreamResponse(
            image=docreader_pb2.ImageRef(
                original_ref="image-1",
                mime_type="image/png",
                image_data=b"\x89PNG\r\n\x1a\n",
            )
        )

    def ListEngines(  # noqa: N802
        self,
        request: docreader_pb2.ListEnginesRequest,
        context: object,
    ) -> docreader_pb2.ListEnginesResponse:
        del request, context
        return docreader_pb2.ListEnginesResponse(
            engines=[
                docreader_pb2.ParserEngineInfo(
                    name="builtin", file_types=["xlsx"], available=True
                ),
                docreader_pb2.ParserEngineInfo(
                    name="markitdown",
                    file_types=["pptx", "csv"],
                    available=True,
                ),
            ]
        )


@contextmanager
def _reader_server() -> Iterator[tuple[str, _Servicer]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        servicer = _Servicer()
        docreader_pb2_grpc.add_DocReaderServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            yield f"127.0.0.1:{port}", servicer
        finally:
            server.stop(0).wait()


def _context(name: str) -> ParseContext:
    return ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
            document_id=f"doc_{'3' * 32}",
            display_name=name,
        )
    )


def test_fixed_proto_stream_and_engine_probe_on_loopback() -> None:
    with _reader_server() as (endpoint, servicer):
        client = WeKnoraDocreaderClient(endpoint)
        result = client.read_file(
            content=b"synthetic",
            file_name="资料.csv",
            file_type="csv",
            engine="markitdown",
        )
        first_request = servicer.last_request
        engines = client.list_engines()
        router = WeKnoraDocumentRouter(
            DocxOoxmlV4Parser(),
            extensions=frozenset({".pptx", ".xlsx", ".csv"}),
            endpoint=endpoint,
        )
        formats = router.probe_upload_extensions()
        presentation = io.BytesIO()
        with zipfile.ZipFile(presentation, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<presentation/>")
        parsed = router.parse(
            ParseSource(
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                ),
                display_name="资料.pptx",
                extension=".pptx",
                content=presentation.getvalue(),
            ),
            ParsingPolicy(),
            _context("资料.pptx"),
        )

    assert first_request is not None
    assert first_request.file_content == b"synthetic"
    assert first_request.url == ""
    assert first_request.config.parser_engine == "markitdown"
    assert result.markdown == "# 解析标题\n\n实际内容"
    assert len(result.images) == 1
    assert {item.name for item in engines} == {"builtin", "markitdown"}
    assert formats == frozenset({".docx", ".pptx", ".xlsx", ".csv"})
    assert parsed.report.parser_id == "weknora-docreader-markitdown"
    parsed_metadata = dict(parsed.document_ir.metadata)
    assert parsed_metadata["citation_basis"] == "parsed_artifact"
    validate_document_ir(parsed.document_ir)


def test_local_markdown_preserves_exact_parsed_artifact_offsets() -> None:
    source_text = "# 标题\n\n第一段\n第二行\n\n结尾"
    source = ParseSource(
        media_type="text/markdown",
        display_name="说明.md",
        extension=".md",
        content=source_text.encode(),
    )
    router = WeKnoraDocumentRouter(
        DocxOoxmlV4Parser(),
        extensions=frozenset({".md"}),
        endpoint="",
    )

    result = router.parse(source, ParsingPolicy(), _context("说明.md"))
    validate_document_ir(result.document_ir)
    parsed_id = dict(result.document_ir.metadata)["parsed_artifact_id"]
    parsed = next(
        artifact
        for artifact in result.artifacts
        if artifact.artifact_id == parsed_id
    )
    parsed_text = parsed.content.decode()
    assert parsed_text == source_text
    for node in result.document_ir.nodes:
        metadata = dict(node.metadata)
        start = metadata["parsed_artifact_start_char"]
        end = metadata["parsed_artifact_end_char"]
        assert node.anchor.part_uri == "/parsed/document.md"
        assert metadata["citation_basis"] == "parsed_artifact"
        assert node.text_payload.exact_text == parsed_text[start:end]
        assert node.anchor.source_start_char == 0
        assert node.anchor.source_end_char == len(node.text_payload.exact_text)


def test_unsafe_ooxml_package_is_blocked_before_remote_call() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape", "x")
    router = WeKnoraDocumentRouter(
        DocxOoxmlV4Parser(),
        extensions=frozenset({".pptx"}),
        endpoint="127.0.0.1:1",
    )
    source = ParseSource(
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        display_name="危险.pptx",
        extension=".pptx",
        content=output.getvalue(),
    )
    with pytest.raises(InvalidDocument):
        router.parse(source, ParsingPolicy(), _context("危险.pptx"))
