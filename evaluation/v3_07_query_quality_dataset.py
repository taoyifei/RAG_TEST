"""V3-07 查询恢复的独立公开合成开发集与 Holdout。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

Split = Literal["tuning", "holdout"]
BlockKind = Literal["heading", "paragraph", "list", "table"]

DATASET_ID = "v3-07-public-query-quality"
DATASET_VERSION = "1.0.0"
CONTENT_CLASSIFICATION = "synthetic_public"

GATES: dict[str, float | int] = {
    "answerable_accuracy_min": 0.90,
    "false_refusal_rate_max": 0.10,
    "false_answer_rate_max": 0.0,
    "citation_source_precision_min": 1.0,
    "citation_span_validity_min": 1.0,
    "paraphrase_consistency_min": 0.90,
    "hard_constraint_violations_max": 0,
    "status_parity_min": 1.0,
}

REQUIRED_SLICE_COUNTS = {
    "definition": 12,
    "purpose": 10,
    "duties": 12,
    "responsible_party": 10,
    "table": 12,
    "process": 12,
    "hard_constraint": 20,
}


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """一个可重复生成 OOXML 的公开结构块。"""

    kind: BlockKind
    text: str = ""
    level: int = 1
    items: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    """公开合成文档及其 split 身份。"""

    document_key: str
    split: Split
    display_name: str
    blocks: tuple[BlockSpec, ...]


@dataclass(frozen=True, slots=True)
class QueryCase:
    """一条只使用公开合成事实的查询验收标签。"""

    case_id: str
    split: Split
    slice_name: str
    paraphrase_group: str
    query: str
    expected_document_keys: tuple[str, ...]
    expected_answer_fragments: tuple[str, ...]
    expected_source_fragments: tuple[str, ...]
    answerable: bool
    expected_answer_type: str
    forbidden_fragments: tuple[str, ...] = ()


def _heading(text: str, level: int = 1) -> BlockSpec:
    return BlockSpec(kind="heading", text=text, level=level)


def _paragraph(text: str) -> BlockSpec:
    return BlockSpec(kind="paragraph", text=text)


def _list(*items: str) -> BlockSpec:
    return BlockSpec(kind="list", items=tuple(items))


def _table(*rows: tuple[str, ...]) -> BlockSpec:
    return BlockSpec(kind="table", rows=tuple(rows))


TUNING_DOCUMENTS = (
    DocumentSpec(
        document_key="tuning_terms",
        split="tuning",
        display_name="云雀术语手册.docx",
        blocks=(
            _heading("术语定义"),
            _table(
                ("术语", "定义说明"),
                (
                    "LPC",
                    "Local Process Capsule，指把一次公开任务的步骤"
                    "与校验结果封装成可复查单元。",
                ),
                (
                    "MVB",
                    "Model Verification Bundle，指用于保存模型核验输入"
                    "与引用清单的公开测试包。",
                ),
            ),
        ),
    ),
    DocumentSpec(
        document_key="tuning_purpose",
        split="tuning",
        display_name="云帆设备交接准则.docx",
        blocks=(
            _heading("概述"),
            _heading("目的", 2),
            _paragraph(
                "本准则用于统一云帆设备交接步骤，并确保验收记录能够追溯。"
            ),
            _heading("适用范围"),
            _paragraph("本准则适用于公开测试设备的入场与离场交接。"),
        ),
    ),
    DocumentSpec(
        document_key="tuning_duties",
        split="tuning",
        display_name="星桥岗位职责手册.docx",
        blocks=(
            _heading("岗位职责"),
            _table(
                ("角色名称", "核心职责"),
                (
                    "星桥协调员",
                    "核对公开任务目标与交付标准。\n协调验证资源并确认时间安排。\n组织最终验收并保存记录。",
                ),
                (
                    "云台观察员",
                    "登记设备状态。\n复核观测日志。\n归档异常说明。",
                ),
            ),
        ),
    ),
    DocumentSpec(
        document_key="tuning_responsible",
        split="tuning",
        display_name="风铃交付责任规范.docx",
        blocks=(
            _heading("交付件责任"),
            _paragraph("风铃验收清单由资料校验员负责维护。"),
            _paragraph("风铃交接记录由现场协调员负责复核。"),
        ),
    ),
    DocumentSpec(
        document_key="tuning_table",
        split="tuning",
        display_name="银杉设备参数表.docx",
        blocks=(
            _heading("设备参数"),
            _table(
                ("设备代码", "维护周期", "额定载荷", "责任小组"),
                ("XQ-71", "18 天", "240 kg", "银杉维护组"),
                ("ZP-44", "9 天", "80 kg", "蓝芒保障组"),
            ),
        ),
    ),
    DocumentSpec(
        document_key="tuning_process",
        split="tuning",
        display_name="露舟巡检全流程规范.docx",
        blocks=(
            _paragraph("露舟巡检全流程规范"),
            _heading("全流程要求"),
            _heading("3.1 预检登记", 2),
            _paragraph("预检登记阶段需要确认设备编号并登记现场状态。"),
            _heading("3.2 现场核验", 2),
            _paragraph("现场核验阶段需要复核安全边界并记录结果。"),
            _heading("3.3 归档复盘", 2),
            _paragraph("归档复盘阶段需要保存记录并完成问题回顾。"),
            _heading("样品入库流程"),
            _paragraph("样品入库流程包括以下步骤："),
            _list("登记样品编号。", "完成双人复核。", "入柜后保存位置记录。"),
        ),
    ),
    DocumentSpec(
        document_key="tuning_constraints",
        split="tuning",
        display_name="霜叶安全约束手册.docx",
        blocks=(
            _heading("批次约束"),
            _paragraph("霜叶批次 RK-27 的允许偏差为 3%。"),
            _paragraph("霜叶设备严禁在断电状态下继续加热。"),
            _paragraph("霜叶校验规程现行版本为 V2，V1 已废止。"),
        ),
    ),
    DocumentSpec(
        document_key="tuning_source_east",
        split="tuning",
        display_name="东岬交接规范.docx",
        blocks=(_paragraph("潮门登记卡由东岸记录员负责。"),),
    ),
    DocumentSpec(
        document_key="tuning_source_west",
        split="tuning",
        display_name="西岬交接规范.docx",
        blocks=(_paragraph("潮门登记卡由西岸复核员负责。"),),
    ),
)

HOLDOUT_DOCUMENTS = (
    DocumentSpec(
        document_key="holdout_terms",
        split="holdout",
        display_name="棱镜术语手册.docx",
        blocks=(
            _heading("术语定义"),
            _table(
                ("术语", "定义说明"),
                (
                    "NWC",
                    "Network Work Cell，指将一次协同任务的输入、步骤"
                    "和复核记录组成可追踪单元。",
                ),
                (
                    "QRT",
                    "Quality Review Ticket，指记录质量复核结论"
                    "与对应来源的公开票据。",
                ),
            ),
        ),
    ),
    DocumentSpec(
        document_key="holdout_purpose",
        split="holdout",
        display_name="鹭塔备件验收规程.docx",
        blocks=(
            _heading("总则"),
            _heading("目的", 2),
            _paragraph(
                "本规程用于统一鹭塔备件验收口径，并保证每次复核都有完整记录。"
            ),
            _heading("执行要求"),
            _paragraph("执行人员应在当日完成公开验收记录。"),
        ),
    ),
    DocumentSpec(
        document_key="holdout_duties",
        split="holdout",
        display_name="鹭塔岗位职责手册.docx",
        blocks=(
            _heading("岗位职责"),
            _table(
                ("角色名称", "核心职责"),
                (
                    "鹭塔质量协调员",
                    "确认备件验收范围。\n协调复核人员与时间。\n汇总结论并归档记录。",
                ),
                (
                    "岚谷记录员",
                    "登记到场时间。\n维护公开台账。\n提交异常清单。",
                ),
            ),
        ),
    ),
    DocumentSpec(
        document_key="holdout_responsible",
        split="holdout",
        display_name="月湾记录责任规范.docx",
        blocks=(
            _heading("记录责任"),
            _paragraph("月湾采样单由数据审核员负责签字。"),
            _paragraph("月湾封存记录由资产管理员负责保管。"),
        ),
    ),
    DocumentSpec(
        document_key="holdout_table",
        split="holdout",
        display_name="青岚设备参数表.docx",
        blocks=(
            _heading("设备参数"),
            _table(
                ("设备代码", "校准周期", "额定压力", "责任小组"),
                ("HC-52", "21 天", "1.6 MPa", "青岚计量组"),
                ("DV-19", "12 天", "0.8 MPa", "远帆保障组"),
            ),
        ),
    ),
    DocumentSpec(
        document_key="holdout_process",
        split="holdout",
        display_name="青穹复核全流程规范.docx",
        blocks=(
            _paragraph("青穹复核全流程规范"),
            _heading("全流程要求"),
            _heading("4.1 材料接收", 2),
            _paragraph("材料接收阶段需要核对清单并登记时间。"),
            _heading("4.2 双方复核", 2),
            _paragraph("双方复核阶段需要确认差异并记录结论。"),
            _heading("4.3 结果封存", 2),
            _paragraph("结果封存阶段需要归档材料并登记保管位置。"),
            _heading("材料封存流程"),
            _paragraph("材料封存流程包括以下步骤："),
            _list("核对材料页数。", "装入带编号封袋。", "登记封袋保管位置。"),
        ),
    ),
    DocumentSpec(
        document_key="holdout_constraints",
        split="holdout",
        display_name="雾杉运行约束手册.docx",
        blocks=(
            _heading("批次约束"),
            _paragraph("雾杉批次 PM-63 的温度上限为 7 摄氏度。"),
            _paragraph("雾杉装置严禁在舱门开启时启动旋转。"),
            _paragraph("雾杉运行规程现行版本为 R4，R3 已废止。"),
        ),
    ),
    DocumentSpec(
        document_key="holdout_source_north",
        split="holdout",
        display_name="北浦巡查规范.docx",
        blocks=(_paragraph("潮汐巡查表由北浦观察员负责填写。"),),
    ),
    DocumentSpec(
        document_key="holdout_source_south",
        split="holdout",
        display_name="南浦巡查规范.docx",
        blocks=(_paragraph("潮汐巡查表由南浦审核员负责复核。"),),
    ),
)


def _case(  # noqa: PLR0913, PLR0917
    case_id: str,
    split: Split,
    slice_name: str,
    group: str,
    query: str,
    document_keys: tuple[str, ...],
    answer_fragments: tuple[str, ...],
    answer_type: str,
    *,
    source_fragments: tuple[str, ...] | None = None,
    answerable: bool = True,
    forbidden: tuple[str, ...] = (),
) -> QueryCase:
    return QueryCase(
        case_id=case_id,
        split=split,
        slice_name=slice_name,
        paraphrase_group=f"{split}_{group}",
        query=query,
        expected_document_keys=document_keys,
        expected_answer_fragments=answer_fragments,
        expected_source_fragments=(
            answer_fragments if source_fragments is None else source_fragments
        ),
        answerable=answerable,
        expected_answer_type=answer_type,
        forbidden_fragments=forbidden,
    )


def _definition_cases(
    split: Split,
    document_key: str,
    terms: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[QueryCase, ...]:
    templates = ("什么是{term}？", "{term}是什么？", "{term}是啥？")
    cases: list[QueryCase] = []
    for term_index, (term, fragments) in enumerate(terms, 1):
        forbidden = tuple(
            value
            for other_index, (other_term, other_fragments) in enumerate(
                terms, 1
            )
            if other_index != term_index
            for value in (other_term, *other_fragments)
        )
        for query_index, template in enumerate(templates, 1):
            cases.append(
                _case(
                    f"eval_v307_{split}_definition_{term_index}_{query_index}",
                    split,
                    "definition",
                    f"definition_{term_index}",
                    template.format(term=term),
                    (document_key,),
                    fragments,
                    "DEFINITION",
                    forbidden=forbidden,
                )
            )
    return tuple(cases)


def _purpose_cases(
    split: Split,
    document_key: str,
    target: str,
    fragments: tuple[str, ...],
) -> tuple[QueryCase, ...]:
    queries = (
        f"{target}的目的是什么？",
        f"{target}主要是为了什么？",
        f"{target}是为了什么？",
        f"{target}的作用是什么？",
        f"请说明{target}的目的。",
    )
    return tuple(
        _case(
            f"eval_v307_{split}_purpose_{index}",
            split,
            "purpose",
            "purpose",
            query,
            (document_key,),
            fragments,
            "PURPOSE",
        )
        for index, query in enumerate(queries, 1)
    )


def _duties_cases(
    split: Split,
    document_key: str,
    role: str,
    fragments: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> tuple[QueryCase, ...]:
    queries = (
        f"{role}的核心职责是什么？",
        f"{role}负责哪些工作？",
        f"{role}需要承担哪些职责？",
        f"{role}具体干什么？",
        f"{role}平时主要管哪些事？",
        f"{role}是干嘛的？",
    )
    return tuple(
        _case(
            f"eval_v307_{split}_duties_{index}",
            split,
            "duties",
            "duties",
            query,
            (document_key,),
            fragments,
            "DUTIES",
            forbidden=forbidden,
        )
        for index, query in enumerate(queries, 1)
    )


def _responsible_cases(
    split: Split,
    document_key: str,
    document_title: str,
    assignments: tuple[tuple[str, str], tuple[str, str]],
) -> tuple[QueryCase, ...]:
    first_target, first_role = assignments[0]
    second_target, second_role = assignments[1]
    entries = (
        (f"{first_target}谁负责？", first_target, first_role, "first"),
        (
            f"{document_title}中，{first_target}谁负责？",
            first_target,
            first_role,
            "first",
        ),
        (f"谁负责{first_target}？", first_target, first_role, "first"),
        (f"{second_target}谁负责？", second_target, second_role, "second"),
        (
            f"{document_title}里，{second_target}到底谁负责？",
            second_target,
            second_role,
            "second",
        ),
    )
    return tuple(
        _case(
            f"eval_v307_{split}_responsible_{index}",
            split,
            "responsible_party",
            f"responsible_{group}",
            query,
            (document_key,),
            (target, role),
            "RESPONSIBLE_PARTY",
            forbidden=(second_role if group == "first" else first_role,),
        )
        for index, (query, target, role, group) in enumerate(entries, 1)
    )


def _table_cases(
    split: Split,
    document_key: str,
    facts: tuple[tuple[str, str, str], ...],
) -> tuple[QueryCase, ...]:
    cases: list[QueryCase] = []
    for fact_index, (target, relation, value) in enumerate(facts, 1):
        forbidden = tuple(
            other_value
            for other_index, (_target, _relation, other_value) in enumerate(
                facts, 1
            )
            if other_index != fact_index
        )
        queries = (
            f"{target}的{relation}是多少？",
            f"请查一下{target}{relation}是什么？",
        )
        for query_index, query in enumerate(queries, 1):
            cases.append(
                _case(
                    f"eval_v307_{split}_table_{fact_index}_{query_index}",
                    split,
                    "table",
                    f"table_{fact_index}",
                    query,
                    (document_key,),
                    (value,),
                    "FACT",
                    source_fragments=(target, value),
                    forbidden=forbidden,
                )
            )
    return tuple(cases)


def _process_cases(  # noqa: PLR0913, PLR0917
    split: Split,
    document_key: str,
    stage_target: str,
    stages: tuple[str, str, str],
    procedure_target: str,
    steps: tuple[str, str, str],
) -> tuple[QueryCase, ...]:
    entries: tuple[
        tuple[str, tuple[str, ...], str, str, tuple[str, ...]], ...
    ] = (
        (
            f"{stage_target}有哪些阶段？",
            stages,
            "ENUMERATION",
            "stages",
            (),
        ),
        (
            f"{stage_target}从开始到结束都要走哪些阶段？",
            stages,
            "ENUMERATION",
            "stages",
            (),
        ),
        (
            f"{stage_target}有几个阶段？",
            ("共 3 项", *stages),
            "COUNT",
            "stages",
            (),
        ),
        (
            f"{stage_target}的第二阶段是什么？",
            (stages[1],),
            "ORDINAL_ITEM",
            "stage_second",
            (stages[0], stages[2]),
        ),
        (
            f"{procedure_target}该怎么做？",
            steps,
            "PROCEDURE",
            "procedure",
            (),
        ),
        (
            f"{procedure_target}的第三步是什么？",
            (steps[2],),
            "ORDINAL_ITEM",
            "procedure_third",
            (steps[0], steps[1]),
        ),
    )
    return tuple(
        _case(
            f"eval_v307_{split}_process_{index}",
            split,
            "process",
            group,
            query,
            (document_key,),
            tuple(fragments),
            answer_type,
            forbidden=forbidden,
        )
        for index, (
            query,
            fragments,
            answer_type,
            group,
            forbidden,
        ) in enumerate(entries, 1)
    )


def _hard_constraint_cases(  # noqa: PLR0913, PLR0917
    split: Split,
    constraints_key: str,
    batch: str,
    attribute: str,
    actual_value: str,
    wrong_value: str,
    device: str,
    negative_fact: str,
    version_target: str,
    current_version: str,
    source_a_key: str,
    source_a_title: str,
    source_a_role: str,
    source_b_key: str,
    source_b_title: str,
    source_b_role: str,
    artifact: str,
    process_target: str,
) -> tuple[QueryCase, ...]:
    missing_batch = "RK-99" if split == "tuning" else "PM-99"
    entries = (
        _case(
            f"eval_v307_{split}_hard_numeric",
            split,
            "hard_constraint",
            "hard_numeric",
            f"{batch}的{attribute}是多少？",
            (constraints_key,),
            (actual_value,),
            "FACT",
            source_fragments=(batch, actual_value),
        ),
        _case(
            f"eval_v307_{split}_hard_wrong_premise",
            split,
            "hard_constraint",
            "hard_wrong_premise",
            f"{batch}的{attribute}是{wrong_value}吗？",
            (constraints_key,),
            (actual_value,),
            "FACT",
            source_fragments=(batch, actual_value),
        ),
        _case(
            f"eval_v307_{split}_hard_missing_entity",
            split,
            "hard_constraint",
            "hard_missing_entity",
            f"{missing_batch}的{attribute}是多少？",
            (),
            (),
            "FACT",
            answerable=False,
            forbidden=(actual_value, wrong_value),
        ),
        _case(
            f"eval_v307_{split}_hard_missing_attribute",
            split,
            "hard_constraint",
            "hard_missing_attribute",
            f"{batch}的联系电话是多少？",
            (),
            (),
            "FACT",
            answerable=False,
        ),
        _case(
            f"eval_v307_{split}_hard_source_a",
            split,
            "hard_constraint",
            "hard_source_a",
            f"{source_a_title}中，{artifact}谁负责？",
            (source_a_key,),
            (artifact, source_a_role),
            "RESPONSIBLE_PARTY",
        ),
        _case(
            f"eval_v307_{split}_hard_source_b",
            split,
            "hard_constraint",
            "hard_source_b",
            f"{source_b_title}中，{artifact}谁负责？",
            (source_b_key,),
            (artifact, source_b_role),
            "RESPONSIBLE_PARTY",
        ),
        _case(
            f"eval_v307_{split}_hard_wrong_source",
            split,
            "hard_constraint",
            "hard_wrong_source",
            f"不存在的公开规范中，{artifact}谁负责？",
            (),
            (),
            "RESPONSIBLE_PARTY",
            answerable=False,
            forbidden=(source_a_role, source_b_role),
        ),
        _case(
            f"eval_v307_{split}_hard_negation",
            split,
            "hard_constraint",
            "hard_negation",
            f"{device}{negative_fact}？",
            (constraints_key,),
            (negative_fact,),
            "FACT",
        ),
        _case(
            f"eval_v307_{split}_hard_version",
            split,
            "hard_constraint",
            "hard_version",
            f"{version_target}当前有效版本是什么？",
            (constraints_key,),
            (current_version,),
            "FACT",
        ),
        _case(
            f"eval_v307_{split}_hard_ordinal_missing",
            split,
            "hard_constraint",
            "hard_ordinal_missing",
            f"{process_target}的第九阶段是什么？",
            (),
            (),
            "ORDINAL_ITEM",
            answerable=False,
        ),
    )
    return entries


TUNING_CASES = (
    *_definition_cases(
        "tuning",
        "tuning_terms",
        (
            ("LPC", ("Local Process Capsule", "可复查单元")),
            ("MVB", ("Model Verification Bundle", "公开测试包")),
        ),
    ),
    *_purpose_cases(
        "tuning",
        "tuning_purpose",
        "云帆设备交接准则",
        ("统一云帆设备交接步骤", "验收记录能够追溯"),
    ),
    *_duties_cases(
        "tuning",
        "tuning_duties",
        "星桥协调员",
        ("核对公开任务目标", "协调验证资源", "组织最终验收"),
        ("云台观察员", "登记设备状态", "复核观测日志", "归档异常说明"),
    ),
    *_responsible_cases(
        "tuning",
        "tuning_responsible",
        "风铃交付责任规范",
        (
            ("风铃验收清单", "资料校验员"),
            ("风铃交接记录", "现场协调员"),
        ),
    ),
    *_table_cases(
        "tuning",
        "tuning_table",
        (
            ("XQ-71", "维护周期", "18 天"),
            ("XQ-71", "额定载荷", "240 kg"),
            ("ZP-44", "维护周期", "9 天"),
        ),
    ),
    *_process_cases(
        "tuning",
        "tuning_process",
        "露舟巡检全流程",
        ("预检登记", "现场核验", "归档复盘"),
        "样品入库流程",
        ("登记样品编号", "完成双人复核", "入柜后保存位置记录"),
    ),
    *_hard_constraint_cases(
        "tuning",
        "tuning_constraints",
        "RK-27",
        "允许偏差",
        "3%",
        "5%",
        "霜叶设备",
        "严禁在断电状态下继续加热",
        "霜叶校验规程",
        "现行版本为 V2",
        "tuning_source_east",
        "东岬交接规范",
        "东岸记录员",
        "tuning_source_west",
        "西岬交接规范",
        "西岸复核员",
        "潮门登记卡",
        "露舟巡检全流程",
    ),
)

HOLDOUT_CASES = (
    *_definition_cases(
        "holdout",
        "holdout_terms",
        (
            ("NWC", ("Network Work Cell", "可追踪单元")),
            ("QRT", ("Quality Review Ticket", "公开票据")),
        ),
    ),
    *_purpose_cases(
        "holdout",
        "holdout_purpose",
        "鹭塔备件验收规程",
        ("统一鹭塔备件验收口径", "复核都有完整记录"),
    ),
    *_duties_cases(
        "holdout",
        "holdout_duties",
        "鹭塔质量协调员",
        ("确认备件验收范围", "协调复核人员", "汇总结论并归档"),
        ("岚谷记录员", "登记到场时间", "维护公开台账", "提交异常清单"),
    ),
    *_responsible_cases(
        "holdout",
        "holdout_responsible",
        "月湾记录责任规范",
        (
            ("月湾采样单", "数据审核员"),
            ("月湾封存记录", "资产管理员"),
        ),
    ),
    *_table_cases(
        "holdout",
        "holdout_table",
        (
            ("HC-52", "校准周期", "21 天"),
            ("HC-52", "额定压力", "1.6 MPa"),
            ("DV-19", "校准周期", "12 天"),
        ),
    ),
    *_process_cases(
        "holdout",
        "holdout_process",
        "青穹复核全流程",
        ("材料接收", "双方复核", "结果封存"),
        "材料封存流程",
        ("核对材料页数", "装入带编号封袋", "登记封袋保管位置"),
    ),
    *_hard_constraint_cases(
        "holdout",
        "holdout_constraints",
        "PM-63",
        "温度上限",
        "7 摄氏度",
        "9 摄氏度",
        "雾杉装置",
        "严禁在舱门开启时启动旋转",
        "雾杉运行规程",
        "现行版本为 R4",
        "holdout_source_north",
        "北浦巡查规范",
        "北浦观察员",
        "holdout_source_south",
        "南浦巡查规范",
        "南浦审核员",
        "潮汐巡查表",
        "青穹复核全流程",
    ),
)

DOCUMENTS = (*TUNING_DOCUMENTS, *HOLDOUT_DOCUMENTS)
CASES = (*TUNING_CASES, *HOLDOUT_CASES)


def dataset_payload() -> dict[str, object]:
    """返回用于摘要、Review 与 runner 的规范数据集投影。"""
    return {
        "schema_version": "1",
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "content_classification": CONTENT_CLASSIFICATION,
        "gates": GATES,
        "required_slice_counts": REQUIRED_SLICE_COUNTS,
        "documents": [asdict(item) for item in DOCUMENTS],
        "cases": [asdict(item) for item in CASES],
    }


def dataset_sha256() -> str:
    """返回不依赖运行目录的公开数据集摘要。"""
    serialized = json.dumps(
        dataset_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def validate_dataset() -> dict[str, object]:
    """验证 split、case 身份、分组隔离和 V3-07 切片数量。"""
    document_keys = [item.document_key for item in DOCUMENTS]
    case_ids = [item.case_id for item in CASES]
    if len(document_keys) != len(set(document_keys)):
        raise ValueError("V3-07 公开数据集存在重复 document_key。")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("V3-07 公开数据集存在重复 case_id。")
    documents_by_key = {item.document_key: item for item in DOCUMENTS}
    group_splits: dict[str, set[str]] = {}
    slice_counts: dict[str, int] = {}
    split_counts: dict[str, int] = {}
    for case in CASES:
        group_splits.setdefault(case.paraphrase_group, set()).add(case.split)
        slice_counts[case.slice_name] = slice_counts.get(case.slice_name, 0) + 1
        split_counts[case.split] = split_counts.get(case.split, 0) + 1
        if case.answerable and (
            not case.expected_document_keys
            or not case.expected_answer_fragments
            or not case.expected_source_fragments
        ):
            raise ValueError(
                f"{case.case_id}: 可回答 Case 缺少来源或事实标签。"
            )
        if not case.answerable and (
            case.expected_document_keys
            or case.expected_answer_fragments
            or case.expected_source_fragments
        ):
            raise ValueError(f"{case.case_id}: 拒答 Case 禁止绑定答案标签。")
        for key in case.expected_document_keys:
            document = documents_by_key.get(key)
            if document is None or document.split != case.split:
                raise ValueError(f"{case.case_id}: 文档身份未知或跨 split。")
    leaking = sorted(
        group for group, splits in group_splits.items() if len(splits) > 1
    )
    if leaking:
        raise ValueError(f"V3-07 paraphrase group 跨 split：{leaking}")
    if split_counts != {"tuning": 44, "holdout": 44}:
        raise ValueError(f"V3-07 split 数量错误：{split_counts}")
    if slice_counts != REQUIRED_SLICE_COUNTS:
        raise ValueError(f"V3-07 切片数量错误：{slice_counts}")
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": dataset_sha256(),
        "case_count": len(CASES),
        "split_counts": split_counts,
        "slice_counts": slice_counts,
        "answerable_count": sum(item.answerable for item in CASES),
        "unanswerable_count": sum(not item.answerable for item in CASES),
    }


__all__ = [
    "CASES",
    "CONTENT_CLASSIFICATION",
    "DATASET_ID",
    "DATASET_VERSION",
    "DOCUMENTS",
    "GATES",
    "QueryCase",
    "dataset_payload",
    "dataset_sha256",
    "validate_dataset",
]
