"""跨工作包的 P06 前置合同验收。"""

from __future__ import annotations

import inspect

from rag_app.application import QueryEmbeddingRouter
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_hot_standby_profile,
    default_offline_profile,
    register_builtin_components,
)
from rag_app.core.identifiers import document_version_id
from rag_app.core.models import ChunkingReport, ParseContext
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import BlobPutResult, BlobStorePort, ParserPort


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    register_builtin_components(registry)
    return registry


def test_identity_and_parse_context_contracts_are_unambiguous() -> None:
    first = document_version_id(f"doc_{'1' * 32}", "a" * 64)
    repeated = document_version_id(f"doc_{'1' * 32}", "a" * 64)
    other_document = document_version_id(f"doc_{'2' * 32}", "a" * 64)

    assert first == repeated
    assert first != other_document
    assert "metadata" not in ParsingPolicy.model_fields
    assert tuple(inspect.signature(ParserPort.parse).parameters) == (
        "self",
        "source",
        "policy",
        "context",
    )
    assert "document" in ParseContext.model_fields


def test_composition_exposes_resolved_policies_and_real_query_router() -> None:
    with build_components(default_offline_profile(), _registry()) as single:
        assert single.chunking_policy.required_embedding_slots == ("primary",)
        assert isinstance(single.query_embedding_router, QueryEmbeddingRouter)
        assert single.query_embedding_router.descriptor.name.endswith("single")

    with build_components(
        default_hot_standby_profile(), _registry()
    ) as hot_standby:
        assert hot_standby.parsing_policy == hot_standby.profile.parsing
        assert hot_standby.chunking_policy.required_embedding_slots == (
            "primary",
            "standby",
        )
        assert isinstance(
            hot_standby.query_embedding_router,
            QueryEmbeddingRouter,
        )


def test_blob_and_report_contracts_are_ready_for_p06_implementation() -> None:
    assert {"put_if_absent", "read", "exists", "delete"}.issubset(
        BlobStorePort.__dict__
    )
    assert {item.value for item in BlobPutResult} == {"created", "existing"}
    assert {
        "total_citable_source_chars",
        "unique_covered_source_chars",
        "missing_source_chars",
        "cross_section_violations",
        "cross_group_violations",
        "orphan_relation_count",
        "missing_child_group_count",
        "missing_note_ref_count",
    }.issubset(ChunkingReport.model_fields)
