"""复合问题在单一检索链中的有界召回。"""

from __future__ import annotations

from pathlib import Path
from types import MethodType

from rag_app.application.answering.grounded import GroundedOutcome
from rag_app.application.retrieval.adaptive import AdaptivePlanOutcome
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import DocumentRef, KnowledgeBaseScope, SearchRequest
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from tests.adapters.parsers.docx_fixtures import build_docx

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
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
        original_rerank = retrieval._reranker.rerank

        def count_rerank(
            _self: object, *args: object, **kwargs: object
        ) -> object:
            nonlocal rerank_calls
            rerank_calls += 1
            return original_rerank(*args, **kwargs)

        retrieval._reranker.rerank = MethodType(  # type: ignore[method-assign]
            count_rerank, retrieval._reranker
        )
        result = retrieval.search_and_answer(
            SearchRequest(scope=scope, text=question)
        )

    assert rerank_calls == 1
    assert observed_plan == [2]
    assert result.reasoning_effort == "DEEP"
    assert result.diagnostics is not None
    assert result.diagnostics.channel_chunk_ids
