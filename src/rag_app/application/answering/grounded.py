"""有界资料生成、逐条来源核验与一次修复，不将引用 ID 当事实证明。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import TYPE_CHECKING, Literal

from rag_app.application.answering.natural_renderer import (
    MissingAtomReason,
    ValidatedNaturalClaim,
    render_natural_answer,
)
from rag_app.application.answering.ocr_guard import (
    claim_pdf_visual_evidence,
    critical_ocr_atoms,
)
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
    OcrVerificationState,
    ProviderCall,
    QueryAnalysis,
    RequestedAnswerType,
    SourceSpanKind,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
)
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from rag_app.core.ports import (
    CancellationPort,
    CriticalOcrVerifierPort,
    GenerationRequest,
    GeneratorPort,
)
from rag_app.core.query_text import (
    duty_heading_path_owns_target,
    normalize_catalog_label,
    normalize_semantic_text,
    section_heading_path_owns_target,
)

if TYPE_CHECKING:
    from rag_app.application.retrieval.generation_evidence import (
        GenerationEvidencePack,
    )

_QUANTITY_UNIT_ATOM = (
    r"(?:%|％|万元|亿元|元|毫秒|分钟|小时|秒|天|周|个月|年|月|"
    r"毫米|厘米|千米|米|公斤|千克|毫克|克|吨|升|毫升|次|个|"
    r"台|件|人|双|套|副|只|张|支|瓶|组|批|份|条|顶|块|辆|"
    r"艘|架|门|床|℃|[A-Za-zμµΩ°]+)"
)
_TABLE_INTERSECTION_SPAN_COUNT = 3
_QUANTITY_UNIT = (
    rf"(?:{_QUANTITY_UNIT_ATOM})(?:\s*/\s*(?:{_QUANTITY_UNIT_ATOM}))*"
)
_NUMBER = re.compile(rf"[+-]?\d+(?:[.,:/-]\d+)*(?:\s*{_QUANTITY_UNIT})?")
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
_ENUMERATION_LEAD_IN = re.compile(
    r"下列|如下|以下|包括|包含|列举|事项|情形|行为|要求|内容|步骤|"
    r"阶段|项目|种类"
)
_NEGATION = re.compile(
    r"不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|"
    r"尚未|没有|并非|不是|未(?!来)|无(?!线(?!索))|"
    r"不(?!同(?!意|步)|断(?!开|电|网|水|气)|仅|但)"
)
_STRONG_MODAL = re.compile(
    r"必须|应当|应予|应由|应在|应向|应将|应按|应对|应及时|"
    r"应(?=\s|[，。：；,;])|须"
)
_PERMISSIVE_MODAL = re.compile(
    r"可以|允许|可由|可在|可按|可向|可将|可免|"
    r"可(?=\s|[，。：；,;])"
)
_INFERENCE_OPERATOR = re.compile(
    r"因此|所以|由此|据此|意味着|推断|通常|一般而言|"
    r"仍然|仍可|自动|即使|无论|跨(?=年|期|版|域)"
)
_CONDITION_SCOPE = re.compile(
    r"(?:如果|若|一旦|只要|只有|即使|无论)"
    r"(?P<scope>[^，,。；;]{2,48})(?:[，,。；;]|$)|"
    r"(?:在|当)(?P<temporal>[^，,。；;]{2,48}?)"
    r"(?:情况下|时)(?=[，,。；;]|可以|应|须|需|必|即|则)"
)
_MODALITY_CLASSES = (
    ("MUST", re.compile(r"必须|(?<!不)须(?!要)")),
    ("SHOULD", re.compile(r"应当|应予|应该|应(?=\s|[，。：；,;])")),
    ("NEED", re.compile(r"需要|需(?=\s|[，。：；,;])")),
    ("MAY", re.compile(r"可以|允许|可(?=\s|[，。：；,;])")),
)
_EXEMPTION_CONDITION_QUESTION = re.compile(
    r"(?:何种|哪些|什么)情况(?:下)?[^?？]{0,20}"
    r"(?:可以不|可不|允许不|无需|不必|不用|免于)"
    r"(?:执行|实施|进行|开展|办理|提交|采取|使用|审批|审核|填写|回滚)"
)
_EXPLICIT_EXEMPTION = re.compile(
    r"可以不|可不|允许不|无需|不必|不用|免于|豁免|可免除|不需要"
)
_SAME_RELATION = re.compile(r"相同|一样|一致")
_DIFFERENT_RELATION = re.compile(r"不同(?!意|步)")
_STOP = re.compile(r"[\W_]|的|了|和|与|及|在|将|其|以|并|为|是", re.UNICODE)
_MIN_QUOTE_CHARS = 2
_FALLBACK_MIN_BIGRAM_OVERLAP = 2
_FALLBACK_MAX_ORDINARY_EXCERPTS = 3
_FALLBACK_TABLE_LABEL_MIN_CHARS = 3
_FALLBACK_TABLE_LABEL_MAX_CHARS = 24
_FALLBACK_NODE_MIN_EXCERPTS = 2
_FALLBACK_DURATION = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个工作日|工作日|天|日|周|个月|月|年|小时|分钟)"
)
_FALLBACK_LIST_MARKER = re.compile(
    r"^[（(]?[一二三四五六七八九十\d]+[）).、]?$"
)
_DIRECT_EXTRACT_MAX_CHARS = 500
# 引用、对象、数字、频率与否定另有独立硬门。这里仅要求自然改写与
# 来源谓语保留基本词面联系，避免把同义概括误判成无支持事实。
_MIN_SUPPORTED_BIGRAM_RATIO = 0.20
_MIN_LIST_ITEM_OVERLAP = 0.20
_MIN_STRUCTURED_LIST_ITEMS = 2
_QUOTED_DOCUMENT_TITLE = re.compile(r"《([^》]{3,200})》")
_CATALOG_ENTRY = re.compile(
    r"^模板目录项：(?P<title>.+?)（模板）。模板正文未入库；"
)
_CATALOG_RELATION = re.compile(r"有|存在|参考|目录项|收录|列出")
_CATALOG_ALLOWED_CLAIM = re.compile(
    r"(?:模板目录项|模板正文未入库|可供参考|应参考|请参考|可以参考|"
    r"可参考|原始模板|目录中|目录项|已收录|已列出|存在|是的|"
    r"这份|一份|该份|具体|填写项|示例|要求|未入库|参考|"
    r"准备|相关|材料|模板|可以|可用|使用|根据|原始|"
    r"时|与|的|该|此|请|应|可|供|为|是|在|中|有|及)*"
)
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

_MIN_MULTI_PART_CLAUSES = 2


@dataclass(frozen=True)
class GroundedOutcome:
    """供检索与历史真实记录的生成结果。"""

    answer: str | None
    mode: Literal["llm", "extractive", "extractive_fallback", "none"]
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str | None = None
    published_support_ids: tuple[str, ...] = ()
    ocr_verification_states: tuple[tuple[str, OcrVerificationState], ...] = ()
    atom_coverage: tuple[tuple[str, str], ...] = ()
    repair_calls: int = 0
    claim_rejection_codes: tuple[tuple[str, int], ...] = ()
    generated_claim_count: int = 0
    accepted_claim_count: int = 0
    published_claim_count: int = 0
    generation_gap_count: int = 0
    missing_atom_reasons: tuple[tuple[str, str], ...] = ()
    false_limited_detected: bool = False
    accepted_support_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ClaimSourceGroup:
    """一个不可跨越的来源组及服务端认证的展示语境。"""

    support_text: str
    trusted_subjects: frozenset[str]
    trusted_contexts: frozenset[str]
    trusted_term_contexts: frozenset[str] = frozenset()
    table_columns: tuple[str, ...] = ()


def _model_candidates_for_query(
    query: str,
    direct_support: tuple[EvidenceItem, ...],
    evidence: tuple[EvidenceItem, ...],
) -> tuple[EvidenceItem, ...]:
    """为生成保留直接支持优先级，复合问题再补充宽候选。

    Args:
        query: 用户当前问题。
        direct_support: 已被本地证据闭合器确认的最小支持集。
        evidence: 检索、融合和重排后的有界模型候选。

    Returns:
        单一事实仅使用直接支持；多问或并列问题先放直接支持，
        再按已有重排顺序补齐其他候选。

    """
    if not direct_support:
        return evidence
    question_parts = tuple(
        part.strip() for part in re.split(r"[?？]+", query) if part.strip()
    )
    if len(question_parts) < _MIN_MULTI_PART_CLAUSES:
        return direct_support
    direct_ids = {item.support_id for item in direct_support}
    return (
        *direct_support,
        *(item for item in evidence if item.support_id not in direct_ids),
    )


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


def _lexical_predicate(text: str) -> str:
    """移除仅有对象名的来源，防止对象词被误当作动作支持。"""
    stripped = text.strip(" \t\r\n，,。；;：:")
    if (
        _STANDALONE_SUBJECT.fullmatch(stripped) is not None
        and re.search(_DUTY_ACTION_VERB, stripped) is None
    ):
        return ""
    return _predicate(text)


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
        left_unit = re.sub(r"\s+", "", match["left_unit"] or right_unit)
        range_tokens.add(match["left"] + left_unit)
        range_tokens.add(match["right"] + right_unit)
        return " "

    text = _QUANTITY_RANGE.sub(close_range, text)
    return (
        identifiers
        | range_tokens
        | {re.sub(r"\s+", "", value) for value in _NUMBER.findall(text)}
    )


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
    return tokens | {number + unit for number in bare_numbers for unit in units}


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
        for segment in _split_enumeration_lead_in(normalized_sentence):
            for clause in re.split(r"[，,]", segment):
                if clause.strip():
                    subject = _subject(clause) or subject
                    clauses.append((clause, subject))
    return clauses


def _split_enumeration_lead_in(sentence: str) -> tuple[str, ...]:
    """将列举引导句与首项拆开，同时保留时间、比例和普通标签冒号。"""
    for match in re.finditer(r"[:：]", sentence):
        prefix = sentence[: match.start()]
        suffix = sentence[match.end() :]
        if (
            prefix.strip()
            and suffix.strip()
            and _ENUMERATION_LEAD_IN.search(prefix)
            and not (
                prefix[-1:].isdigit()
                or suffix[:1].isdigit()
                or suffix.startswith("//")
            )
        ):
            return prefix, suffix
    return (sentence,)


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
    # “岗位：职责”同样断言了职责归属，不能因冒号隔开而绕过对象门。
    label_match = re.match(r"^\s*([^:：]{1,32})[:：]\s*(.*)$", subject_text)
    if (
        label_match is not None
        and _STANDALONE_SUBJECT.fullmatch(label_match[1].strip())
        and _DUTY_ACTION_PREFIX.match(label_match[2]) is not None
    ):
        return label_match[1].strip()
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
        if _SUBJECT_CLAUSE_PREFIX.fullmatch(prefix) is None:
            continue
        if _DUTY_ACTION_PREFIX.match(text[match.end() :]) is not None:
            return True
    return False


def _validate_claim_target(
    claim: AnswerClaim,
    analysis: QueryAnalysis | None,
    *,
    source_groups: tuple[_ClaimSourceGroup, ...] = (),
    cited_items: tuple[EvidenceItem, ...] = (),
    evidence: tuple[EvidenceItem, ...] = (),
) -> None:
    """职责或表格回答必须绑定本次查询目标。"""
    if (
        analysis is not None
        and _EXEMPTION_CONDITION_QUESTION.search(
            analysis.resolved_query or analysis.normalized_query
        )
        and not any(
            _EXPLICIT_EXEMPTION.search(group.support_text)
            for group in source_groups
        )
        and not _certified_list_exemption(cited_items, evidence)
    ):
        raise ValidationFailed(
            "结果中未执行某动作不等于有条件免除该动作。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNSUPPORTED",
        )
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


def _certified_list_exemption(
    cited_items: tuple[EvidenceItem, ...], evidence: tuple[EvidenceItem, ...]
) -> bool:
    """只有同组导语明确允许免除时，编号条目才继承该关系。"""
    for cited in cited_items:
        certificate = dict(cited.metadata).get("answer_support")
        if (
            not isinstance(certificate, dict)
            or certificate.get("support_reason") != "STRUCTURED_LIST_RELATION"
        ):
            continue
        required = certificate.get("supporting_span_ids")
        if not isinstance(required, list) or not required:
            continue
        group = tuple(
            item
            for item in evidence
            if item.document_version_id == cited.document_version_id
            and dict(item.metadata).get("answer_support") == certificate
            and any(span.node_id in required for span in item.source_spans)
        )
        present = {span.node_id for item in group for span in item.source_spans}
        if len(group) < _MIN_STRUCTURED_LIST_ITEMS or not set(
            required
        ).issubset(present):
            continue
        intro = min(
            group,
            key=lambda item: min(
                (
                    span.source_anchor.ordinal
                    for span in item.source_spans
                    if span.source_anchor is not None
                ),
                default=2**31 - 1,
            ),
        )
        if cited is not intro and _EXPLICIT_EXEMPTION.search(
            intro.citation_text
        ):
            return True
    return False


def _certified_catalog_reference_claim(  # noqa: PLR0911
    claim: AnswerClaim,
    cited_items: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
) -> bool:
    """对精确目录存在关系单独核验，不将标题里的版本号当作新事实。"""
    if analysis is None or len(cited_items) != 1:
        return False
    question = analysis.resolved_query or analysis.normalized_query
    titles = _QUOTED_DOCUMENT_TITLE.findall(question)
    if len(titles) != 1:
        return False
    item = cited_items[0]
    certificate = dict(item.metadata).get("answer_support")
    if not isinstance(certificate, dict) or (
        certificate.get("status") != "SUPPORTED"
        or certificate.get("support_reason") != "CATALOG_TITLE_EXISTS"
    ):
        return False
    entry = _CATALOG_ENTRY.match(item.citation_text)
    target = normalize_catalog_label(titles[0])
    if (
        entry is None
        or not item.display_name
        or normalize_catalog_label(entry["title"]) != target
        or normalize_catalog_label(item.display_name) != target
    ):
        return False
    claim_text = unicodedata.normalize("NFKC", unescape(claim.text))
    source_title = unicodedata.normalize("NFKC", unescape(entry["title"]))
    query_title = unicodedata.normalize("NFKC", unescape(titles[0]))
    mentioned = next(
        (
            title
            for title in sorted(
                {source_title, query_title}, key=len, reverse=True
            )
            if title in claim_text
        ),
        None,
    )
    if mentioned is None:
        return False
    remainder = claim_text.replace(mentioned, "")
    if not _CATALOG_RELATION.search(remainder):
        return False
    remainder = re.sub(r"[\s\W_]+", "", remainder)
    return _CATALOG_ALLOWED_CLAIM.fullmatch(remainder) is not None


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
        isinstance(support, dict)
        and support.get("support_reason") == "TABLE_INTERSECTION"
        and isinstance(support.get("supporting_span_ids"), list)
        and table_node_ids.intersection(support["supporting_span_ids"])
    ):
        return {
            (
                "table-intersection",
                item.document_version_id,
                item.section_id,
                item.table_locator,
                tuple(support["supporting_span_ids"]),
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
            dict.fromkeys(quote for row in sorted(rows) for quote in rows[row])
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
            for (
                candidate_table,
                row,
                candidate_column,
                _quote,
                _target,
                _path,
            ) in cells
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
    intersection_cells: dict[
        tuple[object, ...],
        list[tuple[tuple[object, ...], int, int, str]],
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
        if group[0] == "table-intersection" and coordinate is not None:
            table, row, column = coordinate
            intersection_cells.setdefault(group, []).append(
                (table, row, column, support.quote)
            )
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
        intersection_columns = _joined_table_intersection(
            intersection_cells.get(group, [])
        )
        result.append(
            _ClaimSourceGroup(
                support_text="\n".join(quotes),
                trusted_subjects=frozenset(trusted.get(group, set())),
                trusted_contexts=frozenset(contexts.get(group, set()))
                | table_context,
                trusted_term_contexts=table_terms,
                table_columns=intersection_columns
                or _joined_table_columns(table_columns.get(group, {})),
            )
        )
    return tuple(result)


def _joined_table_intersection(
    cells: list[tuple[tuple[object, ...], int, int, str]],
) -> tuple[str, ...]:
    """只将已被共同引用的唯一交点用于本地数值与关系核验。"""
    if len({table for table, _row, _column, _text in cells}) != 1:
        return ()
    labels = [
        text for _table, row, column, text in cells if row > 0 and column == 0
    ]
    headers = [
        (column, text)
        for _table, row, column, text in cells
        if row == 0 and column > 0
    ]
    values = [
        (row, column, text)
        for _table, row, column, text in cells
        if row > 0 and column > 0
    ]
    if len(labels) != 1 or len(headers) != 1 or len(values) != 1:
        return ()
    _row, value_column, value = values[0]
    header_column, header = headers[0]
    if value_column != header_column:
        return ()
    return (" ".join((labels[0], header, value)),)


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
    factual_sources = tuple(
        _lexical_predicate(text) for text in relevant_sources
    )
    source_terms = terms & _terms("\n".join(factual_sources))
    supported_terms = terms & _terms(
        "\n".join((*factual_sources, *trusted_term_contexts))
    )
    if (
        not terms
        or not source_terms
        or len(supported_terms) / len(terms) < (_MIN_SUPPORTED_BIGRAM_RATIO)
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
    complete: bool = True,
) -> None:
    """校验逐字支持、来源关系与关键事实，允许有词汇依据的自然概括。

    Args:
        draft: 模型的结构化事实草稿。
        evidence: 本次已通过范围筛选的有限证据。
        analysis: 可选的服务端查询语义，用于约束职责主体。
        complete: 增量 claim 校验时为 False；最终草稿必须检查完整列表。

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
            cited_items=tuple(units),
            evidence=evidence,
        )
        if any(_CATALOG_ENTRY.match(item.citation_text) for item in units):
            if _certified_catalog_reference_claim(
                claim, tuple(units), analysis
            ):
                continue
            raise ValidationFailed(
                "模板目录项只证明精确标题存在，不能证明正文或其他事实。",
                stage="answer.validate",
                code="CATALOG_CLAIM_UNSUPPORTED",
            )
        support_text = "\n".join(support.quote for support in claim.supports)
        claim_contexts = frozenset(
            context
            for group in source_groups
            for context in group.trusted_contexts
        )
        factual_text = _strip_trusted_context_prefix(claim.text, claim_contexts)
        claim_table_columns = tuple(
            column for group in source_groups for column in group.table_columns
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
    if complete:
        _validate_structured_list_coverage(draft, evidence, analysis)


def _validate_structured_list_coverage(
    draft: AnswerDraft,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
) -> None:
    """列表导语不算条目；每个来源条目都须进入回答并由原文支持。"""
    if analysis is None or analysis.semantics.answer_type not in {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.PROCEDURE,
    }:
        return
    groups: dict[tuple[str, ...], list[EvidenceItem]] = {}
    for item in evidence:
        support = dict(item.metadata).get("answer_support")
        if (
            not isinstance(support, dict)
            or support.get("support_reason") != "STRUCTURED_LIST_RELATION"
        ):
            continue
        span_ids = support.get("supporting_span_ids")
        if isinstance(span_ids, list):
            key = tuple(value for value in span_ids if isinstance(value, str))
            groups.setdefault(key, []).append(item)
    for group in groups.values():
        if len(group) < _MIN_STRUCTURED_LIST_ITEMS:
            continue
        intro = min(
            group,
            key=lambda item: min(
                (
                    span.source_anchor.ordinal
                    for span in item.source_spans
                    if span.source_anchor is not None
                ),
                default=2**31 - 1,
            ),
        )
        for item in group:
            if item is intro:
                continue
            terms = _terms(_LEADING_LIST_MARKER.sub("", item.citation_text))
            represented = (
                any(
                    any(
                        support.support_id == item.support_id
                        for support in claim.supports
                    )
                    and bool(terms & _terms(claim.text))
                    and len(terms & _terms(claim.text)) / len(terms)
                    >= _MIN_LIST_ITEM_OVERLAP
                    for claim in draft.claims
                )
                if terms
                else False
            )
            if not represented:
                raise ValidationFailed(
                    "列举答案遗漏了来源列表条目。",
                    stage="answer.validate",
                    code="ANSWER_LIST_INCOMPLETE",
                )


def _verify_critical_ocr_claims(
    claims: tuple[AnswerClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    verifier: CriticalOcrVerifierPort | None,
    cancellation: CancellationPort | None,
) -> tuple[
    tuple[ProviderCall, ...],
    tuple[tuple[str, OcrVerificationState], ...],
]:
    """在最终草稿上执行有限视觉关键原子复核。"""
    calls: list[ProviderCall] = []
    states: dict[str, OcrVerificationState] = {}
    for claim in claims:
        atoms = critical_ocr_atoms(claim.text)
        visual_evidence = claim_pdf_visual_evidence(claim, evidence)
        visual_support_ids = {item.evidence_id for item in visual_evidence}
        atoms = tuple(
            atom
            for atom in atoms
            if any(
                support.support_id in visual_support_ids
                and atom in support.quote
                for support in claim.supports
            )
        )
        if not atoms or not visual_evidence:
            continue
        _raise_if_cancelled(cancellation)
        if verifier is None:
            error = ValidationFailed(
                "高风险 PDF 视觉文字缺少 PP-OCRv6 复核能力。",
                stage="answer.ocr_verify",
                code="OCR_CRITICAL_ATOM_UNVERIFIED",
            )
            error.provider_calls = tuple(calls)
            raise error
        result = verifier.verify(claim, visual_evidence, atoms)
        if result.state is not OcrVerificationState.VERIFIED:
            error = ValidationFailed(
                (
                    "页面识别结果存在冲突，请查看原页。"
                    if result.state is OcrVerificationState.CONFLICT
                    else "高风险 PDF 视觉文字未能完成二次复核。"
                ),
                stage="answer.ocr_verify",
                code=(
                    "OCR_CRITICAL_ATOM_CONFLICT"
                    if result.state is OcrVerificationState.CONFLICT
                    else "OCR_CRITICAL_ATOM_UNVERIFIED"
                ),
                details={"verification_reason": result.reason_code},
            )
            error.provider_calls = (*calls, *result.provider_calls)
            raise error
        calls.extend(result.provider_calls)
        states.update(result.support_states)
    return tuple(calls), tuple(states.items())


class GroundedAnsweringService:
    """生成不确定性只在合法证据内解决；失败时保留原因并拒答。"""

    def __init__(
        self,
        generator: GeneratorPort,
        *,
        critical_ocr_verifier: CriticalOcrVerifierPort | None = None,
    ) -> None:
        """绑定最多执行初次生成与一次修复的生成器。

        Args:
            generator: 最多接收初次生成与一次修复请求的生成端口。
            critical_ocr_verifier: 最终 PDF 高风险事实的有界复核端口。

        Returns:
            无返回值。

        """
        self.generator = generator
        self._critical_ocr_verifier = critical_ocr_verifier

    def answer(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
        *,
        answer_support_set: tuple[EvidenceItem, ...] | None = None,
        analysis: QueryAnalysis | None = None,
        query_plan: QueryPlan | None = None,
        atom_support_matrix: AtomSupportMatrix | None = None,
        generation_evidence_pack: GenerationEvidencePack | None = None,
        on_claim: Callable[[AnswerClaim], None] | None = None,
        cancellation: CancellationPort | None = None,
    ) -> GroundedOutcome:
        """最多两次生成；正文缺失、权限和索引错误不允许模型覆盖。

        Args:
            query: 已排除纯回答格式指令并完成必要改写的问题。
            evidence: 已通过资源和引用检查的有限资料。
            confidence: 检索置信状态，不允许越过硬性拒绝。
            answer_support_set: 已直接支持所问关系的最小集合，供模型核验使用。
            analysis: 检索、Evidence 与回答共同消费的最终查询分析。
            query_plan: 可选的本次类型化事实原子计划。
            atom_support_matrix: 可选的逐原子检索支持状态。
            generation_evidence_pack: 可选的有界生成准入证据包。
            on_claim: 可选的已校验完整 claim 发布回调。
            cancellation: 可选协作取消端口。

        Returns:
            已核验回答或明确拒答，包含真实调用和终态原因。

        """
        if query_plan is not None or atom_support_matrix is not None:
            if query_plan is None or atom_support_matrix is None:
                raise ValueError(
                    "类型化回答必须同时提供 QueryPlan 和支持矩阵。"
                )
            return self._answer_with_plan(
                query,
                evidence,
                confidence,
                query_plan=query_plan,
                atom_support_matrix=atom_support_matrix,
                generation_evidence_pack=generation_evidence_pack,
                analysis=analysis,
                on_claim=on_claim,
                cancellation=cancellation,
            )
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
            if attempt and not (
                confidence.status is ConfidenceStatus.ANSWERABLE
                and direct_support
                and reason == "ANSWER_LIST_INCOMPLETE"
            ):
                break
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
                    model_evidence_candidates=_model_candidates_for_query(
                        query,
                        direct_support,
                        evidence,
                    ),
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
                            complete=False,
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
                verification_calls, verification_states = (
                    _verify_critical_ocr_claims(
                        draft.claims,
                        evidence,
                        self._critical_ocr_verifier,
                        cancellation,
                    )
                )
                calls.extend(verification_calls)
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
                    verification_states,
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
                    # 模型已经返回但 claims 形状无效，是本次回答未通过校验，
                    # 不是 HTTP Provider 不可用；同时把具体原因传给修复轮。
                    if dict(error.details).get("reason_code") == (
                        "GENERATION_CLAIMS_INVALID"
                    ):
                        reason = "GENERATION_CLAIMS_INVALID"
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

    def _answer_with_plan(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
        *,
        query_plan: QueryPlan,
        atom_support_matrix: AtomSupportMatrix,
        generation_evidence_pack: GenerationEvidencePack | None,
        analysis: QueryAnalysis | None,
        on_claim: Callable[[AnswerClaim], None] | None,
        cancellation: CancellationPort | None,
    ) -> GroundedOutcome:
        """单次自然生成、逐原子事实核验和仅缺项的局部修复。"""
        if confidence.status not in {
            ConfidenceStatus.ANSWERABLE,
            ConfidenceStatus.INSUFFICIENT_EVIDENCE,
        }:
            return GroundedOutcome(
                None, "none", reason_code=confidence.status.value
            )
        if {atom.atom_id for atom in query_plan.atoms} != {
            atom.atom_id for atom in atom_support_matrix.atoms
        }:
            raise ValueError("逐原子支持矩阵与 QueryPlan 不一致。")
        if generation_evidence_pack is not None:
            evidence = generation_evidence_pack.evidence
        linked_ids = (
            dict(generation_evidence_pack.per_atom_candidate_support_ids)
            if generation_evidence_pack is not None
            else {}
        )
        by_id = {item.support_id: item for item in evidence}
        if (
            generation_evidence_pack is not None
            and not any(
                item.status is AtomStatus.CONTRADICTORY
                for item in atom_support_matrix.atoms
            )
            and (direct := _direct_extract(query_plan, evidence, analysis))
            is not None
        ):
            claim, item = direct
            _raise_if_cancelled(cancellation)
            if on_claim is not None:
                on_claim(claim)
            return GroundedOutcome(
                answer=f"根据资料：{claim.text} [{item.support_id}]",
                mode="extractive",
                reason_code="DIRECT_EXTRACT",
                published_support_ids=(item.support_id,),
                atom_coverage=((query_plan.atoms[0].atom_id, "SUPPORTED"),),
                accepted_claim_count=1,
                published_claim_count=1,
                accepted_support_ids=(item.support_id,),
            )
        stream_claims = (
            query_plan.effort == "DIRECT"
            and len(query_plan.atoms) == 1
            and query_plan.atoms[0].answer_shape
            not in {
                AtomAnswerShape.ENUMERATION,
                AtomAnswerShape.PROCEDURE,
                AtomAnswerShape.DUTIES,
            }
        )
        eligible = {
            atom.atom_id
            for atom in query_plan.atoms
            if evidence
            and atom_support_matrix.for_atom(atom.atom_id).status
            is not AtomStatus.CONTRADICTORY
        }
        calls: list[ProviderCall] = []
        accepted: list[ValidatedNaturalClaim] = []
        claim_rejections: Counter[str] = Counter()
        rejected_atoms: Counter[str] = Counter()
        generated_claim_count = 0
        generation_returned = False
        reason: str | None = None
        repair_calls = 0

        def generate(
            repair_atom_ids: tuple[str, ...] = (),
        ) -> AnswerDraft:
            """从准入证据中选出本次 Atom 的候选，不以发布许可过滤。"""
            requested = set(repair_atom_ids) if repair_atom_ids else eligible
            allowed = {
                support_id
                for atom_id in requested
                for support_id in linked_ids.get(
                    atom_id,
                    atom_support_matrix.for_atom(atom_id).supporting_support_ids
                    or tuple(by_id),
                )
            }
            candidates = tuple(
                item for item in evidence if item.support_id in allowed
            )
            candidate_ids = {item.support_id for item in candidates}
            request = GenerationRequest(
                query=query,
                evidence=candidates,
                citation_protocol="support-id-v3-quoted-natural-claims",
                typed_semantics=None
                if analysis is None
                else analysis.semantics,
                model_evidence_candidates=candidates,
                query_plan=query_plan,
                atom_support_matrix=atom_support_matrix,
                per_atom_candidate_support_ids=tuple(
                    (
                        atom_id,
                        tuple(
                            support_id
                            for support_id in linked_ids.get(
                                atom_id,
                                atom_support_matrix.for_atom(
                                    atom_id
                                ).supporting_support_ids
                                or tuple(by_id),
                            )
                            if support_id in candidate_ids
                        ),
                    )
                    for atom_id in requested
                ),
                repair_atom_ids=repair_atom_ids,
                accepted_claim_ids=tuple(item.claim_id for item in accepted),
            )
            stream_generate = getattr(self.generator, "generate_stream", None)
            if (
                stream_claims
                and on_claim is not None
                and cancellation is not None
                and callable(stream_generate)
            ):
                draft = stream_generate(
                    request,
                    on_claim=lambda _claim: None,
                    cancellation=cancellation,
                )
                if not isinstance(draft, AnswerDraft):
                    raise ValueError("流式生成未返回 AnswerDraft。")
                return draft
            return self.generator.generate(request)

        def consume(draft: AnswerDraft) -> None:
            """只保留本地核验通过的 Claim，原文由证据回填。"""
            nonlocal generated_claim_count, generation_returned, reason
            if draft.generation_mode != "natural":
                raise ValidationFailed(
                    "类型化生成返回错误协议。",
                    stage="answer.validate",
                    code="GENERATION_CLAIMS_INVALID",
                )
            calls.extend(draft.provider_calls)
            generation_returned = True
            generated_claim_count += len(draft.natural_claims)
            if not draft.natural_claims:
                reason = draft.reason_code or "GENERATION_ABSTAINED"
            for natural in draft.natural_claims:
                try:
                    claim = _validated_natural_claim(
                        natural,
                        query_plan,
                        atom_support_matrix,
                        evidence,
                        analysis,
                    )
                except (ValidationFailed, ValueError) as error:
                    claim_rejections[_natural_rejection_code(error)] += 1
                    rejected_atoms[natural.atom_id] += 1
                    reason = "CLAIM_NOT_SUPPORTED"
                    continue
                if any(
                    item.atom_ids == (natural.atom_id,) and item.claim == claim
                    for item in accepted
                ):
                    continue
                accepted.append(
                    ValidatedNaturalClaim(
                        claim_id=f"C{len(accepted) + 1}",
                        atom_ids=(natural.atom_id,),
                        claim=claim,
                    )
                )

        if eligible:
            try:
                _raise_if_cancelled(cancellation)
                consume(generate())
                # 资料完整但模型漏掉结构成员时，也只补对应 Atom。
                omitted = tuple(
                    atom.atom_id
                    for atom in query_plan.atoms
                    if atom.atom_id in eligible
                    and atom_support_matrix.for_atom(atom.atom_id).status
                    is AtomStatus.SUPPORTED
                    and not _natural_atom_complete(
                        atom,
                        atom_support_matrix,
                        tuple(accepted),
                        evidence,
                        analysis,
                        generation_evidence_pack=generation_evidence_pack,
                    )
                )
                if omitted and accepted:
                    _raise_if_cancelled(cancellation)
                    repair_calls = 1
                    consume(generate(omitted))
            except QueryCancelled as error:
                error.provider_calls = (*calls, *error.provider_calls)
                raise
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
            except ValueError:
                reason = "GENERATION_OUTPUT_INVALID"

        accepted_claims = tuple(item.claim for item in accepted)
        try:
            verification_calls, verification_states = (
                _verify_critical_ocr_claims(
                    accepted_claims,
                    evidence,
                    self._critical_ocr_verifier,
                    cancellation,
                )
            )
            calls.extend(verification_calls)
        except ValidationFailed as error:
            calls.extend(error.provider_calls)
            return GroundedOutcome(None, "none", tuple(calls), error.code)

        if (
            generation_evidence_pack is not None
            and generation_returned
            and not accepted
        ):
            fallback = _safe_extractive_fallback(
                query_plan,
                evidence,
                linked_ids,
                generation_evidence_pack.complete_group_ids,
            )
            if fallback is not None:
                fallback_answer, fallback_ids, fallback_atoms = fallback
                return GroundedOutcome(
                    answer=fallback_answer,
                    mode="extractive_fallback",
                    calls=tuple(calls),
                    reason_code="EXTRACTIVE_FALLBACK",
                    published_support_ids=fallback_ids,
                    atom_coverage=tuple(
                        (
                            atom.atom_id,
                            (
                                AtomStatus.PARTIAL
                                if atom.atom_id in fallback_atoms
                                else AtomStatus.MISSING
                            ).value,
                        )
                        for atom in query_plan.atoms
                    ),
                    repair_calls=repair_calls,
                    claim_rejection_codes=tuple(
                        sorted(claim_rejections.items())
                    ),
                    generated_claim_count=generated_claim_count,
                    accepted_claim_count=0,
                    published_claim_count=0,
                    missing_atom_reasons=tuple(
                        (atom.atom_id, "GENERATION_INCOMPLETE")
                        for atom in query_plan.atoms
                        if atom.atom_id not in fallback_atoms
                    ),
                )

        covered = {atom_id for item in accepted for atom_id in item.atom_ids}
        coverage: list[tuple[str, str]] = []
        missing: dict[str, MissingAtomReason] = {}
        generation_gap_count = 0
        false_limited_detected = False
        for atom in query_plan.atoms:
            pre = atom_support_matrix.for_atom(atom.atom_id)
            candidate_ids = linked_ids.get(
                atom.atom_id,
                pre.supporting_support_ids
                if generation_evidence_pack is None
                else (),
            )
            if pre.status is AtomStatus.CONTRADICTORY:
                final = AtomStatus.CONTRADICTORY
            elif atom.atom_id in covered and _natural_atom_complete(
                atom,
                atom_support_matrix,
                tuple(accepted),
                evidence,
                analysis,
                generation_evidence_pack=generation_evidence_pack,
            ):
                final = AtomStatus.SUPPORTED
                if pre.status is AtomStatus.PARTIAL:
                    false_limited_detected = True
            elif atom.atom_id in covered:
                final = AtomStatus.PARTIAL
                source_complete = pre.status is AtomStatus.SUPPORTED or (
                    generation_evidence_pack is not None
                    and any(
                        entry.source_group_id
                        in generation_evidence_pack.complete_group_ids
                        and entry.support_id in candidate_ids
                        for entry in generation_evidence_pack.entries
                    )
                )
                if (
                    atom.answer_shape
                    in {
                        AtomAnswerShape.ENUMERATION,
                        AtomAnswerShape.DUTIES,
                        AtomAnswerShape.PROCEDURE,
                    }
                    and not source_complete
                ):
                    missing[atom.atom_id] = (
                        MissingAtomReason.STRUCTURE_INCOMPLETE
                    )
                else:
                    missing[atom.atom_id] = (
                        MissingAtomReason.GENERATION_INCOMPLETE
                    )
                if source_complete:
                    generation_gap_count += 1
            elif not candidate_ids:
                final = AtomStatus.MISSING
                missing[atom.atom_id] = MissingAtomReason.SOURCE_MISSING
            else:
                final = AtomStatus.MISSING
                generation_gap_count += 1
                missing[atom.atom_id] = (
                    MissingAtomReason.CLAIM_REJECTED
                    if rejected_atoms[atom.atom_id]
                    else MissingAtomReason.GENERATION_INCOMPLETE
                )
            coverage.append((atom.atom_id, final.value))
        answer = render_natural_answer(
            query_plan,
            atom_support_matrix,
            tuple(accepted),
            evidence,
            missing_atoms=missing,
        )
        accepted_support_ids = tuple(
            dict.fromkeys(
                support.support_id
                for item in accepted
                for support in item.claim.supports
            )
        )
        if answer is None:
            return GroundedOutcome(
                None,
                "none",
                tuple(calls),
                reason or "GENERATION_ABSTAINED",
                atom_coverage=tuple(coverage),
                repair_calls=repair_calls,
                claim_rejection_codes=tuple(sorted(claim_rejections.items())),
                generated_claim_count=generated_claim_count,
                accepted_claim_count=len(accepted),
                generation_gap_count=generation_gap_count,
                missing_atom_reasons=tuple(
                    (atom_id, value.value) for atom_id, value in missing.items()
                ),
                false_limited_detected=false_limited_detected,
                accepted_support_ids=accepted_support_ids,
            )
        published = list(accepted_support_ids)
        for atom in atom_support_matrix.atoms:
            if atom.status is not AtomStatus.CONTRADICTORY:
                continue
            for support_id in atom.supporting_support_ids:
                item = by_id.get(support_id)
                if (
                    item is not None
                    and item.publishable
                    and item.source_spans
                    and all(span.is_citable for span in item.source_spans)
                ):
                    published.append(support_id)
        published_ids = tuple(dict.fromkeys(published))
        published_claim_count = len(
            {
                (
                    " ".join(item.claim.text.split()),
                    tuple(
                        support.support_id for support in item.claim.supports
                    ),
                )
                for item in accepted
            }
        )
        if stream_claims and on_claim is not None:
            streamed_facts: set[tuple[str, tuple[str, ...]]] = set()
            for accepted_item in accepted:
                fact_key = (
                    " ".join(accepted_item.claim.text.split()),
                    tuple(
                        support.support_id
                        for support in accepted_item.claim.supports
                    ),
                )
                if fact_key in streamed_facts:
                    continue
                streamed_facts.add(fact_key)
                _raise_if_cancelled(cancellation)
                on_claim(accepted_item.claim)
        has_conflict = any(
            item.status is AtomStatus.CONTRADICTORY
            for item in atom_support_matrix.atoms
        )
        return GroundedOutcome(
            answer,
            "llm" if eligible else "none",
            tuple(calls),
            "CONTRADICTORY_EVIDENCE"
            if has_conflict
            else "LIMITED_ANSWER"
            if missing
            else "CLAIMS_VALIDATED",
            published_ids,
            verification_states,
            tuple(coverage),
            repair_calls,
            tuple(sorted(claim_rejections.items())),
            generated_claim_count,
            len(accepted),
            published_claim_count,
            generation_gap_count,
            tuple((atom_id, value.value) for atom_id, value in missing.items()),
            false_limited_detected,
            accepted_support_ids,
        )


def _direct_extract(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
) -> tuple[AnswerClaim, EvidenceItem] | None:
    """单一标量事实已有直接证书时，复制最小原句形成服务端回答。"""
    if len(plan.atoms) != 1:
        return None
    atom = plan.atoms[0]
    if atom.answer_shape not in {
        AtomAnswerShape.FACT,
        AtomAnswerShape.DURATION,
        AtomAnswerShape.COUNT,
        AtomAnswerShape.RESPONSIBLE_PARTY,
        AtomAnswerShape.DEFINITION,
    }:
        return None
    for item in evidence:
        metadata = dict(item.metadata)
        certificate = metadata.get("answer_support")
        fact = item.citation_text.strip()
        if (
            not isinstance(certificate, dict)
            or certificate.get("status") != "SUPPORTED"
            or certificate.get("support_reason")
            not in {"SOURCE_RELATION_AND_VALUE", "LINKED_SUBJECT_ATTRIBUTE"}
            or normalize_semantic_text(
                str(certificate.get("query_target") or "")
            )
            != normalize_semantic_text(atom.target)
            or normalize_semantic_text(
                str(certificate.get("requested_relation_or_attribute") or "")
            )
            != normalize_semantic_text(atom.relation)
            or item.table_context
            or item.source_kind is not SourceSpanKind.ORIGINAL_TEXT
            or item.pdf_block_id is not None
            or not item.publishable
            or len(item.source_spans) != 1
            or not item.source_spans[0].is_citable
            or len(fact) > _DIRECT_EXTRACT_MAX_CHARS
            or len(re.findall(r"[。！？.!?]", fact)) != 1
        ):
            continue
        claim = AnswerClaim(
            text=fact,
            supports=(ClaimSupport(support_id=item.support_id, quote=fact),),
        )
        try:
            _validate_natural_entailment(claim)
            validate_grounded_draft(
                AnswerDraft(
                    text=fact,
                    cited_evidence_ids=(item.support_id,),
                    claims=(claim,),
                    generation_mode="extractive",
                ),
                (item,),
                analysis=_natural_atom_analysis(atom, analysis),
                complete=False,
            )
        except ValidationFailed:
            continue
        return claim, item
    return None


def _fallback_table_row(
    plan: QueryPlan,
    grouped: dict[str, list[tuple[EvidenceItem, str]]],
) -> list[tuple[EvidenceItem, str]]:
    """问题明确点名某个表格行时，优先展示该行的完整单元格。"""
    query = _STOP.sub("", plan.resolved_root_query.casefold())
    matches: list[tuple[int, list[tuple[EvidenceItem, str]]]] = []
    for items in grouped.values():
        if not any(
            dict(item.metadata).get("evidence_group_type") == "TABLE_ROW_GROUP"
            or item.table_context
            for item, _ in items
        ):
            continue
        labels = (
            _STOP.sub("", sentence.casefold())
            for _, sentence in items
        )
        label_length = max(
            (
                len(label)
                for label in labels
                if (
                    _FALLBACK_TABLE_LABEL_MIN_CHARS
                    <= len(label)
                    <= _FALLBACK_TABLE_LABEL_MAX_CHARS
                    and label in query
                )
            ),
            default=0,
        )
        if label_length:
            matches.append((label_length, items))
    return max(matches, key=lambda pair: pair[0])[1] if matches else []


def _fallback_source_node(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    related: set[str],
) -> list[tuple[EvidenceItem, str]]:
    """把同一原文段落分块后的步骤重新按来源位置展示。"""
    query_terms = _terms(plan.resolved_root_query)
    nodes: dict[
        tuple[str | None, str], list[tuple[EvidenceItem, str]]
    ] = {}
    for item in evidence:
        if (
            item.support_id not in related
            or not item.publishable
            or item.table_context
            or not item.source_spans
            or any(not span.is_citable for span in item.source_spans)
        ):
            continue
        node_ids = {span.node_id for span in item.source_spans}
        sentence = item.citation_text.strip()
        if (
            len(node_ids) != 1
            or None in node_ids
            or _FALLBACK_LIST_MARKER.fullmatch(sentence)
        ):
            continue
        node_id = next(iter(node_ids))
        nodes.setdefault((item.document_version_id, node_id), []).append(
            (item, sentence)
        )
    candidates = [
        (len(_terms(" ".join(text for _, text in items)) & query_terms), items)
        for items in nodes.values()
        if len({text for _, text in items}) >= _FALLBACK_NODE_MIN_EXCERPTS
    ]
    if not candidates:
        return []
    overlap, best = max(candidates, key=lambda pair: pair[0])
    if overlap < _FALLBACK_MIN_BIGRAM_OVERLAP:
        return []
    seen: set[str] = set()
    selected: list[tuple[EvidenceItem, str]] = []
    for item, sentence in best:
        if sentence in seen:
            continue
        seen.add(sentence)
        selected.append((item, sentence))
    return selected[:8]


def _safe_extractive_fallback(  # noqa: PLR0912, PLR0915
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    linked_ids: dict[str, tuple[str, ...]],
    complete_group_ids: tuple[str, ...] = (),
) -> tuple[str, tuple[str, ...], frozenset[str]] | None:
    """模型未形成可发布事实时，仅展示相关且可引用的来源原句。"""
    if not linked_ids:
        return None
    related = {
        support_id
        for support_ids in linked_ids.values()
        for support_id in support_ids
    }
    question_terms = _terms(
        " ".join(
            (
                plan.original_query,
                plan.resolved_root_query,
                *(atom.search_text for atom in plan.atoms),
            )
        )
    )
    original_text = _STOP.sub("", plan.resolved_root_query.casefold())
    original_trigrams = {
        original_text[index : index + 3]
        for index in range(len(original_text) - 2)
    }
    complete_ids = frozenset(complete_group_ids)
    direct_duration = len(plan.atoms) == 1 and (
        plan.atoms[0].answer_shape is AtomAnswerShape.DURATION
    )
    grouped: dict[str, list[tuple[EvidenceItem, str]]] = {}
    ordinary: list[tuple[int, int, EvidenceItem, str]] = []
    for index, item in enumerate(evidence):
        metadata = dict(item.metadata)
        group_id = metadata.get("evidence_group_id")
        complete_table_row = (
            (
                metadata.get("evidence_group_type") == "TABLE_ROW_GROUP"
                or item.table_context
            )
            and isinstance(group_id, str)
            and group_id in complete_ids
        )
        if (
            (item.support_id not in related and not complete_table_row)
            or not item.publishable
            or not item.source_spans
            or any(not span.is_citable for span in item.source_spans)
        ):
            continue
        certified = (
            isinstance(support := metadata.get("answer_support"), dict)
            and support.get("status") == "SUPPORTED"
        )
        structured = metadata.get("group_complete") is True or (
            isinstance(group_id, str) and group_id in complete_ids
        )
        sentences = tuple(
            sentence.strip()
            for sentence in re.findall(
                r"[^。！？.!?\n]+[。！？.!?]", item.citation_text
            )
            if sentence.strip()
        )
        if not sentences and structured:
            sentences = (item.citation_text.strip(),)
        if not sentences:
            continue
        matched = max(
            sentences,
            key=lambda sentence: len(_terms(sentence) & question_terms),
        )
        overlap = len(_terms(matched) & question_terms)
        normalized_sentence = _STOP.sub("", matched.casefold())
        sentence_trigrams = {
            normalized_sentence[position : position + 3]
            for position in range(len(normalized_sentence) - 2)
        }
        if (
            structured
            and isinstance(group_id, str)
            and group_id in complete_ids
        ):
            grouped.setdefault(group_id, []).append((item, matched))
        elif (
            (certified or overlap >= _FALLBACK_MIN_BIGRAM_OVERLAP)
            and original_trigrams & sentence_trigrams
            and (not direct_duration or _FALLBACK_DURATION.search(matched))
        ):
            ordinary.append((overlap, index, item, matched))
    selected: list[tuple[EvidenceItem, str]] = []
    if direct_duration and ordinary:
        _, _, item, sentence = max(
            ordinary, key=lambda row: (row[0], -row[1])
        )
        selected = [(item, sentence)]
    multi_part = len(plan.atoms) > 1 or any(
        atom.answer_shape in {
            AtomAnswerShape.ENUMERATION,
            AtomAnswerShape.PROCEDURE,
            AtomAnswerShape.DUTIES,
        }
        for atom in plan.atoms
    )
    if not selected and multi_part:
        selected = _fallback_table_row(plan, grouped)
    if not selected and multi_part:
        selected = _fallback_source_node(plan, evidence, related)
        # 段落片段若属于已闭合的列表或流程，展示同组后续步骤。
        # 只沿当前选中片段的组扩展，避免借用别的文档的相似流程。
        source_groups = {
            group_id
            for item, _ in selected
            if (group_id := dict(item.metadata).get("evidence_group_id"))
            and dict(item.metadata).get("evidence_group_type")
            in {"LIST_GROUP", "PROCEDURE_GROUP"}
        }
        for group_id in source_groups:
            members = grouped.get(group_id, ())
            if len({item.support_id for item, _ in members}) > len(
                {item.support_id for item, _ in selected}
            ):
                selected = list(members)
                break
    if not selected and grouped:
        ranked_groups = sorted(
            grouped.items(),
            key=lambda pair: (
                -len(
                    question_terms
                    & _terms(
                        " ".join(item.citation_text for item, _ in pair[1])
                    )
                ),
                next(
                    index
                    for index, item in enumerate(evidence)
                    if item.support_id == pair[1][0][0].support_id
                ),
            ),
        )
        best_group = ranked_groups[0][1]
        if len(
            question_terms
            & _terms(" ".join(item.citation_text for item, _ in best_group))
        ) >= _FALLBACK_MIN_BIGRAM_OVERLAP:
            selected = best_group
    if not selected:
        selected = [
            (item, sentence)
            for _, _, item, sentence in sorted(
                ordinary, key=lambda row: (-row[0], row[1])
            )[:_FALLBACK_MAX_ORDINARY_EXCERPTS]
        ]
    if not selected:
        return None
    selected.sort(
        key=lambda pair: min(
            (
                (
                    span.source_anchor.ordinal,
                    span.source_start_char,
                )
                for span in pair[0].source_spans
                if span.source_anchor is not None
            ),
            default=(2**31 - 1, 2**31 - 1),
        ),
    )
    lines = ["资料中与该问题直接相关的规定如下："]
    ids: list[str] = []
    for item, sentence in selected:
        lines.append(f"- {sentence} [{item.support_id}]")
        ids.append(item.support_id)
    covered_atoms = frozenset(
        atom_id
        for atom_id, support_ids in linked_ids.items()
        if any(support_id in ids for support_id in support_ids)
    )
    return "\n".join(lines), tuple(ids), covered_atoms


def _validate_natural_support_structure(
    units: tuple[EvidenceItem, ...],
) -> None:
    """单条事实的多个引用必须属于同一结构组或认证表格交点。"""
    if len(units) <= 1:
        return
    group_ids = {dict(item.metadata).get("evidence_group_id") for item in units}
    certificates = tuple(
        dict(item.metadata).get("answer_support") for item in units
    )
    same_group = len(group_ids) == 1 and None not in group_ids
    same_table_fact = (
        all(isinstance(item, dict) for item in certificates)
        and all(item == certificates[0] for item in certificates)
        and certificates[0].get("support_reason") == "TABLE_INTERSECTION"
    )
    if not same_group and not same_table_fact:
        raise ValidationFailed(
            "单条事实不能拼接互不相属的证据。",
            stage="answer.validate",
            code="CLAIM_SOURCE_MISMATCH",
        )


def _validated_natural_claim(
    natural: NaturalClaim,
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
) -> AnswerClaim:
    """核对逐原子来源后复用既有事实与引用安全门。"""
    atoms = {atom.atom_id: atom for atom in plan.atoms}
    by_id = {item.support_id: item for item in evidence}
    if natural.atom_id not in atoms:
        raise ValidationFailed(
            "自然事实引用未知 Atom。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_ATOM",
        )
    support_ids = tuple(item.support_id for item in natural.supports)
    if len(set(support_ids)) != len(support_ids):
        raise ValidationFailed(
            "自然事实重复引用同一 Support ID。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_SUPPORT",
        )
    if not set(support_ids) <= by_id.keys():
        raise ValidationFailed(
            "自然事实引用未知 Support ID。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_SUPPORT",
        )
    support = matrix.for_atom(natural.atom_id)
    units = tuple(by_id[support_id] for support_id in support_ids)
    _validate_natural_support_structure(units)
    claim = AnswerClaim(
        text=natural.text,
        supports=natural.supports,
    )
    atom = atoms[natural.atom_id]
    atom_units = units
    if not all(
        item.publishable
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
        for item in atom_units
    ):
        raise ValidationFailed(
            "自然事实的来源不可发布。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_SUPPORT",
        )
    source_text = "\n".join(item.quote for item in claim.supports)
    claim_subject = _leading_explicit_subject(natural.text)
    if (
        claim_subject is not None
        and _STANDALONE_SUBJECT.fullmatch(atom.target.strip()) is not None
        and _STANDALONE_SUBJECT.fullmatch(claim_subject) is not None
        and not _same_subject(claim_subject, atom.target)
    ):
        raise ValidationFailed(
            "事实明确断言的职责主体与本次提问对象不同。",
            stage="answer.validate",
            code="CLAIM_TARGET_UNSUPPORTED",
        )
    _validate_table_claim_certificate(atom_units)
    direct_relation = any(
        isinstance(
            certificate := dict(item.metadata).get("answer_support"), dict
        )
        and certificate.get("status") == "SUPPORTED"
        for item in atom_units
    )
    group_ids = {
        dict(item.metadata).get("evidence_group_id") for item in atom_units
    }
    group_certified = bool(
        not direct_relation
        and len(group_ids) == 1
        and next(iter(group_ids)) in support.relation_certified_group_ids
        and all(
            dict(item.metadata).get("group_complete") is True
            for item in atom_units
        )
    )
    if group_certified and (
        not any(
            dict(item.metadata).get("group_member_index") == 1
            for item in atom_units
        )
        or not any(
            dict(item.metadata).get("group_member_index", 0) > 1
            for item in atom_units
        )
    ):
        raise ValidationFailed(
            "结构事实缺少同组关系导语的引用。",
            stage="answer.validate",
            code="CLAIM_RELATION_UNSUPPORTED",
        )
    _validate_natural_entailment(claim)
    source_labels = "\n".join(
        " ".join(
            (
                item.source_label,
                item.display_name or "",
                *item.heading_path,
            )
        )
        for item in atom_units
    )
    if any(
        title not in source_text and title not in source_labels
        for title in _QUOTED_DOCUMENT_TITLE.findall(natural.text)
    ):
        raise ValidationFailed(
            "自然事实引入来源没有的文档名称。",
            stage="answer.validate",
            code="CLAIM_ENTITY_DRIFT",
        )
    for constraint in atom.constraints:
        value = unicodedata.normalize("NFKC", constraint.value).casefold()
        source = unicodedata.normalize("NFKC", source_text).casefold()
        identity_text = unicodedata.normalize("NFKC", source_labels).casefold()
        if (
            constraint.kind.value
            in {"NUMBER", "DURATION", "DATE_TIME", "VERSION"}
            and value not in source
            and not (
                _number_tokens(value)
                and _number_tokens(value) <= _number_tokens(source)
            )
        ):
            code = (
                "CLAIM_DATE_VERSION_DRIFT"
                if constraint.kind.value in {"DATE_TIME", "VERSION"}
                else "CLAIM_NUMBER_DRIFT"
            )
            raise ValidationFailed(
                "Atom 的数字、时限或版本限制缺少来源。",
                stage="answer.validate",
                code=code,
            )
        if constraint.kind.value == "NEGATION" and not _NEGATION.search(
            source_text
        ):
            raise ValidationFailed(
                "Atom 的否定限制缺少来源。",
                stage="answer.validate",
                code="CLAIM_NEGATION_MISMATCH",
            )
        if constraint.kind.value == "SOURCE" and value not in identity_text:
            raise ValidationFailed(
                "Atom 的来源范围与引用不一致。",
                stage="answer.validate",
                code="CLAIM_SOURCE_SCOPE_MISMATCH",
            )
        if constraint.kind.value == "ROLE" and value not in source:
            raise ValidationFailed(
                "Atom 的角色限制缺少来源。",
                stage="answer.validate",
                code="CLAIM_CONDITION_UNSUPPORTED",
            )
    atom_analysis = _natural_atom_analysis(atom, analysis)
    validate_grounded_draft(
        AnswerDraft(
            text=claim.text,
            cited_evidence_ids=support_ids,
            claims=(claim,),
            generation_mode="llm",
        ),
        evidence,
        analysis=atom_analysis,
        complete=False,
    )
    return claim


def _validate_table_claim_certificate(units: tuple[EvidenceItem, ...]) -> None:
    """表格 Claim 必须同时引用同表行名、列头和交点值。"""
    for item in units:
        certificate = dict(item.metadata).get("answer_support")
        if (
            not isinstance(certificate, dict)
            or certificate.get("support_reason") != "TABLE_INTERSECTION"
        ):
            continue
        required = certificate.get("supporting_span_ids")
        if (
            not isinstance(required, list)
            or len(required) != _TABLE_INTERSECTION_SPAN_COUNT
        ):
            raise ValidationFailed(
                "表格事实缺少完整交点来源。",
                stage="answer.validate",
                code="CLAIM_RELATION_UNSUPPORTED",
            )
        cited = {
            span.node_id
            for unit in units
            if unit.document_version_id == item.document_version_id
            and dict(unit.metadata).get("answer_support") == certificate
            for span in unit.source_spans
        }
        if not set(required) <= cited:
            raise ValidationFailed(
                "表格事实缺少同一行的对象、列头或交点来源。",
                stage="answer.validate",
                code="CLAIM_RELATION_UNSUPPORTED",
            )


def _modality_class(text: str) -> str | None:
    """保留强制、应当、需要和许可四种不同义务强度。"""
    return next(
        (name for name, pattern in _MODALITY_CLASSES if pattern.search(text)),
        None,
    )


def _validate_natural_entailment(
    claim: AnswerClaim,
) -> None:
    """核对 Claim 的动作、条件与逻辑算子是否由所选引文支持。"""
    text = claim.text
    source = "\n".join(item.quote for item in claim.supports)
    claim_subject = _leading_explicit_subject(text)
    source_with_subjects = _clauses_with_subject(source)
    source_clauses = [clause for clause, _subject in source_with_subjects]
    for operator in _INFERENCE_OPERATOR.findall(text):
        if not any(operator in clause for clause in source_clauses):
            raise ValidationFailed(
                "事实推导了原文没有直接表达的范围或结论。",
                stage="answer.validate",
                code="CLAIM_INFERENTIAL_LEAP",
            )
    normalized_source = "".join(
        unicodedata.normalize("NFKC", source).casefold().split()
    )
    for condition in _CONDITION_SCOPE.finditer(text):
        scope = condition["scope"] or condition["temporal"]
        normalized_scope = "".join(
            unicodedata.normalize("NFKC", scope).casefold().split()
        )
        if normalized_scope not in normalized_source:
            raise ValidationFailed(
                "事实新增或改变了来源中的适用条件。",
                stage="answer.validate",
                code="CLAIM_CONDITION_UNSUPPORTED",
            )
    claim_actions = set(re.findall(_ACTION_VERB, _predicate(text)))
    supported_actions = set(re.findall(_ACTION_VERB, source))
    if claim_actions - supported_actions:
        raise ValidationFailed(
            "事实中的动作关系没有被引用直接表达。",
            stage="answer.validate",
            code="CLAIM_RELATION_UNSUPPORTED",
        )
    source_subjects = {
        subject for _clause, subject in source_with_subjects if subject
    }
    if claim_subject and len(source_subjects) > 1:
        owned_actions = {
            action
            for clause, subject in source_with_subjects
            if subject and _same_subject(subject, claim_subject)
            for action in re.findall(_ACTION_VERB, _predicate(clause))
        }
        if claim_actions - owned_actions:
            raise ValidationFailed(
                "事实借用了另一个主体的动作关系。",
                stage="answer.validate",
                code="CLAIM_RELATION_UNSUPPORTED",
            )
    matched = _best_negation_sources(text, source_clauses) or source_clauses
    claim_modality = _modality_class(text)
    if claim_modality is not None and not any(
        _modality_class(clause) == claim_modality for clause in matched
    ):
        raise ValidationFailed(
            "事实改变了来源中的义务强度。",
            stage="answer.validate",
            code="CLAIM_MODALITY_MISMATCH",
        )
    if (
        claim_modality is None
        and matched
        and all(_modality_class(clause) is not None for clause in matched)
    ):
        raise ValidationFailed(
            "事实遗漏了来源中的义务强度。",
            stage="answer.validate",
            code="CLAIM_MODALITY_MISMATCH",
        )


def _natural_rejection_code(error: ValidationFailed | ValueError) -> str:
    """把既有验证器的细分失败映射到逐 Claim 诊断合同。"""
    if not isinstance(error, ValidationFailed):
        return "CLAIM_SEMANTIC_SUPPORT_FAILED"
    mapping = {
        "CLAIM_QUERY_RELATION_UNSUPPORTED": "CLAIM_RELATION_UNSUPPORTED",
        "CLAIM_QUERY_TARGET_MISMATCH": "CLAIM_TARGET_UNSUPPORTED",
        "CLAIM_NEGATION_CHANGED": "CLAIM_NEGATION_MISMATCH",
        "CLAIM_OBJECT_CHANGED": "CLAIM_ENTITY_DRIFT",
        "CLAIM_NUMBER_UNSUPPORTED": "CLAIM_NUMBER_MISMATCH",
        "CLAIM_FREQUENCY_UNSUPPORTED": "CLAIM_UNIT_MISMATCH",
        "CLAIM_TEXT_UNSUPPORTED": "CLAIM_SEMANTIC_SUPPORT_FAILED",
        "CLAIM_SOURCE_MISMATCH": "CLAIM_SUPPORT_NOT_OWNED",
        "ANSWER_LIST_INCOMPLETE": "CLAIM_STRUCTURE_INCOMPLETE",
        "CATALOG_CLAIM_UNSUPPORTED": "CLAIM_SEMANTIC_SUPPORT_FAILED",
        "CLAIM_NUMBER_DRIFT": "CLAIM_NUMBER_MISMATCH",
        "CLAIM_UNIT_DRIFT": "CLAIM_UNIT_MISMATCH",
        "CLAIM_DATE_VERSION_DRIFT": "CLAIM_CONDITION_UNSUPPORTED",
        "CLAIM_SUPPORT_OUTSIDE_ATOM": "CLAIM_SUPPORT_NOT_OWNED",
    }
    return mapping.get(error.code, error.code)


def _natural_atom_analysis(
    atom: QueryAtom,
    analysis: QueryAnalysis | None,
) -> QueryAnalysis | None:
    """复用本次规则分析，并把对象和回答形状收窄到当前 Atom。"""
    if analysis is None:
        return None
    answer_type = RequestedAnswerType.__members__.get(
        atom.answer_shape.value, RequestedAnswerType.UNKNOWN
    )
    return analysis.model_copy(
        update={
            "semantics": analysis.semantics.model_copy(
                update={
                    "target": atom.target,
                    "relation": atom.relation,
                    "source_qualifier": atom.source_qualifier,
                    "answer_type": answer_type,
                }
            )
        }
    )


def _natural_atom_complete(  # noqa: PLR0911, PLR0913
    atom: QueryAtom,
    matrix: AtomSupportMatrix,
    claims: tuple[ValidatedNaturalClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    generation_evidence_pack: GenerationEvidencePack | None = None,
) -> bool:
    """列表和流程还须通过来源集合完整性门，不能只看相关 Claim。"""
    atom_claims = tuple(
        item.claim for item in claims if atom.atom_id in item.atom_ids
    )
    if not atom_claims:
        return False
    if atom.answer_shape not in {
        AtomAnswerShape.ENUMERATION,
        AtomAnswerShape.PROCEDURE,
        AtomAnswerShape.DUTIES,
    }:
        return True
    support = matrix.for_atom(atom.atom_id)
    if (
        generation_evidence_pack is None
        and support.status is not AtomStatus.SUPPORTED
    ):
        return False
    linked = (
        dict(generation_evidence_pack.per_atom_candidate_support_ids)
        if (generation_evidence_pack is not None)
        else {}
    )
    atom_evidence = tuple(
        item
        for item in evidence
        if item.support_id
        in linked.get(
            atom.atom_id,
            support.supporting_support_ids,
        )
    )
    group_complete = _structural_member_coverage(atom_evidence, atom_claims)
    if generation_evidence_pack is not None and group_complete is not True:
        return False
    if group_complete is False:
        return False
    atom_analysis = _natural_atom_analysis(atom, analysis)
    if atom_analysis is None:
        return True
    try:
        _validate_structured_list_coverage(
            AnswerDraft(
                text="\n".join(item.text for item in atom_claims),
                cited_evidence_ids=tuple(
                    dict.fromkeys(
                        cited.support_id
                        for item in atom_claims
                        for cited in item.supports
                    )
                ),
                claims=atom_claims,
                generation_mode="llm",
            ),
            atom_evidence,
            atom_analysis,
        )
    except ValidationFailed:
        return False
    return True


def _structural_member_coverage(
    evidence: tuple[EvidenceItem, ...],
    claims: tuple[AnswerClaim, ...],
) -> bool | None:
    """完整组按来源成员核对，不把导语当作列表事实。"""
    by_id = {item.support_id: item for item in evidence}
    cited_support_ids = {
        item.support_id
        for claim in claims
        for support in claim.supports
        if (item := by_id.get(support.support_id)) is not None
    }
    groups: dict[str, dict[str, EvidenceItem]] = {}
    expected_counts: dict[str, int] = {}
    saw_group = False
    for item in evidence:
        metadata = dict(item.metadata)
        group_id = metadata.get("evidence_group_id")
        member_count = metadata.get("group_member_count")
        if not isinstance(group_id, str) or not isinstance(member_count, int):
            continue
        saw_group = True
        if metadata.get("group_complete") is not True:
            continue
        groups.setdefault(group_id, {})[item.support_id] = item
        expected_counts[group_id] = member_count
    if not groups:
        return False if saw_group else None
    for group_id, members in groups.items():
        if (
            len({item.chunk_id for item in members.values()})
            != (expected_counts[group_id])
        ):
            continue
        required = {
            support_id
            for support_id, item in members.items()
            if not _structural_lead_in(item)
        }
        if required and required <= cited_support_ids:
            return True
    return False


def _structural_lead_in(item: EvidenceItem) -> bool:
    """只豁免完整组的引导句，不豁免编号事实成员。"""
    metadata = dict(item.metadata)
    if metadata.get("group_member_index") != 1:
        return False
    text = item.citation_text.strip()
    if _LEADING_LIST_MARKER.match(text):
        return False
    return text.endswith(("：", ":")) or bool(re.search(r"以下|如下", text))


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
