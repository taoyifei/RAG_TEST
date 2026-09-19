"""产品封装复核真实来源授权、实际模型与原 HTTP 调用边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import PolicyDenied
from rag_app.core.models.relation_review import RelationReviewRequest
from rag_app.product.grounded_runtime import ProductGroundedModel
from tests.adapters.providers.test_relation_review import (
    _adapter,
    _payload,
    _request,
)


def _model(
    tmp_path: Path, request: RelationReviewRequest, sent: list[httpx.Request]
) -> ProductGroundedModel:
    """建立范围检查实际查询的最小SQLite表和真实HTTP适配器。"""
    model = object.__new__(ProductGroundedModel)
    model.project_id = "project"
    model.knowledge_base_id = "knowledge-base"
    model.connections = SqliteConnectionFactory(tmp_path / "scope.sqlite3")
    model._campaign_required = False
    model.adapter = _adapter(_payload(request), sent)
    model.adapters = (model.adapter,)
    with model.connections.transaction(write=True) as db:
        db.execute(
            "CREATE TABLE documents(document_id TEXT PRIMARY KEY, "
            "project_id TEXT, knowledge_base_id TEXT, "
            "deleted_at TEXT, status TEXT)"
        )
        db.execute(
            "CREATE TABLE document_versions("
            "document_version_id TEXT PRIMARY KEY, "
            "document_id TEXT, content_sha256 TEXT)"
        )
        for item in request.evidence:
            db.execute(
                "INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?)",
                (
                    item.document_id,
                    model.project_id,
                    model.knowledge_base_id,
                    None,
                    "active",
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO document_versions VALUES(?,?,?)",
                (item.document_version_id, item.document_id, "a" * 64),
            )
    return model


def test_product_wrapper_reuses_generation_scope_and_strict_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request().model_copy(update={"generation_model": "synthetic"})
    sent: list[httpx.Request] = []
    model = _model(tmp_path, request, sent)
    scopes: list[tuple[str, tuple[str, ...]]] = []
    original_scope = model._scope

    @contextmanager
    def observed_scope(
        operation: str, hashes: tuple[str, ...]
    ) -> Iterator[None]:
        scopes.append((operation, hashes))
        with original_scope(operation, hashes):
            yield

    monkeypatch.setattr(model, "_scope", observed_scope)
    result = model.review_relations(request)

    assert scopes == [("generation", ("a" * 64,))]
    assert len(sent) == 1
    assert result.call.model == "synthetic"
    assert result.call.call_count == 1
    assert result.prepared_packet.evidence_level == "TRANSPORT_SENT"
    assert (
        model.supplement_timeout_seconds
        == model.adapter.supplement_timeout_seconds
    )
    assert not (tmp_path / "provider-budget.sqlite3").exists()


@pytest.mark.parametrize(
    "boundary", ["deleted", "inactive", "knowledge_base", "version"]
)
def test_product_wrapper_rechecks_sources_before_review_http(
    tmp_path: Path, boundary: str
) -> None:
    request = _request()
    sent: list[httpx.Request] = []
    model = _model(tmp_path, request, sent)
    with model.connections.transaction(write=True) as db:
        if boundary == "deleted":
            db.execute("UPDATE documents SET deleted_at='2026-01-01'")
        elif boundary == "inactive":
            db.execute("UPDATE documents SET status='inactive'")
        elif boundary == "knowledge_base":
            db.execute("UPDATE documents SET knowledge_base_id='other'")
        else:
            db.execute("DELETE FROM document_versions")
    with pytest.raises(PolicyDenied) as failure:
        model.review_relations(request)
    assert failure.value.code == "GENERATION_SOURCE_UNAVAILABLE"
    assert sent == []


def _other_adapter(sent: list[httpx.Request]) -> OpenAICompatibleChatAdapter:
    def unexpected(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise AssertionError("复核不能发送到另一模型")

    return OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(model="other", egress_allowed=True),
        http_client=ProviderHttpClient(
            "https://other.example/v1",
            client=httpx.Client(
                timeout=2.0, transport=httpx.MockTransport(unexpected)
            ),
        ),
        api_key_resolver=lambda: "",
    )


def test_review_selects_original_model_without_rotation(tmp_path: Path) -> None:
    request = _request().model_copy(update={"generation_model": "synthetic"})
    sent: list[httpx.Request] = []
    other_sent: list[httpx.Request] = []
    model = _model(tmp_path, request, sent)
    model.adapters = (_other_adapter(other_sent), model.adapter)
    result = model.review_relations(request)
    assert result.call.model == "synthetic"
    assert len(sent) == 1
    assert other_sent == []
    assert model.supplement_timeout_seconds == 2.0


@pytest.mark.parametrize("model_name", [None, "unconfigured"])
def test_multiple_adapters_require_original_model_identity(
    tmp_path: Path, model_name: str | None
) -> None:
    request = _request().model_copy(update={"generation_model": model_name})
    sent: list[httpx.Request] = []
    model = _model(tmp_path, request, sent)
    model.adapters = (_other_adapter(sent), model.adapter)
    with pytest.raises(PolicyDenied) as failure:
        model.review_relations(request)
    assert failure.value.code == "RELATION_REVIEW_MODEL_IDENTITY_REQUIRED"
    assert sent == []
