"""在本地解析固定公开语料，计划完整 P11 路径而不激活授权。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evaluation.p11_pilot_data import PilotDataset, load_pilot_dataset
from evaluation.p11_pilot_runtime import query_budget_lower_bound
from rag_app.adapters.chunkers.docx_structural import DocxStructuralChunker
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser
from rag_app.adapters.providers.aliyun_qwen37 import AliyunQwen37EmbeddingConfig
from rag_app.adapters.providers.batching import BatchLimits, batch_texts
from rag_app.application.retrieval.reranking import _bounded_text
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    ChunkingPolicy,
    DocumentRef,
    HydratedChunk,
    ParseContext,
    ParseSource,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.policies import ParsingPolicy
from rag_app.core.tokenization import estimate_tokens
from rag_app.product.live_acceptance_payloads import (
    DOCUMENT_NAME,
    QUERIES,
    functional_document,
)
from rag_app.product.provider_runtime import (
    _SYNTHETIC_RERANK_DOCUMENTS,
    _SYNTHETIC_TEXT,
)

_LEVELS = ("lower_bound", "planned", "capped_max")
_PROVIDERS = ("jina", "aliyun")
_MAX_ATTEMPTS = 3
_PLANNED_EXTRA_ATTEMPTS_PER_PROVIDER = 4


@dataclass(frozen=True)
class _DocumentViews:
    embedding: tuple[str, ...]
    rerank: tuple[str, ...]


@dataclass(frozen=True)
class _Operation:
    operation: str
    provider: str
    input_items: int
    requests: int
    tokens: int
    assumptions: str
    reusable: bool = False
    required_http: bool = False
    query_repetition_allowance: int = 0
    maximum_request_tokens: int | None = None

    def report(self) -> dict[str, Any]:
        """输出同一操作的输入估算与三档预算。

        Args:
            无参数；读取当前操作的固定公开数据估算。

        Returns:
            不含批准或供应商实测声明的计划行。

        """
        minimum = self.required_http and not self.reusable
        return {
            "operation": self.operation,
            "provider": self.provider,
            "input_items": self.input_items,
            "http_count": self.requests,
            "estimated_tokens": self.tokens,
            "observed_tokens_if_known": None,
            "cache_reusable": self.reusable,
            "max_attempts": 1
            if self.operation.startswith("canary.")
            else _MAX_ATTEMPTS,
            "assumptions": self.assumptions,
            "query_repetition_allowance": self.query_repetition_allowance,
            "maximum_request_estimated_tokens": (
                self.tokens + self.query_repetition_allowance
                if self.maximum_request_tokens is None
                else self.maximum_request_tokens
            ),
            "lower_bound": {
                "requests": self.requests if minimum else 0,
                "estimated_input_tokens": self.tokens if minimum else 0,
            },
            "planned": {
                "requests": self.requests,
                "estimated_input_tokens": self.tokens,
            },
            "capped_max": {
                "requests": self.requests,
                "estimated_input_tokens": self.tokens
                + self.query_repetition_allowance,
            },
        }


def build_p11_budget_plan(
    history: dict[str, Any],
    *,
    query_instruct: str | None = None,
    retrieval_policy: RetrievalPolicy | None = None,
) -> dict[str, Any]:
    """生成完整路径的 PROPOSED 累计方案，任何估算都不是批准。

    Args:
        history: 当前只读账本 summary（含 providers），不能传入 Secret。
        query_instruct: 当前方案 instruct；缺方案时明确使用产品默认假设。
        retrieval_policy: 当前候选截断/候选数策略；缺方案时使用默认值。

    Returns:
        保留 30 问两路、文档、重排、局部故障与有限重试的安全计划。

    """
    dataset = load_pilot_dataset()
    policy = retrieval_policy or RetrievalPolicy()
    instruct = query_instruct
    if instruct is None:
        instruct = str(
            AliyunQwen37EmbeddingConfig.model_fields["query_instruct"].default
        )
    documents, candidates = _corpus_views(dataset, policy)
    functional = _parse_document(functional_document(), DOCUMENT_NAME, policy)
    operations = _canary_operations(instruct)
    operations.extend(_document_operations(documents, functional))
    operations.extend(_functional_operations(functional, instruct))
    operations.extend(_pilot_operations(dataset, candidates, instruct, policy))
    rows = [operation.report() for operation in operations]
    rows.extend(_retry_rows(rows))
    totals = _totals(rows)
    providers = _provider_totals(rows, history)
    used_requests = int(history.get("reserved", 0))
    used_tokens = int(history.get("estimated_input_tokens", 0))
    request_limit = int(history.get("request_limit", 25))
    token_limit = int(history.get("estimated_token_limit", 1000))
    return {
        "status": "PROPOSED",
        "activated": False,
        "approver": None,
        "approved_at": None,
        "new_provider_http": 0,
        "dataset_sha256": dataset.dataset_sha256,
        "sample_count": len(dataset.cases),
        "lanes": ["primary", "standby"],
        "quality_thresholds_changed": False,
        "query_embedding_only_lower_bound": query_budget_lower_bound(
            dataset, instruct
        ),
        "query_instruct_identity": canonical_sha256(instruct),
        "configuration_assumption": (
            "当前未提供 Profile，按产品默认 query_instruct 和 RetrievalPolicy；"
            "实际方案确定后必须重新生成，不能直接激活本计划。"
            if query_instruct is None or retrieval_policy is None
            else "按调用方已读取的当前非秘密 Profile 策略。"
        ),
        "estimate_semantics": (
            "estimated 是仓库 Unicode code point 启发式输入估算，"
            "不是账单 usage，也不保证供应商 Token 严格上界。"
            "planned 重排按作用域全部可能候选（受原候选上限约束），"
            "不读取标准答案；capped_max 另计候选重复 query 余量。"
        ),
        "lower_bound_semantics": (
            "仅保留必须实际 HTTP 的 canary 和两路查询；未证实文档缓存"
            "与候选命中时允许文档/重排为零下限，不作为完整扩额建议。"
        ),
        "history": history,
        "current_authorization": {
            "request_limit": request_limit,
            "estimated_token_limit": token_limit,
            "provider_token_limits": history.get(
                "provider_token_limits", {"jina": 600, "aliyun": 600}
            ),
            "used_requests": used_requests,
            "used_estimated_tokens": used_tokens,
            "remaining_requests": max(0, request_limit - used_requests),
            "remaining_estimated_tokens": max(0, token_limit - used_tokens),
        },
        "operations": rows,
        "totals_additional_work": totals,
        "per_provider": providers,
        "proposed_cumulative_limits": {
            "request_limit": max(
                request_limit, used_requests + totals["capped_max"]["requests"]
            ),
            "estimated_token_limit": max(
                token_limit,
                used_tokens + totals["capped_max"]["estimated_input_tokens"],
            ),
            "provider_request_limits": {
                key: value["cumulative_capped_requests"]
                for key, value in providers.items()
            },
            "provider_token_limits": {
                key: value["cumulative_capped_estimated_tokens"]
                for key, value in providers.items()
            },
            "step_request_limits": _step_limits(rows, history),
        },
        "local_fault_injection": _local_faults(dataset, instruct),
        "cache_contract": (
            "文档向量仅在文本/角色/模型/维度/策略/slot 身份全相同时复用；"
            "当前未扣除任何未验证命中。真实 canary、failover、recovery "
            "和逐题两路 HTTP 证据不可由缓存替代。"
        ),
        "retry_policy": {
            "sdk_max_attempts_including_initial": _MAX_ATTEMPTS,
            "planned_extra_attempts_per_provider": (
                _PLANNED_EXTRA_ATTEMPTS_PER_PROVIDER
            ),
            "capped_max_extra_attempts_per_operation": _MAX_ATTEMPTS - 1,
            "canary_max_attempts_including_initial": 1,
            "unbounded_or_repeated_runs_included": False,
        },
    }


def _parse_document(
    content: bytes, display_name: str, policy: RetrievalPolicy
) -> _DocumentViews:
    reference = DocumentRef(
        project_id=deterministic_id("prj", "p11-budget-plan"),
        knowledge_base_id=deterministic_id("kb", "p11-budget-plan"),
        document_id=deterministic_id("doc", "p11-budget-plan", display_name),
        display_name=display_name,
    )
    parsed = WordDocumentV1Parser().parse(
        ParseSource(
            media_type="application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            display_name=display_name,
            extension=".docx",
            content=content,
        ),
        ParsingPolicy(),
        ParseContext(document=reference),
    )
    chunker = DocxStructuralChunker(ChunkingPolicy())
    result = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id("irev", "p11-budget-plan"),
        ),
    )
    return _DocumentViews(
        tuple(chunk.embedding_text for chunk in result.chunks),
        tuple(
            _bounded_text(
                RankedChunk(
                    hydrated=HydratedChunk(
                        chunk=chunk, display_name=display_name
                    ),
                    fusion_rank=1,
                ),
                policy.rerank_text_char_limit,
            )
            for chunk in result.chunks
        ),
    )


def _corpus_views(
    dataset: PilotDataset, policy: RetrievalPolicy
) -> tuple[list[_DocumentViews], dict[str, list[str]]]:
    documents = []
    active: dict[str, list[str]] = {}
    for item in dataset.documents:
        for index, version in enumerate(item.document.versions):
            views = _parse_document(
                item.content(index), version.display_name, policy
            )
            documents.append(views)
            if index == len(item.document.versions) - 1:
                active.setdefault(item.document.knowledge_base_id, []).extend(
                    views.rerank
                )
    return documents, active


def _canary_operations(instruct: str) -> list[_Operation]:
    text_tokens = estimate_tokens(_SYNTHETIC_TEXT)
    result: list[_Operation] = []
    for provider in _PROVIDERS:
        result.extend(
            _Operation(
                f"canary.embedding.{role}",
                provider,
                1,
                1,
                text_tokens
                + (
                    estimate_tokens(instruct)
                    if provider == "aliyun" and role == "query"
                    else 0
                ),
                "过期或未验证 canary 预留一次；若同操作证据仍有效可依法复用。",
                required_http=True,
            )
            for role in ("document", "query")
        )
    result.append(
        _Operation(
            "canary.reranking",
            "jina",
            1 + len(_SYNTHETIC_RERANK_DOCUMENTS),
            1,
            text_tokens
            + sum(
                estimate_tokens(text) for text in _SYNTHETIC_RERANK_DOCUMENTS
            ),
            "Jina reranking 有效性未证明时保留原验证操作。",
            required_http=True,
            query_repetition_allowance=text_tokens
            * (len(_SYNTHETIC_RERANK_DOCUMENTS) - 1),
        )
    )
    return result


def _document_operations(
    documents: list[_DocumentViews], functional: _DocumentViews
) -> list[_Operation]:
    rows: list[_Operation] = []
    for name, views in (("functional", [functional]), ("pilot", documents)):
        rows.extend(
            _Operation(
                f"{name}.embedding.document",
                provider,
                sum(len(item.embedding) for item in views),
                sum(
                    len(batch_texts(item.embedding, BatchLimits()))
                    for item in views
                ),
                sum(
                    estimate_tokens(text)
                    for item in views
                    for text in item.embedding
                ),
                "实际 Parser/Chunker，全部版本逐 ingestion 分批，"
                "冷缓存；不合并不同 job。",
                reusable=True,
                maximum_request_tokens=max(
                    sum(estimate_tokens(text) for text in batch)
                    for item in views
                    for batch in batch_texts(item.embedding, BatchLimits())
                ),
            )
            for provider in _PROVIDERS
        )
    return rows


def _functional_operations(
    document: _DocumentViews, instruct: str
) -> list[_Operation]:
    rows = []
    for step, provider in (
        ("primary_query", "jina"),
        ("standby_failover", "aliyun"),
        ("recovery", "jina"),
        ("standby_unavailable", "local"),
    ):
        query = QUERIES[step]
        tokens = estimate_tokens(query)
        if provider != "local":
            rows.append(
                _Operation(
                    f"functional.{step}.embedding.query",
                    provider,
                    1,
                    1,
                    tokens
                    + (
                        estimate_tokens(instruct) if provider == "aliyun" else 0
                    ),
                    "每次必须有真实路由 HTTP；本地注入另列。",
                    required_http=True,
                )
            )
        rows.append(
            _Operation(
                f"functional.{step}.reranking",
                "jina",
                1 + len(document.rerank),
                1,
                tokens + sum(estimate_tokens(text) for text in document.rerank),
                "按完整功能语料候选预留，不缩小到标准答案。",
                query_repetition_allowance=tokens
                * max(0, len(document.rerank) - 1),
            )
        )
    return rows


def _pilot_operations(
    dataset: PilotDataset,
    candidates: dict[str, list[str]],
    instruct: str,
    policy: RetrievalPolicy,
) -> list[_Operation]:
    query_tokens = sum(estimate_tokens(case.query) for case in dataset.cases)
    rows = [
        _Operation(
            "pilot.embedding.query",
            provider,
            len(dataset.cases),
            len(dataset.cases),
            query_tokens
            + (
                len(dataset.cases) * estimate_tokens(instruct)
                if provider == "aliyun"
                else 0
            ),
            "固定30问每题两路；当前逐题执行，不声称供应商不能batch。",
            required_http=True,
            maximum_request_tokens=max(
                estimate_tokens(case.query)
                + (estimate_tokens(instruct) if provider == "aliyun" else 0)
                for case in dataset.cases
            ),
        )
        for provider in _PROVIDERS
    ]
    tokens = 0
    input_items = 0
    repetition = 0
    maximum = 0
    for case in dataset.cases:
        eligible = sorted(
            (
                estimate_tokens(text)
                for text in candidates[case.knowledge_base_id]
            ),
            reverse=True,
        )[: policy.rerank_candidate_limit]
        tokens += 2 * (estimate_tokens(case.query) + sum(eligible))
        input_items += 2 * (1 + len(eligible))
        repetition += (
            2 * estimate_tokens(case.query) * max(0, len(eligible) - 1)
        )
        maximum = max(
            maximum,
            estimate_tokens(case.query) * max(1, len(eligible)) + sum(eligible),
        )
    rows.append(
        _Operation(
            "pilot.reranking",
            "jina",
            input_items,
            len(dataset.cases) * 2,
            tokens,
            "每题每路预留重排；取同KB当前版本全部候选中最长的原上限数量，实际命中待运行。",
            query_repetition_allowance=repetition,
            maximum_request_tokens=maximum,
        )
    )
    return rows


def _retry_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for provider in _PROVIDERS:
        selected = [
            row
            for row in rows
            if row["provider"] == provider and row["max_attempts"] > 1
        ]
        total_extra = sum(
            row["http_count"] * (row["max_attempts"] - 1) for row in selected
        )
        maximum = max(
            row["maximum_request_estimated_tokens"] for row in selected
        )
        planned = min(total_extra, _PLANNED_EXTRA_ATTEMPTS_PER_PROVIDER)
        row = _Operation(
            "retry.allowance",
            provider,
            0,
            planned,
            planned * maximum,
            "planned每供应商最多4次额外尝试，以最大单HTTP输入估算预留；"
            "capped_max包含每次SDK请求最多2次重试；所有canary无重试。",
        ).report()
        row["capped_max"] = {
            "requests": total_extra,
            "estimated_input_tokens": sum(
                item["capped_max"]["estimated_input_tokens"]
                * (item["max_attempts"] - 1)
                for item in selected
            ),
        }
        result.append(row)
    return result


def _totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        level: {
            key: sum(row[level][key] for row in rows)
            for key in ("requests", "estimated_input_tokens")
        }
        for level in _LEVELS
    }


def _provider_totals(
    rows: list[dict[str, Any]], history: dict[str, Any]
) -> dict[str, Any]:
    result = {}
    for provider in _PROVIDERS:
        totals = _totals([row for row in rows if row["provider"] == provider])
        used = history.get("providers", {}).get(provider, {})
        token_limit = int(
            history.get("provider_token_limits", {}).get(provider, 600)
        )
        result[provider] = {
            "additional_work": totals,
            "current_token_limit": token_limit,
            "used_estimated_tokens": used.get("estimated_input_tokens", 0),
            "remaining_estimated_tokens": max(
                0, token_limit - int(used.get("estimated_input_tokens", 0))
            ),
            "cumulative_capped_requests": int(used.get("reserved", 0))
            + totals["capped_max"]["requests"],
            "cumulative_capped_estimated_tokens": max(
                token_limit,
                int(used.get("estimated_input_tokens", 0))
                + totals["capped_max"]["estimated_input_tokens"],
            ),
        }
    return result


def _step_limits(
    rows: list[dict[str, Any]], history: dict[str, Any]
) -> dict[str, int]:
    limits = {
        step: max(1, int(value["reserved"]))
        for step, value in history.get("steps", {}).items()
    } or {"historical": max(1, int(history.get("reserved", 0)))}
    for row in rows:
        operation = row["operation"]
        if operation == "retry.allowance":
            continue
        if operation.startswith("pilot."):
            step = "citation_quality"
        elif operation == "functional.embedding.document":
            step = "dual_index"
        elif operation.startswith("canary."):
            step = (
                "jina_connection"
                if row["provider"] == "jina"
                else "aliyun_document_canary"
                if operation.endswith("document")
                else "aliyun_query_canary"
            )
        else:
            step = operation.split(".")[1]
            if step == "standby_unavailable":
                step = "recovery"
        limits[step] = (
            limits.get(step, 0) + row["http_count"] * row["max_attempts"]
        )
    return limits


def _local_faults(dataset: PilotDataset, instruct: str) -> dict[str, Any]:
    queries = [QUERIES["standby_failover"], QUERIES["standby_unavailable"]]
    query_tokens = sum(estimate_tokens(text) for text in queries)
    pilot_tokens = sum(estimate_tokens(case.query) for case in dataset.cases)
    return {
        "provider_http": 0,
        "planned_attempts": len(dataset.cases) + 3,
        "capped_attempts": (len(dataset.cases) + 3) * _MAX_ATTEMPTS,
        "estimated_tokens_if_intercepted_before_forwarding": (
            pilot_tokens
            + query_tokens
            + estimate_tokens(QUERIES["standby_unavailable"])
            + estimate_tokens(instruct)
        ),
        "assumptions": (
            "standby每题局部拦主路，功能failover拦主路，双槽不可用拦两路；"
            "本地短路/熔断可能减少attempt，不算Provider HTTP，"
            "不自动退款未知转发。"
        ),
    }
