"""有界资料生成、逐条来源核验与一次修复，不将引用 ID 当事实证明。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rag_app.core.errors import (
    ProviderInvalidResponse,
    QueryCancelled,
    RagError,
    StreamDeliveryError,
    ValidationFailed,
)
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    ProviderCall,
    QueryAnalysis,
    RequestedAnswerType,
)
from rag_app.core.ports import (
    CancellationPort,
    GenerationRequest,
    GeneratorPort,
)

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
_TEMPORAL_FREQUENCY = re.compile(
    r"每(?:秒|分钟|小时|日|天|周|星期|月|季度|季|年|次)"
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
    r"用于|用来|支持|拥有|具备|配备|完成|启动|停止"
)
_DUTY_ACTION_VERB = (
    rf"(?:{_ACTION_VERB})|协助|贯彻|制定|落实|指导|监督|主持|督促|"
    r"抓好|策划|确保|营造|任命|明确|确定|推行|报告"
)
_LEADING_ACTION_CONTEXT = re.compile(
    r"^\s*(?:在)?(?:"
    r"[^，,。；;！？?]{1,24}(?:完成|结束|通过|确认|测完)(?:了)?"
    r"(?:之后|以后|后)|"
    r"(?:(?:版本|系统|应用|产品|功能|代码|制品|项目)\s*)?(?:正式)?"
    r"(?:上线|发布|提交|开始|执行|操作|处理|准备|验收|交付)"
    r"(?:之前|之后|以前|以后|前|后)"
    r")\s*[，,]?\s*"
)
_LEADING_MODAL = re.compile(
    r"^\s*(?:(?:还|就|再|也|都|只)?"
    r"(?:需要|应该|应当|必须|可以|要|得|应|须|需|可))"
    r"(?:把|将)?\s*"
)
_MODAL_ACTION = re.compile(
    r"^(?:准备|整理|提交|上传|填报|填写|确认|补充|完成|检查|核对|"
    r"提供|记录|归档|执行)"
)
_ENTITY_SUBJECT = re.compile(
    r"^\s*((?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|用户|客户|人员|岗位|角色|小组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库)))"
    r"\s*(?=(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|尚未|没有|未|无|不)?"
    rf"(?:{_ACTION_MODIFIER})?(?:{_ACTION_VERB}))"
    r"|的(?:核心)?职责|的(?:维护)?周期)"
)
_STANDALONE_SUBJECT = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|用户|客户|人员|岗位|角色|小组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库|管代))"
)
_SECTION_NUMBER_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\s*")
_DUTY_ACTION_PREFIX = re.compile(
    r"^\s*(?:[）)】\]]\s*)?(?:不仅|还|也|同时)?"
    r"(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|"
    r"尚未|没有|未|无|不)?"
    rf"(?:{_ACTION_MODIFIER})?(?:{_DUTY_ACTION_VERB})"
    r"|的(?:核心)?职责|的(?:维护)?周期)"
)
_SUBJECT_CLAUSE_PREFIX = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*|[A-Za-z])\s*[.)、）]?\s*)?"
    r"[（(【\[]?\s*$"
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
    mode: Literal["llm", "none"]
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str | None = None
    published_support_ids: tuple[str, ...] = ()


def _terms(text: str) -> set[str]:
    # 只统一温度属性与摄氏单位名称，不翻译其他内容或改变词汇支持阈值。
    text = _TEMPERATURE_ATTRIBUTE.sub("温度", text)
    text = _CELSIUS_QUANTITY.sub(r"\g<value>℃", text)
    normalized = _STOP.sub("", text.casefold())
    return {
        normalized[index : index + 2] for index in range(len(normalized) - 1)
    }


def _subject(text: str) -> str | None:
    subject_text = _LEADING_ACTION_CONTEXT.sub("", text)
    without_modal = _LEADING_MODAL.sub("", subject_text)
    if without_modal != subject_text and _MODAL_ACTION.match(without_modal):
        return None
    subject_text = without_modal
    if re.match(
        rf"^\s*(?:{_ACTION_MODIFIER})?(?:{_ACTION_VERB})",
        subject_text,
    ):
        return None
    match = _ENTITY_SUBJECT.search(subject_text)
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


def _frequency_tokens(text: str) -> set[str]:
    """保留不带阿拉伯数字的周期词，防止每周被概括成每月。"""
    return set(_TEMPORAL_FREQUENCY.findall(text))


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
        normalized_sentence = _LEADING_ACTION_CONTEXT.sub("", sentence)
        subject: str | None = None
        for clause in re.split(r"[，,]", normalized_sentence):
            if clause.strip():
                subject = _subject(clause) or subject
                clauses.append((clause, subject))
    return clauses


def _standalone_subjects(text: str) -> set[str]:
    """提取同一来源组中的独立岗位标题或表格角色单元。"""
    subjects: set[str] = set()
    for raw_line in text.splitlines():
        line = _SECTION_NUMBER_PREFIX.sub("", raw_line).strip(
            " \t:：。；;.!！？?"
        )
        for alias in re.split(r"[/／、]", line):
            value = alias.strip()
            if _STANDALONE_SUBJECT.fullmatch(value):
                subjects.add(value)
    return subjects


def _same_subject(left: str, right: str) -> bool:
    """按完整职责标签比较对象，禁止把较长岗位名当成短岗位名。"""
    return (
        re.sub(r"\s+", "", left).casefold()
        == re.sub(r"\s+", "", right).casefold()
    )


def _leading_explicit_subject(text: str) -> str | None:
    """读取分句开头且后接职责动作的完整岗位标签。"""
    subject_text = _LEADING_MODAL.sub("", _LEADING_ACTION_CONTEXT.sub("", text))
    match = _STANDALONE_SUBJECT.match(subject_text)
    if (
        match is None
        or _DUTY_ACTION_PREFIX.match(subject_text[match.end() :]) is None
    ):
        return None
    return match[0]


def _source_has_explicit_subject(subject: str, text: str) -> bool:
    """要求对象在来源中处于主语或独立标题位置。"""
    if any(
        _same_subject(subject, candidate)
        for candidate in _standalone_subjects(text)
    ):
        return True
    for match in re.finditer(re.escape(subject), text, re.IGNORECASE):
        clause_start = max(
            (text.rfind(delimiter, 0, match.start()) + 1)
            for delimiter in "\n。；;.!！？?，,:："
        )
        if (
            _SUBJECT_CLAUSE_PREFIX.fullmatch(text[clause_start : match.start()])
            is None
        ):
            continue
        if _DUTY_ACTION_PREFIX.match(text[match.end() :]) is not None:
            return True
    return False


def _validate_claim_target(
    claim: AnswerClaim, analysis: QueryAnalysis | None
) -> None:
    """职责回答必须明确指向本次查询的职责主体。"""
    if (
        analysis is None
        or analysis.semantics.answer_type is not RequestedAnswerType.DUTIES
        or not analysis.semantics.target
    ):
        return
    clauses = _clauses_with_subject(claim.text)
    subject = _leading_explicit_subject(clauses[0][0]) if clauses else None
    if subject is None or not _same_subject(subject, analysis.semantics.target):
        raise ValidationFailed(
            "职责事实没有明确回答所问岗位。",
            stage="answer.validate",
            code="CLAIM_QUERY_TARGET_MISMATCH",
        )


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
    """表格按真实行分组，普通正文按真实来源节点分组。"""
    groups: set[tuple[object, ...]] = set()
    for span in item.source_spans:
        anchor = span.source_anchor
        if anchor is None:
            continue
        if not item.table_context and item.table_locator is None:
            groups.add(
                (
                    "node",
                    item.document_version_id,
                    item.section_id,
                    anchor.part_uri,
                    anchor.story_kind,
                    span.node_id,
                )
            )
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
        groups.add(
            (
                "table-row",
                item.document_version_id,
                item.section_id,
                item.table_locator,
                anchor.part_uri,
                anchor.story_kind,
                row,
            )
        )
    return groups


def _claim_source_texts(
    claim: AnswerClaim, units: list[EvidenceItem]
) -> tuple[str, ...]:
    """按可独立证明事实的表格行或原文节点聚合逐字引用。"""
    grouped: dict[tuple[object, ...], list[str]] = {}
    for support, item in zip(claim.supports, units, strict=True):
        groups = _source_groups(item)
        if len(groups) != 1:
            raise ValidationFailed(
                "一个引用跨越不同来源结构。",
                stage="answer.validate",
                code="CLAIM_SOURCE_MISMATCH",
            )
        group = next(iter(groups))
        grouped.setdefault(group, []).append(support.quote)
    return tuple("\n".join(quotes) for quotes in grouped.values())


def _validate_clause_support(
    clause: str, clause_subject: str | None, support_text: str
) -> None:
    """核验一个分句的对象、数值、措辞和否定均由同一来源组支持。"""
    source_clauses = _clauses_with_subject(support_text)
    subjects = set(_NAMED_SUBJECT.findall(clause))
    general_subject = _leading_explicit_subject(clause) or _subject(clause)
    if general_subject:
        subjects.add(general_subject)
    if any(
        not _source_has_explicit_subject(subject, support_text)
        for subject in subjects
    ):
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
        or _same_subject(clause_subject, subject)
    ]
    numeric_sources = [
        text
        for text in relevant_sources
        if _action_terms(clause) & _action_terms(text)
        and _quantity_relation_matches(clause, text)
    ]
    if not _number_tokens(clause) <= _number_tokens("\n".join(numeric_sources)):
        raise ValidationFailed(
            "事实中的数字或单位缺少来源。",
            stage="answer.validate",
            code="CLAIM_NUMBER_UNSUPPORTED",
        )
    if not _frequency_tokens(clause) <= _frequency_tokens(
        "\n".join(relevant_sources)
    ):
        raise ValidationFailed(
            "事实中的周期频率缺少来源。",
            stage="answer.validate",
            code="CLAIM_FREQUENCY_UNSUPPORTED",
        )
    # 对象名本身不能为新编职责提供词汇支持，独立检查谓语事实。
    predicate = _predicate(clause)
    terms = _terms(predicate)
    supported_terms = terms & _terms("\n".join(relevant_sources))
    if not terms or len(supported_terms) / len(terms) < (
        _MIN_SUPPORTED_BIGRAM_RATIO
    ):
        raise ValidationFailed(
            "事实与所引原文缺少支持关系。",
            stage="answer.validate",
            code="CLAIM_TEXT_UNSUPPORTED",
        )
    _check_negations(clause, relevant_sources)


def validate_grounded_draft(
    draft: AnswerDraft,
    evidence: tuple[EvidenceItem, ...],
    *,
    analysis: QueryAnalysis | None = None,
) -> None:
    """校验逐字支持、来源关系与关键事实，允许有词汇依据的自然概括。

    Args:
        draft: 模型的结构化事实草稿。
        evidence: 本次已通过范围筛选的有限证据。
        analysis: 可选的服务端查询语义，用于约束职责主体。

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
        _validate_claim_target(claim, analysis)
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
        source_texts = _claim_source_texts(claim, units)
        support_text = "\n".join(support.quote for support in claim.supports)
        for clause, clause_subject in _clauses_with_subject(claim.text):
            for source_text in source_texts:
                try:
                    _validate_clause_support(
                        clause, clause_subject, source_text
                    )
                except ValidationFailed:
                    continue
                break
            else:
                # 联合所有引用仍不成立时保留精确语义错误；只有跨来源
                # 拼接才能成立时，明确标记来源结构不一致。
                _validate_clause_support(clause, clause_subject, support_text)
                raise ValidationFailed(
                    "单个分句只能通过拼接不同来源结构才成立。",
                    stage="answer.validate",
                    code="CLAIM_SOURCE_MISMATCH",
                )


