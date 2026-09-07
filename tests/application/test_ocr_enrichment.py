"""合成内嵌图片的离线 OCR 增补、批准与缓存合同。"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from PIL import Image

from rag_app.adapters.providers.aliyun_chat import ChatUsage
from rag_app.adapters.providers.aliyun_ocr import (
    OcrRecognition,
)
from rag_app.adapters.providers.budget_errors import BudgetBlockedError
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.composition.p06_runtime import P06Runtime
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    DocumentRef,
    ParseContext,
    ParseResult,
    ProviderCall,
)
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.ocr_enrichment import ProductOcrEnrichment
from tests.adapters.chunkers.test_docx_structural import _chunk
from tests.adapters.parsers.docx.fixtures import parse_package
from tests.adapters.parsers.docx_fixtures import IMAGE, build_docx
from tests.persistence.helpers import runtime_with_kb


def _fixture(project: str, kb: str) -> ParseResult:
    stream = io.BytesIO()
    Image.new("RGB", (256, 64), color="white").save(stream, format="PNG")
    media = stream.getvalue()
    content = build_docx(
        "<w:p><w:r><w:t>原生正文保持完整。</w:t></w:r></w:p>" + IMAGE + IMAGE,
        relationships=(
            '<Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/image" Target="media/ocr.png"/>'
        ),
        extra_entries={"word/media/ocr.png": media},
    )
    return parse_package(
        content,
        parse_context=ParseContext(
            document=DocumentRef(
                project_id=project,
                knowledge_base_id=kb,
                document_id=deterministic_id("doc", kb, "ocr"),
                display_name="ocr.docx",
            )
        ),
    )


def _setup(
    tmp_path: Path, *, enabled: bool = True, approved: bool = True
) -> tuple[P06Runtime, ParseResult, ProductOcrEnrichment, list[str]]:
    runtime, project, kb = runtime_with_kb(tmp_path)
    parsed = _fixture(project, kb)
    sha = next(
        node.image_attributes.content_sha256
        for node in parsed.document_ir.nodes
        if node.image_attributes
    )
    settings = KnowledgeBaseModelSettings(
        ocr_enabled=enabled,
        ocr_connection_id="connection",
        ocr_model="qwen3.5-ocr",
        budget_campaign_id="ocr-test",
        ocr_media_hashes=(sha,),
    )
    path = tmp_path / "ledger.sqlite"
    ledger = ProviderBudgetLedger(path)
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="ocr-test",
            authorization_id="test-authorization",
            scope="test-ocr",
            request_limit=2,
            estimated_token_limit=20000,
            approved_request_identities=("1" * 64,),
            scope_mode="knowledge_base",
            project_id=project,
            knowledge_base_id=kb,
            approved_source_hashes=(parsed.document_ir.source.content_sha256,),
            approved_media_hashes=(sha,) if approved else (),
            allowed_models=("qwen3.5-ocr",),
            allowed_operations=("image.ocr",),
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            operation_request_limits={"image.ocr": 2},
        )
    )
    calls: list[str] = []

    def recognize(
        _content: bytes, *, media_type: str, media_sha256: str
    ) -> OcrRecognition:
        calls.append(media_sha256)
        return OcrRecognition(
            text="巡检灯异常时，不得启动设备。",
            media_sha256=media_sha256,
            media_type=media_type,
            width=320,
            height=96,
            model="qwen3.5-ocr",
            policy_version="embedded-image-ocr-v1",
            usage=ChatUsage(),
            call=ProviderCall(
                provider_id="offline-test",
                operation="image.ocr",
                call_count=0,
                retry_count=0,
                elapsed_ms=0,
            ),
        )

    service = ProductOcrEnrichment(
        runtime.connections,
        SimpleNamespace(get=lambda _: settings),
        SimpleNamespace(
            ocr_adapter=lambda *_args, **_kwargs: SimpleNamespace(
                recognize=recognize
            )
        ),
        path,
        runtime.components.blob_store,
    )
    return runtime, parsed, service, calls


def test_ocr_preserves_native_text_and_citable_media_provenance(
    tmp_path: Path,
) -> None:
    runtime, parsed, service, calls = _setup(tmp_path)
    try:
        scan = service.scan_document(
            parsed.document_ir, artifacts=parsed.artifacts
        )
        assert scan["media_count"] == 1
        assert scan["pending_count"] == 1
        assert calls == []
        assert scan["media"][0]["part_uri"] == "/word/media/ocr.png"
        assert scan["media"][0]["supported"] is True
        assert scan["media"][0]["approved"] is True
        enriched = service.enrich_result(parsed)
        assert len(calls) == 1
        assert "原生正文保持完整。" in [
            node.text for node in enriched.document_ir.nodes
        ]
        ocr_nodes = [
            node
            for node in enriched.document_ir.nodes
            if dict(node.metadata).get("origin") == "ocr"
        ]
        assert len(ocr_nodes) == 2
        assert all(
            node.parent_node_id
            in {item.node_id for item in parsed.document_ir.nodes}
            for node in ocr_nodes
        )
        assert all("bbox" not in dict(node.metadata) for node in ocr_nodes)
        chunks = _chunk(enriched.document_ir)
        ocr_spans = [
            span
            for chunk in chunks.chunks
            for span in chunk.source_spans
            if dict(span.metadata).get("origin") == "ocr"
        ]
        assert ocr_spans
        assert all(
            dict(span.metadata)["artifact_id"].startswith("sha256:")
            for span in ocr_spans
        )
        assert chunks.report.source_span_coverage == 1.0
        again = service.enrich_result(parsed)
        assert len(calls) == 1
        assert again.document_ir.nodes == enriched.document_ir.nodes
        assert (
            service.enrich_result(enriched).document_ir.nodes
            == enriched.document_ir.nodes
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("enabled,approved", [(False, True), (True, False)])
def test_unapproved_or_disabled_ocr_keeps_native_document_without_calls(
    tmp_path: Path,
    enabled: bool,
    approved: bool,
) -> None:
    runtime, parsed, service, calls = _setup(
        tmp_path, enabled=enabled, approved=approved
    )
    try:
        enriched = service.enrich_result(parsed)
        assert enriched.document_ir.nodes == parsed.document_ir.nodes
        assert calls == []
        assert (
            dict(dict(enriched.document_ir.metadata)["ocr_enrichment"])[
                "status"
            ]
            == "PARTIAL"
        )
        assert enriched.report.issues[-1].code in {
            "OCR_DISABLED",
            "OCR_MEDIA_NOT_APPROVED",
        }
    finally:
        runtime.close()


def test_failed_image_keeps_native_text_and_does_not_cache(
    tmp_path: Path,
) -> None:
    runtime, parsed, service, calls = _setup(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise ValueError("synthetic failure must not leak provider content")

    service.providers = SimpleNamespace(ocr_adapter=fail)
    try:
        enriched = service.enrich_result(parsed)
        assert enriched.document_ir.nodes == parsed.document_ir.nodes
        assert enriched.report.issues[-1].code == "OCR_RECOGNITION_FAILED"
        assert (
            service.scan_document(
                parsed.document_ir, artifacts=parsed.artifacts
            )["pending_count"]
            == 1
        )
        assert calls == []
    finally:
        runtime.close()


def test_empty_media_selection_does_not_reuse_or_send_ocr(
    tmp_path: Path,
) -> None:
    runtime, parsed, service, calls = _setup(tmp_path)
    try:
        service.enrich_result(parsed)
        settings = service.models.get(
            parsed.document_ir.document.knowledge_base_id
        )
        original_identity = service.content_identity(
            parsed.document_ir.document.knowledge_base_id
        )
        service.models = SimpleNamespace(
            get=lambda _: settings.model_copy(update={"ocr_media_hashes": ()})
        )
        untouched = service.enrich_result(parsed)
        assert untouched.document_ir.nodes == parsed.document_ir.nodes
        assert len(calls) == 1
        assert untouched.report.issues[-1].code == "OCR_MEDIA_NOT_SELECTED"
        assert (
            service.content_identity(
                parsed.document_ir.document.knowledge_base_id
            )
            != original_identity
        )
    finally:
        runtime.close()


def test_budget_exhaustion_preserves_native_text_and_safe_reason(
    tmp_path: Path,
) -> None:
    runtime, parsed, service, calls = _setup(tmp_path)

    def blocked(*_args: object, **_kwargs: object) -> NoReturn:
        raise BudgetBlockedError("OPERATION_REQUEST_LIMIT")

    service.providers = SimpleNamespace(ocr_adapter=blocked)
    try:
        enriched = service.enrich_result(parsed)
        assert enriched.document_ir.nodes == parsed.document_ir.nodes
        assert enriched.report.issues[-1].code == "OPERATION_REQUEST_LIMIT"
        assert calls == []
    finally:
        runtime.close()


def test_cache_and_campaign_cannot_cross_knowledge_base(tmp_path: Path) -> None:
    runtime, parsed, service, calls = _setup(tmp_path)
    try:
        service.enrich_result(parsed)
        other = _fixture(
            parsed.document_ir.document.project_id,
            deterministic_id("kb", "other-ocr-scope"),
        )
        result = service.enrich_result(other)
        assert result.document_ir.nodes == other.document_ir.nodes
        assert result.report.issues[-1].code == "OCR_MEDIA_NOT_APPROVED"
        assert len(calls) == 1
    finally:
        runtime.close()
