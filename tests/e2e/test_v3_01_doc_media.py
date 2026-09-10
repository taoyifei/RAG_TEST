"""V3-01 DOC→IR→Artifact→MediaScan→Product 持久化闭环。"""

from __future__ import annotations

# ruff: noqa: E501
import hashlib
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from rag_app.adapters.parsers.doc_conversion import (
    DocConversion,
    DocConversionFailedError,
    DocConversionTimeoutError,
    SandboxedLibreOfficeConverter,
)
from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.models import NodeKind
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.adapters.parsers.docx.fixtures import build_package
from tests.ole_doc_fixture import build_ole_word_container
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_IMAGE = b"synthetic-public-image"
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/pure-image.png"/>
</Relationships>
"""
_PURE_IMAGE_BLOCK = """
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="Picture 1"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
"""


def _derived_image_docx() -> bytes:
    return build_package(
        _PURE_IMAGE_BLOCK,
        document_relationships=_RELATIONSHIPS,
        extra_entries={"word/media/pure-image.png": _IMAGE},
        content_types=_CONTENT_TYPES,
    )


def _wait_job(harness: ProductHarness, job_id: str) -> dict[str, object]:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = dict(response.json())
        if job["state"] not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError("V3-01 Product 入库超时。")


def test_pure_image_doc_persists_source_derived_media_and_survives_restart(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = _derived_image_docx()

    def convert(
        _converter: SandboxedLibreOfficeConverter,
        _content: bytes,
        _policy: object,
        _cancel_check: object = None,
    ) -> DocConversion:
        return DocConversion(derived, "synthetic", "1", "bounded-v1")

    monkeypatch.setattr(SandboxedLibreOfficeConverter, "convert", convert)
    original = build_ole_word_container()
    harness = build_product_harness(tmp_path)
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    base = f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
    settings = harness.runtime.settings
    bootstrap_token = harness.bootstrap_token
    try:
        upload = harness.client.post(
            base + "/documents",
            params={"display_name": "公开合成纯图.doc"},
            content=original,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "v3-01-pure-image",
                "Content-Type": _DOC_MEDIA_TYPE,
            },
        )
        assert upload.status_code == 202, upload.text
        job = _wait_job(harness, str(upload.json()["job_id"]))
        assert job["state"] == "succeeded", job
        document_id = str(job["document_id"])
        document = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert document.current_version_id is not None
        assert document.active_index_revision_id is not None
        version_id = document.current_version_id
        artifacts = harness.runtime.sdk.list_artifacts(
            project_id,
            knowledge_base_id,
            document_id,
            version_id,
        )
        assert {artifact.role for artifact in artifacts} == {
            "source_document",
            "derived_document",
            "embedded_media",
        }
        rows = harness.runtime.p09.control.parse_rows(
            document.active_index_revision_id
        )
        counts = harness.runtime.p09.control.revision_counts(
            document.active_index_revision_id
        )
        assert counts == (1, 0, 0)
        document_ir = rows[0][0]
        image = next(
            node.image_attributes
            for node in document_ir.nodes
            if node.kind is NodeKind.IMAGE
        )
        assert image is not None
        assert (
            document_ir.version.content_sha256
            == hashlib.sha256(original).hexdigest()
        )
        assert dict(document_ir.metadata)["media_inventory"] == "partial"
        scan = harness.client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}/ocr"
        )
        assert scan.status_code == 200, scan.text
        assert scan.json()["media_count"] == 1
        image_path = base + f"/documents/{document_id}/images/{image.blob_ref}"
        image_response = harness.client.get(image_path)
        assert image_response.status_code == 200, image_response.text
        assert image_response.content == _IMAGE
        active_revision_id = document.active_index_revision_id
    finally:
        harness.close()

    runtime = build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
    )
    client = TestClient(create_product_app(runtime))
    try:
        session = client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": bootstrap_token},
        )
        assert session.status_code == 200, session.text
        reopened = runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert reopened.active_index_revision_id == active_revision_id
        persisted = runtime.sdk.list_artifacts(
            project_id, knowledge_base_id, document_id, version_id
        )
        assert {artifact.role for artifact in persisted} == {
            "source_document",
            "derived_document",
            "embedded_media",
        }
        response = client.get(image_path)
        assert response.status_code == 200, response.text
        assert response.content == _IMAGE
        derived_artifact = next(
            artifact
            for artifact in persisted
            if artifact.role == "derived_document"
        )
        derived_blob = runtime.sdk.read_artifact(
            project_id,
            knowledge_base_id,
            document_id,
            version_id,
            derived_artifact.artifact_id,
        )
        assert derived_blob.media_type == _DOCX_MEDIA_TYPE
        assert derived_blob.content != original
    finally:
        client.close()
        runtime.close()


def test_product_retry_rebuilds_after_transient_conversion_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = _derived_image_docx()
    calls = 0

    def convert(
        _converter: SandboxedLibreOfficeConverter,
        _content: bytes,
        _policy: object,
        _cancel_check: object = None,
    ) -> DocConversion:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DocConversionTimeoutError("synthetic timeout")
        return DocConversion(derived, "synthetic", "1", "bounded-v1")

    monkeypatch.setattr(SandboxedLibreOfficeConverter, "convert", convert)
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        base = (
            f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
        )
        upload = harness.client.post(
            base + "/documents",
            params={"display_name": "公开合成重试.doc"},
            content=build_ole_word_container(),
            headers={
                **harness.write_headers,
                "Idempotency-Key": "v3-01-retry",
                "Content-Type": _DOC_MEDIA_TYPE,
            },
        )
        assert upload.status_code == 202, upload.text
        job_id = str(upload.json()["job_id"])
        failed = _wait_job(harness, job_id)
        assert failed["state"] == "failed_retryable", failed
        assert failed["stage"] == "word-document-v1.timeout"
        retry = harness.client.post(
            f"/api/v1/jobs/{job_id}:retry",
            headers=harness.write_headers,
        )
        assert retry.status_code == 200, retry.text
        completed = _wait_job(harness, job_id)
        assert completed["state"] == "succeeded", completed
        assert calls == 2
    finally:
        harness.close()


def test_failed_doc_replacement_keeps_previous_active_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = _derived_image_docx()
    fail = False

    def convert(
        _converter: SandboxedLibreOfficeConverter,
        _content: bytes,
        _policy: object,
        _cancel_check: object = None,
    ) -> DocConversion:
        if fail:
            raise DocConversionFailedError("synthetic conversion failure")
        return DocConversion(derived, "synthetic", "1", "bounded-v1")

    monkeypatch.setattr(SandboxedLibreOfficeConverter, "convert", convert)
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        base = (
            f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
        )
        upload = harness.client.post(
            base + "/documents",
            params={"display_name": "公开合成影子构建.doc"},
            content=build_ole_word_container(marker=1),
            headers={
                **harness.write_headers,
                "Idempotency-Key": "v3-01-shadow-initial",
                "Content-Type": _DOC_MEDIA_TYPE,
            },
        )
        assert upload.status_code == 202, upload.text
        first = _wait_job(harness, str(upload.json()["job_id"]))
        assert first["state"] == "succeeded", first
        document_id = str(first["document_id"])
        before = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert before.current_version_id is not None
        assert before.active_index_revision_id is not None

        fail = True
        replacement = harness.client.post(
            base + f"/documents/{document_id}/versions",
            content=build_ole_word_container(marker=2),
            headers={
                **harness.write_headers,
                "Idempotency-Key": "v3-01-shadow-failure",
                "Content-Type": _DOC_MEDIA_TYPE,
            },
        )
        assert replacement.status_code == 202, replacement.text
        failed = _wait_job(harness, str(replacement.json()["job_id"]))
        assert failed["state"] == "failed_terminal", failed
        after = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert after.current_version_id == before.current_version_id
        assert after.active_index_revision_id == before.active_index_revision_id
    finally:
        harness.close()
