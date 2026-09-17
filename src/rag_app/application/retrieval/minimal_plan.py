"""将单次 Planner 的最小问题拆分转换为可信 QueryAtom。"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.models import QueryAnalysis, SearchRequest
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomConstraintKind,
    QueryAtom,
    QueryConstraint,
)

_PUNCTUATION = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_CLAUSE_BOUNDARY = re.compile(r"[，,；;。！？?!]+")
_VERSION = re.compile(r"(?i)(?<![a-z0-9])v\d+(?:\.\d+)*(?![a-z0-9])")
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ALNUM_IDENTIFIER = re.compile(r"(?i)[a-z][a-z0-9._/-]*\d[a-z0-9._/-]*")
_NEGATION = re.compile(r"不得|无需|不必|禁止|严禁|没有|未|不")
_DURATION_UNIT = re.compile(r"^(秒|分钟|小时|日|天|周|月|年)")
_UNIT = re.compile(r"^(秒|分钟|小时|日|天|周|月|年|万元|元|%|％|千克|公斤|米)")
_MAX_ATOMS = 4
_MIN_TARGET_ANCHOR_CHARS = 1


class MinimalAtomPayload(FrozenModel):
    """模型只能给出输入中的片段、目标、关系和回答形状。"""

    fragment: str = Field(min_length=1, max_length=320)
    target: str = Field(min_length=1, max_length=160)
    relation: str = Field(min_length=1, max_length=160)
    answer_shape: AtomAnswerShape


class MinimalPlanPayload(FrozenModel):
    """一次可审计的最小 Planner 输出，无答案和检索路由字段。"""

    intent: Literal["SINGLE", "COMPOUND", "FOLLOW_UP", "CLARIFICATION"]
    needs_clarification: StrictBool
    clarification_question: str | None = Field(default=None, max_length=200)
    atoms: tuple[MinimalAtomPayload, ...] = Field(
        min_length=1, max_length=_MAX_ATOMS
    )

    @model_validator(mode="after")
    def _clarification_consistent(self) -> MinimalPlanPayload:
        if self.needs_clarification != bool(self.clarification_question):
            raise ValueError("澄清状态与问题不一致。")
        if self.needs_clarification != (self.intent == "CLARIFICATION"):
            raise ValueError("澄清意图与状态不一致。")
        return self


def build_query_atoms(
    payload: MinimalPlanPayload,
    request: SearchRequest,
    analysis: QueryAnalysis,
) -> tuple[QueryAtom, ...]:
    """只从用户原问与最近上下文提取限制，不信任模型补充事实。"""
    available = (request.text, *request.conversation_context[-2:])
    normalized_available = _normalized(" ".join(available))
    fragments = tuple(atom.fragment.strip() for atom in payload.atoms)
    if not all(
        any(fragment in source for source in available)
        for fragment in fragments
    ):
        raise ValueError("Planner 片段不属于输入。")
    if not any(fragment in request.text for fragment in fragments):
        raise ValueError("Planner 未覆盖当前问题。")
    if len(fragments) > 1 and len(set(fragments)) == 1:
        raise ValueError("复合问题不能重复同一个片段。")
    _validate_clause_coverage(request.text, fragments)
    analyzer = QueryAnalyzer()
    available_literals = _protected_literals(
        " ".join(available), analyzer, request
    )
    built: list[QueryAtom] = []
    for index, (atom, fragment) in enumerate(
        zip(payload.atoms, fragments, strict=True), 1
    ):
        target = atom.target.strip()
        if not _target_anchored(target, normalized_available):
            raise ValueError("Planner 目标不属于输入。")
        for literal in _protected_literals(
            f"{target} {atom.relation}", analyzer, request
        ):
            if literal not in available_literals:
                raise ValueError("Planner 新增了受保护字面值。")
        built.append(
            QueryAtom(
                atom_id=f"A{index}",
                target=target,
                relation=atom.relation.strip(),
                answer_shape=atom.answer_shape,
                source_qualifier=analysis.semantics.source_qualifier,
                constraints=_constraints_for_fragment(fragment, analysis),
                original_fragment=fragment,
            )
        )
    return tuple(built)


def _normalized(value: str) -> str:
    return _PUNCTUATION.sub(
        "", unicodedata.normalize("NFKC", value).casefold()
    )


def _target_anchored(target: str, available: str) -> bool:
    normalized = _normalized(target)
    return (
        len(normalized) >= _MIN_TARGET_ANCHOR_CHARS
        and normalized in available
    )


def _protected_literals(
    text: str, analyzer: QueryAnalyzer, request: SearchRequest
) -> set[str]:
    analyzed = analyzer.analyze(request.model_copy(update={"text": text}))
    return {
        _normalized(value)
        for value in (
            *analyzed.identifiers,
            *analyzed.quoted_phrases,
            *analyzed.date_version_signals,
            *analyzed.numbers,
            *[match[0] for match in _VERSION.finditer(text)],
            *[match[0] for match in _DATE.finditer(text)],
            *[match[0] for match in _NUMBER.finditer(text)],
            *[match[0] for match in _ALNUM_IDENTIFIER.finditer(text)],
        )
        if _normalized(value)
    }


def _validate_clause_coverage(
    question: str, fragments: tuple[str, ...]
) -> None:
    normalized_fragments = tuple(_normalized(item) for item in fragments)
    for clause in _CLAUSE_BOUNDARY.split(question):
        normalized = _normalized(clause)
        if not normalized:
            continue
        if not any(
            normalized in fragment or fragment in normalized
            for fragment in normalized_fragments
            if fragment
        ):
            raise ValueError("Planner 未覆盖独立问句。")


def _constraints_for_fragment(
    fragment: str,
    analysis: QueryAnalysis,
) -> tuple[QueryConstraint, ...]:
    constraints: list[QueryConstraint] = []
    source = analysis.semantics.source_qualifier
    scoped_text = " ".join((fragment, source or ""))
    protected_spans: list[tuple[int, int]] = []
    for pattern, kind in (
        (_VERSION, AtomConstraintKind.VERSION),
        (_DATE, AtomConstraintKind.DATE_TIME),
    ):
        for match in pattern.finditer(scoped_text):
            protected_spans.append(match.span())
            constraints.append(QueryConstraint(kind=kind, value=match[0]))
    for match in _NUMBER.finditer(fragment):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in protected_spans
        ):
            continue
        suffix = fragment[match.end() :]
        unit_match = _UNIT.match(suffix)
        constraints.append(
            QueryConstraint(
                kind=AtomConstraintKind.DURATION
                if _DURATION_UNIT.match(suffix)
                else AtomConstraintKind.NUMBER,
                value=match[0],
                unit=unit_match[0] if unit_match else None,
            )
        )
    constraints.extend(
        QueryConstraint(
            kind=AtomConstraintKind.NEGATION,
            value=match[0],
            polarity="NEGATIVE",
        )
        for match in _NEGATION.finditer(fragment)
    )
    if source:
        constraints.append(
            QueryConstraint(
                kind=AtomConstraintKind.SOURCE,
                value=source,
            )
        )
    return tuple(dict.fromkeys(constraints))[:12]


__all__ = ["MinimalAtomPayload", "MinimalPlanPayload", "build_query_atoms"]