class GroundedAnsweringService:
    """生成不确定性只在合法证据内解决；失败时保留原因并拒答。"""

    def __init__(self, generator: GeneratorPort) -> None:
        """绑定最多执行初次生成与一次修复的生成器。

        Args:
            generator: 最多接收初次生成与一次修复请求的生成端口。

        Returns:
            无返回值。

        """
        self.generator = generator

    def answer(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
        *,
        answer_support_set: tuple[EvidenceItem, ...] | None = None,
        analysis: QueryAnalysis | None = None,
        on_claim: Callable[[AnswerClaim], None] | None = None,
        cancellation: CancellationPort | None = None,
    ) -> GroundedOutcome:
        """最多两次生成；正文缺失、权限和索引错误不允许模型覆盖。

        Args:
            query: 用户的原始问题。
            evidence: 已通过资源和引用检查的有限资料。
            confidence: 检索置信状态，不允许越过硬性拒绝。
            answer_support_set: 已直接支持所问关系的最小集合，供模型核验使用。
            analysis: 检索、Evidence 与回答共同消费的最终查询分析。
            on_claim: 可选的已校验完整 claim 发布回调。
            cancellation: 可选协作取消端口。

        Returns:
            已核验回答或明确拒答，包含真实调用和终态原因。

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
        direct_support = (
            evidence if answer_support_set is None else answer_support_set
        )
        for attempt in range(2):
            _raise_if_cancelled(cancellation)
            published: list[AnswerClaim] = []
            try:
                generation_request = GenerationRequest(
                    query=query,
                    evidence=evidence,
                    citation_protocol="support-id-v1-claims",
                    repair_reason=reason if attempt else None,
                    typed_semantics=(
                        None if analysis is None else analysis.semantics
                    ),
                    answer_support_set=direct_support,
                    model_evidence_candidates=evidence,
                )
                stream_generate = getattr(
                    self.generator, "generate_stream", None
                )
                if on_claim is not None and callable(stream_generate):

                    def publish(
                        claim: AnswerClaim,
                        published_claims: list[AnswerClaim] = published,
                    ) -> None:
                        """逐条执行完整业务证据门，再允许 HTTP 层发布。

                        Args:
                            claim: Adapter 刚形成的完整来源匹配事实。
                            published_claims: 本次尝试已成功交付的事实列表。

                        Returns:
                            无返回值；发布回调返回后才记录为已交付。

                        """
                        _raise_if_cancelled(cancellation)
                        validate_grounded_draft(
                            AnswerDraft(
                                text=claim.text,
                                cited_evidence_ids=tuple(
                                    support.support_id
                                    for support in claim.supports
                                ),
                                claims=(claim,),
                                generation_mode="llm",
                            ),
                            evidence,
                            analysis=analysis,
                        )
                        if claim in published_claims:
                            raise ValidationFailed(
                                "模型重复输出同一事实。",
                                stage="answer.validate",
                                code="DUPLICATE_CLAIM",
                            )
                        on_claim(claim)
                        # HTTP 交付回调返回才算已发布；来源或授权门禁拒绝时
                        # 不能把尚未发送的 claim 误报为 partial 前缀。
                        published_claims.append(claim)

                    if cancellation is None:
                        raise RuntimeError("流式生成缺少 cancellation。")
                    draft = stream_generate(
                        generation_request,
                        on_claim=publish,
                        cancellation=cancellation,
                    )
                else:
                    draft = self.generator.generate(generation_request)
                calls.extend(draft.provider_calls)
                if draft.reason_code == "GENERATION_ABSTAINED":
                    reason = draft.reason_code
                    break
                validate_grounded_draft(draft, evidence, analysis=analysis)
                if published and draft.claims != tuple(published):
                    raise ValidationFailed(
                        "增量事实与最终草稿不一致。",
                        stage="answer.validate",
                        code="STREAMED_CLAIMS_MISMATCH",
                    )
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
                    answer,
                    "llm",
                    tuple(calls),
                    "CLAIMS_VALIDATED",
                    tuple(
                        dict.fromkeys(
                            support.support_id
                            for claim in draft.claims
                            for support in claim.supports
                        )
                    ),
                )
            except QueryCancelled as error:
                error.provider_calls = (*calls, *error.provider_calls)
                raise
            except ValidationFailed as error:
                calls.extend(
                    error.provider_calls
                    or (
                        ()
                        if error.provider_call is None
                        else (error.provider_call,)
                    )
                )
                reason = error.code
                if published:
                    raise _partial_stream_error(calls) from error
                if reason == "GENERATION_ABSTAINED":
                    break
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
                if published:
                    raise _partial_stream_error(calls) from error
                if isinstance(error, ProviderInvalidResponse):
                    continue
                break
            except ValueError as error:
                if published:
                    raise _partial_stream_error(calls) from error
                reason = "GENERATION_OUTPUT_INVALID"
        return GroundedOutcome(
            None,
            "none",
            tuple(calls),
            reason,
        )


def _raise_if_cancelled(cancellation: CancellationPort | None) -> None:
    """在开始下一阶段前停止已取消查询。"""
    if cancellation is not None and cancellation.is_cancelled():
        raise QueryCancelled("QUERY_CANCELLED")


def _partial_stream_error(calls: list[ProviderCall]) -> StreamDeliveryError:
    """保留已经发生的 Provider 调用，同时禁止发布第二份答案。"""
    error = StreamDeliveryError(
        "流式回答在已发布事实后未能安全收束。",
        stage="answer.stream",
        code="STREAM_PARTIAL_FAILED",
    )
    error.provider_calls = tuple(calls)
    return error
