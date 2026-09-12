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
from rag_app.core.query_text import (
    duty_heading_path_owns_target,
    section_heading_path_owns_target,
)

_QUANTITY_UNIT_ATOM = (
    r"(?:%|％|万元|亿元|元|毫秒|分钟|小时|秒|天|周|个月|年|月|"
    r"毫米|厘米|千米|米|公斤|千克|毫克|克|吨|升|毫升|次|个|"
    r"台|件|人|双|套|副|只|张|支|瓶|组|批|份|条|顶|块|辆|"
    r"艘|架|门|床|℃|[A-Za-zμµΩ°]+)"
)
_QUANTITY_UNIT = (
    rf"(?:{_QUANTITY_UNIT_ATOM})(?:\s*/\s*(?:{_QUANTITY_UNIT_ATOM}))*"
)
_NUMBER = re.compile(
    rf"[+-]?\d+(?:[.,:/-]\d+)*(?:\s*{_QUANTITY_UNIT})?"
)
_QUANTITY_RANGE = re.compile(
    rf"(?P<left>[+-]?\d+(?:[.,:/]\d+)*)\s*"
    rf"(?P<left_unit>{_QUANTITY_UNIT})?\s*"
    rf"(?:-|–|—|~|～|至|到)\s*"
    rf"(?P<right>[+-]?\d+(?:[.,:/]\d+)*)\s*"
    rf"(?P<right_unit>{_QUANTITY_UNIT})"
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
_MIN_TABLE_COLUMN_ROWS = 2
_NAMED_SUBJECT = re.compile(
    r"(?:(?:并|且|同时|以及)?由)\s*"
    r"([A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]{1,16}"
    r"(?:负责人|经理|主管|专员|工程师|部门|人员|设备|系统|模式|部))"
    r"\s*(?:负责(?!人)|承担|的(?:核心)?职责|的(?:维护)?周期)"
)
_ACTION_MODIFIER = r"(?:牵头|主要|直接|统一|共同|定期|自行|独立|擅自)"
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
_LEADING_AGENT_PREFIX = re.compile(r"^\s*(?:(?:并|且|同时|以及)\s*)?由\s*")
_LEADING_OBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:并|且|同时|以及)\s*)?(?:对|向|给|为|与|同|跟)\s*"
)
_MODAL_ACTION = re.compile(
    r"^(?:准备|整理|提交|上传|填报|填写|确认|补充|完成|检查|核对|"
    r"提供|记录|归档|执行)"
)
_ENTITY_SUBJECT = re.compile(
    r"^\s*((?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|用户|客户|人员|岗位|角色|小组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库|部)))"
    r"\s*(?=(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|尚未|没有|未|无|不)?"
    rf"(?:{_ACTION_MODIFIER})?(?:{_DUTY_ACTION_VERB}))"
    r"|的(?:核心)?职责|的(?:维护)?周期)"
)
_STANDALONE_SUBJECT = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|用户|客户|人员|岗位|角色|小组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库|管代|部))"
)
_SECTION_NUMBER_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\s*")
_LEADING_LIST_MARKER = re.compile(
    r"^\s*(?:[（(]?(?:\d+(?:\.\d+)*|[A-Za-z])\s*[.)、）]\s*)"
)
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


@dataclass(frozen=True, slots=True)
class _ClaimSourceGroup:
    """一个不可跨越的来源组及服务端认证的展示语境。"""

    support_text: str
    trusted_subjects: frozenset[str]
    trusted_contexts: frozenset[str]
    trusted_term_contexts: frozenset[str] = frozenset()
    table_columns: tuple[str, ...] = ()


def _terms(text: str) -> set[str]:
    # 只统一温度属性与摄氏单位名称，不翻译其他内容或改变词汇支持阈值。
    text = _TEMPERATURE_ATTRIBUTE.sub("温度", text)
    text = _CELSIUS_QUANTITY.sub(r"\g<value>℃", text)
    normalized = _STOP.sub("", text.casefold())
    return {
        normalized[index : index + 2] for index in range(len(normalized) - 1)
    }


