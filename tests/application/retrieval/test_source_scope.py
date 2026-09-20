"""显式来源必须解析为精确活动文档身份，不能退回标题相似度。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.source_scope import (
    resolve_query_plan_source_scopes,
    source_identity_allowed,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    QueryPlan,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
)
from rag_app.core.ports.evidence_source import CatalogDocument


def _document(number: int, title: str, **metadata: object) -> CatalogDocument:
    return CatalogDocument(
        document_id=f"doc_{number:032x}",
        document_version_id=f"dver_{number:032x}",
        chunk_id=f"chunk_{number:032x}",
        title=title,
        metadata=freeze_json_object(metadata),
    )


def _plan(query: str, *, source_qualifier: str | None = None) -> QueryPlan:
    return QueryPlan(
        plan_id=f"sha256:{'a' * 64}",
        standalone_query=query,
        original_query=query,
        resolved_root_query=query,
        context_digest=f"sha256:{'b' * 64}",
        intent="FACT",
        effort="DIRECT",
        atoms=(
            QueryAtom(
                atom_id="A1",
                target="评审会议纪要",
                relation="填写要求",
                answer_shape=AtomAnswerShape.FACT,
                source_qualifier=source_qualifier,
            ),
        ),
        planner_reason_code="TEST",
    )


def _scope(
    query: str, documents: tuple[CatalogDocument, ...]
) -> SourceScopeDecision:
    resolved = resolve_query_plan_source_scopes(
        _plan(query),
        documents,
        registry_revision="test-registry-v1",
    )
    scope = resolved.atoms[0].source_scope
    assert scope is not None
    return scope


def test_leading_book_title_resolves_exact_document_identity() -> None:
    expected = _document(1, "2-需求阶段-需求变更评审会议纪要模板（模板）.docx")
    similar = _document(2, "需求变更评审会议纪要模板使用说明")

    scope = _scope(
        "《2-需求阶段-需求变更评审会议纪要模板（模板）》的填写要求是什么？",
        (expected, similar),
    )

    assert scope.source_intent is SourceIntent.DOCUMENT_AUTHORITY
    assert scope.resolution is SourceResolution.RESOLVED
    assert tuple(
        (item.document_id, item.document_version_id)
        for item in scope.allowed_documents
    ) == ((expected.document_id, expected.document_version_id),)
    assert source_identity_allowed(
        scope,
        document_id=expected.document_id,
        document_version_id=expected.document_version_id,
    )
    assert not source_identity_allowed(
        scope,
        document_id=similar.document_id,
        document_version_id=similar.document_version_id,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("《不存在的模板》的填写要求是什么？", SourceResolution.UNRESOLVED),
        ("需求变更评审会议纪要怎么填？", SourceResolution.OPEN),
    ),
)
def test_unresolved_source_never_becomes_open(
    query: str, expected: SourceResolution
) -> None:
    scope = _scope(query, (_document(1, "另一个模板"),))

    assert scope.resolution is expected
    assert not scope.allowed_documents
    assert source_identity_allowed(
        scope,
        document_id=f"doc_{1:032x}",
        document_version_id=f"dver_{1:032x}",
    ) is (expected is SourceResolution.OPEN)


def test_registered_alias_is_exact_but_title_substring_is_not() -> None:
    document = _document(
        1,
        "需求阶段配套资料",
        trusted_aliases=("需求变更评审会议纪要模板",),
    )
    alias_scope = _scope(
        "《需求变更评审会议纪要模板》中规定了什么？", (document,)
    )
    substring_scope = _scope("《需求变更评审》中规定了什么？", (document,))

    assert alias_scope.resolution is SourceResolution.RESOLVED
    assert substring_scope.resolution is SourceResolution.UNRESOLVED


def test_original_explicit_source_wins_planner_qualifier() -> None:
    expected = _document(1, "用户明确指定的模板")
    planner_choice = _document(2, "Planner 推测的模板")
    query = "《用户明确指定的模板》的填写要求是什么？"
    resolved = resolve_query_plan_source_scopes(
        _plan(query, source_qualifier="Planner 推测的模板"),
        (expected, planner_choice),
        registry_revision="test-registry-v1",
    )
    scope = resolved.atoms[0].source_scope

    assert scope is not None
    assert scope.resolution is SourceResolution.RESOLVED
    assert scope.allowed_documents[0].document_id == expected.document_id


def test_comparison_requires_every_named_document_to_resolve() -> None:
    first = _document(1, "甲模板")
    second = _document(2, "乙模板")

    resolved = _scope("比较《甲模板》和《乙模板》的差异", (first, second))
    unresolved = _scope("比较《甲模板》和《丙模板》的差异", (first, second))

    assert resolved.source_intent is SourceIntent.DOCUMENT_SET
    assert resolved.resolution is SourceResolution.RESOLVED
    assert len(resolved.allowed_documents) == 2
    assert unresolved.resolution is SourceResolution.UNRESOLVED
    assert not unresolved.allowed_documents
