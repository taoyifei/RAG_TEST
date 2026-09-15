"""轻量 PaddleOCR 官方 API 客户端的线级合同。"""

from __future__ import annotations

import hashlib
import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.parsers.pdf.official_api_client import (
    AuthError,
    OfficialPaddleOcrApiClient,
    RateLimitError,
    ServiceUnavailableError,
)

_TOKEN = hashlib.sha256(b"public-contract-token").hexdigest()
_BASE_URL = "https://paddle.test"
_RESULT_URL = "https://result.test/result.jsonl"


def _multipart_fields(request: httpx.Request) -> dict[str, bytes]:
    content_type = request.headers["content-type"]
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.content
    )
    message = BytesParser(policy=default).parsebytes(envelope)
    return {
        str(part.get_param("name", header="content-disposition")): (
            part.get_payload(decode=True) or b""
        )
        for part in message.iter_parts()
    }


def test_document_api_matches_official_submit_poll_and_result_contract(
    tmp_path: Path,
) -> None:
    source = tmp_path / "公开合成.pdf"
    source.write_bytes(b"%PDF-1.7 public synthetic")
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            observed["submit_auth"] = request.headers.get("authorization")
            observed["fields"] = _multipart_fields(request)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"jobId": "job-document"}},
            )
        if request.url.host == "paddle.test":
            observed["poll_auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": _RESULT_URL},
                    },
                },
            )
        observed["result_auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "result": {
                        "dataInfo": {"pages": 1},
                        "layoutParsingResults": [
                            {
                                "markdown": {"text": "第十二页答案"},
                                "prunedResult": {
                                    "width": 100,
                                    "height": 200,
                                    "parsing_res_list": [],
                                },
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
        )

    with OfficialPaddleOcrApiClient(
        token=_TOKEN,
        base_url=_BASE_URL,
        transport=httpx.MockTransport(handle),
    ) as client:
        result = client.parse_document(
            file_path=str(source),
            model="PaddleOCR-VL-1.6",
            options={
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_layout_detection": True,
                "use_chart_recognition": False,
                "prettify_markdown": False,
                "return_markdown_images": False,
            },
            page_ranges="12",
        )

    fields = observed["fields"]
    assert isinstance(fields, dict)
    assert observed["submit_auth"] == f"Bearer {_TOKEN}"
    assert observed["poll_auth"] == f"Bearer {_TOKEN}"
    assert observed["result_auth"] is None
    assert fields["model"] == b"PaddleOCR-VL-1.6"
    assert fields["pageRanges"] == b"12"
    assert json.loads(fields["optionalPayload"]) == {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useLayoutDetection": True,
        "useChartRecognition": False,
        "prettifyMarkdown": False,
        "returnMarkdownImages": False,
    }
    assert fields["file"] == source.read_bytes()
    assert result.job_id == "job-document"
    assert result.data_info == {"pages": 1}
    assert result.pages[0].markdown_text == "第十二页答案"


def test_ocr_api_parses_official_ppocrv6_result(tmp_path: Path) -> None:
    source = tmp_path / "critical.png"
    source.write_bytes(b"public synthetic png")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            fields = _multipart_fields(request)
            assert fields["model"] == b"PP-OCRv6"
            assert json.loads(fields["optionalPayload"]) == {
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useTextlineOrientation": False,
            }
            return httpx.Response(
                200,
                json={"code": 0, "data": {"jobId": "job-ocr"}},
            )
        if request.url.host == "paddle.test":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": _RESULT_URL},
                    },
                },
            )
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "result": {
                        "ocrResults": [
                            {"prunedResult": {"rec_texts": ["-12.5 °C"]}}
                        ]
                    }
                },
                ensure_ascii=False,
            ),
        )

    with OfficialPaddleOcrApiClient(
        token=_TOKEN,
        base_url=_BASE_URL,
        transport=httpx.MockTransport(handle),
    ) as client:
        result = client.ocr(
            file_path=str(source),
            options={
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            },
        )

    assert result.job_id == "job-ocr"
    assert result.pages[0].pruned_result == {"rec_texts": ["-12.5 °C"]}


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (401, AuthError),
        (429, RateLimitError),
        (503, ServiceUnavailableError),
    ],
)
def test_http_failures_keep_official_error_types(
    tmp_path: Path,
    status_code: int,
    expected_type: type[Exception],
) -> None:
    source = tmp_path / "公开合成.pdf"
    source.write_bytes(b"%PDF-1.7 public synthetic")

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "synthetic"})

    with (
        OfficialPaddleOcrApiClient(
            token=_TOKEN,
            base_url=_BASE_URL,
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(expected_type),
    ):
        client.parse_document(file_path=str(source))
