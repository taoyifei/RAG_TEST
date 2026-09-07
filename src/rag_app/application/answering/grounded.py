"""有界资料生成、逐条来源核验与一次修复，不将引用 ID 当事实证明。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rag_app.application.answering.service import ExtractiveAnsweringService
from rag_app.core.errors import (
    ProviderInvalidResponse,
    RagError,
    ValidationFailed,
)
from rag_app.core.models import (
    AnswerDraft,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    ProviderCall,
)
from rag_app.core.ports import GenerationRequest, GeneratorPort

_NUMBER = re.compile(
    r"[+-]?\d+(?:[.,:/-]\d+)*(?:\s*(?:%|％|万元|亿元|元|"
    r"毫秒|分钟|小时|秒|天|周|个月|年|月|毫米|厘米|千米|米|"
    r"公斤|千克|毫克|克|吨|升|毫升|次|个|台|件|人|℃|"
    r"[A-Za-zμµΩ°]+(?:/[A-Za-z]+)?))?"
)
_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_])"
)
_TEMPERATURE_ATTRIBUTE = re.compile(
    r"温度|(?<![A-Za-z])temperature(?![A-Za-z])", re.IGNORECASE
)
_CELSIUS_QUANTITY = re.compile(
    r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*"
    r"(?:摄氏度|℃|°\s*c|(?:degrees?\s+)?celsius)(?![A-Za-z])",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|"
    r"尚未|没有|并非|不是|未(?!来)|无(?!线(?!索))|"
    r"不(?!同(?!意|步)|断(?!开|电|网|水|气)|仅|但)"
)
_SAME_RELATION = re.compile(r"相同|一样|一致")
_DIFFERENT_RELATION = re.compile(r"不同(?!意|步)")
_STOP = re.compile(r"[\W_]|的|了|和|与|及|在|将|其|以|并|为|是", re.UNICODE)
_MIN_QUOTE_CHARS = 2
_MIN_SUPPORTED_BIGRAM_RATIO = 0.35
_MIN_NEGATION_SHARED_TERMS = 2
_NAMED_SUBJECT = re.compile(
    r"([A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]{1,16}"
    r"(?:负责人|经理|主管|专员|工程师|设备|系统|模式))"
    r"\s*(?:负责|承担|的(?:核心)?职责|的(?:维护)?周期)"
)
_ACTION_MODIFIER = r"(?:牵头|主要|直接|统一|共同|定期)"
_ACTION_VERB = (
    r"负责(?!人)|承担|组织|协调|审批|批准|维护|检修|检查|核对|"
    r"保存|归档|销毁|执行|提供|记录|属于|位于|采用|包括|包含|参与|"
    r"支持|拥有|具备|配备|完成|启动|停止"
)
_GENERAL_SUBJECT = re.compile(
    r"^\s*([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff ]{0,40}?)"
    r"\s*(?=(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|尚未|没有|未|无|不)?"
    rf"(?:{_ACTION_MODIFIER})?(?:{_ACTION_VERB}))"
    r"|的(?:核心)?职责|的(?:维护)?周期)"
)
_NOT_SUBJECTS = frozenset(
    {
        "应",
        "需",
        "须",
        "必须",
        "应当",
        "可以",
        "可",
        "已",
        "未",
        "不",
        "不得",
        "无需",
        "不必",
    }
)
_NEGATION_CLASSES = {
    "不得": "forbidden",
    "禁止": "forbidden",
    "严禁": "forbidden",
    "不允许": "forbidden",
    "不准": "forbidden",
    "不能": "cannot",
    "不可": "cannot",
    "无需": "unnecessary",
    "不必": "unnecessary",
    "不需要": "unnecessary",
    "未": "not_yet",
    "尚未": "not_yet",
    "没有": "absent",
    "无": "absent",
    "并非": "negative",
    "不是": "negative",
    "不": "negative",
}


@dataclass(frozen=True)
class GroundedOutcome:
    """供检索与历史真实记录的生成结果。"""

    answer: str | None
    mode: Literal["llm", "extractive", "extractive_fallback", "none"]
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str | None = None


def _terms(text: str) -> set[str]:
    # 只统一温度属性与摄氏单位名称，不翻译其他内容或改变词汇支持阈值。
    text = _TEMPERATURE_ATTRIBUTE.sub("温度", text)
    text = _CELSIUS_QUANTITY.sub(r"\g<value>℃", text)
    normalized = _STOP.sub("", text.casefold())
    return {
        normalized[index : index + 2] for index in range(len(normalized) - 1)
    }


def _subject(text: str) -> str | None:
    if re.match(
        rf"^\s*(?:{_ACTION_MODIFIER})?(?:{_ACTION_VERB})",
        text,
    ):
        return None
    match = _GENERAL_SUBJECT.search(text)
    value = match[1].strip() if match is not None else None
    if value:
        value = (
            re.split(
                r"应当|必须|可以|自行|独立|直接|擅自|已经|" + _NEGATION.pattern,
                value,
                maxsplit=1,
            )[0].strip()
            or None
        )
    return None if value in _NOT_SUBJECTS else value


def _predicate(text: str) -> str:
    subject = _subject(text)
    return text[text.index(subject) + len(subject) :] if subject else text


def _number_tokens(text: str) -> set[str]:
    # 标识独立保留，防止尾号吸附后续英文属性，也不能通过改尾号偷换对象。
    identifiers = {"id:" + value for value in _IDENTIFIER.findall(text)}
    text = _IDENTIFIER.sub(" ", text)
    text = _CELSIUS_QUANTITY.sub(r"\g<value>℃", text)
    return identifiers | {
        re.sub(r"\s+", "", value) for value in _NUMBER.findall(text)
    }


def _quantity_relation_matches(clause: str, source: str) -> bool:
    """温度不能借用其他属性的同值数量；表格纯数值片段保留支持资格。"""
    if not _TEMPERATURE_ATTRIBUTE.search(clause):
        return True
    if re.search(
        r"\b(?:not|no|never|unknown|unavailable)\b", source, re.IGNORECASE
    ):
        return False
    return bool(
        _TEMPERATURE_ATTRIBUTE.search(source)
        or _CELSIUS_QUANTITY.fullmatch(source.strip("。:： \t"))
    )


def _clauses_with_subject(text: str) -> list[tuple[str, str | None]]:
    """同一句逗号后的省略主体沿用前项，跨句重新识别。"""
    clauses: list[tuple[str, str | None]] = []
    for sentence in re.split(r"[。；;！!？?\n]", text):
        subject: str | None = None
        for clause in re.split(r"[，,]", sentence):
            if clause.strip():
                subject = _subject(clause) or subject
                clauses.append((clause, subject))
    return clauses


def _negations(text: str) -> set[str]:
    # 普通词中的字形不代表句子否定；例如“未批准未来计划”只计前一个“未”。
    return {_NEGATION_CLASSES[match] for match in _NEGATION.findall(text)}


def _action_terms(text: str) -> set[str]:
    text = _IDENTIFIER.sub(
        lambda match: re.sub(r"\d", " ", match[0]), _predicate(text)
    )
    text = _CELSIUS_QUANTITY.sub(" ", text)
    return _terms(_NEGATION.sub("", _NUMBER.sub("", text)))


def _check_negations(clause: str, source_clauses: list[str]) -> None:
    """只比较同一动作的极性，不能借用其他职责中的否定词。"""
    action = _action_terms(clause)
    relevant = [
        source
        for source in source_clauses
        if action
        and len(_action_terms(source) & action)
        >= min(_MIN_NEGATION_SHARED_TERMS, len(action))
    ]
    expected = _negations(clause)
    # “不同需求”不是动作否定，但仍不能把明确的异同关系反转为“相同”。
    if _SAME_RELATION.search(clause) and any(
        _DIFFERENT_RELATION.search(source) for source in relevant
    ):
        raise ValidationFailed(
            "事实反转了来源的异同关系。",
            stage="answer.validate",
            code="CLAIM_NEGATION_CHANGED",
        )
    if expected and not any(
        _negations(source) == expected for source in relevant
    ):
        raise ValidationFailed(
            "事实改变了相应动作的否定含义。",
            stage="answer.validate",
            code="CLAIM_NEGATION_CHANGED",
        )
    if not expected and any(_negations(source) for source in relevant):
        raise ValidationFailed(
            "事实丢失相应动作的否定。",
            stage="answer.validate",
            code="CLAIM_NEGATION_CHANGED",
        )


def _source_groups(item: EvidenceItem) -> set[tuple[object, ...]]:
    """表格联合引用按真实行锚点分组，未知行只允许同一个来源节点。"""
    base = (item.document_version_id, item.section_id, item.table_locator)
    if not item.table_context and item.table_locator is None:
        return {base}
    groups: set[tuple[object, ...]] = set()
    for span in item.source_spans:
        anchor = span.source_anchor
        if anchor is None:
            groups.add((*base, span.node_id))
            continue
        row_ends = [
            index + 1
            for index, part in enumerate(anchor.structural_path)
            if re.fullmatch(r"tr:\d+", part)
        ]
        if row_ends:
            row: object = anchor.structural_path[: row_ends[-1]]
        elif anchor.table_index is not None and anchor.row_index is not None:
            row = (anchor.table_index, anchor.row_index)
        else:
            row = span.node_id
        groups.add((*base, anchor.part_uri, anchor.story_kind, row))
    return groups


def validate_grounded_draft(
    draft: AnswerDraft, evidence: tuple[EvidenceItem, ...]
) -> None:
    """校验逐字支持、来源关系与关键事实，允许有词汇依据的自然概括。

    Args:
        draft: 模型的结构化事实草稿。
        evidence: 本次已通过范围筛选的有限证据。

    Returns:
        无返回值；校验通过后调用方才可发布。

    Raises:
        ValidationFailed: 引用、对象、数字、否定或词汇支持不满足契约。

    """
    by_id = {item.support_id: item for item in evidence}
    if not draft.claims:
        raise ValidationFailed(
            "模型未给出可验证事实。",
            stage="answer.validate",
            code="GENERATION_ABSTAINED",
        )
    for claim in draft.claims:
        units: list[EvidenceItem] = []
        for support in claim.supports:
            item = by_id.get(support.support_id)
            if (
                item is None
                or not item.publishable
                or not item.source_spans
                or any(not span.is_citable for span in item.source_spans)
                or support.quote not in item.citation_text
                or len(support.quote.strip()) < _MIN_QUOTE_CHARS
            ):
                raise ValidationFailed(
                    "引用原文无法核验。",
                    stage="answer.validate",
                    code="CLAIM_QUOTE_INVALID",
                )
            units.append(item)
        # 同一事实可以联合同一行/段落的多个 span，不能混接不同表格角色。
        source_groups = {
            group for item in units for group in _source_groups(item)
        }
        if len(source_groups) != 1:
            raise ValidationFailed(
                "单条事实跨越不同来源结构。",
                stage="answer.validate",
                code="CLAIM_SOURCE_MISMATCH",
            )
        support_text = "\n".join(support.quote for support in claim.supports)
        source_clauses = _clauses_with_subject(support_text)
        for clause, clause_subject in _clauses_with_subject(claim.text):
            subjects = set(_NAMED_SUBJECT.findall(clause))
            general_subject = _subject(clause)
            if general_subject:
                subjects.add(general_subject)
            if any(subject not in support_text for subject in subjects):
                raise ValidationFailed(
                    "事实偷换了所引资料的对象。",
                    stage="answer.validate",
                    code="CLAIM_OBJECT_CHANGED",
                )
            relevant_sources = [
                text
                for text, subject in source_clauses
                if clause_subject is None
                or subject is None
                or clause_subject in subject
            ]
            numeric_sources = [
                text
                for text in relevant_sources
                if _action_terms(clause) & _action_terms(text)
                and _quantity_relation_matches(clause, text)
            ]
            if not _number_tokens(clause) <= _number_tokens(
                "\n".join(numeric_sources)
            ):
                raise ValidationFailed(
                    "事实中的数字或单位缺少来源。",
                    stage="answer.validate",
                    code="CLAIM_NUMBER_UNSUPPORTED",
                )
            # 对象名本身不能为新编职责提供词汇支持，独立检查谓语事实。
            predicate = _predicate(clause)
            terms = _terms(predicate)
            supported_terms = terms & _terms("\n".join(relevant_sources))
            if (
                not terms
                or len(supported_terms) / len(terms)
                < _MIN_SUPPORTED_BIGRAM_RATIO
            ):
                raise ValidationFailed(
                    "事实与所引原文缺少支持关系。",
                    stage="answer.validate",
                    code="CLAIM_TEXT_UNSUPPORTED",
                )
            _check_negations(clause, relevant_sources)


class GroundedAnsweringService:
    """生成不确定性只在合法证据内解决；失败回退保留真实原因。"""

    def __init__(
        self, generator: GeneratorPort, fallback: GeneratorPort
    ) -> None:
        """绑定生成器和经验证据摘录回退。

        Args:
            generator: 最多接收初次生成与一次修复请求的生成端口。
            fallback: 生成失败后使用的证据摘录端口。

        Returns:
            无返回值。

        """
        self.generator = generator
        self.fallback = ExtractiveAnsweringService(fallback)

    def answer(
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
    ) -> GroundedOutcome:
        """最多两次生成；正文缺失、权限和索引错误不允许模型覆盖。

        Args:
            query: 用户的原始问题。
            evidence: 已通过资源和引用检查的有限资料。
            confidence: 检索置信状态，不允许越过硬性拒绝。

        Returns:
            已核验回答或回退结果，包含真实调用和终态原因。

        """
        if not evidence or confidence.status not in {
            ConfidenceStatus.ANSWERABLE,
            ConfidenceStatus.INSUFFICIENT_EVIDENCE,
        }:
            return GroundedOutcome(
                None,
                "none",
                reason_code="EVIDENCE_SELECTION_EMPTY"
                if not evidence
                else confidence.status.value,
            )
        calls: list[ProviderCall] = []
        reason: str | None = None
        for attempt in range(2):
            try:
                draft = self.generator.generate(
                    GenerationRequest(
                        query=query,
                        evidence=evidence,
                        citation_protocol="support-id-v1-claims",
                        repair_reason=reason if attempt else None,
                    )
                )
                calls.extend(draft.provider_calls)
                if draft.reason_code == "GENERATION_ABSTAINED":
                    return GroundedOutcome(
                        None, "none", tuple(calls), draft.reason_code
                    )
                validate_grounded_draft(draft, evidence)
                # 只发布已逐条核验的 claim，忽略任何多余模型正文。
                answer = "\n".join(
                    claim.text
                    + " "
                    + " ".join(
                        f"[{support.support_id}]" for support in claim.supports
                    )
                    for claim in draft.claims
                )
                return GroundedOutcome(
                    answer, "llm", tuple(calls), "CLAIMS_VALIDATED"
                )
            except ValidationFailed as error:
                reason = error.code
                if reason == "GENERATION_ABSTAINED":
                    return GroundedOutcome(None, "none", tuple(calls), reason)
            except RagError as error:
                calls.extend(
                    error.provider_calls
                    or (
                        ()
                        if error.provider_call is None
                        else (error.provider_call,)
                    )
                )
                reason = error.code
                if isinstance(error, ProviderInvalidResponse):
                    continue
                break
            except ValueError:
                reason = "GENERATION_OUTPUT_INVALID"
        fallback_answer = self.fallback.answer(query, evidence, confidence)
        return GroundedOutcome(
            fallback_answer,
            "extractive_fallback" if fallback_answer else "none",
            tuple(calls),
            reason,
        )
