"""中文自然问法的共享类型化语义，不包含业务实体白名单。"""

from __future__ import annotations

import re
import unicodedata

from rag_app.core.models import QuerySemantics, RequestedAnswerType

_NUMERAL = r"[零一二三四五六七八九十百两\d]+"
_RELATION = re.compile(
    r"工作模式|协作方式|运行方式|工作方式|模式|类型|种类|类别|分类|职责|步骤|流程"
)
_DUTY = re.compile(
    r"(?:(?:平时|日常|通常|一般|具体|主要)(?:地)?)*"
    r"(?:职责(?:是什么|是啥|有哪些|包括什么)?|"
    r"负责(?:什么|啥|哪些)(?:工作|事项|内容)?|"
    r"(?:需要)?承担(?:什么|哪些)(?:职责|工作|事项|内容)?|"
    r"(?:(?:都|主要)?(?:要|需要|得)?)(?:是)?干"
    r"(?:什么|啥|嘛|些(?:什么|啥)?)(?:的)?|"
    r"(?:(?:都|主要)?(?:要|需要|得)?)(?:是)?(?:做|管)"
    r"(?:什么|啥|些(?:什么|啥)?|哪些)(?:事|工作|事项|内容)?(?:的)?)"
    r"(?:工作|事项|内容)?[呢吗呀啊？?\s]*$"
)
_CONDITION_OR_ORDER = re.compile(
    r"如果|一旦|若|当.+时|之前|之后|以前|以后|期间|过程中|"
    r"(?:先|再|然后|随后|接着|前|后|时)(?:应|要|需|应该|需要)?$"
)
_ORDINAL = re.compile(rf"第(?P<number>{_NUMERAL})(?:种|类|项|步|条|阶段|环节)")
_COUNT_QUESTION = re.compile(
    r"(?:有)?多少(?:种|个)?(?:步骤|步|项|阶段|环节)?|(?<!哪)几种|"
    r"几(?:个)?(?:步骤|阶段|环节)"
)
_EXPECTED_COUNT = re.compile(
    rf"(?<!第)(?P<number>{_NUMERAL})(?:种|类|项|步)(?![类型])"
)
_ENUMERATION_QUESTION = re.compile(
    r"是什么|是啥|(?:都)?有啥|(?:都)?有哪些|有哪(?:几|"
    + _NUMERAL
    + r")种|是哪(?:几|"
    + _NUMERAL
    + r")种|哪(?:几|"
    + _NUMERAL
    + r")种|分别(?:是|指)?(?:什么|啥)|"
    r"(?:具体)?(?:是)?怎么(?:说|表述)(?:的)?|请列举|请列出|列出来|列一下"
)
_PROCEDURE_QUESTION = re.compile(
    r"是什么|是啥|(?:都)?有哪些|有哪(?:几|"
    + _NUMERAL
    + r")步|哪(?:几|"
    + _NUMERAL
    + r")步|分别(?:是|指)?(?:什么|啥)|请列举|请列出|列出来|列一下|"
    r"如何|怎么|怎样"
)
_LEADING_REQUEST = re.compile(
    r"^(?:请查一下|帮我查一下|查一下|请问|请告诉我|告诉我|我想知道|"
    r"请帮我|请介绍|请说明|"
    r"请解释|请列举|请列出|请)"
)
_TRAILING_TARGET_SYNTAX = re.compile(
    rf"(?:的)?(?:第{_NUMERAL}(?:种|类|项|步|条)|"
    rf"{_NUMERAL}(?:种|类)|(?:是)?哪(?:几|{_NUMERAL})(?:种|类)|"
    r"有多少种|(?:都)?有哪些|(?:都)?有啥|采用的|现有的|主要的|核心的|的)$"
)
_TRAILING_PARTICLES = re.compile(
    r"(?:(?:嘛|吧)[，,]|[呢吗呀啊？?。！!，,\s])+$"
)
_SOURCE_QUALIFIED_DUTY = re.compile(
    r"^(?P<source>.+?(?:规范|文档|制度|手册))(?:里|中)"
    r"[，,：:\s]*(?P<target>.+)$"
)
_PURPOSE_QUESTION = re.compile(
    r"^(?P<target>.+?)(?:的)?(?P<relation>目的|目标|作用)"
    r"(?:(?:是|为)?(?:什么|啥)|如何)?$"
)
_NATURAL_PURPOSE_QUESTION = re.compile(
    r"^(?P<target>.+?)(?:的)?(?:主要|核心)?(?:是)?"
    r"(?:为了|为的是|旨在|用于|用来)(?:解决)?"
    r"(?:什么|啥|哪些|哪类)(?:问题|事情|事)?$"
)
_RESPONSIBLE_SUFFIX = re.compile(
    r"^(?P<target>.+?)(?:这(?:件)?事)?(?:到底|究竟)?"
    r"(?:(?:应该|应当|应|要|该|需要)?(?:找|由|归)?谁(?:来)?"
    r"(?:负责|牵头|管理|受理)?|"
    r"(?:的)?(?:责任角色|责任人|负责人|牵头人|主责角色)"
    r"(?:是|为)?(?:谁|哪位))$"
)
_RESPONSIBLE_PREFIX = re.compile(
    r"^(?:由)?谁(?:来)?(?:负责|牵头|管理|受理)(?P<target>.+)$"
)
_SECTION_SUMMARY = re.compile(
    rf"^(?P<target>.+?(?:第{_NUMERAL}(?:章|节)|章节|章|节|管理要求|"
    r"工作要求|要求))(?:(?:是|指)?(?:什么|啥)|如何规定)?$"
)
_DEFINITION_PREFIX = re.compile(r"^(?:什么|啥)是(?P<target>.+)$")
_DEFINITION_SUFFIX = re.compile(
    r"^(?P<target>.+?)(?:(?:说白了|简单来说|通俗地说)?"
    r"(?:是|指(?:的)?是|定义为)(?:什么|啥)|(?:是)?什么意思)$"
)
_FACT_QUESTION_END = re.compile(
    r"(?:(?:是|为)?(?:什么|啥|多少|多大)|是多少|是什么|是啥)$"
)
# 这些是通用事实属性，不是术语定义。若把“邮箱是什么”一概解析为定义，
# 会让类型化语义抢走已有的属性证据路径。
_FACT_ATTRIBUTE_SUFFIX = re.compile(
    r"(?:联系方式|联系电话|电话号码|手机号码|手机号|电话|分机号|分机|"
    r"邮箱|电子邮件|采购价格|采购价|采购成本|购买价格|售价|销售价格|"
    r"价格|价钱|费用|工资|薪资|保证金|补助|预付款|建筑面积|占地面积|"
    r"面积|温度上限|储存温度|温度|额定压力|压力|额定载荷|载荷上限|"
    r"载荷|重量|质量|"
    r"允许偏差|偏差|比例|百分比|保管期限|保存期限|借阅期限|期限|"
    r"维护周期|校准周期|检验周期|检查周期|保养周期|复核周期|更新周期|"
    r"轮换周期|当前有效版本|现行版本|当前版本|有效版本|版本|"
    r"日期|时间|数据库品牌|品牌|型号|单位|数值)$"
)
_GENERIC_ENUMERATION = re.compile(
    r"^(?P<target>.+?)(?:都)?(?:有|包含)?"
    r"(?:哪些(?:阶段|环节)?|有啥|哪几项|哪几个阶段|分别是什么)$"
)
_STAGE_ENUMERATION = re.compile(
    r"^(?P<target>.+?)(?:从[^，,]{1,40}(?:到|至)[^，,]{1,40})?[，,]?"
    r"(?:一共)?(?:都)?(?:分为|分成|分|包括|包含|经历|要走|需走|会走)"
    r"(?:了)?(?:哪些|哪几个|几大|什么)(?:主要)?(?:阶段|环节|步骤)$"
)
_STAGE_COUNT = re.compile(
    r"^(?P<target>.+?(?:全流程|流程))(?:一共)?(?:有)?"
    r"(?:多少|几)(?:个)?(?:阶段|环节)$"
)
_DELIVERABLE_ENUMERATION = re.compile(
    r"^(?P<target>.+?)(?:(?:做|办理|执行)?(?:完成|做完|结束)"
    r"(?:以后|之后|后)?[，,]?)?(?:必须|需要|要|应当|应)?"
    r"(?:交|提交|输出|提供|产出)(?:哪些|什么)"
    r"(?:东西|材料|交付物|交付件|成果|文件)"
    r"(?:才能|才算|用于|以便)?.*$"
)
_OBJECT_FIRST_PROCEDURE = re.compile(
    r"^(?:.+?[，,])?(?:要|该|应该|应当)?(?:怎么|如何|怎样)"
    r"(?:把)?(?P<target>.+?)(?P<action>导进去|导入|上传|录入|登记|"
    r"创建|配置)(?:进去|完成|好)?$"
)
_TARGET_FIRST_PROCEDURE = re.compile(
    r"^(?P<target>.+?)(?:要|该|应该|应当)?(?:怎么|如何|怎样)"
    r"(?P<action>导入|上传|录入|登记|创建|配置|完成|办理|操作|处理|"
    r"执行|使用|做)$"
)
_MIN_SOURCE_QUALIFIER_TERM_LENGTH = 2
_LEADING_CONTEXT_CLAUSE = re.compile(
    r"^(?:在|做|进行|处理)?[^，,]{1,40}(?:时|的时候)[，,]"
)
_LEADING_PROJECT_CONTEXT = re.compile(
    r"^(?:在|做|进行|处理)?(?P<context>[^，,]{1,40}?)"
    r"项目(?:时|的时候)[，,]"
)


