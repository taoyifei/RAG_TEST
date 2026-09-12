"""V3-00.7 多问法语义一致性与无证据拒答回归。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.clients.resilience import StreamCancellation
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    ActiveRevisionEmbeddingState,
    AnswerClaim,
    AnswerDraft,
    ClaimSupport,
    ConfidenceStatus,
    DocumentRef,
    KnowledgeBaseScope,
    QueryAnalysis,
    QueryEmbeddingRequest,
    QuerySemantics,
    QueryVariant,
    RequestedAnswerType,
    RetrievalPolicy,
    RoutedEmbeddingResult,
    SearchRequest,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import CancellationPort, GenerationRequest
from rag_app.core.ports.query_interpret import InterpretOutcome
from rag_app.core.ports.query_rewrite import RewriteOutcome
from tests.adapters.parsers.docx_fixtures import build_docx

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PRODUCT_EVIDENCE_POLICY = RetrievalPolicy(
    per_document_cap=8,
    per_section_cap=8,
    max_evidence_items_per_chunk=8,
)


def _scope_with_document(data_dir: Path) -> KnowledgeBaseScope:
    return _scope_with_blocks(
        data_dir,
        (
            "<w:p><w:r><w:t>"
            "蓝鹊小组现有工作模式，分为“轮值维护”、“专项修理”、“计划改造”。"
            "</w:t></w:r></w:p>"
        ),
        namespace="modes",
    )


def _scope_with_blocks(
    data_dir: Path, blocks: str, *, namespace: str
) -> KnowledgeBaseScope:
    """将一份公开合成 OOXML 建成独立 Active Revision。"""
    project_id = deterministic_id("prj", "semantic-v3", namespace)
    knowledge_base_id = deterministic_id(
        "kb", project_id, "semantic-v3", namespace
    )
    document_id = deterministic_id(
        "doc", project_id, knowledge_base_id, namespace
    )
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=data_dir) as runtime:
        runtime.persistence.control.put_project(project_id, "合成语义项目")
        runtime.persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            "合成语义知识库",
            profile_id="dev-p06-memory",
        )
        document = DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name=f"合成-{namespace}.docx",
        )
        runtime.persistence.control.upsert_document(document)
        runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=document,
                    content=build_docx(blocks),
                    media_type=_MEDIA_TYPE,
                ),
            ),
            idempotency_key=f"v3-semantic-query-{namespace}",
            budgets=runtime.persistence.default_budgets(),
        )
    return scope


@pytest.mark.parametrize(
    "question",
    (
        "蓝鹊小组的三种工作模式是什么",
        "蓝鹊小组的三种工作模式是啥",
        "蓝鹊小组有哪些工作模式？",
        "蓝鹊小组是哪三种工作模式",
        "蓝鹊小组的工作模式分别指什么",
        "请把蓝鹊小组的工作模式列出来",
        "蓝鹊小组通常会用哪几种方式参与项目，每一种分别怎么配合？",
    ),
)
def test_synonymous_questions_share_evidence_but_require_model(
    tmp_path: Path, question: str
) -> None:
    scope = _scope_with_document(tmp_path)

    with build_p07_runtime(
        _PROFILE, data_dir=tmp_path, policy=_PRODUCT_EVIDENCE_POLICY
    ) as runtime:
        result = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text=question)
        )

    assert result.status is ConfidenceStatus.CONFIGURATION_REQUIRED
    assert result.answer is None
    assert result.generation_mode == "none"
    assert result.generation_reason_code == "GENERATOR_NOT_CONFIGURED"
    assert len(result.evidence) == 1
    assert "轮值维护" in result.evidence[0].citation_text
    assert "专项修理" in result.evidence[0].citation_text
    assert "计划改造" in result.evidence[0].citation_text


@pytest.mark.parametrize(
    "question",
    (
        "我家在哪",
        "Codex 和豆包比谁厉害",
    ),
)
def test_unsupported_relation_is_refused(tmp_path: Path, question: str) -> None:
    """无回答模型时，即使只有弱主题命中也统一明确拒答。"""
    scope = _scope_with_document(tmp_path)

    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        result = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text=question)
        )

    assert result.status is ConfidenceStatus.CONFIGURATION_REQUIRED
    assert result.answer is None
    assert result.evidence == ()
    assert result.generation_mode == "none"


def test_structured_procedure_keeps_evidence_but_requires_model(
    tmp_path: Path,
) -> None:
    intro = "设备入库流程包括以下步骤："
    steps = ("核对交接清单。", "完成双人复核。", "按顺序登记入库。")
    list_item = (
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
        '<w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:t>{}</w:t>'
        "</w:r></w:p>"
    )
    blocks = f"<w:p><w:r><w:t>{intro}</w:t></w:r></w:p>" + "".join(
        list_item.format(step) for step in steps
    )
    scope = _scope_with_blocks(tmp_path, blocks, namespace="procedure")

    with build_p07_runtime(
        _PROFILE, data_dir=tmp_path, policy=_PRODUCT_EVIDENCE_POLICY
    ) as runtime:
        result = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="设备入库流程有哪些步骤？")
        )

    assert result.status is ConfidenceStatus.CONFIGURATION_REQUIRED
    assert [item.citation_text for item in result.evidence] == [intro, *steps]
    assert result.answer is None
    assert result.generation_mode == "none"


class _EvidenceTriggeredRewriter:
    """仅在首轮证据不足时返回通用、受控的唯一变体。"""

    def __init__(self) -> None:
        self.triggers: list[bool] = []

    def rewrite(
        self, request: SearchRequest, *, recall_insufficient: bool = False
    ) -> RewriteOutcome:
        del request
        self.triggers.append(recall_insufficient)
        if not recall_insufficient:
            return RewriteOutcome()
        text = "蓝鹊小组有哪些工作模式"
        return RewriteOutcome(
            variant=QueryVariant(
                text=text,
                kind="rewrite",
                identity=canonical_sha256({"query": text}),
            ),
            reason_code="REWRITE_APPLIED",
            attempted=True,
        )


class _StructuredInterpreter:
    """模拟一次已授权解释，只返回语义，不携带任何答案。"""

    def __init__(self) -> None:
        self.analyses: list[QueryAnalysis] = []

    def interpret(
        self, request: SearchRequest, analysis: QueryAnalysis
    ) -> InterpretOutcome:
        self.analyses.append(analysis)
        assert request.text == "甲部门这块是怎么回事？"
        return InterpretOutcome(
            standalone_query="甲部门的职责是什么？",
            semantics=QuerySemantics(
                target="甲部门",
                relation="职责",
                answer_type=RequestedAnswerType.DUTIES,
                source="LLM_INTERPRET",
                reason_codes=("STRUCTURED_QUERY_INTERPRET",),
            ),
            reason_code="INTERPRET_APPLIED",
            attempted=True,
        )


class _EvidenceEchoGenerator:
    """仅按收到的证据构造可验证 claim，不按问题返回预置答案。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        self.queries.append(request.query)
        return self._draft(request)

    def generate_stream(
        self,
        request: GenerationRequest,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: CancellationPort,
    ) -> AnswerDraft:
        """用同一确定性草稿模拟 Provider 的逐条完整 claim。"""
        assert not cancellation.is_cancelled()
        self.queries.append(request.query)
        draft = self._draft(request)
        on_claim(draft.claims[0])
        return draft

    @staticmethod
    def _draft(request: GenerationRequest) -> AnswerDraft:
        """只从本次真实召回证据构造最终草稿。"""
        evidence = request.evidence[0]
        return AnswerDraft(
            text=evidence.citation_text,
            cited_evidence_ids=(evidence.evidence_id,),
            claims=(
                AnswerClaim(
                    text=evidence.citation_text,
                    supports=(
                        ClaimSupport(
                            support_id=evidence.evidence_id,
                            quote=evidence.citation_text,
                        ),
                    ),
                ),
            ),
            generation_mode="llm",
        )


