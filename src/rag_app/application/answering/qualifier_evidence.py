"""分离限定命题提取、证据定位与执行期证明。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.answer_plan import (
    AnswerQualifier,
    AnswerQualifierKind,
    QualifierEvidenceResult,
    QualifierProofReference,
    QualifierStatus,
)
from rag_app.core.models.retrieval import EvidenceItem
from rag_app.core.query_text import normalize_document_label

_BEFORE = re.compile(r"之前|开始前|事前|预先")
_AFTER = re.compile(r"之后|完成后|结束后|事后")
_MUST_POSITIVE = re.compile(r"必须|应当|应该|须要|须先")
_MUST_NEGATIVE = re.compile(r"无需|不必|不需要|并非必须|不是必须")
_PROHIBITED = re.compile(r"不得|禁止|严禁")
_NEGATED_ACTION = re.compile(
    r"无需|不必|不需要|并非必须|不是必须|不得|禁止|严禁|"
    r"不在[^，,。；;！？?!]{0,32}(?:之前|开始前|之后|完成后)"
)
_ACTION = re.compile(
    r"准备|提供|提交|备齐|完成|确定|归档|输出|输入|申请|交付|上传"
)
_CONDITION = re.compile(
    r"(?:仅当|如果|若|除非)[^，,。；;！？?!]{1,80}"
    r"|在[^，,。；;！？?!]{1,48}(?:时|情况下)"
)
_SUBJECT_NOISE = re.compile(
    r"之前|开始前|事前|预先|之后|完成后|结束后|事后|"
    r"必须|应当|应该|须要|须先|无需|不必|不需要|并非|不是|"
    r"不得|禁止|严禁|开始|完成|结束|这些|哪些|什么|所有|全部|"
    r"在|于|由|对|把|将|的|了|吗|呢|[《》‘’“”（）()，,。；;！？?!\s]"
)
_PARENTHETICAL = re.compile(r"[（(][^）)]*[）)]")
_TITLE_METADATA_VALUES = frozenset(
    {
        "heading",
        "title",
        "document_title",
        "section_heading",
        "table_title",
    }
)


@dataclass(frozen=True, slots=True)
class QualifierSpec:
    """原问中的一个限定词、极性和局部字符跨度。"""

    kind: AnswerQualifierKind
    text: str
    polarity: Literal["POSITIVE", "NEGATIVE"]
    start: int
    end: int


def extract_qualifier_specs(text: str) -> tuple[QualifierSpec, ...]:
    """提取限定要求；否定义务不会被正向 ``必须`` 覆盖。"""
    result: list[QualifierSpec] = []
    for kind, pattern in (
        (AnswerQualifierKind.BEFORE, _BEFORE),
        (AnswerQualifierKind.AFTER, _AFTER),
    ):
        if match := pattern.search(text):
            result.append(
                QualifierSpec(
                    kind=kind,
                    text=match.group(0),
                    polarity="POSITIVE",
                    start=match.start(),
                    end=match.end(),
                )
            )
    negative_must = _MUST_NEGATIVE.search(text)
    positive_must = _MUST_POSITIVE.search(text)
    if negative_must is not None:
        result.append(
            QualifierSpec(
                kind=AnswerQualifierKind.MUST,
                text=negative_must.group(0),
                polarity="NEGATIVE",
                start=negative_must.start(),
                end=negative_must.end(),
            )
        )
    elif positive_must is not None:
        result.append(
            QualifierSpec(
                kind=AnswerQualifierKind.MUST,
                text=positive_must.group(0),
                polarity="POSITIVE",
                start=positive_must.start(),
                end=positive_must.end(),
            )
        )
    if prohibited := _PROHIBITED.search(text):
        result.append(
            QualifierSpec(
                kind=AnswerQualifierKind.PROHIBITED,
                text=prohibited.group(0),
                polarity="NEGATIVE",
                start=prohibited.start(),
                end=prohibited.end(),
            )
        )
    return tuple(result)


def qualifier_action(text: str) -> str | None:
    """读取限定命题显式动作；不做动作同义扩写。"""
    match = _ACTION.search(text)
    return None if match is None else match.group(0)


def qualifier_conditions(text: str) -> tuple[str, ...]:
    """保留原问显式条件，不把有条件义务提升成无条件义务。"""
    return tuple(
        dict.fromkeys(
            match.group(0).strip() for match in _CONDITION.finditer(text)
        )
    )


def qualifier_subject(
    text: str,
    *,
    action: str | None,
    event_anchor: str | None,
) -> str | None:
    """保守提取动作前的显式主体；隐含主体保持未知。"""
    if action is None or action not in text:
        return None
    prefix = text.split(action, 1)[0]
    for condition in qualifier_conditions(prefix):
        prefix = prefix.replace(condition, "")
    if event_anchor:
        prefix = prefix.replace(event_anchor, "")
    subject = _SUBJECT_NOISE.sub("", prefix).strip()
    return subject[:320] if subject else None


def _source_span_digests(item: EvidenceItem) -> tuple[str, ...]:
    """把真实可引用跨度投影成不含正文的稳定证明身份。"""
    return tuple(
        canonical_sha256(span.model_dump(mode="json"))
        for span in item.source_spans
        if span.is_citable
    )


def _proof_group_id(item: EvidenceItem) -> str | None:
    """读取已有结构组身份；不存在时不伪造。"""
    metadata = dict(item.metadata)
    for key in ("evidence_group_id", "source_group_id", "table_group_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_title_only(item: EvidenceItem) -> bool:
    """识别只重复标题身份、没有正文命题的来源。"""
    value = normalize_document_label(item.citation_text)
    labels = (
        item.source_label,
        item.display_name or "",
        *item.heading_path,
    )
    if value and any(
        value == normalize_document_label(label) for label in labels
    ):
        return True
    metadata = dict(item.metadata)
    return any(
        isinstance(metadata.get(key), str)
        and str(metadata[key]).casefold() in _TITLE_METADATA_VALUES
        for key in ("role", "node_role", "block_type", "structure_role")
    )


def _has_locator(kind: AnswerQualifierKind, text: str) -> bool:
    """定位同类或显式相反限定，定位本身不构成证明。"""
    if kind is AnswerQualifierKind.BEFORE:
        return bool(_BEFORE.search(text) or _AFTER.search(text))
    if kind is AnswerQualifierKind.AFTER:
        return bool(_AFTER.search(text) or _BEFORE.search(text))
    if kind is AnswerQualifierKind.MUST:
        return bool(
            _MUST_POSITIVE.search(text)
            or _MUST_NEGATIVE.search(text)
            or _PROHIBITED.search(text)
        )
    return bool(_PROHIBITED.search(text) or _MUST_POSITIVE.search(text))


def _source_relation(  # noqa: PLR0911
    kind: AnswerQualifierKind,
    text: str,
) -> tuple[str | None, str | None]:
    """返回来源限定的方向/极性和命中词。"""
    if kind is AnswerQualifierKind.BEFORE:
        if match := _BEFORE.search(text):
            return (
                "OPPOSITE" if _NEGATED_ACTION.search(text) else "POSITIVE",
                match.group(0),
            )
        if match := _AFTER.search(text):
            return "OPPOSITE", match.group(0)
        return None, None
    if kind is AnswerQualifierKind.AFTER:
        if match := _AFTER.search(text):
            return (
                "OPPOSITE" if _NEGATED_ACTION.search(text) else "POSITIVE",
                match.group(0),
            )
        if match := _BEFORE.search(text):
            return "OPPOSITE", match.group(0)
        return None, None
    if kind is AnswerQualifierKind.MUST:
        if match := _MUST_NEGATIVE.search(text):
            return "NEGATIVE", match.group(0)
        if match := _PROHIBITED.search(text):
            return "NEGATIVE", match.group(0)
        if match := _MUST_POSITIVE.search(text):
            return "POSITIVE", match.group(0)
        return None, None
    if match := _PROHIBITED.search(text):
        return "NEGATIVE", match.group(0)
    if match := _MUST_POSITIVE.search(text):
        return "POSITIVE", match.group(0)
    return None, None


def _member_coverage(
    source: str,
    member_sources: tuple[tuple[str, tuple[str, ...]], ...],
    registry: dict[str, EvidenceItem],
) -> tuple[str, ...]:
    """只把同一句实际包含的成员登记为已覆盖。"""
    return tuple(
        member_key
        for member_key, support_ids in member_sources
        if any(
            (
                member_text := normalize_document_label(
                    registry[support_id].citation_text
                )
            )
            and member_text in source
            for support_id in support_ids
            if support_id in registry
        )
    )


def _same_subject(requirement: AnswerQualifier, item: EvidenceItem) -> bool:
    """拒绝把另一主体的义务借给当前问题中的主体或隐含主体。"""
    source_text = item.citation_text
    if requirement.object_label:
        axis = _PARENTHETICAL.sub("", requirement.object_label).strip()
        if axis:
            source_text = source_text.replace(axis, "")
    source_subject = qualifier_subject(
        source_text,
        action=requirement.action,
        event_anchor=requirement.target_label,
    )
    required = normalize_document_label(requirement.subject or "")
    actual = normalize_document_label(source_subject or "")
    if not required:
        return not actual
    return bool(actual) and (required in actual or actual in required)


def _structural_event_compatible(
    requirement: AnswerQualifier,
    item: EvidenceItem,
) -> bool:
    """结构表头可继承当前行事件，但不能覆盖另一个显式事件。"""
    if requirement.kind not in {
        AnswerQualifierKind.BEFORE,
        AnswerQualifierKind.AFTER,
    }:
        return True
    source = item.citation_text
    target = normalize_document_label(requirement.event_anchor or "")
    if target and target in normalize_document_label(source):
        return True
    axis = _PARENTHETICAL.sub("", requirement.object_label or "").strip()
    if axis:
        source = source.replace(axis, "")
    pattern = (
        _BEFORE if requirement.kind is AnswerQualifierKind.BEFORE else _AFTER
    )
    match = pattern.search(source)
    if match is None:
        return False
    prefix = source[: match.start()]
    prefix = _SUBJECT_NOISE.sub("", prefix)
    return not normalize_document_label(prefix)


def evaluate_qualifier(  # noqa: PLR0912, PLR0915
    requirement: AnswerQualifier,
    *,
    member_sources: tuple[tuple[str, tuple[str, ...]], ...],
    registry: dict[str, EvidenceItem],
    allowed_document_pairs: frozenset[tuple[str, str]],
) -> QualifierEvidenceResult:
    """在执行期按主体、动作、事件、极性、条件和成员形成三态结果。"""
    required_members = set(requirement.selected_member_refs)
    locators: list[str] = []
    supporting: list[QualifierProofReference] = []
    contradicting: list[QualifierProofReference] = []
    covered: set[str] = set()
    source_conditions: list[str] = []
    structural = set(requirement.structural_predicate_support_ids)
    for support_id in requirement.candidate_support_ids:
        item = registry.get(support_id)
        if item is None or not item.document_id or not item.document_version_id:
            continue
        if (
            item.document_id,
            item.document_version_id,
        ) not in allowed_document_pairs:
            continue
        if not _has_locator(requirement.kind, item.citation_text):
            continue
        locators.append(support_id)
        if (
            _is_title_only(item)
            or not item.publishable
            or not item.source_spans
            or any(not span.is_citable for span in item.source_spans)
        ):
            continue
        span_digests = _source_span_digests(item)
        if not span_digests or requirement.action is None:
            continue
        source = normalize_document_label(item.citation_text)
        source_actions = {
            normalize_document_label(match.group(0))
            for match in _ACTION.finditer(item.citation_text)
        }
        if normalize_document_label(requirement.action) not in source_actions:
            continue
        inherited = support_id in structural
        event = normalize_document_label(requirement.event_anchor or "")
        if requirement.kind in {
            AnswerQualifierKind.BEFORE,
            AnswerQualifierKind.AFTER,
        } and (
            (not inherited and (not event or event not in source))
            or (
                inherited
                and not _structural_event_compatible(requirement, item)
            )
        ):
            continue
        if not _same_subject(requirement, item):
            continue
        found_conditions = qualifier_conditions(item.citation_text)
        source_conditions.extend(found_conditions)
        if tuple(
            normalize_document_label(value) for value in found_conditions
        ) != tuple(
            normalize_document_label(value) for value in requirement.conditions
        ):
            continue
        member_keys = (
            tuple(requirement.selected_member_refs)
            if inherited
            else _member_coverage(source, member_sources, registry)
        )
        if not member_keys:
            continue
        relation, _matched_text = _source_relation(
            requirement.kind, item.citation_text
        )
        expected = (
            requirement.polarity
            if requirement.kind
            in {AnswerQualifierKind.MUST, AnswerQualifierKind.PROHIBITED}
            else "POSITIVE"
        )
        proof = QualifierProofReference(
            support_id=support_id,
            source_span_digests=span_digests,
            proof_group_id=_proof_group_id(item),
            covered_member_keys=member_keys,
        )
        if relation == expected:
            supporting.append(proof)
            covered.update(member_keys)
        elif relation in {"POSITIVE", "NEGATIVE", "OPPOSITE"}:
            contradicting.append(proof)

    all_covered = bool(required_members) and required_members <= covered
    if all_covered and not contradicting:
        status = QualifierStatus.SUPPORTED
        reason = "QUALIFIER_PROPOSITION_PROVED"
        proofs = tuple(supporting)
    elif contradicting and not supporting:
        status = QualifierStatus.CONTRADICTED
        reason = "QUALIFIER_PROPOSITION_CONTRADICTED"
        proofs = tuple(contradicting)
        covered = {
            member
            for proof in contradicting
            for member in proof.covered_member_keys
        }
    else:
        status = QualifierStatus.NOT_ESTABLISHED
        reason = (
            "QUALIFIER_EVIDENCE_CONFLICT"
            if contradicting and supporting
            else "QUALIFIER_MEMBER_PROOF_PARTIAL"
            if supporting
            else "QUALIFIER_PROPOSITION_NOT_ESTABLISHED"
        )
        proofs = (*supporting, *contradicting)
    return QualifierEvidenceResult(
        obligation_id=requirement.obligation_id,
        qualifier_id=requirement.qualifier_id,
        status=status,
        locator_support_ids=tuple(dict.fromkeys(locators)),
        proof_references=proofs,
        covered_member_keys=tuple(sorted(covered)),
        source_conditions=tuple(dict.fromkeys(source_conditions)),
        reason_code=reason,
    )


__all__ = [
    "QualifierSpec",
    "evaluate_qualifier",
    "extract_qualifier_specs",
    "qualifier_action",
    "qualifier_conditions",
    "qualifier_subject",
]
