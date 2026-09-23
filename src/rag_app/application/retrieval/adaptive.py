"""通用查询工作量分级与可信文档目录匹配。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Protocol

from rag_app.core.models import (
    FieldCandidate,
    FieldResolution,
    ProviderCall,
    QueryAnalysis,
    ResolvedQueryView,
    SearchRequest,
)
from rag_app.core.models.query_plan import QueryAtom
from rag_app.core.ports.evidence_source import CatalogDocument

_TITLE = re.compile(r"《([^》]{2,200})》")
_NAVIGATION = re.compile(
    r"哪份(?:材料|资料|文档|文件|模板)"
    r"|(?:什么|哪些)(?:资料|文档|文件|模板)(?:可|可以|能|能够)?(?:参考|查阅|下载)"
    r"|(?:哪个|哪份)(?:纪要|记录|报告|方案|计划|规范|办法|制度|表格)"
    r"|(?:用|选|采用|参考)哪份"
    r"|有没有.{0,50}模板|是否有.{0,50}模板|找.{0,50}(?:文档|材料|模板)"
    r"|参考.{0,50}(?:文档|材料|模板)|(?:文档|材料|模板).{0,12}(?:在哪|哪里)"
    r"|(?:文档|材料|模板|模版).{0,12}(?:哪找|在哪|哪里|有吗|上哪找)"
    r"|(?:有|存在).{0,50}(?:文档|材料|模板|模版)"
)
_CONTENT_REQUEST = re.compile(
    r"(?:怎么|如何)(?:填写|编写|操作)|填写项|字段|正文内容|具体要求"
)
_CONTENT_ENUMERATION = re.compile(
    r"(?:什么|哪些)(?:材料|资料|文档|文件|模板)"
    r"(?!(?:可|可以|能|能够)?(?:参考|查阅|下载))"
)
_COMPOUND = re.compile(
    r"(?:同时|分别|并且|以及|此外)|(?:输入|前提|条件).{0,35}(?:流程|时限|步骤)"
    r"|(?:流程|步骤).{0,35}(?:时限|条件)|(?:谁|哪个部门).{0,35}(?:何时|多久)"
    r"|(?:先|首先).{0,35}(?:再|然后|最后)"
)
_CONTEXT_REFERENCE = re.compile(r"这个|那个|上述|前者|后者|其中|它|这些|那些")
_ROLE_ENUMERATION = re.compile(
    r"[^，,；;。！？?!、]{1,24}(?:、[^，,；;。！？?!、]{1,24}){2,}"
)
_NAVIGATION_FILLER = re.compile(
    r"上哪找|哪找|有没有|是否有|哪份|哪些|什么|哪个|应该|应当|需要|可以|请问|请|"
    r"准备|参考|查找|找|看|使用|用|阶段|时候|时|的|材料|资料|文档|"
    r"文件|模板|在哪|哪里|相关|有关|一下|一份|一张|一套|是|有|该|做|吗"
)
_PUNCTUATION = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_MIN_SUBSTRING_CHARS = 3
_MIN_OVERLAP = 2
_FUZZY_TARGET_MIN_CHARS = 6
_FUZZY_SHARED_MIN_PAIRS = 3
_FUZZY_SHARED_RATIO = 0.8
_MIN_TARGET_CHARS = 2
_MIN_CATALOG_SCORE = 0.78
_MAX_SCORE_GAP = 0.12
_MAX_CATALOG_CANDIDATES = 3
_FRAGMENT_WIDTH = 2
_FRAGMENT_MIN_CHARS = 4
_FRAGMENT_MAX_CHARS = 10
_MIN_FRAGMENT_COUNT = 2


class ReasoningEffort(StrEnum):
    """单次查询允许的语义理解工作量。"""

    DIRECT = "DIRECT"
    ASSISTED = "ASSISTED"
    DEEP = "DEEP"


class FieldResolutionExecutionState(StrEnum):
    """与字段语义结果正交的 Provider 执行状态。"""

    NOT_NEEDED = "NOT_NEEDED"
    SUCCEEDED = "SUCCEEDED"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"


@dataclass(frozen=True, slots=True)
class AdaptivePlanOutcome:
    """轻量 Planner 的严格输出和实际调用记录。"""

    standalone_query: str | None = None
    intent: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    atoms: tuple[QueryAtom, ...] = ()
    route_hints: tuple[str, ...] = ()
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str = "ADAPTIVE_PLAN_NOT_NEEDED"
    attempted: bool = False
    schema_fallback_detail: str | None = None
    structured_output_mode: str = "none"
    schema_revision: str | None = None
    schema_sha256: str | None = None
    failure_category: str | None = None
    planner_latency_ms: int = 0
    planner_input_tokens: int | None = None
    planner_output_tokens: int | None = None
    planner_finish_reason: str | None = None
    planner_transport_timeout_ms: int = 0


@dataclass(frozen=True, slots=True)
class FieldResolutionOutcome:
    """后移到真实 schema 之后的一次字段解释结果。"""

    resolutions: tuple[FieldResolution, ...] = ()
    calls: tuple[ProviderCall, ...] = ()
    execution_state: FieldResolutionExecutionState = (
        FieldResolutionExecutionState.NOT_NEEDED
    )
    reason_code: str = "FIELD_RESOLUTION_NOT_NEEDED"
    attempted: bool = False
    failure_category: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    transport_timeout_ms: int = 0
    schema_revision: str | None = None
    schema_sha256: str | None = None
    contract_sha256: str | None = None
    capability_profile_sha256: str | None = None
    response_content_sha256: str | None = None
    response_content_length: int | None = None
    invalid_atom_id: str | None = None
    invalid_status: str | None = None
    invalid_candidate_count: int | None = None
    invalid_query_fragment_length: int | None = None
    private_diagnostic_status: str | None = None


class AdaptivePlannerPort(Protocol):
    """复用当前回答连接的一次轻量语义规划。"""

    def plan_adaptive(
        self,
        request: SearchRequest,
        analysis: QueryAnalysis,
        effort: ReasoningEffort,
    ) -> AdaptivePlanOutcome:
        """只解释问题，不生成答案。"""
        ...

    def resolve_fields(
        self,
        request: SearchRequest,
        candidates: tuple[FieldCandidate, ...],
        *,
        query_view: ResolvedQueryView,
        atoms: tuple[QueryAtom, ...],
        timeout_seconds: float | None = None,
    ) -> FieldResolutionOutcome:
        """在真实 schema 已知后选择短字段候选 ID。"""
        ...

    @property
    def field_resolution_total_deadline_seconds(self) -> float:
        """返回一个逻辑请求全部字段小包共享的总时限。"""
        ...


def reasoning_effort(
    analysis: QueryAnalysis, *, has_context: bool = False
) -> ReasoningEffort:
    """按通用问句形状决定是否值得调用模型 Planner。"""
    query = analysis.normalized_query
    if is_navigation_query(query):
        return ReasoningEffort.DIRECT
    if (
        _COMPOUND.search(query)
        or _ROLE_ENUMERATION.search(query)
        or query.count("？") + query.count("?") > 1
    ):
        return ReasoningEffort.DEEP
    ambiguous_duties = (
        analysis.semantics.answer_type.value == "DUTIES"
        and analysis.semantics.target
        and re.search(r"负责|承担|需要|哪些|什么|啥", analysis.semantics.target)
    )
    if (
        _CONTEXT_REFERENCE.search(query)
        or ambiguous_duties
        or analysis.semantics.answer_type.value == "UNKNOWN"
        or analysis.semantics.source == "ORIGINAL_FALLBACK"
        or (has_context and not analysis.semantics.target)
    ):
        return ReasoningEffort.ASSISTED
    return ReasoningEffort.DIRECT


def is_navigation_query(query: str) -> bool:
    """仅识别询问文档身份/存在性的通用语言形状。"""
    if _CONTENT_REQUEST.search(query) or _CONTENT_ENUMERATION.search(query):
        return False
    return bool(
        _NAVIGATION.search(query)
        or (_TITLE.search(query) and re.search(r"在哪|哪里|哪找", query))
    )


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return _PUNCTUATION.sub("", re.sub(r"\.docx$", "", text))


def _bigrams(value: str) -> set[str]:
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _fragment_score(target: str, title: str) -> float:
    """短语被标题前后修饰语隔开时，要求各片段仍全部出现。"""
    if (
        not _FRAGMENT_MIN_CHARS <= len(target) <= _FRAGMENT_MAX_CHARS
        or len(target) % _FRAGMENT_WIDTH
    ):
        return 0.0
    fragments = tuple(
        target[index : index + _FRAGMENT_WIDTH]
        for index in range(0, len(target), _FRAGMENT_WIDTH)
    )
    if len(set(fragments)) < _MIN_FRAGMENT_COUNT or not all(
        fragment in title for fragment in fragments
    ):
        return 0.0
    return 0.84


def _match_score(target: str, document: CatalogDocument) -> float:
    metadata = dict(document.metadata)
    title = _normalized(document.title)
    if target == title:
        return 1.0
    if len(target) >= _MIN_SUBSTRING_CHARS and target in title:
        return 0.96
    if len(title) >= _MIN_SUBSTRING_CHARS and title in target:
        return 0.92
    values = [title]
    for key in ("department_name", "category_path", "topic_keys"):
        value = metadata.get(key)
        if isinstance(value, str):
            values.append(_normalized(value))
        elif isinstance(value, (tuple, list)):
            values.extend(
                _normalized(item) for item in value if isinstance(item, str)
            )
    query_pairs = _bigrams(target)
    if not query_pairs:
        return 0.0
    best = _fragment_score(target, title)
    for value in values:
        pairs = _bigrams(value)
        if not pairs:
            continue
        overlap = len(query_pairs & pairs)
        if overlap < _MIN_OVERLAP:
            continue
        best = max(best, 2 * overlap / (len(query_pairs) + len(pairs)))
        if (
            len(target) >= _FUZZY_TARGET_MIN_CHARS
            and overlap >= _FUZZY_SHARED_MIN_PAIRS
        ):
            shared = sum(
                block.size
                for block in SequenceMatcher(
                    None, target, value, autojunk=False
                ).get_matching_blocks()
            )
            if shared / len(target) >= _FUZZY_SHARED_RATIO:
                best = max(best, 0.82)
    return best


def catalog_matches(
    query: str,
    documents: tuple[CatalogDocument, ...],
) -> tuple[CatalogDocument, ...]:
    """只依据活动文档元数据选出高置信目录项，最多返回三个。"""
    quoted = _TITLE.search(query)
    target = (
        _normalized(quoted[1])
        if quoted
        else _normalized(_NAVIGATION_FILLER.sub("", query))
    )
    if len(target) < _MIN_TARGET_CHARS:
        return ()
    requires_template = "模板" in query or "模版" in query
    eligible = (
        document
        for document in documents
        if not requires_template
        or "模板" in document.title
        or "模版" in document.title
        or "模板"
        in str(dict(document.metadata).get("source_relative_path", ""))
    )
    ranked = sorted(
        ((_match_score(target, document), document) for document in eligible),
        key=lambda pair: (-pair[0], pair[1].title, pair[1].document_id),
    )
    if not ranked or ranked[0][0] < (0.95 if quoted else _MIN_CATALOG_SCORE):
        return ()
    best = ranked[0][0]
    plausible = tuple(
        document
        for score, document in ranked
        if score >= _MIN_CATALOG_SCORE and best - score <= _MAX_SCORE_GAP
    )
    return (
        plausible[:_MAX_CATALOG_CANDIDATES]
        if len(plausible) <= _MAX_CATALOG_CANDIDATES
        else ()
    )


__all__ = [
    "AdaptivePlanOutcome",
    "AdaptivePlannerPort",
    "FieldResolutionOutcome",
    "ReasoningEffort",
    "catalog_matches",
    "is_navigation_query",
    "reasoning_effort",
]