@pytest.mark.parametrize(
    "question",
    (
        "蓝鹊小组的三种工作模式是什么",
        "蓝鹊小组的三种工作模式是啥",
        "蓝鹊小组有哪些工作模式？",
        "蓝鹊小组是哪三种工作模式",
        "蓝鹊小组的工作模式分别指什么",
        "请把蓝鹊小组的工作模式列出来",
        "蓝鹊小组通常会用哪几种方式参与项目，每一种分别怎么配合？",
    ),
)
def test_synonymous_questions_are_equivalent_for_sync_and_streaming(
    tmp_path: Path, question: str
) -> None:
    """同步与流式复用同一语义、检索、证据门和最终收束。"""
    scope = _scope_with_document(tmp_path)
    generator = _EvidenceEchoGenerator()

    with build_p07_runtime(
        _PROFILE, data_dir=tmp_path, policy=_PRODUCT_EVIDENCE_POLICY
    ) as runtime:
        service = runtime.retrieval.with_generation(
            generator,
            serving_identity=canonical_sha256("semantic-stream-equivalence"),
        )
        request = SearchRequest(scope=scope, text=question)
        regular = service.search_and_answer(request, cache_result=False)
        emitted: list[AnswerClaim] = []
        streamed = service.search_and_answer(
            request,
            on_claim=lambda claim, _revision_id: emitted.append(claim),
            cancellation=StreamCancellation(),
            cache_result=False,
        )

    assert regular.status is ConfidenceStatus.ANSWERABLE
    assert regular.generation_mode == "llm"
    assert streamed.answer is not None
    assert len(emitted) == 1
    assert emitted[0].text == streamed.evidence[0].citation_text
    assert streamed.model_dump(exclude={"trace_id"}) == regular.model_dump(
        exclude={"trace_id"}
    )


