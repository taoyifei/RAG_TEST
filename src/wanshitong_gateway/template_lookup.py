"""从当前资料目录选出可直接下载的模板，歧义时只问一个办理场景。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal, TypedDict

_EDITABLE_FORMATS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx"}
_FILE_WORDS = re.compile(
    r"模板|表格|清单|[个份张][^，。！？?]{0,8}表|空白|空表|样表|现成|原件|"
    r"电子版|可编辑|可填写|能改|"
    r"可套|能套|可以套|套用|下载|ppt|excel|xlsx|word|docx",
    re.I,
)
_FILE_REQUEST = re.compile(
    r"有|求|想|要|发|给|找|拿|哪|来一?[份张个]|下载|能套|可以套|套用"
)
_ASK_FOR_DETAILS = re.compile(
    r"怎么|怎样|如何|包含|哪些|哪几|为什么|谁审批|什么流程|有什么要求"
)
_GET_FILE = re.compile(
    r"下载|哪里找|在哪[儿里]?[？?。 ]*$|[发给]我|从哪.{0,8}[拿下]|"
    r"有现成|能套|可以套"
)
_VERSION = re.compile(r"[（(](?:20\d{2}年\d{1,2}月版|[vV][\d.]+)[）)]")
_CHECKLIST = "采购需求标准化清单"
_MIN_TITLE_LENGTH = 4

# 业务说法和目录中的稳定标题片段关联。实际文件及版本仍来自当轮目录。
_BUSINESS_NAMES: dict[str, tuple[tuple[str, ...], ...]] = {
    "项目采购需求的汇报": (
        ("采购", "要买", "这次买", "买什么", "为什么买", "买设备", "买东西"),
        ("上会", "汇报", "立项会", "跟领导讲", "给领导讲", "ppt", "演示"),
    ),
    _CHECKLIST: (
        ("采购", "要买", "这次买"),
        ("核对", "对照", "检查项", "标准化清单", "需求清单"),
    ),
    "采购订单到货情况确认表": (
        ("采购", "订单"),
        ("到货", "货到了", "货已经到", "货到齐", "东西到了"),
    ),
    "代报账付款确认单（会议、培训及成本类）": (
        ("会议", "培训", "成本类"),
        ("代报", "垫付", "先付", "帮我付", "替我付", "付款确认"),
    ),
    "工程类代报账付款确认单": (
        ("工程", "施工"),
        ("代报", "垫付", "先付", "帮我付", "替我付", "付款确认"),
    ),
    "工程项目付款申请审批表": (
        ("工程", "施工"),
        ("付款申请", "申请付款", "申请支付", "请款", "付工程款", "付款审批"),
    ),
    "工程项目考核成绩汇总表": (
        ("工程", "施工"),
        ("考核成绩", "考核分数", "考核结果", "考核得分", "打分汇总"),
    ),
    "工程项目初验与终验报告": (
        ("工程", "施工"),
        ("验收", "初验", "终验"),
    ),
    "无合同费用计提测算": (
        ("无合同", "没合同", "还没签", "合同还没", "合同没签"),
        ("计提", "预提", "测算"),
    ),
    "非计费类收入确认表": (
        ("收入", "进账"),
        ("非计费", "不计费"),
    ),
}

_TOPICS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "采购": (
        "采购|要买|这次买|买设备|买东西",
        (
            ("项目采购需求的汇报", "做采购需求汇报"),
            (_CHECKLIST, "核对采购需求"),
            ("采购订单到货情况确认表", "确认到货"),
        ),
    ),
    "工程": (
        "工程|施工",
        (
            ("工程项目付款申请审批表", "申请工程付款"),
            ("工程项目初验与终验报告", "办理验收"),
            ("工程项目考核成绩汇总表", "汇总考核成绩"),
        ),
    ),
    "代报账": (
        "代报|垫付|先付|付款确认|代付",
        (
            (
                "代报账付款确认单（会议、培训及成本类）",
                "代报会议、培训或成本类费用",
            ),
            ("工程类代报账付款确认单", "代报工程类费用"),
        ),
    ),
    "报账": (
        "报账|报销",
        (
            ("代报账付款确认单（会议、培训及成本类）", "确认别人代付"),
            ("工程项目付款申请审批表", "申请工程付款"),
            ("无合同费用计提测算", "做费用计提"),
        ),
    ),
}


class TemplateFile(TypedDict):
    """只传递资料目录提供的文件标识和显示信息。"""

    document_id: str
    filename: str
    format: str
    kind: Literal["template", "checklist"]


@dataclass(frozen=True, slots=True)
class TemplateReply:
    """网关给出的目录查找结果或一次待答复的澄清。"""

    answer: str
    file: TemplateFile | None = None
    topic: str | None = None


def _compact(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _template_name(filename: str) -> str:
    title = _VERSION.sub("", PurePath(filename).stem)
    title = re.sub(r"^报账附件模板[－-]", "", title)
    return _compact(re.sub(r"(?:填写)?(?:模板|示例)$", "", title))


def _wants_file(question: str) -> bool:
    clause = re.split(r"[，。！？?]", question.rstrip("，。！？? "))[-1]
    if _ASK_FOR_DETAILS.search(clause) and not _GET_FILE.search(clause):
        return False
    return bool(
        _FILE_WORDS.search(question)
        and (
            _FILE_REQUEST.search(question)
            or question.rstrip("？?。 ")
            .lower()
            .endswith(
                ("模板", "表格", "清单", "空表", "原件", "ppt", "excel", "word")
            )
        )
    )


def wants_template_lookup(question: str, pending_topic: str | None) -> bool:
    """普通流程或制度问题保持原有问答路径。"""
    return _wants_file(question) or bool(
        pending_topic and not _ASK_FOR_DETAILS.search(question)
    )


def template_file(
    document_id: str,
    filename: str,
    kind: str,
) -> TemplateFile | None:
    """只把实际可编辑的模板与指定的原始核对清单放入候选。"""
    extension = PurePath(filename).suffix.removeprefix(".").lower()
    if extension not in _EDITABLE_FORMATS:
        return None
    if kind == "template" or (kind == "policy" and _CHECKLIST in filename):
        return {
            "document_id": document_id,
            "filename": filename,
            "format": extension.upper(),
            "kind": "template" if kind == "template" else "checklist",
        }
    return None


def _matches_business(filename: str, question: str) -> bool:
    return any(
        name in filename
        and all(any(term in question for term in group) for group in groups)
        for name, groups in _BUSINESS_NAMES.items()
    )


def _requested_format(question: str, file: TemplateFile) -> bool:
    for name, formats in (
        ("excel", {"XLS", "XLSX"}),
        ("xlsx", {"XLSX"}),
        ("word", {"DOC", "DOCX"}),
        ("docx", {"DOCX"}),
        ("ppt", {"PPT", "PPTX"}),
    ):
        if name in question and file["format"] not in formats:
            return False
    return True


def lookup_template(
    question: str,
    files: Sequence[TemplateFile],
    pending_topic: str | None,
) -> TemplateReply | None:
    """对文件名和业务说法匹配，只有明确命中一个文件才直接返回。"""
    if not wants_template_lookup(question, pending_topic):
        return None
    query = question.casefold()
    compact = _compact(query)
    matches: list[TemplateFile] = []
    named: list[tuple[int, TemplateFile]] = []
    for file in files:
        if not _requested_format(query, file):
            continue
        filename = file["filename"]
        name = _template_name(filename)
        if len(name) >= _MIN_TITLE_LENGTH and name in compact:
            named.append((len(name), file))
        elif _matches_business(filename, query) or (
            pending_topic and _matches_business(filename, pending_topic + query)
        ):
            matches.append(file)
    if named:
        longest = max(size for size, _ in named)
        matches = [file for size, file in named if size == longest]
    if len(matches) == 1:
        return TemplateReply(
            "这是当前发布的原件，可以直接下载。", file=matches[0]
        )
    if len(matches) > 1:
        titles = "、".join(
            PurePath(file["filename"]).stem for file in matches[:3]
        )
        topic = next(
            (
                name
                for name, (terms, _) in _TOPICS.items()
                if re.search(terms, query)
            ),
            pending_topic,
        )
        return TemplateReply(f"你需要哪一份？找到的有 {titles}。", topic=topic)
    if not _wants_file(question):
        return None
    for topic, (terms, choices) in _TOPICS.items():
        if not re.search(terms, query):
            continue
        options = [
            label
            for key, label in choices
            if any(key in file["filename"] for file in files)
        ]
        if options:
            examples = "、".join(options)
            return TemplateReply(
                f"你准备用这份文件做什么？比如{examples}。", topic=topic
            )
    return None