def parse_query_semantics(  # noqa: PLR0911, PLR0912, PLR0915
    query: str,
) -> QuerySemantics:
    """从确认的问句位置提取对象、关系和答案形状。

    Args:
        query: NFKC 前后均可接受的单个用户问题。

    Returns:
        规则命中时返回类型化语义；未覆盖表达保持 unknown。

    """
    normalized = unicodedata.normalize("NFKC", query).strip()
    core = _question_core(normalized)

    fact_core = _FACT_QUESTION_END.sub("", core).strip()
    fact_attribute = _FACT_ATTRIBUTE_SUFFIX.search(fact_core)
    if fact_core != core and fact_attribute is not None:
        raw_target = fact_core[: fact_attribute.start()].removesuffix("的")
        target, source = _target_and_source(raw_target)
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation=fact_attribute.group(0),
                answer_type=RequestedAnswerType.FACT,
                source="RULE",
                reason_codes=("FACT_ATTRIBUTE_QUESTION_SYNTAX",),
            )

    purpose = _PURPOSE_QUESTION.fullmatch(core)
    if purpose is None:
        purpose = _NATURAL_PURPOSE_QUESTION.fullmatch(core)
    if purpose is not None:
        target, source = _target_and_source(purpose["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation=purpose.groupdict().get("relation") or "目的",
                answer_type=RequestedAnswerType.PURPOSE,
                source="RULE",
                reason_codes=("PURPOSE_QUESTION_SYNTAX",),
            )

    responsible = _RESPONSIBLE_SUFFIX.fullmatch(
        core
    ) or _RESPONSIBLE_PREFIX.fullmatch(core)
    if responsible is not None:
        target, source = _target_and_source(responsible["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="责任角色",
                answer_type=RequestedAnswerType.RESPONSIBLE_PARTY,
                source="RULE",
                reason_codes=("RESPONSIBLE_PARTY_QUESTION_SYNTAX",),
            )

    duty = _DUTY.search(normalized)
    if duty is not None and not _CONDITION_OR_ORDER.search(
        normalized[: duty.start()]
    ):
        prefix = normalized[: duty.start()]
        context_qualifier = _context_qualifier(prefix)
        target, source = _target_and_source(prefix, duty=True)
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                context_qualifier=context_qualifier,
                relation="职责",
                answer_type=RequestedAnswerType.DUTIES,
                source="RULE",
                reason_codes=(
                    "AMBIGUOUS_ACTION_QUESTION_SYNTAX"
                    if "干" in duty.group(0)
                    else "DUTY_QUESTION_SYNTAX",
                ),
            )

    section = _SECTION_SUMMARY.fullmatch(core)
    if section is not None:
        target, source = _target_and_source(section["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="章节内容",
                answer_type=RequestedAnswerType.SECTION_SUMMARY,
                source="RULE",
                reason_codes=("SECTION_SUMMARY_QUESTION_SYNTAX",),
            )

    definition = _DEFINITION_PREFIX.fullmatch(core)
    if definition is None:
        suffix_definition = _DEFINITION_SUFFIX.fullmatch(core)
        suffix_target = (
            _target_and_source(suffix_definition["target"])[0]
            if suffix_definition is not None
            else ""
        )
        if (
            suffix_definition is not None
            and _RELATION.search(suffix_target) is None
            and _FACT_ATTRIBUTE_SUFFIX.search(suffix_target) is None
        ):
            definition = suffix_definition
    if definition is not None:
        target, source = _target_and_source(definition["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="定义",
                answer_type=RequestedAnswerType.DEFINITION,
                source="RULE",
                reason_codes=("DEFINITION_QUESTION_SYNTAX",),
            )

    stages = _STAGE_ENUMERATION.fullmatch(core)
    if stages is not None:
        target, source = _target_and_source(stages["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="主要阶段",
                answer_type=RequestedAnswerType.ENUMERATION,
                source="RULE",
                reason_codes=("STAGE_ENUMERATION_QUESTION_SYNTAX",),
            )

    stage_count = _STAGE_COUNT.fullmatch(core)
    if stage_count is not None:
        target, source = _target_and_source(stage_count["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="主要阶段",
                answer_type=RequestedAnswerType.COUNT,
                source="RULE",
                reason_codes=("STAGE_COUNT_QUESTION_SYNTAX",),
            )

    deliverables = _DELIVERABLE_ENUMERATION.fullmatch(core)
    if deliverables is not None:
        target, source = _target_and_source(deliverables["target"])
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation="交付物",
                answer_type=RequestedAnswerType.ENUMERATION,
                source="RULE",
                reason_codes=("DELIVERABLE_ENUMERATION_QUESTION_SYNTAX",),
            )

    procedure = _OBJECT_FIRST_PROCEDURE.fullmatch(
        core
    ) or _TARGET_FIRST_PROCEDURE.fullmatch(core)
    if procedure is not None:
        target, source = _target_and_source(procedure["target"])
        if target:
            action = procedure["action"]
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation=(
                    "导入"
                    if action == "导进去"
                    else (
                        "流程"
                        if action
                        in {"完成", "办理", "操作", "处理", "执行", "做"}
                        else action
                    )
                ),
                answer_type=RequestedAnswerType.PROCEDURE,
                source="RULE",
                reason_codes=("OBJECT_FIRST_PROCEDURE_QUESTION_SYNTAX",),
            )

    generic_enumeration = _GENERIC_ENUMERATION.fullmatch(core)
    if generic_enumeration is not None and (
        _RELATION.search(generic_enumeration["target"]) is None
        or generic_enumeration["target"].endswith("全流程")
    ):
        target, source = _target_and_source(generic_enumeration["target"])
        if target:
            relation = "主要阶段" if "流程" in target else "组成"
            return QuerySemantics(
                target=target,
                source_qualifier=source,
                relation=relation,
                answer_type=RequestedAnswerType.ENUMERATION,
                source="RULE",
                reason_codes=("GENERIC_ENUMERATION_QUESTION_SYNTAX",),
            )

    relation_match = _RELATION.search(normalized)
    if relation_match is None:
        return _fallback_semantics(normalized)

    prefix = normalized[: relation_match.start()]
    suffix = normalized[relation_match.end() :]
    relation = relation_match.group(0)
    ordinal_match = _ORDINAL.search(prefix + suffix)
    count_question = _COUNT_QUESTION.search(prefix + suffix)
    count_surface = prefix + suffix
    expected_match = _EXPECTED_COUNT.search(count_surface)
    if (
        expected_match is not None
        and expected_match["number"] == "一"
        and expected_match.start("number") > 0
        and count_surface[expected_match.start("number") - 1] == "哪"
    ):
        expected_match = None
    expected_count = (
        _number_value(expected_match["number"])
        if expected_match is not None
        else None
    )
    ordinal = (
        _number_value(ordinal_match["number"])
        if ordinal_match is not None
        else None
    )
    if ordinal is not None:
        answer_type = RequestedAnswerType.ORDINAL_ITEM
    elif count_question is not None:
        answer_type = RequestedAnswerType.COUNT
        expected_count = None
    elif relation in {"步骤", "流程"} and _PROCEDURE_QUESTION.search(
        normalized
    ):
        answer_type = RequestedAnswerType.PROCEDURE
    elif _ENUMERATION_QUESTION.search(normalized):
        answer_type = RequestedAnswerType.ENUMERATION
    elif relation == "职责":
        answer_type = RequestedAnswerType.DUTIES
    else:
        answer_type = RequestedAnswerType.UNKNOWN
    if answer_type is RequestedAnswerType.UNKNOWN:
        return QuerySemantics(
            relation=relation,
            answer_type=answer_type,
            source="ORIGINAL_FALLBACK",
            reason_codes=("QUERY_SEMANTICS_UNKNOWN",),
        )
    target, source = _target_and_source(
        prefix, duty=answer_type is RequestedAnswerType.DUTIES
    )
    if not target:
        return QuerySemantics(
            relation=relation,
            answer_type=RequestedAnswerType.UNKNOWN,
            source="ORIGINAL_FALLBACK",
            reason_codes=("QUERY_TARGET_UNKNOWN",),
        )
    return QuerySemantics(
        target=target,
        source_qualifier=source,
        relation=relation,
        answer_type=answer_type,
        expected_count=expected_count,
        ordinal=ordinal,
        source="RULE",
        reason_codes=("SHARED_QUERY_SEMANTICS_V2",),
    )


def _fallback_semantics(query: str) -> QuerySemantics:
    answer_type = (
        RequestedAnswerType.FACT
        if re.search(
            r"谁|什么|啥|多少|如何|怎么|怎样|哪|何时|是否|能否|几|[?？]",
            query,
        )
        else RequestedAnswerType.UNKNOWN
    )
    return QuerySemantics(
        answer_type=answer_type,
        source="ORIGINAL_FALLBACK",
        reason_codes=("QUERY_SEMANTICS_UNKNOWN",),
    )


def _clean_target(value: str, *, duty: bool) -> str:
    """只移除对象边界上的问句语法，绝不全字符串删词。"""
    target = value.strip(" \t\r\n，,：:；;")
    target = _TRAILING_PARTICLES.sub("", target)
    target = _LEADING_REQUEST.sub("", target).strip()
    if target.startswith("把"):
        target = target[1:].strip()
    previous = None
    while target and target != previous:
        previous = target
        target = _TRAILING_TARGET_SYNTAX.sub("", target).strip()
        target = _TRAILING_PARTICLES.sub("", target).strip(" \t\r\n，,：:；;")
    if duty:
        target = re.split(r"(?:规范|文档|制度|手册)(?:里|中)", target)[-1]
        target = _LEADING_CONTEXT_CLAUSE.sub("", target).strip()
        target = re.sub(r"(?:的)?(?:主要|核心|具体)$", "", target).strip()
    return target


def _question_core(value: str) -> str:
    """只裁剪完整问句边界，不删除实体内部字符。"""
    core = _LEADING_REQUEST.sub("", value.strip()).strip()
    if core.startswith("把"):
        core = core[1:].strip()
    return _TRAILING_PARTICLES.sub("", core).strip()


def _target_and_source(
    value: str, *, duty: bool = False
) -> tuple[str, str | None]:
    """从对象边界切出动态来源限定，保留对象内部原词。"""
    candidate = _clean_target(value, duty=False)
    qualified = _SOURCE_QUALIFIED_DUTY.fullmatch(candidate)
    if qualified is None:
        return _clean_target(candidate, duty=duty), None
    source = qualified["source"].strip(" 的")
    return _clean_target(qualified["target"], duty=duty), source or None


def _source_qualifier(value: str) -> str | None:
    """提取“某规范中/里”的动态来源限定，不维护文档名白名单。"""
    candidate = _LEADING_REQUEST.sub("", value.strip()).strip()
    match = _SOURCE_QUALIFIED_DUTY.fullmatch(candidate)
    if match is None:
        return None
    qualifier = match["source"].strip(" 的")
    return qualifier or None


def _context_qualifier(value: str) -> str | None:
    """从“做 X 项目时”保留职责问句中的动态场景对象。"""
    match = _LEADING_PROJECT_CONTEXT.match(value.strip())
    if match is None:
        return None
    context = match["context"].strip(" 的")
    return f"{context}项目" if context else None


def source_qualifier_matches(
    display_name: str,
    heading_path: tuple[str, ...],
    source_qualifier: str,
) -> bool:
    """匹配动态来源限定与文档标签，短词或歧义继续拒绝。

    Args:
        display_name: 当前文档的安全显示名。
        heading_path: 当前候选的结构标题路径。
        source_qualifier: 从原问保留下来的来源限定。

    Returns:
        完整限定或全部安全核心词能在文档标签中定位时为 True。

    """
    qualifier = unicodedata.normalize("NFKC", source_qualifier).casefold()
    label = unicodedata.normalize(
        "NFKC", " ".join((display_name, *heading_path))
    ).casefold()
    if qualifier in label:
        return True
    core = re.sub(r"(?:规范|文档|制度|手册)$", "", qualifier).strip()
    terms = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", core)
    if not terms or any(
        len(term) < _MIN_SOURCE_QUALIFIER_TERM_LENGTH
        and re.fullmatch(r"[\u3400-\u9fff]", term)
        for term in terms
    ):
        return False
    return all(term in label for term in terms)


def _number_value(value: str) -> int | None:
    """解析问句中小规模阿拉伯数字或常用中文序数。"""
    if value.isdigit():
        number = int(value)
        return number if number > 0 else None
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "百" in value:
        head, _, tail = value.partition("百")
        return digits.get(head or "一", 1) * 100 + (_number_value(tail) or 0)
    if "十" in value:
        head, _, tail = value.partition("十")
        return digits.get(head or "一", 1) * 10 + digits.get(tail, 0)
    return digits.get(value)


__all__ = ["parse_query_semantics", "source_qualifier_matches"]