def test_response_style_directive_is_not_sent_to_generator(
    tmp_path: Path,
) -> None:
    """只有语义问题进入生成器，原始请求仍可在分析中审计。"""
    scope = _scope_with_document(tmp_path)
    generator = _EvidenceEchoGenerator()
    semantic_question = "蓝鹊小组有哪些工作模式？"
    resolved_question = "蓝鹊小组有哪些工作模式?"

    with build_p07_runtime(
        _PROFILE, data_dir=tmp_path, policy=_PRODUCT_EVIDENCE_POLICY
    ) as runtime:
        service = runtime.retrieval.with_generation(
            generator,
            serving_identity=canonical_sha256("response-directive-boundary"),
        )
        result = service.search_and_answer(
            SearchRequest(
                scope=scope,
                text=semantic_question + "请仅依据原文完整作答。",
            ),
            cache_result=False,
        )

    assert result.status is ConfidenceStatus.ANSWERABLE, result.model_dump()
    assert generator.queries == [resolved_question]


def test_evidence_shortfall_rewrite_reaches_lexical_dense_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope_with_document(tmp_path)
    rewriter = _EvidenceTriggeredRewriter()
    generator = _EvidenceEchoGenerator()

    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        routed_queries: list[str] = []
        router = runtime.persistence.components.query_embedding_router
        original_embed = router.embed_query

        def record_embed(
            request: QueryEmbeddingRequest,
            revision: ActiveRevisionEmbeddingState,
            egress: EgressPolicy,
        ) -> RoutedEmbeddingResult:
            routed_queries.append(request.text)
            return original_embed(request, revision, egress)

        monkeypatch.setattr(router, "embed_query", record_embed)
        service = runtime.retrieval.with_generation(
            generator,
            serving_identity=canonical_sha256("semantic-rewrite-test"),
            rewriter=rewriter,
        )
        result = service.search_and_answer(
            SearchRequest(
                scope=scope,
                text="蓝鹊小组到底都有哪一些安排呢",
            )
        )

    assert rewriter.triggers == [False, True]
    assert routed_queries == [
        "蓝鹊小组到底都有哪一些安排呢",
        "蓝鹊小组有哪些工作模式",
    ]
    assert result.status is ConfidenceStatus.ANSWERABLE, result.model_dump()
    assert result.answer is not None
    assert generator.queries == ["蓝鹊小组有哪些工作模式"]
    assert result.evidence
    assert result.rewrite_reason_code == "REWRITE_APPLIED"
    assert result.diagnostics is not None
    for item in result.diagnostics.fusion:
        families = {
            "lexical"
            if contribution.channel.startswith("lexical:")
            else "dense"
            if contribution.channel.startswith("dense:")
            else contribution.channel
            for contribution in item.contributions
        }
        assert len(families) == len(item.contributions)


def test_interpretation_is_consumed_once_by_structural_retrieval_and_evidence(
    tmp_path: Path,
) -> None:
    """解释结果进入同一分析，结构通道和证据门共同消费。"""
    scope = _scope_with_blocks(
        tmp_path,
        (
            "<w:p><w:r><w:t>"
            "甲部门的职责包括设备巡检、故障复核和维护记录归档。"
            "</w:t></w:r></w:p>"
        ),
        namespace="interpreted-duties",
    )
    interpreter = _StructuredInterpreter()
    generator = _EvidenceEchoGenerator()

    with build_p07_runtime(
        _PROFILE, data_dir=tmp_path, policy=_PRODUCT_EVIDENCE_POLICY
    ) as runtime:
        service = runtime.retrieval.with_generation(
            generator,
            serving_identity=canonical_sha256("structured-interpret-test"),
            interpreter=interpreter,
        )
        result = service.search_and_answer(
            SearchRequest(scope=scope, text="甲部门这块是怎么回事？"),
            cache_result=False,
        )

    assert len(interpreter.analyses) == 1
    original = interpreter.analyses[0]
    assert original.semantics.source == "ORIGINAL_FALLBACK"
    assert result.status is ConfidenceStatus.ANSWERABLE, result.model_dump()
    assert result.answer is not None
    assert generator.queries == ["甲部门的职责是什么?"]
    assert result.interpret_reason_code == "INTERPRET_APPLIED"
    assert result.interpret_called_this_request is False
    assert result.diagnostics is not None
    assert any(
        contribution.channel == "structural:canonical-v1"
        for item in result.diagnostics.fusion
        for contribution in item.contributions
    )