def _subject(text: str) -> str | None:
    subject_text = _LEADING_LIST_MARKER.sub(
        "", _LEADING_ACTION_CONTEXT.sub("", text)
    )
    without_modal = _LEADING_MODAL.sub("", subject_text)
    if without_modal != subject_text and _MODAL_ACTION.match(without_modal):
        return None
    subject_text = without_modal
    if _LEADING_OBJECT_PREFIX.match(subject_text) is not None:
        return None
    subject_text = _LEADING_AGENT_PREFIX.sub("", subject_text)
    if re.match(
        rf"^\s*(?:{_ACTION_MODIFIER})?(?:{_DUTY_ACTION_VERB})",
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
    text = _LEADING_LIST_MARKER.sub("", text)
    identifiers = {"id:" + value for value in _IDENTIFIER.findall(text)}
    text = _IDENTIFIER.sub(" ", text)
    text = _CELSIUS_QUANTITY.sub(r"\g<value>℃", text)
    range_tokens: set[str] = set()

    def close_range(match: re.Match[str]) -> str:
        """把等价区间写法投影成带单位的两个端点。"""
        right_unit = re.sub(r"\s+", "", match["right_unit"])
        left_unit = re.sub(
            r"\s+", "", match["left_unit"] or right_unit
        )
        range_tokens.add(match["left"] + left_unit)
        range_tokens.add(match["right"] + right_unit)
        return " "

    text = _QUANTITY_RANGE.sub(close_range, text)
    return identifiers | range_tokens | {
        re.sub(r"\s+", "", value) for value in _NUMBER.findall(text)
    }


def _table_number_tokens(text: str) -> set[str]:
    """将认证同列中分开的纯数值和复合单位闭合为数量 token。"""
    tokens = _number_tokens(text)
    bare_numbers = {
        token
        for token in tokens
        if re.fullmatch(r"[+-]?\d+(?:[.,:/-]\d+)*", token)
    }
    unit_candidates: list[str] = []
    for line in text.splitlines():
        unit_candidates.append(line.strip(" \t:：。；;"))
        unit_candidates.extend(
            value.strip()
            for value in re.findall(r"[（(]([^()（）]+)[）)]", line)
        )
        _prefix, separator, suffix = line.rpartition("：")
        if separator:
            unit_candidates.append(suffix.strip())
    units = {
        re.sub(r"\s+", "", unit)
        for unit in unit_candidates
        if re.fullmatch(_QUANTITY_UNIT, unit)
    }
    return tokens | {
        number + unit for number in bare_numbers for unit in units
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
    subject_text = _LEADING_LIST_MARKER.sub(
        "",
        _LEADING_MODAL.sub("", _LEADING_ACTION_CONTEXT.sub("", text)),
    )
    if _LEADING_OBJECT_PREFIX.match(subject_text) is not None:
        return None
    subject_text = _LEADING_AGENT_PREFIX.sub("", subject_text)
    if _DUTY_ACTION_PREFIX.match(subject_text) is not None:
        return None
    match = _ENTITY_SUBJECT.match(subject_text)
    if match is None:
        return None
    return match[1]


def _source_has_explicit_subject(subject: str, text: str) -> bool:
    """要求对象在来源中处于主语或独立标题位置。"""
    if _IDENTIFIER.fullmatch(subject):
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(subject) + r"(?![A-Za-z0-9_])",
                text,
                re.IGNORECASE,
            )
        )
    if any(
        _same_subject(subject, candidate)
        for candidate in _standalone_subjects(text)
    ):
        return True
    if any(
        _same_subject(subject, candidate)
        for candidate in _NAMED_SUBJECT.findall(text)
    ):
        return True
    for match in re.finditer(re.escape(subject), text, re.IGNORECASE):
        clause_start = max(
            (text.rfind(delimiter, 0, match.start()) + 1)
            for delimiter in "\n。；;.!！？?，,:："
        )
        prefix = text[clause_start : match.start()]
        prefix = _LEADING_AGENT_PREFIX.sub("", prefix)
        if (
            _SUBJECT_CLAUSE_PREFIX.fullmatch(prefix) is None
        ):
            continue
        if _DUTY_ACTION_PREFIX.match(text[match.end() :]) is not None:
            return True
    return False


