"""复合问题在单一检索链中的有界召回。"""

from __future__ import annotations

from pathlib import Path
from types import MethodType

from rag_app.application.answering.grounded import GroundedOutcome
from rag_app.application.retrieval.adaptive import AdaptivePlanOutcome
from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.application.retrieval.service import (
    _atom_scoped_candidates,
    _numeric_conflict,
    _scope_enum_candidates_to_target_group,
)
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    DocumentRef,
    EvidenceGroup,
    EvidenceGroupKind,
    EvidenceItem,
    GroupSourceMap,
    KnowledgeBaseScope,
    RankedChunk,
    SearchRequest,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    QueryAtom,
)
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.application.retrieval.helpers import make_ranked_chunk

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_atom_grounding_uses_own_hits_and_closed_group_members() -> None:
    """共享重排之后，原子不能借用另一原子的来源或结构成员。"""
    first = make_ranked_chunk(1, "甲提交材料。")
    first_member = make_ranked_chunk(2, "甲还需附上清单。")
    second = make_ranked_chunk(3, "乙审核材料。", document_number=3)
    corrected = make_ranked_chunk(4, "甲提交后确认。")
    corrected = corrected.model_copy(
        update={"expansion_seed_ids": (first.hydrated.chunk.chunk_id,)}
    )

    def group(
        number: int, members: tuple[RankedChunk, ...]
    ) -> GroupCandidate:
        chunk = members[0].hydrated.chunk
        return GroupCandidate(
            group=EvidenceGroup(
                group_id=f"egrp_{number:032x}",
                kind=EvidenceGroupKind.SECTION_GROUP,
                document_id=chunk.version.document_id,
                document_version_id=chunk.version.document_version_id,
                index_revision_id=chunk.index_revision_id,
                section_id=chunk.section_id,
                display_name="合成资料",
                member_chunk_ids=tuple(
                    member.hydrated.chunk.chunk_id for member in members
                ),
                member_source_maps=tuple(
                    GroupSourceMap(
                        chunk_id=member.hydrated.chunk.chunk_id,
                        citation_text=member.hydrated.chunk.citation_text,
                        source_spans=member.hydrated.chunk.source_spans,
                    )
                    for member in members
                ),
                member_ranks=tuple(member.fusion_rank for member in members),
                complete=True,
                token_cost=sum(
                    member.hydrated.chunk.token_count for member in members
                ),
            ),
            members=members,
            rerank_text=" ".join(
                member.hydrated.chunk.citation_text for member in members
            ),
        )

    first_group = group(1, (first, first_member))
    second_group = group(2, (second,))
    links = tuple(
        AtomCandidateLink(
            atom_id=atom_id,
            chunk_id=member.hydrated.chunk.chunk_id,
            channels=("lexical",),
            best_rank=1,
            score=1.0,
        )
        for atom_id, member in (("A1", first), ("A2", second))
    )
    candidates = (first, second, corrected)
    groups = (first_group, second_group)

    scoped_first, groups_first = _atom_scoped_candidates(
        "A1", candidates, groups, links, multi_atom=True
    )
    scoped_second, groups_second = _atom_scoped_candidates(
        "A2", candidates, groups, links, multi_atom=True
    )

    assert {item.hydrated.chunk.chunk_id for item in scoped_first} == {
        first.hydrated.chunk.chunk_id,
        first_member.hydrated.chunk.chunk_id,
        corrected.hydrated.chunk.chunk_id,
    }
    assert groups_first == (first_group,)
    assert scoped_second == (second,)
    assert groups_second == (second_group,)
    assert _atom_scoped_candidates(
        "A3", candidates, groups, links, multi_atom=True
    ) == ((), ())


