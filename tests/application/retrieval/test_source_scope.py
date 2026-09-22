"""显式来源必须解析为精确活动文档身份，不能退回标题相似度。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.source_scope import (
    query_requires_source_resolution,
    resolve_query_plan_source_scopes,
    resolve_query_source_context,
    source_identity_allowed,
)
from rag_app.core.models.answer_plan import SourceMentionRole
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


def test_same_name_source_is_ambiguous_and_has_no_allowed_document() -> None:
    first = _document(1, "同名流程模板")
    second = _document(2, "同名流程模板")

    scope = _scope(
        "《同名流程模板》中的填写要求是什么？",
        (first, second),
    )

    assert scope.resolution is SourceResolution.AMBIGUOUS
    assert not scope.allowed_documents


def test_catalog_only_source_and_real_body_are_a_paired_boundary() -> None:
    catalog_only = _document(1, "需求评审模板", catalog_only=True)
    body_available = _document(2, "需求评审模板")
    query = "《需求评审模板》中的填写要求是什么？"

    catalog_scope = _scope(query, (catalog_only,))
    body_scope = _scope(query, (body_available,))

    assert catalog_scope.resolution is SourceResolution.CATALOG_ONLY
    assert not catalog_scope.allowed_documents
    assert body_scope.resolution is SourceResolution.RESOLVED
    assert body_scope.allowed_documents[0].document_id == (
        body_available.document_id
    )


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
    query = "比较《甲模板》和《乙模板》的差异"

    context = resolve_query_source_context(
        query,
        (first, second),
        registry_revision="test-registry-v1",
    )
    plan = resolve_query_plan_source_scopes(
        _plan(query),
        (first, second),
        registry_revision="test-registry-v1",
        root_context=context,
    )
    unresolved = _scope("比较《甲模板》和《丙模板》的差异", (first, second))

    assert context.source_intent is SourceIntent.DOCUMENT_SET
    assert context.resolution is SourceResolution.RESOLVED
    assert context.query_view.business_query == (
        "比较【SOURCE_A】和【SOURCE_B】的差异"
    )
    assert tuple(
        (mention.scope_key, mention.scope_digest)
        for mention in context.query_view.source_mentions
    ) == tuple(
        (item.scope_key, item.scope_digest) for item in context.mention_scopes
    )
    assert len(plan.atoms) == 2
    allowed_by_atom = tuple(
        tuple(item.document_id for item in atom.source_scope.allowed_documents)
        for atom in plan.atoms
        if atom.source_scope is not None
    )
    assert allowed_by_atom == ((first.document_id,), (second.document_id,))
    assert unresolved.resolution is SourceResolution.UNRESOLVED
    assert not unresolved.allowed_documents


def test_comparison_atoms_keep_disjoint_clause_source_scopes() -> None:
    first = _document(1, "甲规范")
    second = _document(2, "乙规范")
    query = "比较《甲规范》和《乙规范》的流程差异"
    context = resolve_query_source_context(
        query,
        (first, second),
        registry_revision="test-registry-v1",
    )
    base = _plan(query)
    planned = QueryPlan(
        **{
            **base.model_dump(mode="python"),
            "atoms": (
                QueryAtom(
                    atom_id="A1",
                    target="【SOURCE_A】流程",
                    relation="流程要求",
                    answer_shape=AtomAnswerShape.COMPARISON,
                    original_fragment="【SOURCE_A】流程",
                ),
                QueryAtom(
                    atom_id="A2",
                    target="【SOURCE_B】流程",
                    relation="流程要求",
                    answer_shape=AtomAnswerShape.COMPARISON,
                    original_fragment="【SOURCE_B】流程",
                ),
            ),
        }
    )

    resolved = resolve_query_plan_source_scopes(
        planned,
        (first, second),
        registry_revision="test-registry-v1",
        root_context=context,
    )

    assert len(resolved.atoms) == 2
    assert tuple(
        atom.source_scope.allowed_documents[0].document_id
        for atom in resolved.atoms
        if atom.source_scope is not None
    ) == (first.document_id, second.document_id)


def test_comparison_with_leading_document_clause_still_resolves_both() -> None:
    first = _document(1, "甲规范")
    second = _document(2, "乙规范")

    context = resolve_query_source_context(
        "《甲规范》中流程与《乙规范》中流程有什么差异？",
        (first, second),
        registry_revision="test-registry-v1",
    )

    assert context.source_intent is SourceIntent.DOCUMENT_SET
    assert context.resolution is SourceResolution.RESOLVED
    assert "甲规范" not in context.query_view.business_query
    assert "乙规范" not in context.query_view.business_query
    assert tuple(
        item.scope_key for item in context.query_view.source_mentions
    ) == ("SOURCE_A", "SOURCE_B")


def test_source_context_is_frozen_before_business_analysis() -> None:
    title = "开发中心三种输出工作模式"
    document = _document(1, title)
    query = f"《{title}》中，需求快验的输入项是什么？"

    context = resolve_query_source_context(
        query,
        (document,),
        registry_revision="test-registry-v1",
    )

    assert query_requires_source_resolution(query)
    assert context.resolution is SourceResolution.RESOLVED
    assert context.query_view.business_query == "需求快验的输入项是什么?"
    assert tuple(
        (mention.text, mention.role)
        for mention in context.query_view.source_mentions
    ) == ((title, SourceMentionRole.AUTHORITY),)
    assert (
        context.query_view.source_mentions[0].scope_digest
        == context.scope_digest
    )


def test_referenced_title_is_not_removed_with_source_owner() -> None:
    owner = _document(1, "乙文档")
    query = "乙文档是否提到《甲规范》？"

    context = resolve_query_source_context(
        query,
        (owner,),
        registry_revision="test-registry-v1",
    )

    assert context.resolution is SourceResolution.RESOLVED
    assert context.query_view.business_query == "是否提到《甲规范》?"
    assert tuple(
        (mention.text, mention.role)
        for mention in context.query_view.source_mentions
    ) == (
        ("乙文档", SourceMentionRole.SOURCE_OWNER),
        ("甲规范", SourceMentionRole.REFERENCED_OBJECT),
    )


def test_document_navigation_keeps_title_as_business_object() -> None:
    query = "《开发中心三种输出工作模式》在哪里？"

    context = resolve_query_source_context(
        query,
        (),
        registry_revision="test-registry-v1",
    )

    assert not query_requires_source_resolution(query)
    assert context.resolution is SourceResolution.OPEN
    assert (
        context.query_view.business_query
        == "《开发中心三种输出工作模式》在哪里?"
    )
    assert context.query_view.source_mentions[0].role is (
        SourceMentionRole.REFERENCED_OBJECT
    )
