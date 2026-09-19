"""问题与已安全核验事实之间的三态判定；词法未知不能充当矛盾。"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import ParamSpec, TypeVar

from rag_app.core.errors import ValidationFailed
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    EvidenceItem,
    QueryAnalysis,
    RequestedAnswerType,
)
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import QueryAtom
from rag_app.core.query_text import normalize_semantic_text

_SUBJECT = re.compile(r"^(.+?(?:部门|团队|小组|组|经理|主管|负责人|系统))(.*)$")
_ACTION = re.compile(
    r"负责|承担|包括|包含|保存|归档|核对|审核|审批|制定|安排|完成"
)
_P = ParamSpec("_P")
_T = TypeVar("_T")
_REVIEWED: ContextVar[set[str] | None] = ContextVar(
    "reviewed_relations", default=None
)


def relation_review_scope(function: Callable[_P, _T]) -> Callable[_P, _T]:
    """每个回答请求独立保存已重核的质量信号，异常和取消都销毁作用域。"""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        token = _REVIEWED.set(set())
        try:
            return function(*args, **kwargs)
        finally:
            _REVIEWED.reset(token)

    return wrapped


def relation_review_key(
    atom: QueryAtom, claim: AnswerClaim, units: tuple[EvidenceItem, ...]
) -> str:
    """审批仅绑定原 Atom、原事实及稳定来源身份，不绑定临时 S 别名。"""
    keys = {item.support_id: stable_support_key(item) for item in units}
    return canonical_sha256(
        {
            "atom": atom.model_dump(mode="json"),
            "claim": claim.text,
            "supports": [(keys[s.support_id], s.quote) for s in claim.supports],
        }
    )


def record_relation_review(key: str) -> None:
    """仅在调用方完成响应与来源重核后记录当前请求内信号。"""
    reviewed = _REVIEWED.get()
    if reviewed is None:
        raise ValueError("关系复核必须位于回答请求作用域。")
    reviewed.add(key)


def has_relation_review(key: str) -> bool:
    """请求外或另一请求不可重用信号。"""
    return key in (_REVIEWED.get() or ())


def _phrase_covered(phrase: str, source: str) -> bool:
    """仅在一个已绑定主体的谓词内核对词片，不用于跨句语义判断。"""
    phrase = re.sub(r"和|及|与|、", "", phrase)
    covered: set[int] = set()
    for index in range(len(phrase) - 1):
        if phrase[index : index + 2] in source:
            covered.update((index, index + 1))
    return bool(phrase) and (
        phrase in source or covered == set(range(len(phrase)))
    )


class RequestRelationStatus(StrEnum):
    """与来源安全、事实蕴含分开的最终问题关系状态。"""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED_OR_IRRELEVANT = "CONTRADICTED_OR_IRRELEVANT"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class RequestRelationDecision:
    """保留判断来源和稳定理由，不通过改错误名声称质量改善。"""

    status: RequestRelationStatus
    reason: str
    validator: str = "request_relation"


class RequestRelationUndetermined(ValidationFailed):
    """只在全部来源和事实硬门通过后携带待复核事实，正文不进 details。"""

    def __init__(self, claim: AnswerClaim, reason: str) -> None:
        self.claim = claim
        super().__init__(
            "有限规则尚不能证明当前问题关系。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNDETERMINED",
            details={"validator": "request_relation", "reason": reason},
        )


def decide_request_relation(
    analysis: QueryAnalysis, source: str
) -> RequestRelationDecision:
    """只用同一语义和单个源句证明关系，未命中规则保留未知。"""
    from rag_app.application.retrieval.answer_support import (  # noqa: PLC0415
        SupportStatus,
        evaluate_span_support,
    )

    supported = RequestRelationStatus.SUPPORTED
    proof = evaluate_span_support(analysis, source)
    if proof.status is SupportStatus.SUPPORTED:
        return RequestRelationDecision(supported, proof.support_reason)
    semantics = analysis.semantics
    target = normalize_semantic_text(semantics.target or "")
    relation = normalize_semantic_text(semantics.relation or "")
    generic = relation in {
        "规定",
        "事实",
        "事实关系",
        "原文内容",
        "字面查找",
        "内容",
        "信息",
        "职责",
    }
    owner = _SUBJECT.match(target)
    saw_different_subject = False
    saw_requested_subject = False
    for sentence in re.split(r"[。；;！？!?\n，,]", source):
        normalized = normalize_semantic_text(sentence)
        if not target or not normalized:
            continue
        source_owner = _SUBJECT.match(normalized)
        if owner and source_owner:
            if owner[1] != source_owner[1]:
                saw_different_subject = True
                continue
            saw_requested_subject = True
        # 完整对象在同一句内且句中实际有谓词，不能跨主体拼词或只命中标题。
        if (
            generic
            and target in normalized
            and re.search(
                r"负责|包括|包含|承担|为|是|应|需|可|不得|保存|归档", sentence
            )
        ):
            return RequestRelationDecision(
                supported, "SAME_CLAUSE_TARGET_PREDICATE"
            )
        if (
            generic
            and _ACTION.search(sentence)
            and _phrase_covered(target, normalized)
            and (
                owner is None or (source_owner and owner[1] == source_owner[1])
            )
        ):
            return RequestRelationDecision(
                supported, "BOUND_CLAUSE_OBJECT_AND_ACTION"
            )
        if semantics.answer_type is RequestedAnswerType.RESPONSIBLE_PARTY:
            ownership = re.match(r"^(.+?)(?:负责|牵头|承担)(.+)$", normalized)
            requested = tuple(filter(None, re.split(r"和|及|与|、", target)))
            if (
                ownership
                and requested
                and all(part in ownership[2] for part in requested)
            ):
                return RequestRelationDecision(
                    supported, "SAME_SUBJECT_REQUESTED_ACTION"
                )
    if owner and saw_different_subject and not saw_requested_subject:
        return RequestRelationDecision(
            RequestRelationStatus.CONTRADICTED_OR_IRRELEVANT,
            "EXPLICIT_DIFFERENT_SUBJECT",
        )
    return RequestRelationDecision(
        RequestRelationStatus.UNDETERMINED, "LEXICAL_RELATION_NOT_PROVED"
    )
