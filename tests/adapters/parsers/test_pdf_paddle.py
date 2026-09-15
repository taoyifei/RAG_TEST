"""PaddleOCR PDF 双路径、物理页和 Document IR 的定向合同。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfWriter

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.adapters.parsers.pdf import (
    PaddleOfficialApiPdfConfig,
    PaddleOfficialApiPdfParser,
    PaddleSelfHostedPdfConfig,
    PaddleSelfHostedPdfParser,
    PdfIrParser,
    inspect_pdf_source,
)
from rag_app.core.errors import (
    InvalidDocument,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    RagError,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    DocumentRef,
    NodeKind,
    ParseContext,
    ParseSource,
    PdfParserMode,
    SourceSpanKind,
)
from rag_app.core.policies import ParsingPolicy

_PDF_MEDIA_TYPE = "application/pdf"


def _pdf_bytes(page_count: int, *, encrypted: bool = False) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=200)
    if encrypted:
        writer.encrypt("public-synthetic-password")
    writer.write(output)
    return output.getvalue()


def _source(content: bytes, name: str = "公开合成.pdf") -> ParseSource:
    return ParseSource(
        media_type=_PDF_MEDIA_TYPE,
        display_name=name,
        extension=".pdf",
        content=content,
    )


def _official_config() -> PaddleOfficialApiPdfConfig:
    return PaddleOfficialApiPdfConfig(
        access_token=hashlib.sha256(b"public-contract-token").hexdigest()
    )


def _context() -> ParseContext:
    return ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
            document_id=f"doc_{'3' * 32}",
            display_name="公开合成.pdf",
        ),
        job_id=f"job_{'4' * 32}",
        revision_id=f"irev_{'5' * 32}",
    )


def _raw_page(
    text: str,
    *,
    block_id: str,
    label: str = "text",
    bbox: tuple[int, int, int, int] = (5, 10, 95, 40),
) -> dict[str, object]:
    return {
        "prunedResult": {
            "width": 100,
            "height": 200,
            "parsing_res_list": [
                {
                    "block_id": block_id,
                    "block_label": label,
                    "block_order": 0,
                    "block_content": text,
                    "block_bbox": list(bbox),
                }
            ],
        }
    }


def _self_hosted_parser(
    handler: httpx.MockTransport,
) -> PaddleSelfHostedPdfParser:
    client = httpx.Client(
        base_url="http://paddle.test",
        transport=handler,
    )
    return PaddleSelfHostedPdfParser(
        PaddleSelfHostedPdfConfig(base_url="http://paddle.test"),
        client=client,
    )


def test_pdf_inspection_uses_physical_pages_and_rejects_encryption() -> None:
    content = _pdf_bytes(12)
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )

    assert inspection.page_count == 12
    assert inspection.source_sha256 == hashlib.sha256(content).hexdigest()
    assert inspection.native_text_layer == (False,) * 12

    with pytest.raises(InvalidDocument) as captured:
        inspect_pdf_source(
            _source(_pdf_bytes(1, encrypted=True)),
            ParsingPolicy(),
            _context(),
        )
    assert captured.value.code == "PDF_ENCRYPTED"


def test_self_hosted_contract_sends_exact_document_options() -> None:
    content = _pdf_bytes(2)
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        _raw_page("第一页", block_id="p0"),
                        _raw_page("第二页", block_id="p1"),
                    ]
                },
            },
        )

    parser = _self_hosted_parser(httpx.MockTransport(handle))
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )
    result = parser.parse_pdf(
        _source(content), ParsingPolicy(), _context(), inspection
    )

    assert observed == {
        "file": base64.b64encode(content).decode("ascii"),
        "fileType": 0,
        "useLayoutDetection": True,
        "prettifyMarkdown": False,
        "restructurePages": False,
        "returnMarkdownImages": False,
        "visualize": False,
    }
    assert result.parser_mode is PdfParserMode.SELF_HOSTED
    assert [page.page_index for page in result.pages] == [0, 1]


def test_self_hosted_truncation_never_returns_partial_document() -> None:
    content = _pdf_bytes(12)
    progress: list[tuple[int, tuple[int, ...], bool]] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        _raw_page(f"第{index + 1}页", block_id=f"p{index}")
                        for index in range(10)
                    ]
                },
            },
        )

    parser = _self_hosted_parser(httpx.MockTransport(handle))
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )

    with pytest.raises(ProviderInvalidResponse) as captured:
        parser.parse_pdf(
            _source(content),
            ParsingPolicy(),
            _context().model_copy(
                update={
                    "page_progress": lambda parsed, failed, truncated: (
                        progress.append((parsed, failed, truncated))
                    )
                }
            ),
            inspection,
        )
    assert captured.value.code == "PDF_PARSER_TRUNCATED"
    assert progress == [(10, (10, 11), True)]


class _OfficialClient:
    """先返回缺页，再按一基 page_ranges 返回每个物理页。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def parse_document(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        page_range = kwargs.get("page_ranges")
        if page_range is None:
            return SimpleNamespace(
                pages=[_raw_page("缺页结果", block_id="partial")],
                job_id="job-whole",
            )
        page_number = int(str(page_range))
        return SimpleNamespace(
            pages=[
                _raw_page(
                    f"物理第{page_number}页",
                    block_id=f"page-{page_number}",
                )
            ],
            job_id=f"job-page-{page_number}",
        )

    def close(self) -> None:
        """合同替身没有真实资源。"""


def test_official_client_recovers_missing_pages_with_one_based_ranges() -> None:
    content = _pdf_bytes(2)
    client = _OfficialClient()
    parser = PaddleOfficialApiPdfParser(
        _official_config(),
        client_factory=lambda _token, _request, _poll: client,
    )
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )

    result = parser.parse_pdf(
        _source(content), ParsingPolicy(), _context(), inspection
    )

    assert [call.get("page_ranges") for call in client.calls] == [
        None,
        "1",
        "2",
    ]
    assert all("model" in call and "options" in call for call in client.calls)
    assert client.calls[0]["options"] == {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_layout_detection": True,
        "use_chart_recognition": False,
        "prettify_markdown": False,
        "restructure_pages": False,
        "return_markdown_images": False,
        "visualize": False,
    }
    assert [page.page_index for page in result.pages] == [0, 1]
    assert result.provider_job_ids == (
        "job-whole",
        "job-page-1",
        "job-page-2",
    )


