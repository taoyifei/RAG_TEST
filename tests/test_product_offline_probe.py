from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from rag_app.product import offline_probe


def test_offline_probe_completes_internal_product_workflow(
    tmp_path: Path,
) -> None:
    token = hashlib.sha256(b"offline-probe-test").hexdigest()
    token_file = tmp_path / "bootstrap-token"
    token_file.write_text(token, encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/live":
            response = httpx.Response(200, json={"status": "live"})
        elif request.method == "GET" and request.url.path == "/":
            response = httpx.Response(200, text='<div id="root"></div>')
        elif request.url.path == "/api/v1/console/session":
            assert token.encode() in request.content
            response = httpx.Response(
                200,
                json={"csrf_token": "csrf-test"},
                headers={"set-cookie": "rag_session=test; Path=/; HttpOnly"},
            )
        else:
            assert request.headers.get("cookie") == "rag_session=test"
            if request.url.path == "/api/v1/system/components":
                response = httpx.Response(
                    200,
                    json=[{"component_id": "offline"}],
                )
            elif request.url.path == "/api/v1/jobs/job_test":
                response = httpx.Response(
                    200,
                    json={
                        "state": "succeeded",
                        "revision_id": "irev_test",
                    },
                )
            else:
                assert request.headers.get("origin") == (
                    "http://127.0.0.1:38123"
                )
                assert request.headers.get("x-csrf-token") == "csrf-test"
                response = write_response(request)
        return response

    def write_response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/projects":
            response = httpx.Response(200, json={"project_id": "prj_test"})
        elif request.url.path == "/api/v1/projects/prj_test/knowledge-bases":
            response = httpx.Response(
                200,
                json={"knowledge_base_id": "kb_test"},
            )
        elif request.url.path.endswith("/documents"):
            with zipfile.ZipFile(io.BytesIO(request.content)) as archive:
                document = archive.read("word/document.xml").decode("utf-8")
            assert "V305-OFFLINE-ALPHA-739" in document
            response = httpx.Response(200, json={"job_id": "job_test"})
        elif request.url.path.endswith(":search"):
            response = httpx.Response(
                200,
                json={
                    "active_index_revision_id": "irev_test",
                    "evidence": [{"chunk_id": "chunk_test"}],
                    "knowledge_base_id": "kb_test",
                    "project_id": "prj_test",
                    "related_contents": [],
                    "trace_id": "trace_" + "f" * 32,
                },
            )
        else:
            raise AssertionError(
                f"unexpected request: {request.method} {request.url}"
            )
        return response

    result = offline_probe.run_offline_product_probe(
        base_url="http://127.0.0.1:8088",
        request_origin="http://127.0.0.1:38123",
        bootstrap_token_file=token_file,
        transport=httpx.MockTransport(handler),
    )

    assert result.active_revision_id == "irev_test"
    assert result.trace_id == "trace_" + "f" * 32
    assert calls[0] == ("GET", "/live")
    assert calls[-1][1].endswith(":search")


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8088",
        "http://example.invalid:8088",
        "http://127.0.0.1:8088/private",
    ],
)
def test_offline_probe_rejects_non_loopback_or_ambiguous_url(
    tmp_path: Path,
    value: str,
) -> None:
    token_file = tmp_path / "bootstrap-token"
    token_file.write_text("x" * 32, encoding="utf-8")

    with pytest.raises(offline_probe.OfflineProductProbeError, match="回环"):
        offline_probe.run_offline_product_probe(
            base_url=value,
            request_origin="http://127.0.0.1:38123",
            bootstrap_token_file=token_file,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
