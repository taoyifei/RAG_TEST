"""Planner 传输超时与 JSON/Schema 错误分别审计且不重试。"""

from __future__ import annotations

from rag_app.application.retrieval.adaptive import ReasoningEffort
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.errors import ProviderUnavailable
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings


def test_planner_timeout_has_separate_category_and_one_attempt() -> None:
    request = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "planner-timeout"),
            knowledge_base_id=deterministic_id("kb", "planner-timeout"),
        ),
        text="甲什么时候提交，乙多久审核？",
    )

    class TimeoutAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.timeout_seconds = 0.0

        def complete(self, *_args: object, **kwargs: object) -> object:
            self.calls += 1
            self.timeout_seconds = kwargs["timeout_seconds"]  # type: ignore[assignment]
            raise ProviderUnavailable(
                "模型暂不可用。",
                stage="test.planner",
                details={"reason_code": "READ_TIMEOUT"},
            )

    adapter = TimeoutAdapter()
    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = adapter  # type: ignore[assignment]
    model.settings = KnowledgeBaseModelSettings()  # type: ignore[assignment]

    outcome = model.plan_adaptive(
        request, QueryAnalyzer().analyze(request), ReasoningEffort.DEEP
    )

    assert adapter.calls == 1
    assert adapter.timeout_seconds == 8.0
    assert outcome.reason_code == "PLANNER_PROVIDER_TIMEOUT"
    assert outcome.failure_category == "PLANNER_PROVIDER_TIMEOUT"
    assert outcome.schema_fallback_detail == "READ_TIMEOUT"
    assert outcome.planner_transport_timeout_ms == 8000
