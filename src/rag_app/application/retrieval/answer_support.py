"""有界、无模型的事实支持判定；相关性和可引用性均不能替代属性证据。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from rag_app.application.retrieval.semantics import parse_query_semantics
from rag_app.core.models import QueryAnalysis, RequestedAnswerType


class SupportStatus(StrEnum):
    """所问关系在来源中的支持状态。"""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class AnswerSupport:
    """保留对象、关系、值类型和真实片段标识的最小判定。"""

    status: SupportStatus
    query_target: str
    requested_relation_or_attribute: str
    answer_type: str
    support_reason: str
    supporting_span_ids: tuple[str, ...] = ()


# 仅为语言和量纲类别，不含语料实体、文档身份或题目映射。
_ATTRIBUTES = (
    (
        "CONTACT",
        r"联系电话|联系方式|电话号码|手机号码|手机号|电话|分机号|分机|邮箱|电子邮件",
    ),
    (
        "MONEY",
        r"采购价格|采购价|采购成本|购买价格|售价|销售价格|价格|价钱|费用|工资|薪资|保证金|补助|预付款",
    ),
    ("AREA", r"建筑面积|占地面积|面积"),
    ("TEMPERATURE", r"储存温度|温度|(?<![a-z])temperature(?![a-z])"),
    ("PRESSURE", r"额定压力|压力"),
    ("MASS", r"载荷上限|载荷|重量|质量"),
    ("RATIO", r"允许偏差|偏差|比例|百分比"),
    (
        "DURATION",
        r"维护周期|校准周期|检验周期|检查周期|保养周期|复核周期|"
        r"更新周期|轮换周期|保管期限|保存期限|借阅期限|期限|周期|"
        r"多久|多少天|几天|多少小时",
    ),
    ("TIME", r"日期|何时|什么时候|什么时间"),
    ("VERSION", r"当前有效版本|现行版本|当前版本|有效版本|版本"),
    ("BRAND", r"数据库品牌|品牌|型号"),
)
_NUMBER = r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十百千万两]+)"
_VALUES = {
    "MONEY": (
        rf"{_NUMBER}\s*(?:万?元|美元|欧元|英镑|人民币|USD|CNY|EUR)"
        rf"|[¥￥$€]\s*{_NUMBER}"
    ),
    "AREA": rf"{_NUMBER}\s*(?:平方米|平方公里|公顷|亩|m2|km2)",
    "CONTACT": (
        r"(?:\+?\d[\d ()-]{5,}\d)"
        r"|(?:分机(?:号)?\s*(?:为|是|[:：])?\s*\d{2,6})"
        r"|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})"
    ),
    "TEMPERATURE": (
        rf"{_NUMBER}(?:至{_NUMBER})?\s*"
        r"(?:摄氏度|℃|°\s*c|度|(?:degrees?\s+)?celsius(?![a-z]))"
    ),
    "PRESSURE": rf"{_NUMBER}\s*(?:mpa|kpa|pa|bar)",
    "MASS": rf"{_NUMBER}\s*(?:kg|千克|公斤|吨|g|克)",
    "RATIO": rf"{_NUMBER}\s*[%％]|百分之{_NUMBER}",
    "DURATION": rf"{_NUMBER}\s*(?:个)?(?:工作日|天|小时|分钟|周|月|年)",
    "TIME": r"(?:每日|每周|每月|凌晨|上午|下午|晚上|\d{4}年|\d{1,2}[:：]\d{2})",
    "VERSION": r"(?<![a-z0-9])(?:v|r)?\d+(?:[._-]\d+)*(?![a-z0-9])",
    "BRAND": r"(?:品牌|型号|数据库)(?:为|是|采用|使用|[:：])\s*\S+",
    "MEASUREMENT": (
        rf"{_NUMBER}\s*(?:kg|g|mg|mm|cm|km|m|mpa|kpa|pa|℃|°c|千克|米)"
    ),
}
_UNKNOWN = re.compile(
    r"未提供|未公布|未确定|尚未|未知|不详|暂无|没有提供|不提供|未记录|待定|未给出|无记录"
)
_ROLE = re.compile(
    r"(?:由[^，。；;]{1,30}(?:受理|负责|审核|复核|登记|签字|提出)|需要[^，。；;]{1,20}签字|[^，。；;]{1,20}负责)"
)
_RELATION = re.compile(
    r"为|是|由|需|应|先|后|存放|位于|保持|允许|禁止|不得|备份|预热|采用|使用|负责|进行|核对|不直接"
)
_LEADING_REQUEST_WORDS = re.compile(
    r"^(?:请查(?:一下)?|帮我查(?:一下)?|查一下|请问|请|我想知道|"
    r"我想了解|想知道|想了解|告诉我)"
)
_TRAILING_REQUEST_WORDS = re.compile(
    r"(?:(?:的)?(?:是多少|是什么|是啥|有多少|多大|是否|怎样|如何|怎么|"
    r"哪里|哪位|何时)|[？?，,。\s])+$"
)
_QUESTION = re.compile(
    r"谁|什么|啥|多少|多大|如何|怎么|怎样|哪|何时|是否|能否|几|[?？]"
)
_LOOKUP_SCAFFOLDING = re.compile(
    r"查找|搜索|检索|表格|记录|文档|内容|信息|条目"
)
_MIN_ENUMERATION_ITEMS = 2
_MIN_DOMINANT_FRAGMENT_SHARE = 0.45
_MIN_IDENTIFIER_CONTEXT_OVERLAP = 0.25
_TYPED_ANSWER_TYPES = frozenset(
    {
        RequestedAnswerType.DEFINITION,
        RequestedAnswerType.PURPOSE,
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.DUTIES,
        RequestedAnswerType.RESPONSIBLE_PARTY,
        RequestedAnswerType.PROCEDURE,
        RequestedAnswerType.SECTION_SUMMARY,
    }
)
_TYPED_ANSWER_VALUES = frozenset(item.value for item in _TYPED_ANSWER_TYPES)
_TABLE_HEADER_TYPES = {
    "DEFINITION": re.compile(r"定义|释义|说明|含义|描述|交付件说明|内容说明"),
    "PURPOSE": re.compile(r"目的|目标|作用|用途|宗旨"),
    "DUTIES": re.compile(r"职责|工作内容|岗位任务|负责事项|主要工作|任务说明"),
    "RESPONSIBLE_PARTY": re.compile(
        r"责任角色|责任人|负责人|主责|牵头|经办|承办|受理角色|受理人|"
        r"负责部门|责任部门|责任单位"
    ),
}
_NAMED_ARTIFACT_TARGET = re.compile(
    r"(?:登记表|申请表|清单|台账|文档|报告|记录|方案|计划|表|单)$"
)
_DUTY_SUBJECT_ACTION = (
    r"(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|"
    r"尚未|没有|未|无|不)?"
    r"(?:牵头|主要|直接|统一|共同|定期)?"
    r"(?:负责(?!人)|承担|组织|协调|审批|批准|维护|检修|检查|核对|"
    r"保存|归档|销毁|执行|提供|记录|参与|完成|协助|贯彻|制定|"
    r"落实|指导|监督|主持|督促|抓好|策划|确保|营造|任命|明确|"
    r"确定|推行|报告|履行|配备))"
)


def _normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    # 词形统一仅用于关系动词，不给产品实体添加演示别名。
    for source, target in (
        ("纠正", "更正"),
        ("复查", "复核"),
        ("保留", "保存"),
    ):
        value = value.replace(source, target)
    return value


def _analysis_query(analysis: QueryAnalysis) -> str:
    """返回证据门应共同消费的已解析查询。"""
    return analysis.resolved_query or analysis.normalized_query


def _request(analysis: QueryAnalysis) -> tuple[str, str, str]:
    semantics = analysis.semantics
    if (
        semantics.target
        and semantics.relation
        and semantics.answer_type in _TYPED_ANSWER_TYPES
    ):
        return (
            _normalized(semantics.target),
            _normalized(semantics.relation),
            semantics.answer_type.value,
        )
    query = _normalized(analysis.resolved_query or analysis.normalized_query)
    requested: tuple[str, str, str] | None
    identifier_only = (
        analysis.identifiers
        and not _QUESTION.search(query)
        and not _literal_terms(
            _identifier_residual(query, analysis.identifiers)
        )
    )
    if identifier_only:
        requested = (query, "字面查找", "FACT")
    elif re.search(r"谁(?!的)|哪位|找.{0,4}人员", query):
        return _responsible_target(query), "责任角色", "PERSON_OR_ROLE"
    elif re.search(r"数值.*单位|单位.*数值", query):
        return _strip_request_boundaries(query), "数值与单位", "MEASUREMENT"
    else:
        requested = _attribute_or_descriptive_request(query)
    if requested is not None:
        return requested
    relation = "事实关系" if _QUESTION.search(query) else "字面查找"
    return _strip_request_boundaries(query), relation, "FACT"


def _attribute_or_descriptive_request(
    query: str,
) -> tuple[str, str, str] | None:
    """识别属性或描述类请求，供通用回退路径复用。"""
    for answer_type, pattern in _ATTRIBUTES:
        match = re.search(pattern, query)
        if match:
            target = _strip_request_boundaries(query[: match.start()])
            target = re.sub(
                r"(?:应该|应当|需要|需|要)?"
                r"(?:保持|允许|储存)?(?:什么|多少|多大)$",
                "",
                target,
            ).strip()
            target = re.sub(r"(?:保持|允许|储存)$", "", target)
            return target, match[0], answer_type
    return descriptive_request(query)


def descriptive_request(query: str) -> tuple[str, str, str] | None:
    """识别通用列举与职责问法，不包含语料实体或预写答案。

    Args:
        query: 用户原始问句或确定性规范化后的问句。

    Returns:
        对象、关系和描述类型三元组；不属于描述类问法时返回 None。

    """
    semantics = parse_query_semantics(query)
    if (
        not semantics.target
        or not semantics.relation
        or semantics.answer_type not in _TYPED_ANSWER_TYPES
    ):
        return None
    return (
        _normalized(semantics.target),
        _normalized(semantics.relation),
        semantics.answer_type.value,
    )


def _strip_request_boundaries(value: str) -> str:
    """仅裁剪问句首尾语法，绝不删除实体内部字符。"""
    target = value.strip()
    previous = None
    while target and target != previous:
        previous = target
        target = _LEADING_REQUEST_WORDS.sub("", target).strip()
        target = _TRAILING_REQUEST_WORDS.sub("", target).strip()
        target = re.sub(r"(?:的|由|归)$", "", target).strip()
    return target


def _responsible_target(query: str) -> str:
    """从责任人问法的边界提取对象，保留对象内部的“谁/的”。"""
    suffix = re.fullmatch(
        r"(?P<target>.+?)(?:(?:由|归)?谁(?:来)?(?:负责|牵头|管理|受理)|"
        r"(?:应|要|该|需要)?找谁|"
        r"(?:的)?(?:责任角色|责任人|负责人|牵头人)(?:是|为)?(?:谁|哪位))"
        r"[？?，,。\s]*",
        query,
    )
    if suffix is not None:
        return _strip_request_boundaries(suffix["target"])
    prefix = re.fullmatch(
        r"(?:由)?谁(?:来)?(?:负责|牵头|管理|受理)(?P<target>.+?)"
        r"[？?，,。\s]*",
        query,
    )
    if prefix is not None:
        return _strip_request_boundaries(prefix["target"])
    return _strip_request_boundaries(query)


def _literal_lookup_supports(
    query: str,
    text: str,
    *,
    allow_partial: bool,
) -> bool:
    """关键词查找只返回实际出现的词，不推导未提出的属性值。"""
    query = _normalized(query)
    text = _normalized(text)
    if _QUESTION.search(query):
        return False
    terms = _literal_terms(query)
    actual = _literal_terms(text)
    if terms and Counter(terms) <= Counter(actual):
        return True
    if not allow_partial:
        return False
    total = len(terms)
    if total == 0:
        return False
    fragments = re.findall(
        r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+", query
    )
    return any(
        fragment in text
        and len(_literal_terms(fragment)) / total
        >= _MIN_DOMINANT_FRAGMENT_SHARE
        for fragment in fragments
    )


def _literal_terms(text: str) -> list[str]:
    """返回用于有界字面覆盖判断的英文词、标识符和单个汉字。"""
    return re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]", text)


def _identifier_literal_supports(
    analysis: QueryAnalysis,
    clause: str,
) -> bool:
    """要求标识符完整命中，并用剩余词面排除同 ID 的无关来源。"""
    query = _normalized(_analysis_query(analysis))
    text = _normalized(clause)
    identifiers = tuple(_normalized(item) for item in analysis.identifiers)
    if not identifiers or any(item not in text for item in identifiers):
        return False
    if analysis.negation_signals and any(
        _normalized(signal) not in text for signal in analysis.negation_signals
    ):
        return False
    residual = _identifier_residual(query, identifiers)
    residual_terms = _literal_terms(residual)
    if not residual_terms:
        return True
    actual = Counter(_literal_terms(text))
    required = Counter(residual_terms)
    overlap = sum((required & actual).values()) / len(residual_terms)
    return overlap >= _MIN_IDENTIFIER_CONTEXT_OVERLAP


def _identifier_residual(
    query: str,
    identifiers: tuple[str, ...],
) -> str:
    """移除已验证标识符和无语义的查找载体词。"""
    residual = query
    for identifier in identifiers:
        residual = residual.replace(_normalized(identifier), " ")
    return _LOOKUP_SCAFFOLDING.sub(" ", residual)


def _literal_clause_supports(
    analysis: QueryAnalysis,
    clause: str,
) -> bool:
    """在硬约束存在时禁止用局部字面片段补成完整证据。"""
    allow_partial = not (
        analysis.negation_signals
        or analysis.numbers
        or analysis.units
        or analysis.date_version_signals
        or analysis.quoted_phrases
    )
    return _literal_lookup_supports(
        _analysis_query(analysis), clause, allow_partial=allow_partial
    )


def _identifier_relation_supports(
    target: str,
    answer_type: str,
    clause: str,
    analysis: QueryAnalysis,
) -> bool:
    """核验含标识符事实，避免同一标识符的无关关系借值。"""
    if not all(item.casefold() in clause for item in analysis.identifiers):
        return False
    query = _normalized(_analysis_query(analysis))
    if answer_type != "FACT":
        return bool(_RELATION.search(clause)) or query in clause
    if _identifier_literal_supports(analysis, clause):
        return True
    identifier_targets = {_normalized(item) for item in analysis.identifiers}
    if target in identifier_targets and not analysis.negation_signals:
        return True
    return query in clause


def _target_matches(target: str, text: str, *, strict: bool) -> bool:
    if not target:
        return False
    if target in text:
        return True
    if strict:
        return False
    terms = set(re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", target))
    for run in re.findall(r"[\u3400-\u9fff]+", target):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    terms -= {
        "申请",
        "办理",
        "负责",
        "需要",
        "应找",
        "复核",
        "审核",
        "登记",
        "提出",
        "更正",
        "签字",
        "拍照",
    }
    return any(term in text for term in terms)


def evaluate_span_support(
    analysis: QueryAnalysis,
    quote: str,
    *,
    span_id: str = "",
    table_relation: bool = False,
    table_header: str = "",
) -> AnswerSupport:
    """检查对象、关系与值；只在已定位表格交点时允许标签来自表头。

    Args:
        analysis: 当前请求的确定性分析，禁止读取评测题库。
        quote: 一个真实 SourceSpan 的原文。
        span_id: 当前真实来源片段的标识。
        table_relation: 调用方已用同表行列元数据证明唯一交点。
        table_header: 同一逻辑列的真实表头，可提供数值单位和角色关系。

    Returns:
        明确支持、明确缺失或无法确定的关系判定。

    """
    target, relation, answer_type = _request(analysis)
    text = _normalized(quote)
    supported = False
    if table_relation:
        supported = _table_value_supports(
            answer_type, relation, text, table_header
        )
    else:
        supported = any(
            _clause_supports(
                target,
                relation,
                answer_type,
                clause,
                analysis,
            )
            for clause in re.split(r"[。；;\n]", text)
            if clause.strip()
        )
    count_correction = supported and _source_corrects_count_premise(
        analysis, text
    )
    return AnswerSupport(
        status=SupportStatus.SUPPORTED
        if supported
        else (
            SupportStatus.UNCERTAIN
            if answer_type == "FACT"
            else SupportStatus.UNSUPPORTED
        ),
        query_target=target,
        requested_relation_or_attribute=relation,
        answer_type=answer_type,
        support_reason=(
            "SOURCE_CORRECTS_COUNT_PREMISE"
            if count_correction
            else (
                "LITERAL_LOOKUP_SOURCE"
                if relation == "字面查找"
                else "SOURCE_RELATION_AND_VALUE"
            )
        )
        if supported
        else "REQUESTED_RELATION_NOT_SUPPORTED",
        supporting_span_ids=(span_id,) if supported and span_id else (),
    )


def _quoted_restriction_supports(
    relation: str,
    answer_type: str,
    clause: str,
    analysis: QueryAnalysis,
) -> bool:
    """识别带精确引号对象的禁止条款，避免把“无记录”误作缺失值。"""
    return (
        answer_type == RequestedAnswerType.SECTION_SUMMARY.value
        and relation == "限制要求"
        and bool(analysis.quoted_phrases)
        and all(
            _normalized(item) in clause for item in analysis.quoted_phrases
        )
        and bool(
            re.search(
                r"禁止|不得|严禁|不允许|不可|不能|不准|仅限|只允许",
                clause,
            )
        )
    )


def _clause_supports(  # noqa: PLR0911
    target: str,
    relation: str,
    answer_type: str,
    clause: str,
    analysis: QueryAnalysis,
) -> bool:
    unknown = bool(
        _UNKNOWN.search(clause)
    ) and not _quoted_restriction_supports(
        relation,
        answer_type,
        clause,
        analysis,
    )
    if unknown or answer_type in _TYPED_ANSWER_VALUES:
        return not unknown and _descriptive_clause_supports(
            target, relation, answer_type, clause, analysis
        )
    if answer_type in {"MONEY", "AREA", "CONTACT", "TEMPERATURE"}:
        # 不从逗号后另一个对象的金额或号码借值。
        return any(
            _target_matches(target, part, strict=True)
            and (
                answer_type != "TEMPERATURE"
                or not re.fullmatch(r"[a-z][a-z0-9_-]*", target)
                or re.search(
                    r"(?<![a-z0-9_-])" + re.escape(target) + r"(?![a-z0-9_-])",
                    part,
                )
            )
            and _attribute_matches(relation, answer_type, part)
            and not re.search(r"不是|并非|不等于", part)
            and not (
                answer_type == "TEMPERATURE"
                and re.search(r"\b(?:not|no|never|unknown|unavailable)\b", part)
            )
            and _typed_value_matches(answer_type, relation, part)
            for part in re.split(r"[，,]", clause)
        )
    if relation == "字面查找":
        return (
            _identifier_literal_supports(analysis, clause)
            if analysis.identifiers
            else _literal_clause_supports(analysis, clause)
        )
    if not _target_matches(target, clause, strict=False):
        return False
    if answer_type == "PERSON_OR_ROLE":
        actions = tuple(
            word
            for word in (
                "复核",
                "审核",
                "签字",
                "销毁",
                "登记",
                "更正",
                "拍照",
                "提出",
            )
            if word in _normalized(_analysis_query(analysis))
        )
        return bool(_ROLE.search(clause)) and all(
            word in clause for word in actions
        )
    if answer_type in _VALUES:
        return (
            bool(re.search(_VALUES[answer_type], clause, re.IGNORECASE))
            and (
                relation in clause
                or answer_type in {"DURATION", "TIME", "VERSION"}
            )
            and (
                not (answer_type == "DURATION" and "期限" in relation)
                or _target_matches(target, clause, strict=True)
            )
        )
    if analysis.identifiers:
        return _identifier_relation_supports(
            target,
            answer_type,
            clause,
            analysis,
        )
    if analysis.quoted_phrases:
        return all(
            item.casefold() in clause for item in analysis.quoted_phrases
        )
    actions = tuple(
        word
        for word in ("先", "复核", "核对", "签字", "充电", "上架", "登记")
        if word in _normalized(_analysis_query(analysis))
    )
    if actions and not all(word in clause for word in actions):
        return False
    if actions:
        return bool(_RELATION.search(clause))
    query = _normalized(_analysis_query(analysis))
    if re.search(r"哪里|何处|什么区域|在何|在哪", query):
        return bool(re.search(r"存放|位于|在|区域|于", clause))
    method = re.search(r"(?:如何|怎么|怎样)(.+)", query)
    if method is not None:
        action = _strip_request_boundaries(method[1])
        general_goal = re.fullmatch(r"(?:保护|处理|操作)(.*)", action)
        return (
            action in clause
            or (
                general_goal is not None
                and (
                    not general_goal[1]
                    or _target_matches(general_goal[1], clause, strict=True)
                )
            )
        ) and bool(_RELATION.search(clause))
    # 未识别关系只能验证完整字面事实；不能用同主题的另一关系补齐。
    return target in clause


def _descriptive_clause_supports(  # noqa: PLR0911
    target: str,
    relation: str,
    answer_type: str,
    clause: str,
    analysis: QueryAnalysis,
) -> bool:
    if answer_type in {"ENUMERATION", "COUNT", "ORDINAL_ITEM"}:
        supports_collection = (
            _target_matches(target, clause, strict=True)
            and relation in clause
            and bool(re.search(r"分为|分成|包括|包含|分别为|有|采用", clause))
            and bool(re.search(r"、|[“”\"]|(?:一|二|三|四|\d)[)）]", clause))
        )
        if not supports_collection:
            return False
        if answer_type == "ORDINAL_ITEM" and analysis.semantics.ordinal:
            count = _enumeration_item_count(clause)
            return count is None or count >= analysis.semantics.ordinal
        return True
    if answer_type == "DUTIES":
        return _duty_subject_supports(target, clause)
    if answer_type == "RESPONSIBLE_PARTY":
        actions = tuple(
            word
            for word in (
                "复核",
                "审核",
                "签字",
                "销毁",
                "登记",
                "更正",
                "拍照",
                "提出",
                "受理",
                "牵头",
                "管理",
            )
            if word in _normalized(_analysis_query(analysis))
        )
        remainder = clause.replace(target, "", 1)
        return (
            _target_matches(
                target,
                clause,
                strict=_NAMED_ARTIFACT_TARGET.search(target) is not None,
            )
            and all(word in clause for word in actions)
            and bool(
                re.search(
                    r"(?:由|归)[^，。；;]{1,30}"
                    r"(?:负责|牵头|管理|受理|复核|审核|签字|销毁|"
                    r"登记|更正|拍照|提出)"
                    r"|(?:责任(?:角色|人)|负责人|牵头人|"
                    r"主责(?:角色|部门)?)(?:是|为|[:：])"
                    r"[^，。；;]{1,30}",
                    remainder,
                )
            )
        )
    if answer_type == "DEFINITION":
        if analysis.identifiers and all(
            item.casefold() in clause for item in analysis.identifiers
        ):
            return True
        remainder = clause.replace(target, "", 1).strip(" ：:")
        return _target_matches(target, clause, strict=True) and bool(
            re.match(
                r"(?:是指|指的是|定义为|是|指|即|称为|表示)"
                r"(?!什么|啥)[^，。；;]{2,}",
                remainder,
            )
        )
    if answer_type == "PURPOSE":
        remainder = clause.replace(target, "", 1)
        return _target_matches(target, clause, strict=True) and bool(
            re.search(
                r"(?:目的|目标|作用|用途|宗旨)"
                r"(?:是|为|在于|包括|[:：])?[^，。；;]{2,}"
                r"|(?:旨在|用于|用来)[^，。；;]{2,}",
                remainder,
            )
        )
    if answer_type == "SECTION_SUMMARY":
        remainder = clause.replace(target, "", 1).strip(" ：:")
        if relation == "原文内容":
            return _target_matches(target, clause, strict=True)
        return _target_matches(target, clause, strict=True) and bool(
            re.search(
                r"(?:规定|要求|包括|包含|说明|应当|必须|禁止|不得|"
                r"严禁|不允许|不可|不能|不准|须|需)|[:：].+",
                remainder,
            )
        )
    if answer_type == "PROCEDURE":
        bare_lead_in = re.search(
            r"(?:包括|包含|分为|分成|具体如下|步骤如下|流程如下|如下)"
            r"[^。；;\n]{0,16}[:：。]?$",
            clause,
        )
        return (
            _target_matches(target, clause, strict=True)
            and relation in clause
            and bare_lead_in is None
            and bool(
                re.search(
                    r"先.+(?:再|然后|随后|接着)|"
                    r"(?:步骤|流程)(?:是|为|[:：])",
                    clause,
                )
            )
        )
    return False


def _duty_subject_supports(target: str, clause: str) -> bool:
    """只把完整目标岗位作为职责主语的正文判为直接支持。

    Args:
        target: 查询解析出的完整岗位名。
        clause: 单个规范化来源分句。

    Returns:
        目标位于句首主语位置且紧接职责关系或职责动作时返回 True。

    """
    if not target:
        return False
    prefix = (
        r"^\s*(?:(?:\d+(?:\.\d+)*|[a-z])\s*[.)、）]?\s*)?"
        r"(?:在[^，,。；;]{1,30}[，,]\s*)?[（(【\[]?\s*"
    )
    relation = (
        r"(?:的)?(?:(?:岗位|安全|主要|核心|工作)?)职责"
        r"(?:是|为|包括|包含|[:：])"
    )
    return bool(
        re.match(
            prefix
            + re.escape(target)
            + r"\s*(?:[）)】\]]\s*)?(?:"
            + relation
            + "|"
            + _DUTY_SUBJECT_ACTION
            + ")",
            clause,
        )
    )


def _source_corrects_count_premise(analysis: QueryAnalysis, text: str) -> bool:
    expected = analysis.semantics.expected_count
    if expected is None:
        return False
    actual = _enumeration_item_count(text)
    return actual is not None and actual != expected


def _enumeration_item_count(text: str) -> int | None:
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", text)
    if len(quoted) >= _MIN_ENUMERATION_ITEMS:
        return len(quoted)
    marker = re.search(r"分为|分成|包括|包含|分别为|采用", text)
    if marker is None:
        return None
    value = re.split(r"[。；;\n]", text[marker.end() :], maxsplit=1)[0]
    parts = [part.strip(" ，,：:") for part in value.split("、")]
    parts = [part for part in parts if part]
    return len(parts) if len(parts) >= _MIN_ENUMERATION_ITEMS else None


def _attribute_matches(relation: str, answer_type: str, text: str) -> bool:
    if answer_type == "CONTACT":
        pattern = _contact_pattern(relation)
        return bool(re.search(pattern, text))
    if answer_type == "MONEY":
        if "采购" in relation or "购买" in relation:
            return bool(re.search(r"采购价|采购成本|购买价格", text))
        if "售价" in relation or "销售" in relation:
            return bool(re.search(r"售价|销售价格", text))
    pattern = next(
        pattern for kind, pattern in _ATTRIBUTES if kind == answer_type
    )
    return bool(re.search(pattern, text))


def _contact_pattern(relation: str) -> str:
    if "邮箱" in relation or "邮件" in relation:
        return r"邮箱|电子邮件"
    if "分机" in relation:
        return r"分机(?:号)?"
    return r"联系电话|电话号码|手机号码|手机号|电话|联系方式"


def _typed_value_matches(answer_type: str, relation: str, text: str) -> bool:
    if answer_type == "CONTACT":
        if "邮箱" in relation or "邮件" in relation:
            pattern = r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
        elif "分机" in relation:
            pattern = r"(?<!\d)\d{2,6}(?!\d)"
        else:
            pattern = r"(?<!\w)\+?\d[\d ()-]{5,}\d(?!\w)"
        return bool(re.search(pattern, text))
    return bool(re.search(_VALUES[answer_type], text, re.IGNORECASE))


def _table_value_supports(
    answer_type: str, relation: str, text: str, header: str
) -> bool:
    if _UNKNOWN.search(text) or re.search(r"不是|并非|不等于", text):
        return False
    if answer_type == "PERSON_OR_ROLE":
        return bool(
            re.search(r"受理|负责|审核|复核|角色|责任人", header)
        ) and bool(text.strip())
    header_type = _TABLE_HEADER_TYPES.get(answer_type)
    if header_type is not None:
        return bool(header_type.search(header) and text.strip())
    if answer_type not in _VALUES:
        return bool(text.strip())
    if _typed_value_matches(answer_type, relation, text):
        return True
    unit = re.search(r"[（(]([^()（）]+)[）)]", _normalized(header))
    return bool(
        unit
        and re.fullmatch(_NUMBER, text.strip())
        and _typed_value_matches(answer_type, relation, text + unit[1])
    )


def evaluate_linked_support(
    analysis: QueryAnalysis,
    subject: str,
    attribute: str,
    span_ids: tuple[str, ...],
) -> AnswerSupport | None:
    """只将独立对象标签与紧邻的无主体属性句组成支持关系。

    Args:
        analysis: 当前确定性请求分析。
        subject: 前一个真实片段中的独立对象标签。
        attribute: 后一个真实片段中的属性或责任角色关系。
        span_ids: 调用方已验证相邻边界的两个真实来源标识。

    Returns:
        两个片段共同支持时返回判定，否则返回空值。

    """
    target, relation, answer_type = _request(analysis)
    if _normalized(subject).strip("。:： ") != target:
        return None
    normalized_attribute = _normalized(attribute).strip()
    if answer_type in {"PERSON_OR_ROLE", "RESPONSIBLE_PARTY"}:
        prefix_matches = normalized_attribute.startswith("由")
    else:
        pattern = next(
            (p for kind, p in _ATTRIBUTES if kind == answer_type), "(?!)"
        )
        prefix_matches = re.match(pattern, normalized_attribute) is not None
    if not prefix_matches:
        return None
    support = evaluate_span_support(analysis, subject + attribute)
    if support.status is not SupportStatus.SUPPORTED:
        return None
    return AnswerSupport(
        status=support.status,
        query_target=target,
        requested_relation_or_attribute=relation,
        answer_type=answer_type,
        support_reason="LINKED_SUBJECT_ATTRIBUTE",
        supporting_span_ids=span_ids,
    )