@pytest.mark.parametrize(
    ("error_name", "expected_type", "expected_code"),
    [
        (
            "AuthError",
            ProviderAuthenticationError,
            "PADDLE_AUTHENTICATION_FAILED",
        ),
        ("RateLimitError", ProviderRateLimited, "PADDLE_RATE_LIMITED"),
        ("PollTimeoutError", ProviderInvalidResponse, "PADDLE_POLL_TIMEOUT"),
        ("JobFailedError", ProviderInvalidResponse, "PADDLE_JOB_FAILED"),
        (
            "ResultParseError",
            ProviderInvalidResponse,
            "PADDLE_RESULT_PARSE_FAILED",
        ),
    ],
)
def test_official_client_errors_have_stable_codes(
    error_name: str,
    expected_type: type[RagError],
    expected_code: str,
) -> None:
    client_error = type(error_name, (Exception,), {})

    class FailingClient:
        """抛出一种命名与官方 API Client 一致的错误。"""

        def parse_document(self, **_kwargs: object) -> object:
            raise client_error("synthetic")

        def close(self) -> None:
            """合同替身没有真实资源。"""

    parser = PaddleOfficialApiPdfParser(
        _official_config(),
        client_factory=lambda _token, _request, _poll: FailingClient(),
    )
    content = _pdf_bytes(1)
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )

    with pytest.raises(expected_type) as captured:
        parser.parse_pdf(
            _source(content), ParsingPolicy(), _context(), inspection
        )
    assert captured.value.code == expected_code


def test_pdf_tables_remain_isolated_by_page_through_existing_chunker() -> None:
    content = _pdf_bytes(2)
    pages = [
        _raw_page(
            "|名称|压力|\n|---|---|\n|高压表第1行|12 MPa|",
            block_id="table-1",
            label="table",
        ),
        _raw_page(
            "|名称|压力|\n|---|---|\n|低压表第1行|3 MPa|",
            block_id="table-1",
            label="table",
        ),
    ]

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {"layoutParsingResults": pages},
            },
        )

    parser = PdfIrParser(
        _self_hosted_parser(httpx.MockTransport(handle)),
    )
    parsed = parser.parse(_source(content), ParsingPolicy(), _context())
    table_nodes = [
        node for node in parsed.document_ir.nodes if node.kind is NodeKind.TABLE
    ]

    assert [node.anchor.page_index for node in table_nodes] == [0, 1]
    assert [node.anchor.pdf_table_id for node in table_nodes] == [
        "table-1",
        "table-1",
    ]
    assert table_nodes[0].node_id != table_nodes[1].node_id

    chunker = DocxStructuralChunker()
    chunked = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev",
                parsed.document_ir.version.document_version_id,
                chunker.fingerprint,
            ),
        ),
    )
    for phrase, expected_page in (("12 MPa", 0), ("3 MPa", 1)):
        matches = [chunk for chunk in chunked.chunks if phrase in chunk.text]
        assert len(matches) == 1
        pages_in_chunk = {
            span.source_anchor.page_index
            for span in matches[0].source_spans
            if span.span_type is SourceSpanKind.PDF_PARSED_TEXT
            and span.source_anchor is not None
        }
        assert pages_in_chunk == {expected_page}