def _validate_claim_target(
    claim: AnswerClaim,
    analysis: QueryAnalysis | None,
    *,
    source_groups: tuple[_ClaimSourceGroup, ...] = (),
) -> None:
    """职责或表格回答必须绑定本次查询目标。"""
    if (
        analysis is not None
        and analysis.semantics.answer_type
        is RequestedAnswerType.SECTION_SUMMARY
        and analysis.semantics.relation == "对应内容"
        and analysis.semantics.target
    ):
        if any(
            analysis.semantics.target in group.trusted_contexts
            for group in source_groups
        ):
            return
        raise ValidationFailed(
            "表格事实没有闭合所问行名、列头和值。",
            stage="answer.validate",
            code="CLAIM_QUERY_TARGET_MISMATCH",
        )
    if (
        analysis is None
        or analysis.semantics.answer_type is not RequestedAnswerType.DUTIES
        or not analysis.semantics.target
    ):
        return
    clauses = _clauses_with_subject(claim.text)
    subject = _leading_explicit_subject(clauses[0][0]) if clauses else None
    if subject is not None:
        if _same_subject(subject, analysis.semantics.target):
            return
        raise ValidationFailed(
            "职责事实没有明确回答所问岗位。",
            stage="answer.validate",
            code="CLAIM_QUERY_TARGET_MISMATCH",
        )
    trusted_target_in_every_group = bool(source_groups) and all(
        any(
            _same_subject(candidate, analysis.semantics.target)
            for candidate in group.trusted_subjects
        )
        for group in source_groups
    )
    if not trusted_target_in_every_group:
        raise ValidationFailed(
            "职责事实没有明确回答所问岗位。",
            stage="answer.validate",
            code="CLAIM_QUERY_TARGET_MISMATCH",
        )


