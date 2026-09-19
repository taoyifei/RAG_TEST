"""WB08R-01 自适应预算和活动目录的定向回归。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_app.adapters.providers.aliyun_chat import ChatMessage
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
    openai_compatible_chat_payload,
)
from rag_app.application.answering.grounded import GroundedAnsweringService
from rag_app.application.retrieval.adaptive import (
    AdaptivePlanOutcome,
    ReasoningEffort,
    catalog_matches,
    is_navigation_query,
    reasoning_effort,
)
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.application.revision_builder import IngestionDocument
from rag_app.clients.resilience import StreamCancellation
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.errors import ValidationFailed
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    DocumentRef,
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.core.ports.evidence_source import CatalogDocument
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.wanshitong.public_stream import render_public_final
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.application.retrieval.helpers import make_ranked_chunk

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@pytest.mark.parametrize(
    ("question", "navigation"),
    (
        ("办理申请要备哪些材料、走哪些步骤？", False),
        ("申请阶段要提交什么材料？", False),
        ("申请阶段应参考哪份材料？", True),
        ("申请阶段有哪些文档可参考？", True),
        ("纪要该用哪份？", True),
        ("纪要要写哪些内容？", False),
    ),
)
def test_document_navigation_excludes_required_input_materials(
    question: str, navigation: bool
) -> None:
    """输入材料和办理步骤属于内容问答，文件身份才走目录。"""
    assert is_navigation_query(question) is navigation


def _documents(
    scope: KnowledgeBaseScope, titles: tuple[str, ...]
) -> tuple[IngestionDocument, ...]:
    return tuple(
        IngestionDocument(
            document=DocumentRef(
                project_id=scope.project_id,
                knowledge_base_id=scope.knowledge_base_id,
                document_id=deterministic_id("doc", title),
                display_name=f"{title}.docx",
                metadata={
                    "document_title": title,
                    "department_name": "综合事务",
                    "category_path": ["项目", "发布"],
                    "topic_keys": [],
                    "source_relative_path": f"综合事务/{title}.docx",
                },
            ),
            content=build_docx(
                f"<w:p><w:r><w:t>{title}的正文。</w:t></w:r></w:p>"
            ),
            media_type=_MEDIA_TYPE,
        )
        for title in titles
    )


@pytest.mark.parametrize(
    ("titles", "question", "expected_status", "expected_count"),
    (
        (
            ("2-部署阶段-设备部署方案模板",),
            "准备设备部署阶段时应该参考哪份材料",
            ConfidenceStatus.ANSWERABLE,
            1,
        ),
        (
            (
                "2-部署阶段-设备部署方案模板",
                "2-部署阶段-设备部署验收报告",
            ),
            "准备设备部署阶段时应该参考哪份材料",
            ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
            2,
        ),
        (
            ("2-部署阶段-设备部署方案模板",),
            "准备员工培训时应该参考哪份材料",
            ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
            0,
        ),
    ),
)
def test_catalog_uses_active_metadata_without_generation(
    tmp_path: Path,
    titles: tuple[str, ...],
    question: str,
    expected_status: ConfidenceStatus,
    expected_count: int,
) -> None:
    project_id = deterministic_id("prj", "wb08r-catalog")
    scope = KnowledgeBaseScope(
        project_id=project_id,
        knowledge_base_id=deterministic_id("kb", project_id, "catalog"),
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(project_id, "Catalog Project")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            project_id,
            "Catalog KB",
            profile_id="dev-p06-memory",
        )
        if len(titles) > 1:
            runtime.persistence.builder.build_and_activate(
                project_id=project_id,
                knowledge_base_id=scope.knowledge_base_id,
                documents=_documents(scope, titles[:1]),
                idempotency_key="wb08r-catalog-first-revision",
                budgets=runtime.persistence.default_budgets(),
            )
        runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=_documents(scope, titles),
            idempotency_key="wb08r-catalog",
            budgets=runtime.persistence.default_budgets(),
        )

        class ForbiddenPlanner:
            """目录快速路径不能发出语义 Planner 请求。"""

            def plan_adaptive(self, *args: object) -> None:
                del args
                raise AssertionError("目录快速路径调用了 Planner")

        runtime.retrieval._adaptive_planner = ForbiddenPlanner()  # type: ignore[assignment]
        request = SearchRequest(scope=scope, text=question)
        result = runtime.retrieval.search_and_answer(request)
        identity = runtime.retrieval.execution_identity(request)
        padded_identity = runtime.retrieval.execution_identity(
            SearchRequest(scope=scope, text=f"  {question}   ")
        )
        filtered_identity = runtime.retrieval.execution_identity(
            SearchRequest(
                scope=scope,
                text=question,
                metadata_filters={
                    "document_id": deterministic_id("doc", "other")
                },
            )
        )
        contextual_identity = runtime.retrieval.execution_identity(
            SearchRequest(
                scope=scope,
                text=question,
                conversation_context=("此前讨论了另一份资料。",),
            )
        )
        explicit = (
            runtime.retrieval.search_and_answer(
                SearchRequest(scope=scope, text=f"《{titles[0]}》在哪里？")
            )
            if expected_count == 1
            else None
        )

    assert result.status is expected_status
    assert len(result.catalog_citations) == expected_count
    assert result.rerank_execution_mode == "catalog_fast_path"
    assert not result.generation_called_this_request
    assert not result.interpret_called_this_request
    assert identity.key_hash == padded_identity.key_hash
    assert identity.key_hash != filtered_identity.key_hash
    assert identity.key_hash != contextual_identity.key_hash
    if explicit is not None:
        assert explicit.status is ConfidenceStatus.ANSWERABLE
        assert not explicit.interpret_called_this_request
        assert not explicit.generation_called_this_request
        public = render_public_final(explicit)
        assert public["citations"][0]["source_kind"] == "catalog_metadata"
    if expected_count == 0:
        assert result.answer is not None
        assert all(title not in result.answer for title in titles)


def test_catalog_matches_colloquial_and_typo_without_answer_table() -> None:
    document = CatalogDocument(
        document_id=deterministic_id("doc", "generic-template"),
        document_version_id=deterministic_id("dver", "generic-template"),
        chunk_id=deterministic_id("chunk", "generic-template"),
        title="需求变更评审会议纪要模板",
        metadata=(),
    )
    query = "需求变更评审纪要模版在哪？"

    assert is_navigation_query(query)
    assert catalog_matches(query, (document,)) == (document,)
    assert is_navigation_query("设备变更用哪个纪要？")
    assert catalog_matches("设备变更用哪个纪要？", (document,)) == ()


def test_contextual_document_navigation_uses_trusted_previous_target(
    tmp_path: Path,
) -> None:
    """短追问只用上一条用户问句的对象定位唯一活动目录项。"""
    title = "2-部署阶段-设备变更评审会议纪要模板"
    project_id = deterministic_id("prj", "contextual-catalog")
    scope = KnowledgeBaseScope(
        project_id=project_id,
        knowledge_base_id=deterministic_id("kb", project_id, "catalog"),
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(project_id, "Catalog Project")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            project_id,
            "Catalog KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=_documents(scope, (title,)),
            idempotency_key="contextual-catalog",
            budgets=runtime.persistence.default_budgets(),
        )
        result = runtime.retrieval.search_and_answer(
            SearchRequest(
                scope=scope,
                text="纪要该用哪份？",
                conversation_context=("上一问：设备变更需要开评审会。",),
            )
        )

    assert result.status is ConfidenceStatus.ANSWERABLE
    assert result.rerank_execution_mode == "catalog_fast_path"
    assert len(result.catalog_citations) == 1
    assert result.catalog_citations[0].document_title == title


def test_catalog_matches_separated_title_fragments() -> None:
    """目录短语可以跨标题修饰语匹配，仍须有全部关键片段。"""
    titles = (
        "2-安装阶段-设备安装验收报告",
        "2-部署阶段-设备现场部署方案",
        "2-部署阶段-现场调试计划",
    )
    documents = tuple(
        CatalogDocument(
            document_id=deterministic_id("doc", title),
            document_version_id=deterministic_id("dver", title),
            chunk_id=deterministic_id("chunk", title),
            title=title,
            metadata=(),
        )
        for title in titles
    )

    assert catalog_matches("准备设备部署阶段时应该参考哪份材料", documents) == (
        documents[1],
    )
    assert catalog_matches("安装验收用哪个报告？", documents) == (documents[0],)


@pytest.mark.parametrize(
    "operation", ("generation", "query.interpret", "query.rewrite")
)
def test_chat_strategy_and_timeout_reach_provider_http_client(
    operation: str,
) -> None:
    """各 Chat 用途使用同一显式推理策略，超时继续穿透 Adapter。"""
    transport = Mock()
    transport.request_json.side_effect = ValueError("sentinel")
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="test-chat",
            egress_allowed=True,
            disable_thinking_supported=True,
            disable_thinking=True,
        ),
        http_client=transport,
        api_key_resolver=lambda: "",
    )

    with pytest.raises(ValueError, match="sentinel"):
        adapter.complete(
            (ChatMessage(role="user", content="只理解检索问题。"),),
            operation=operation,
            max_output_tokens=256,
            timeout_seconds=3.0,
        )

    assert transport.request_json.call_args.kwargs["timeout_seconds"] == 3.0
    assert transport.request_json.call_args.kwargs["payload"][
        "chat_template_kwargs"
    ] == {"enable_thinking": False}


def test_chat_strategy_reaches_stream_provider_http_client() -> None:
    """流式正式回答与同步生成使用同一个显式推理策略。"""
    transport = Mock()
    transport.request_stream.side_effect = ValueError("sentinel")
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="test-chat",
            egress_allowed=True,
            disable_thinking_supported=True,
            disable_thinking=True,
        ),
        http_client=transport,
        api_key_resolver=lambda: "",
    )

    with pytest.raises(ValueError, match="sentinel"):
        adapter.complete_stream(
            (ChatMessage(role="user", content="只回答检索结果。"),),
            on_delta=lambda _delta: None,
            cancellation=StreamCancellation(),
        )

    assert transport.request_stream.call_args.kwargs["payload"][
        "chat_template_kwargs"
    ] == {"enable_thinking": False}


def test_reasoning_effort_is_bounded_by_question_shape() -> None:
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "wb08r-effort"),
        knowledge_base_id=deterministic_id("kb", "wb08r-effort"),
    )
    analyzer = QueryAnalyzer()

    def classify(question: str) -> ReasoningEffort:
        return reasoning_effort(
            analyzer.analyze(SearchRequest(scope=scope, text=question))
        )

    assert (
        classify("《项目发布阶段参考说明》在哪里？") is ReasoningEffort.DIRECT
    )
    assert classify("项目发布有哪些职责？") is ReasoningEffort.DIRECT
    assert classify("甲计划包括哪些类型？") is ReasoningEffort.DIRECT
    assert (
        classify("甲机构在乙项目中需要负责哪些具体职责？")
        is ReasoningEffort.ASSISTED
    )
    assert (
        classify("甲机构负责审核丙事项的职责是什么？")
        is ReasoningEffort.ASSISTED
    )
    assert classify("发布前看啥？") is ReasoningEffort.ASSISTED
    assert (
        classify("谁负责输入资料，流程如何进行，同时需要多少天？")
        is ReasoningEffort.DEEP
    )


def test_invalid_adaptive_plan_returns_deterministic_fallback() -> None:
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "wb08r-plan"),
        knowledge_base_id=deterministic_id("kb", "wb08r-plan"),
    )
    request = SearchRequest(scope=scope, text="甲流程咋办？")
    analysis = QueryAnalyzer().analyze(request)

    class InvalidPlannerAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.calls += 1
            return SimpleNamespace(content="{invalid", call=None)

    adapter = InvalidPlannerAdapter()
    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = adapter  # type: ignore[assignment]
    model.settings = KnowledgeBaseModelSettings()  # type: ignore[assignment]
    outcome = model.plan_adaptive(request, analysis, ReasoningEffort.ASSISTED)

    assert adapter.calls == 1
    assert outcome.attempted
    assert outcome.reason_code == "PLANNER_INVALID_JSON"
    assert outcome.standalone_query is None


@pytest.mark.parametrize(
    ("question", "effort"),
    (
        ("这个流程咋办？", ReasoningEffort.ASSISTED),
        (
            "谁负责输入资料，同时流程需要几天？",
            ReasoningEffort.DEEP,
        ),
    ),
)
def test_assisted_and_deep_use_one_planner(
    tmp_path: Path, question: str, effort: ReasoningEffort
) -> None:
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "wb08r-plan-routing"),
        knowledge_base_id=deterministic_id("kb", "wb08r-plan-routing"),
    )

    class CountingPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def plan_adaptive(self, *args: object) -> AdaptivePlanOutcome:
            del args
            self.calls += 1
            return AdaptivePlanOutcome(
                reason_code="ADAPTIVE_PLAN_INVALID", attempted=True
            )

    planner = CountingPlanner()
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(scope.project_id, "Plan")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "Plan KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=_documents(scope, ("通用审批流程",)),
            idempotency_key="wb08r-plan-routing",
            budgets=runtime.persistence.default_budgets(),
        )
        runtime.retrieval._adaptive_planner = planner  # type: ignore[assignment]
        result = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text=question)
        )

    assert planner.calls == 1
    assert result.reasoning_effort == effort.value
    assert result.interpret_reason_code == "ADAPTIVE_PLAN_INVALID"


def test_insufficient_evidence_never_repeats_full_generation() -> None:
    evidence = EvidenceAssembler().assemble(
        (make_ranked_chunk(1, "资料目录只有标题。"),), RetrievalPolicy()
    )

    class InvalidGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, request: object) -> None:
            del request
            self.calls += 1
            raise ValidationFailed(
                "目录声明缺少正文支持。",
                stage="answer.validate",
                code="CATALOG_CLAIM_UNSUPPORTED",
            )

    generator = InvalidGenerator()
    outcome = GroundedAnsweringService(generator).answer(  # type: ignore[arg-type]
        "资料目录说明了什么？",
        evidence,
        ConfidenceDecision(
            status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
            score=0.0,
        ),
        answer_support_set=(),
    )

    assert generator.calls == 1
    assert outcome.answer is None


def test_disable_thinking_requires_declared_compatibility() -> None:
    messages = (ChatMessage(role="user", content="测试"),)
    unsupported = openai_compatible_chat_payload(
        messages,
        OpenAICompatibleChatConfig(model="Qwen/Qwen3-8B-AWQ"),
        disable_thinking=True,
    )
    supported = openai_compatible_chat_payload(
        messages,
        OpenAICompatibleChatConfig(
            model="Qwen/Qwen3-8B-AWQ",
            disable_thinking_supported=True,
        ),
        disable_thinking=True,
    )

    assert unsupported["temperature"] == 0
    assert "chat_template_kwargs" not in unsupported
    assert supported["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(
    ("mode", "field"),
    [
        ("response_format", "response_format"),
        ("structured_outputs", "structured_outputs"),
        ("guided_json", "guided_json"),
        ("none", None),
    ],
)
def test_structured_output_payload_uses_one_explicit_mode(
    mode: str, field: str | None
) -> None:
    payload = openai_compatible_chat_payload(
        (ChatMessage(role="user", content="虚构检查"),),
        OpenAICompatibleChatConfig(
            model="Qwen/Qwen3-8B-AWQ",
            structured_output_mode=mode,
        ),
        json_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        schema_revision="fictional-probe-v1",
    )
    structured_fields = {"response_format", "structured_outputs", "guided_json"}
    assert structured_fields.intersection(payload) == (
        {field} if field else set()
    )