def test_pdf_paragraph_chunks_never_cross_physical_page_boundaries() -> None:
    content = _pdf_bytes(2)
    pages = [
        _raw_page("第一页短段落", block_id="paragraph-1"),
        _raw_page("第二页短段落", block_id="paragraph-2"),
    ]

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {"layoutParsingResults": pages},
            },
        )

    parser = PdfIrParser(
        _self_hosted_parser(httpx.MockTransport(handle)),
    )
    parsed = parser.parse(_source(content), ParsingPolicy(), _context())
    chunker = DocxStructuralChunker()

    chunked = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev",
                parsed.document_ir.version.document_version_id,
                chunker.fingerprint,
            ),
        ),
    )

    assert len(chunked.chunks) == 2
    assert [
        {
            span.source_anchor.page_index
            for span in chunk.source_spans
            if span.span_type is SourceSpanKind.PDF_PARSED_TEXT
            and span.source_anchor is not None
        }
        for chunk in chunked.chunks
    ] == [{0}, {1}]


def test_pdf_heading_context_follows_physical_page_order() -> None:
    content = _pdf_bytes(2)
    pages = [
        {
            "prunedResult": {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "acid-heading",
                        "block_label": "paragraph_title",
                        "block_order": 0,
                        "block_content": "5.4 耐酸性能",
                        "block_bbox": [5, 10, 95, 30],
                    },
                    {
                        "block_id": "acid-method",
                        "block_label": "text",
                        "block_order": 1,
                        "block_content": "耐酸试验方法甲。",
                        "block_bbox": [5, 35, 95, 55],
                    },
                ],
            }
        },
        {
            "prunedResult": {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {
                        "block_id": "alkali-heading",
                        "block_label": "paragraph_title",
                        "block_order": 0,
                        "block_content": "5.5 耐碱性能",
                        "block_bbox": [5, 10, 95, 30],
                    },
                    {
                        "block_id": "alkali-method",
                        "block_label": "text",
                        "block_order": 1,
                        "block_content": "耐碱试验方法乙。",
                        "block_bbox": [5, 35, 95, 55],
                    },
                ],
            }
        },
    ]

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {"layoutParsingResults": pages},
            },
        )

    parser = PdfIrParser(
        _self_hosted_parser(httpx.MockTransport(handle)),
    )
    parsed = parser.parse(_source(content), ParsingPolicy(), _context())
    chunker = DocxStructuralChunker()
    chunks = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev",
                parsed.document_ir.version.document_version_id,
                chunker.fingerprint,
            ),
        ),
    ).chunks

    by_text = {
        chunk.citation_text: chunk
        for chunk in chunks
        if "试验方法" in chunk.citation_text
    }
    assert by_text["耐酸试验方法甲。"].heading_path[-1] == "5.4 耐酸性能"
    assert by_text["耐碱试验方法乙。"].heading_path[-1] == "5.5 耐碱性能"


def test_table_without_provider_identity_is_rejected() -> None:
    content = _pdf_bytes(1)

    def handle(_request: httpx.Request) -> httpx.Response:
        page = _raw_page("|键|值|\n|---|---|", block_id="temporary")
        result = page["prunedResult"]
        assert isinstance(result, dict)
        blocks = result["parsing_res_list"]
        assert isinstance(blocks, list)
        block = blocks[0]
        assert isinstance(block, dict)
        block.pop("block_id")
        block["block_label"] = "table"
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {"layoutParsingResults": [page]},
            },
        )

    parser = _self_hosted_parser(httpx.MockTransport(handle))
    inspection = inspect_pdf_source(
        _source(content), ParsingPolicy(), _context()
    )

    with pytest.raises(ProviderInvalidResponse) as captured:
        parser.parse_pdf(
            _source(content), ParsingPolicy(), _context(), inspection
        )
    assert captured.value.code == "PDF_TABLE_ID_MISSING"


def test_self_hosted_and_official_cache_identities_do_not_collide() -> None:
    self_hosted = PaddleSelfHostedPdfParser(
        PaddleSelfHostedPdfConfig(base_url="http://paddle.test"),
        client=httpx.Client(
            base_url="http://paddle.test",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        ),
    )
    official = PaddleOfficialApiPdfParser(
        _official_config(),
        client_factory=lambda _token, _request, _poll: _OfficialClient(),
    )

    assert self_hosted.parser_mode is PdfParserMode.SELF_HOSTED
    assert official.parser_mode is PdfParserMode.OFFICIAL_API
    assert canonical_sha256(
        (self_hosted.parser_mode, self_hosted.parser_options_identity)
    ) != canonical_sha256(
        (official.parser_mode, official.parser_options_identity)
    )