def _render_claim_target(
    claim: AnswerClaim, analysis: QueryAnalysis | None
) -> AnswerClaim:
    """为经结构认证但省略主语的职责事实添加明确展示标签。"""
    if (
        analysis is None
        or analysis.semantics.answer_type is not RequestedAnswerType.DUTIES
        or not analysis.semantics.target
    ):
        return claim
    clauses = _clauses_with_subject(claim.text)
    subject = _leading_explicit_subject(clauses[0][0]) if clauses else None
    if subject is not None:
        return claim
    return claim.model_copy(
        update={"text": f"{analysis.semantics.target}：{claim.text}"}
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


def _best_negation_sources(clause: str, sources: list[str]) -> list[str]:
    """将极性绑定到词汇重合最高的原子动作，而非同段相邻动作。"""
    action = _action_terms(clause)
    if action:
        signature = action
        source_signature = _action_terms
    else:
        # “无误”一类短状态去掉否定词后没有二元词，保留原词才能对齐。
        signature = _terms(_NUMBER.sub("", _predicate(clause)))

        def source_signature(text: str) -> set[str]:
            return _terms(_NUMBER.sub("", _predicate(text)))

    if not signature:
        return []
    minimum = min(_MIN_NEGATION_SHARED_TERMS, len(signature))
    scored = [
        (len(source_signature(source) & signature), source)
        for source in sources
    ]
    best = max((score for score, _ in scored), default=0)
    if best < minimum:
        return []
    return [source for score, source in scored if score == best]


def _check_negations(clause: str, source_clauses: list[str]) -> None:
    """只比较同一动作的极性，不能借用其他职责中的否定词。"""
    relevant = _best_negation_sources(clause, source_clauses)
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
    support = dict(item.metadata).get("answer_support")
    table_node_ids = {
        span.node_id
        for span in item.source_spans
        if span.node_id is not None
        and span.source_anchor is not None
        and (
            (
                span.source_anchor.table_index is not None
                and span.source_anchor.row_index is not None
            )
            or any(
                re.fullmatch(r"tr:\d+", part)
                for part in span.source_anchor.structural_path
            )
        )
    }
    if (
        item.table_locator is not None
        and item.table_context
        and isinstance(support, dict)
        and support.get("support_reason") == "TABLE_ROW_CONTENT"
    ):
        supporting_ids = support.get("supporting_span_ids")
        if (
            isinstance(supporting_ids, list)
            and supporting_ids
            and table_node_ids.intersection(
                value for value in supporting_ids if isinstance(value, str)
            )
        ):
            return {
                (
                    "table-row-content",
                    item.document_version_id,
                    item.section_id,
                    item.table_locator,
                    tuple(
                        value
                        for value in supporting_ids
                        if isinstance(value, str)
                    ),
                )
            }
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


def _table_cell_coordinate(
    item: EvidenceItem,
) -> tuple[tuple[object, ...], int, int] | None:
    """读取 Evidence 的唯一逻辑表格、行和列坐标。"""
    cells: set[tuple[tuple[object, ...], int, int]] = set()
    for span in item.source_spans:
        anchor = span.source_anchor
        if anchor is None or span.node_id is None:
            continue
        path = span.structural_path
        located = False
        for index in range(len(path) - 2):
            if not path[index].startswith("tbl:"):
                continue
            row = re.fullmatch(r"tr:(\d+)", path[index + 1])
            column = re.fullmatch(r"tc:(\d+)", path[index + 2])
            if row is None or column is None:
                continue
            if any(part.startswith("tbl:") for part in path[index + 1 :]):
                continue
            cells.add(
                (
                    (
                        item.document_version_id,
                        item.section_id,
                        item.table_locator,
                        anchor.part_uri,
                        anchor.story_kind,
                        path[: index + 1],
                    ),
                    int(row[1]),
                    int(column[1]),
                )
            )
            located = True
        if located:
            continue
        if (
            anchor.table_index is not None
            and anchor.row_index is not None
            and anchor.cell_index is not None
        ):
            cells.add(
                (
                    (
                        item.document_version_id,
                        item.section_id,
                        item.table_locator,
                        anchor.part_uri,
                        anchor.story_kind,
                        ("table-index", anchor.table_index),
                    ),
                    anchor.row_index,
                    anchor.cell_index,
                )
            )
    return next(iter(cells)) if len(cells) == 1 else None


def _trusted_duty_subjects(
    item: EvidenceItem,
    analysis: QueryAnalysis | None,
) -> set[str]:
    """读取同一 Evidence 内由精确标题路径认证的职责主体。"""
    if (
        analysis is None
        or analysis.semantics.answer_type is not RequestedAnswerType.DUTIES
        or not analysis.semantics.target
    ):
        return set()
    support = dict(item.metadata).get("answer_support")
    if not isinstance(support, dict):
        return set()
    target = support.get("query_target")
    supporting_ids = support.get("supporting_span_ids")
    item_node_ids = {
        span.node_id for span in item.source_spans if span.node_id is not None
    }
    if (
        support.get("status") != "SUPPORTED"
        or support.get("answer_type") != RequestedAnswerType.DUTIES.value
        or support.get("support_reason") != "SECTION_HEADING_BODY"
        or not isinstance(target, str)
        or not _same_subject(target, analysis.semantics.target)
        or not duty_heading_path_owns_target(target, item.heading_path)
        or not isinstance(supporting_ids, list)
        or not item_node_ids.intersection(
            value for value in supporting_ids if isinstance(value, str)
        )
    ):
        return set()
    return {target}


def _trusted_section_contexts(
    item: EvidenceItem,
    analysis: QueryAnalysis | None,
) -> set[str]:
    """读取由精确标题路径与完整支持组共同认证的章节语境。"""
    if (
        analysis is None
        or analysis.semantics.answer_type
        is not RequestedAnswerType.SECTION_SUMMARY
        or not analysis.semantics.target
    ):
        return set()
    support = dict(item.metadata).get("answer_support")
    if not isinstance(support, dict):
        return set()
    target = support.get("query_target")
    supporting_ids = support.get("supporting_span_ids")
    item_node_ids = {
        span.node_id for span in item.source_spans if span.node_id is not None
    }
    if (
        support.get("status") != "SUPPORTED"
        or support.get("answer_type")
        != RequestedAnswerType.SECTION_SUMMARY.value
        or support.get("support_reason") != "SECTION_HEADING_BODY"
        or not isinstance(target, str)
        or target != analysis.semantics.target
        or not section_heading_path_owns_target(target, item.heading_path)
        or not isinstance(supporting_ids, list)
        or not item_node_ids.intersection(
            value for value in supporting_ids if isinstance(value, str)
        )
    ):
        return set()
    return {target}


def _trusted_table_contexts(
    item: EvidenceItem,
    analysis: QueryAnalysis | None,
) -> set[str]:
    """读取与真实表格坐标和完整行支持组闭合的查询行名。"""
    if (
        analysis is None
        or analysis.semantics.answer_type
        is not RequestedAnswerType.SECTION_SUMMARY
        or analysis.semantics.relation != "对应内容"
        or not analysis.semantics.target
        or item.table_locator is None
        or not item.table_context
        or _table_cell_coordinate(item) is None
    ):
        return set()
    support = dict(item.metadata).get("answer_support")
    if not isinstance(support, dict):
        return set()
    target = support.get("query_target")
    supporting_ids = support.get("supporting_span_ids")
    item_node_ids = {
        span.node_id for span in item.source_spans if span.node_id is not None
    }
    if (
        support.get("status") != "SUPPORTED"
        or support.get("answer_type")
        != RequestedAnswerType.SECTION_SUMMARY.value
        or support.get("support_reason") != "TABLE_ROW_CONTENT"
        or support.get("requested_relation_or_attribute") != "对应内容"
        or not isinstance(target, str)
        or target != analysis.semantics.target
        or not isinstance(supporting_ids, list)
        or not item_node_ids.intersection(
            value for value in supporting_ids if isinstance(value, str)
        )
    ):
        return set()
    return {target}


def _joined_table_columns(
    columns: dict[tuple[object, ...], dict[int, list[str]]],
) -> tuple[str, ...]:
    """只联合至少跨两个真实行的同列表头和值。"""
    joined: list[str] = []
    for column in sorted(columns, key=repr):
        rows = columns[column]
        if len(rows) < _MIN_TABLE_COLUMN_ROWS:
            continue
        quotes = tuple(
            dict.fromkeys(
                quote
                for row in sorted(rows)
                for quote in rows[row]
            )
        )
        joined.append("\n".join(quotes))
    return tuple(joined)


def _closed_table_contexts(
    cells: list[
        tuple[
            tuple[object, ...],
            int,
            int,
            str,
            str,
            tuple[str, ...],
        ]
    ],
) -> tuple[frozenset[str], frozenset[str]]:
    """只有行名、目标行值和同列表头齐全时才认证表格语境。"""
    targets = {target for _table, _row, _column, _quote, target, _path in cells}
    if len(targets) != 1:
        return frozenset(), frozenset()
    target = next(iter(targets))
    labels = {
        (table, row, column)
        for table, row, column, quote, _target, _path in cells
        if quote.strip() == target.strip()
    }
    if len(labels) != 1:
        return frozenset(), frozenset()
    table, target_row, label_column = next(iter(labels))
    value_columns = {
        column
        for candidate_table, row, column, _quote, _target, _path in cells
        if candidate_table == table
        and row == target_row
        and column != label_column
    }
    closed_columns = {
        column
        for column in value_columns
        if any(
            candidate_table == table
            and candidate_column == column
            and row < target_row
            for candidate_table, row, candidate_column, _quote, _target, _path
            in cells
        )
    }
    if not closed_columns:
        return frozenset(), frozenset()
    headings = frozenset(
        heading
        for candidate_table, _row, column, _quote, _target, path in cells
        if candidate_table == table
        and (column == label_column or column in closed_columns)
        for heading in path
    )
    return frozenset({target}), headings


def _merge_table_numeric_continuations(
    clauses: list[tuple[str, str | None]],
    table_columns: tuple[str, ...],
) -> list[tuple[str, str | None]]:
    """把唯一同列支持的逗号后纯数值续项绑定回前一分句。"""
    merged: list[tuple[str, str | None]] = []
    for clause, subject in clauses:
        numbers = _number_tokens(clause)
        if merged and numbers:
            previous, previous_subject = merged[-1]
            matching_columns = [
                column
                for column in table_columns
                if numbers <= _table_number_tokens(column)
                and _action_terms(previous) & _action_terms(column)
            ]
            if len(matching_columns) == 1:
                merged[-1] = (
                    f"{previous}，{clause}",
                    previous_subject,
                )
                continue
        merged.append((clause, subject))
    return merged


def _claim_source_groups(
    claim: AnswerClaim,
    units: list[EvidenceItem],
    analysis: QueryAnalysis | None,
) -> tuple[_ClaimSourceGroup, ...]:
    """按来源组聚合逐字引用及其同组结构化职责主体。"""
    grouped: dict[tuple[object, ...], list[str]] = {}
    trusted: dict[tuple[object, ...], set[str]] = {}
    contexts: dict[tuple[object, ...], set[str]] = {}
    table_cells: dict[
        tuple[object, ...],
        list[
            tuple[
                tuple[object, ...],
                int,
                int,
                str,
                str,
                tuple[str, ...],
            ]
        ],
    ] = {}
    table_columns: dict[
        tuple[object, ...],
        dict[tuple[object, ...], dict[int, list[str]]],
    ] = {}
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
        trusted.setdefault(group, set()).update(
            _trusted_duty_subjects(item, analysis)
        )
        contexts.setdefault(group, set()).update(
            _trusted_section_contexts(item, analysis)
        )
        table_contexts = _trusted_table_contexts(item, analysis)
        coordinate = _table_cell_coordinate(item)
        if group[0] == "table-row-content" and coordinate is not None:
            table, row, column = coordinate
            table_columns.setdefault(group, {}).setdefault(
                (*table, column), {}
            ).setdefault(row, []).append(support.quote)
            if table_contexts:
                table_cells.setdefault(group, []).append(
                    (
                        table,
                        row,
                        column,
                        support.quote,
                        next(iter(table_contexts)),
                        item.heading_path,
                    )
                )
    result: list[_ClaimSourceGroup] = []
    for group, quotes in grouped.items():
        table_context, table_terms = _closed_table_contexts(
            table_cells.get(group, [])
        )
        result.append(
            _ClaimSourceGroup(
                support_text="\n".join(quotes),
                trusted_subjects=frozenset(trusted.get(group, set())),
                trusted_contexts=frozenset(contexts.get(group, set()))
                | table_context,
                trusted_term_contexts=table_terms,
                table_columns=_joined_table_columns(
                    table_columns.get(group, {})
                ),
            )
        )
    return tuple(result)


def _strip_trusted_context_prefix(
    clause: str, trusted_contexts: frozenset[str]
) -> str:
    """只移除句首精确认证的章节展示前缀，不把标题当正文事实。

    Args:
        clause: 模型输出的单个分句。
        trusted_contexts: 与当前来源节点闭合的精确章节标题。

    Returns:
        去掉受控展示框架后的事实正文；未精确命中时保持原文。

    """
    for context in sorted(trusted_contexts, key=len, reverse=True):
        escaped = re.escape(context.strip())
        if not escaped:
            continue
        framed = rf"[‘'“\"]?{escaped}[’'”\"]?"
        patterns = (
            rf"^\s*{framed}\s*[:：]\s*",
            rf"^\s*{framed}\s*(?:包括|包含)(?:\s*[:：])?\s*",
            rf"^\s*{framed}\s*对应(?:的)?\s*",
            rf"^\s*关于\s*{framed}\s*[,，:：]\s*",
            rf"^\s*在\s*{framed}\s*(?:中|内)\s*[,，:：]?\s*",
        )
        for pattern in patterns:
            match = re.match(pattern, clause, re.IGNORECASE)
            if match is not None and clause[match.end() :].strip():
                return clause[match.end() :].strip()
    return clause


def _validate_clause_support(
    clause: str,
    clause_subject: str | None,
    source_group: _ClaimSourceGroup,
) -> None:
    """核验一个分句的对象、数值、措辞和否定均由同一来源组支持。"""
    support_text = source_group.support_text
    trusted_subjects = source_group.trusted_subjects
    trusted_contexts = source_group.trusted_contexts
    trusted_term_contexts = source_group.trusted_term_contexts
    table_columns = source_group.table_columns
    factual_clause = _strip_trusted_context_prefix(clause, trusted_contexts)
    if factual_clause != clause:
        clause = factual_clause
        clause_subject = _leading_explicit_subject(clause) or _subject(clause)
    source_clauses = _clauses_with_subject(support_text)
    source_subjects = {
        detected
        for source_clause, subject in source_clauses
        if (detected := subject or _leading_explicit_subject(source_clause))
    }
    subjects = set(_NAMED_SUBJECT.findall(clause))
    general_subject = _leading_explicit_subject(clause) or _subject(clause)
    if general_subject:
        subjects.add(general_subject)
    if any(
        not _source_has_explicit_subject(subject, support_text)
        and not (
            not source_subjects
            and any(
                _same_subject(subject, trusted) for trusted in trusted_subjects
            )
        )
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
    table_numeric_sources = [
        text
        for text in table_columns
        if _action_terms(clause) & _action_terms(text)
        and _quantity_relation_matches(clause, text)
    ]
    supported_numbers = _number_tokens("\n".join(numeric_sources))
    for text in table_numeric_sources:
        supported_numbers.update(_table_number_tokens(text))
    if not _number_tokens(clause) <= supported_numbers:
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
    source_terms = terms & _terms("\n".join(relevant_sources))
    supported_terms = terms & _terms(
        "\n".join((*relevant_sources, *trusted_term_contexts))
    )
    if not terms or not source_terms or len(supported_terms) / len(terms) < (
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
        units: list[EvidenceItem] = []
        for support in claim.supports:
            item = by_id.get(support.support_id)
            if (
                item is None
                or not item.publishable
                or not item.source_spans
                or any(not span.is_citable for span in item.source_spans)
                or support.quote not in item.citation_text
                or (
                    len(support.quote.strip()) < _MIN_QUOTE_CHARS
                    and not _trusted_table_contexts(item, analysis)
                )
            ):
                raise ValidationFailed(
                    "引用原文无法核验。",
                    stage="answer.validate",
                    code="CLAIM_QUOTE_INVALID",
                )
            units.append(item)
        source_groups = _claim_source_groups(claim, units, analysis)
        _validate_claim_target(
            claim,
            analysis,
            source_groups=source_groups,
        )
        support_text = "\n".join(support.quote for support in claim.supports)
        claim_contexts = frozenset(
            context
            for group in source_groups
            for context in group.trusted_contexts
        )
        factual_text = _strip_trusted_context_prefix(
            claim.text, claim_contexts
        )
        claim_table_columns = tuple(
            column
            for group in source_groups
            for column in group.table_columns
        )
        clauses = _merge_table_numeric_continuations(
            _clauses_with_subject(factual_text),
            claim_table_columns,
        )
        for clause, clause_subject in clauses:
            for source_group in source_groups:
                try:
                    _validate_clause_support(
                        clause,
                        clause_subject,
                        source_group,
                    )
                except ValidationFailed:
                    continue
                break
            else:
                # 联合所有引用仍不成立时保留精确语义错误；只有跨来源
                # 拼接才能成立时，明确标记来源结构不一致。
                _validate_clause_support(
                    clause,
                    clause_subject,
                    _ClaimSourceGroup(
                        support_text=support_text,
                        trusted_subjects=frozenset(
                            subject
                            for group in source_groups
                            for subject in group.trusted_subjects
                        ),
                        trusted_contexts=frozenset(
                            context
                            for group in source_groups
                            for context in group.trusted_contexts
                        ),
                        trusted_term_contexts=frozenset(
                            context
                            for group in source_groups
                            for context in group.trusted_term_contexts
                        ),
                        table_columns=claim_table_columns,
                    ),
                )
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
            buffered: list[AnswerClaim] = []
            delivered: list[AnswerClaim] = []
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
                    model_evidence_candidates=direct_support or evidence,
                )
                stream_generate = getattr(
                    self.generator, "generate_stream", None
                )
                if on_claim is not None and callable(stream_generate):

                    def validate_and_buffer(
                        claim: AnswerClaim,
                        buffered_claims: list[AnswerClaim] = buffered,
                    ) -> None:
                        """逐条执行证据门，整份草稿收束前只在内存缓冲。

                        Args:
                            claim: Adapter 刚形成的完整来源匹配事实。
                            buffered_claims: 本次尝试已核验但尚未交付的事实。

                        Returns:
                            无返回值；模型完整响应通过后才统一交付。

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
                        if claim in buffered_claims:
                            raise ValidationFailed(
                                "模型重复输出同一事实。",
                                stage="answer.validate",
                                code="DUPLICATE_CLAIM",
                            )
                        buffered_claims.append(claim)

                    if cancellation is None:
                        raise RuntimeError("流式生成缺少 cancellation。")
                    draft = stream_generate(
                        generation_request,
                        on_claim=validate_and_buffer,
                        cancellation=cancellation,
                    )
                else:
                    draft = self.generator.generate(generation_request)
                calls.extend(draft.provider_calls)
                if draft.reason_code == "GENERATION_ABSTAINED":
                    reason = draft.reason_code
                    break
                validate_grounded_draft(draft, evidence, analysis=analysis)
                rendered_claims = tuple(
                    _render_claim_target(claim, analysis)
                    for claim in draft.claims
                )
                if buffered and draft.claims != tuple(buffered):
                    raise ValidationFailed(
                        "增量事实与最终草稿不一致。",
                        stage="answer.validate",
                        code="STREAMED_CLAIMS_MISMATCH",
                    )
                if on_claim is not None:
                    # 只有完整 JSON、最终 claims 列表和所有事实都已通过后，
                    # 才开始向 HTTP 流发布，杜绝“合法前缀 + 失败终态”。
                    for rendered_claim in rendered_claims:
                        _raise_if_cancelled(cancellation)
                        on_claim(rendered_claim)
                        delivered.append(rendered_claim)
                # 只发布已逐条核验的 claim，忽略任何多余模型正文。
                answer = "\n".join(
                    claim.text
                    + " "
                    + " ".join(
                        f"[{support.support_id}]" for support in claim.supports
                    )
                    for claim in rendered_claims
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
                if delivered:
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
                if delivered:
                    raise _partial_stream_error(calls) from error
                if isinstance(error, ProviderInvalidResponse):
                    continue
                break
            except ValueError as error:
                if delivered:
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
