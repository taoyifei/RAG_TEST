"""在独立候选容器内对冻结问题执行 Planner-only 诊断。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.application.retrieval.adaptive import reasoning_effort
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.wanshitong.internal_model_settings import (
    InternalCredentialSettings,
    InternalModelSettings,
)

_CASE_COUNT = 24
_PLANNER_P95_BUDGET_MS = 5_000


def _credential(settings: InternalCredentialSettings) -> str:
    if settings.source == "environment":
        return os.environ.get(settings.environment_name or "", "")
    if settings.source == "file":
        return settings.database_secret()
    return ""


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _cases(path: Path) -> tuple[dict[str, Any], ...]:
    rows = tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len(rows) != _CASE_COUNT or len({row["case_id"] for row in rows}) != len(
        rows
    ):
        raise ValueError("Planner-only 冻结子集必须恰好包含 24 个唯一问题。")
    if any(
        hashlib.sha256(row["question"].encode()).hexdigest()
        != row["question_sha256"]
        for row in rows
    ):
        raise ValueError("冻结问题摘要不一致。")
    return rows


def run(path: Path) -> int:
    """只发送 Planner 请求，结果不包含问题正文或模型原始 JSON。"""
    settings = InternalModelSettings.from_environment()
    client = ProviderHttpClient(
        settings.llm_base_url,
        max_attempts=1,
        allow_http=True,
        use_budget_transport=False,
    )
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model=settings.llm_model,
            egress_allowed=True,
            disable_thinking_supported=(
                settings.llm_disable_thinking_supported
            ),
            structured_output_mode=settings.llm_structured_output_mode,
        ),
        http_client=client,
        api_key_resolver=lambda: _credential(settings.llm_credential),
    )
    planner = object.__new__(ProductGroundedModel)
    planner._campaign_required = False
    planner.adapter = adapter
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "planner-probe"),
        knowledge_base_id=deterministic_id("kb", "planner-probe"),
    )
    analyzer = QueryAnalyzer()
    records: list[dict[str, object]] = []
    try:
        for row in _cases(path):
            context = row.get("context_question")
            request = SearchRequest(
                scope=scope,
                text=row["question"],
                conversation_context=(context,)
                if isinstance(context, str)
                else (),
            )
            analysis = analyzer.analyze(request)
            effort = reasoning_effort(
                analysis, has_context=bool(request.conversation_context)
            )
            started = time.perf_counter()
            outcome = planner.plan_adaptive(request, analysis, effort)
            record: dict[str, object] = {
                "case_id": row["case_id"],
                "effort": effort.value,
                "planner_called": outcome.attempted,
                "schema_mode": outcome.structured_output_mode,
                "schema_fallback_detail": outcome.schema_fallback_detail,
                "reason_code": outcome.reason_code,
                "atom_count": len(outcome.atoms),
                "fragment_coverage": (
                    1.0 if outcome.atoms and outcome.attempted else None
                ),
                "latency_ms": round(
                    (time.perf_counter() - started) * 1000, 2
                ),
            }
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        adapter.close()
    called = [row for row in records if row["planner_called"]]
    latencies = [float(row["latency_ms"]) for row in called]
    fallbacks = sum(
        row["reason_code"] == "ADAPTIVE_PLAN_SCHEMA_FALLBACK"
        for row in called
    )
    summary = {
        "count": len(records),
        "planner_calls": len(called),
        "schema_mode": settings.llm_structured_output_mode,
        "thinking_disable_supported": settings.llm_disable_thinking_supported,
        "schema_fallbacks": fallbacks,
        "fallback_rate": fallbacks / len(called) if called else 0.0,
        "p50_ms": statistics.median(latencies) if latencies else None,
        "p95_ms": _percentile(latencies, 0.95),
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
    return 0 if (
        len(records) == _CASE_COUNT
        and fallbacks == 0
        and (summary["p95_ms"] or 0) <= _PLANNER_P95_BUDGET_MS
    ) else 1


def main() -> None:
    """只读取候选容器内的临时冻结子集文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.input))


if __name__ == "__main__":
    main()