def test_enumeration_target_anchor_excludes_sibling_structure_groups() -> None:
    """动态目标只闭合当前行，不能借同章节相邻类别作成员。"""

    def item(
        number: int,
        text: str,
        group_id: str,
        *,
        document_number: int = 1,
    ) -> EvidenceItem:
        chunk = make_ranked_chunk(
            number, text, document_number=document_number
        ).hydrated.chunk
        return EvidenceItem(
            evidence_id=f"S{number}",
            chunk_id=chunk.chunk_id,
            citation_text=text,
            source_label="合成清单",
            source_spans=chunk.source_spans,
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            metadata={"evidence_group_id": group_id},
        )

    evidence = (
        item(1, "甲类研究包含条目一。", "G1"),
        item(2, "条目二。", "G1"),
        item(3, "乙类研究包含条目三。", "G2"),
        item(4, "其他文档的条目。", "G1", document_number=2),
    )
    atom = QueryAtom(
        atom_id="A1",
        target="甲类研究项目",
        relation="类型",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    anchored = atom.model_copy(update={"target": "甲类研究"})

    assert _scope_enum_candidates_to_target_group(anchored, evidence) == (
        evidence[0],
        evidence[1],
    )
    assert _scope_enum_candidates_to_target_group(atom, evidence) == evidence


def test_conflicting_source_values_require_same_target_relation_and_unit() -> (
    None
):
    """不同文档对同一关系的明确数值冲突保留两边原文。"""
    atom = QueryAtom(
        atom_id="A1",
        target="甲设备",
        relation="保管期限",
        answer_shape=AtomAnswerShape.DURATION,
    )

    def evidence(number: int, text: str, document_number: int) -> EvidenceItem:
        ranked = make_ranked_chunk(
            number, text, document_number=document_number
        )
        chunk = ranked.hydrated.chunk
        return EvidenceItem(
            evidence_id=f"S{number}",
            chunk_id=chunk.chunk_id,
            citation_text=text,
            source_label=f"文档{document_number}",
            source_spans=chunk.source_spans,
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
        )

    first = evidence(1, "甲设备的保管期限为14天。", 2)
    second = evidence(2, "甲设备的保管期限为7天。", 3)
    unrelated = evidence(3, "乙设备的保管期限为5天。", 4)
    other_unit = evidence(4, "甲设备的保管期限为7个月。", 5)

    assert _numeric_conflict(atom, (first, second)) == (first, second)
    assert _numeric_conflict(atom, (first, unrelated)) == ()
    assert _numeric_conflict(atom, (first, other_unit)) == ()
    assert (
        _numeric_conflict(
            atom,
            (
                first,
                second.model_copy(update={"document_id": first.document_id}),
            ),
        )
        == ()
    )


def test_atom_channels_merge_before_one_rerank(tmp_path: Path) -> None:
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "atom-retrieval"),
        knowledge_base_id=deterministic_id("kb", "atom-retrieval"),
    )
    question = "甲提交材料，同时乙审核材料要多久？"

    class Planner:
        def plan_adaptive(self, *_args: object) -> AdaptivePlanOutcome:
            return AdaptivePlanOutcome(
                standalone_query=question,
                intent="COMPOUND",
                atoms=(
                    QueryAtom(
                        atom_id="A1",
                        target="甲",
                        relation="提交材料",
                        answer_shape=AtomAnswerShape.FACT,
                    ),
                    QueryAtom(
                        atom_id="A2",
                        target="乙",
                        relation="审核材料时限",
                        answer_shape=AtomAnswerShape.DURATION,
                    ),
                ),
                reason_code="ADAPTIVE_PLAN_APPLIED",
                attempted=True,
            )

    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(
            scope.project_id, "Atom Project"
        )
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "Atom KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=DocumentRef(
                        project_id=scope.project_id,
                        knowledge_base_id=scope.knowledge_base_id,
                        document_id=deterministic_id("doc", "atom-retrieval"),
                        display_name="通用材料流程.docx",
                    ),
                    content=build_docx(
                        "<w:p><w:r><w:t>甲提交材料。</w:t></w:r></w:p>"
                        "<w:p><w:r><w:t>乙审核材料需要五天。</w:t></w:r></w:p>"
                    ),
                    media_type=_DOCX,
                ),
            ),
            idempotency_key="atom-retrieval",
            budgets=runtime.persistence.default_budgets(),
        )
        retrieval = runtime.retrieval
        retrieval._adaptive_planner = Planner()  # type: ignore[assignment]
        observed_plan: list[int] = []

        class GroundedSpy:
            def answer(
                self, *_args: object, **kwargs: object
            ) -> GroundedOutcome:
                plan = kwargs["query_plan"]
                matrix = kwargs["atom_support_matrix"]
                observed_plan.append(len(plan.atoms))
                assert len(matrix.atoms) == len(plan.atoms)
                assert all(
                    len(item.supporting_support_ids) <= 4
                    for item in matrix.atoms
                )
                return GroundedOutcome(
                    None, "none", reason_code="GENERATION_ABSTAINED"
                )

        retrieval._grounded = GroundedSpy()  # type: ignore[assignment]
        rerank_calls = 0
        rerank_queries: list[str] = []
        lexical_texts: list[str] = []
        dense_batches: list[tuple[str, ...]] = []
        original_rerank = retrieval._reranker.rerank
        original_lexical = retrieval._lexical.search
        original_dense_many = retrieval._dense.search_many

        def trace_lexical(
            _self: object, snapshot: object, variant: object, **kwargs: object
        ) -> object:
            lexical_texts.append(variant.text)  # type: ignore[attr-defined]
            return original_lexical(snapshot, variant, **kwargs)  # type: ignore[arg-type]

        def trace_dense_batch(
            _self: object,
            snapshot: object,
            queries: tuple[str, ...],
            egress: object,
            **kwargs: object,
        ) -> object:
            dense_batches.append(queries)
            return original_dense_many(  # type: ignore[arg-type]
                snapshot, queries, egress, **kwargs
            )

        def count_rerank(
            _self: object, *args: object, **kwargs: object
        ) -> object:
            nonlocal rerank_calls
            rerank_calls += 1
            rerank_queries.append(args[0])  # type: ignore[arg-type]
            return original_rerank(*args, **kwargs)

        retrieval._lexical.search = MethodType(  # type: ignore[method-assign]
            trace_lexical, retrieval._lexical
        )
        retrieval._dense.search_many = MethodType(  # type: ignore[method-assign]
            trace_dense_batch, retrieval._dense
        )
        retrieval._reranker.rerank = MethodType(  # type: ignore[method-assign]
            count_rerank, retrieval._reranker
        )
        result = retrieval.search_and_answer(
            SearchRequest(scope=scope, text=question)
        )

    assert rerank_calls == 1
    assert question in lexical_texts
    assert len(dense_batches) == 1
    assert dense_batches[0][0] == question
    assert len(dense_batches[0]) == 3
    assert rerank_queries[0].startswith("原始问题：" + question)
    assert "A1" in rerank_queries[0] and "A2" in rerank_queries[0]
    assert observed_plan == [2]
    assert result.reasoning_effort == "DEEP"
    assert result.diagnostics is not None
    assert result.diagnostics.channel_chunk_ids
