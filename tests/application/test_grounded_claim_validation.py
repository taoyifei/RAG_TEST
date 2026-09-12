"""引用有效不代表事实成立：对象、数字、否定和来源结构的反例。"""

from collections.abc import Callable
from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    validate_grounded_draft,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import (
    PolicyDenied,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    ValidationFailed,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    EvidenceSelectionContext,
    ProviderCall,
    QueryAnalysis,
    QueryKind,
    QuerySemantics,
    RequestedAnswerType,
    RetrievalPolicy,
)
from rag_app.core.ports import CancellationPort, GenerationRequest
from rag_app.product.model_settings import (
    KnowledgeBaseModelSettings,
    ProductModelSettings,
)
from tests.application.retrieval.test_descriptive_answers import (
    _candidates,
    _paragraph,
)


@pytest.mark.parametrize(
    "claim,code",
    [
        ("乙部门负责人负责归档，保存 14 天。", "CLAIM_OBJECT_CHANGED"),
        ("甲部门负责人负责归档，保存 15 天。", "CLAIM_NUMBER_UNSUPPORTED"),
        ("甲部门负责人负责归档，保存 14 元。", "CLAIM_NUMBER_UNSUPPORTED"),
        (
            "甲部门负责人负责归档，保存 14 天。可以自行销毁记录。",
            "CLAIM_NEGATION_CHANGED",
        ),
        ("甲部门负责人负责采购天文望远镜。", "CLAIM_TEXT_UNSUPPORTED"),
    ],
)
def test_claim_ids_and_real_quote_do_not_authorize_changed_facts(
    claim: str, code: str
) -> None:
    text = "甲部门负责人负责归档，保存 14 天。不得自行销毁记录。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    assert evidence
    draft = AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == code


def test_grounded_paraphrase_can_combine_same_source_role_and_action() -> None:
    text = "甲部门负责人负责核对交付标准，协调维护资源并组织最终验收。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    claim = "甲部门负责人负责核对标准、协调资源和组织验收。"
    draft = AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )
    validate_grounded_draft(draft, evidence)


def test_purpose_wording_does_not_become_part_of_the_subject() -> None:
    """“用于记录”中的用途连接词不能被误识别为业务对象。"""
    evidence, draft = _supported_draft(
        "上线申请单 | 发布经理 | 包含版本号、变更说明和回滚方案，"
        "作为发布准入凭证",
        "上线申请单用于记录版本号、变更说明和回滚方案，并作为发布准入凭证。",
    )

    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "claim",
    [
        "需要准备测试报告和版本需求确认记录。",
        "上线前需要准备测试报告和版本需求确认记录。",
        "版本上线前还需要准备测试报告和版本需求确认记录。",
        "版本发布前，要准备测试报告和版本需求确认记录。",
    ],
)
def test_action_context_is_not_mistaken_for_a_changed_subject(
    claim: str,
) -> None:
    """口语化时间和动作前缀不是模型新编的业务对象。"""
    evidence, draft = _supported_draft(
        "输入 | 测试报告、版本需求确认记录（灵畿系统）",
        claim,
    )

    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "claim",
    [
        "需要乙部门负责设备维护。",
        "上线前乙部门负责设备维护。",
        "版本上线前乙部门负责设备维护。",
    ],
)
def test_action_context_does_not_hide_a_changed_subject(claim: str) -> None:
    evidence, draft = _supported_draft(
        "甲部门负责设备维护。",
        claim,
    )

    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_OBJECT_CHANGED"


@pytest.mark.parametrize(
    "text,claim,code",
    [
        (
            "甲部门负责人负责归档，保存 14 天。",
            "甲部门负责人负责归档，保存 4 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门负责人不得自行销毁记录。",
            "甲部门负责人无需自行销毁记录。",
            "CLAIM_NEGATION_CHANGED",
        ),
        (
            "甲部门负责设备维护，协调资源并组织验收。",
            "乙部门负责设备维护，协调资源并组织验收。",
            "CLAIM_OBJECT_CHANGED",
        ),
        (
            "甲部门负责人未批准设备维护申请。",
            "甲部门负责人批准设备维护申请。",
            "CLAIM_NEGATION_CHANGED",
        ),
        (
            "设备延时 14 毫秒。",
            "设备延时 14 分钟。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门保存 14 天，乙部门保存 7 天。",
            "甲部门保存 7 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "甲部门保存 14 天，检修间隔 7 天。",
            "甲部门保存 7 天。",
            "CLAIM_NUMBER_UNSUPPORTED",
        ),
        (
            "资料员每周核对设备清单。",
            "资料员每月核对设备清单。",
            "CLAIM_FREQUENCY_UNSUPPORTED",
        ),
    ],
)
def test_grounded_hard_constraints_close_observed_counterexamples(
    text: str,
    claim: str,
    code: str,
) -> None:
    evidence, draft = _supported_draft(text, claim)
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("source", "claim"),
    (
        (
            "协助总经理制定并落实各部门的质量方针和质量目标的分解和实施监督；"
            "负责贯彻总经理的各项决策，协调好各部门的工作，并对总经理负责。",
            "总经理负责贯彻各项决策，协调各部门工作，并对总经理负责。",
        ),
        (
            "协助总经理制定并落实本公司的质量方针和质量目标的分解和实施监督；"
            "指导、协调、监督和检查其分管部门的工作。",
            "总经理协助制定并落实公司的质量方针和质量目标的分解和实施监督；"
            "指导、协调、监督和检查其分管部门的工作。",
        ),
        (
            "主持开好生产调度会、专题会和各种例会，检查督促会议指令的落实情况，"
            "经常深入车间、岗位监督检查工作，抓好车间内部管理，"
            "落实好每月生产经营工作计划，抓好车间成本核算和考核工作。",
            "总经理主持开好生产调度会、专题会和各种例会，"
            "检查督促会议指令的落实情况，经常深入车间、岗位监督检查工作，"
            "抓好车间内部管理，落实好每月生产经营工作计划，"
            "抓好车间成本核算和考核工作。",
        ),
        (
            "副总经理负责组织生产调度。",
            "总经理负责组织生产调度。",
        ),
        (
            "协助 总经理制定质量目标。",
            "总经理制定质量目标。",
        ),
        (
            "副 总经理负责组织生产调度。",
            "总经理负责组织生产调度。",
        ),
    ),
)
def test_role_mentioned_as_object_cannot_be_promoted_to_subject(
    source: str, claim: str
) -> None:
    """职责对象必须来自来源主语，不能只在原文任意位置出现。"""
    evidence, draft = _supported_draft(source, claim)

    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)

    assert error.value.code == "CLAIM_OBJECT_CHANGED"


def test_standalone_role_heading_can_support_its_following_duty() -> None:
    """同一来源组内的独立岗位标题可以为后续职责提供对象。"""
    evidence, draft = _supported_draft(
        "4.1 总经理\n主持质量评审。",
        "总经理主持质量评审。",
    )

    validate_grounded_draft(draft, evidence)


def _general_manager_duty_analysis() -> QueryAnalysis:
    """返回本次回归所需的稳定职责查询语义。"""
    return QueryAnalysis(
        original_query="总经理干嘛的",
        normalized_query="总经理干嘛的",
        semantics=QuerySemantics(
            target="总经理",
            relation="职责",
            answer_type=RequestedAnswerType.DUTIES,
            source="RULE",
        ),
        conversation_fingerprint=canonical_sha256({"conversation": []}),
    )


@pytest.mark.parametrize(
    "claim",
    ("主持生产调度会。", "生产经理主持生产调度会。"),
)
def test_duty_claim_must_name_the_requested_role(claim: str) -> None:
    """候选原文真实也不能回答另一个岗位的职责问题。"""
    evidence, draft = _supported_draft(
        "4.7 生产经理\n主持生产调度会。",
        claim,
    )

    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(
            draft,
            evidence,
            analysis=_general_manager_duty_analysis(),
        )

    assert error.value.code == "CLAIM_QUERY_TARGET_MISMATCH"


@pytest.mark.parametrize(
    "text,claim",
    [
        (
            "甲部门负责人未批准设备维护申请。",
            "甲部门负责人未批准设备维护申请。",
        ),
        ("甲部门负责人负责归档，不得自行销毁记录。", "甲部门负责人负责归档。"),
        ("甲部门负责人不得自行销毁记录。", "甲部门负责人禁止自行销毁记录。"),
        (
            "甲部门负责设备维护，协调资源并组织验收。",
            "甲部门负责设备维护、协调资源和组织验收。",
        ),
    ],
)
def test_grounded_negation_stays_bound_to_its_action_and_keeps_paraphrases(
    text: str,
    claim: str,
) -> None:
    evidence, draft = _supported_draft(text, claim)
    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "text,claim",
    [
        (
            "现有协作类型，依据不同业务需求与整理方式，"
            "分为资料整理、需求分析两类。",
            "现有协作类型包括资料整理和需求分析。",
        ),
        (
            "质量主管负责不断优化核验流程。",
            "质量主管负责优化核验流程。",
        ),
        (
            "维护主管负责制定未来设备维护计划。",
            "维护主管负责制定设备维护计划。",
        ),
        (
            "维护主管负责无线设备的检修。",
            "维护主管负责检修无线设备。",
        ),
        (
            "质量主管不仅负责核对标准，还负责协调验收。",
            "质量主管负责核对标准和协调验收。",
        ),
    ],
)
def test_ordinary_words_do_not_negate_enumerations_or_duties(
    text: str, claim: str
) -> None:
    evidence, draft = _supported_draft(text, claim)
    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "text,claim",
    [
        ("维护主管不得检修无线设备。", "维护主管负责检修无线设备。"),
        ("维护主管未批准未来维护计划。", "维护主管批准未来维护计划。"),
        ("质量主管不负责优化核验流程。", "质量主管负责优化核验流程。"),
        ("维护主管无设备维护权限。", "维护主管有设备维护权限。"),
        ("两种维护方式不同。", "两种维护方式相同。"),
        ("质量主管不同意核验方案。", "质量主管同意核验方案。"),
        ("两台设备不同步运行。", "两台设备同步运行。"),
        ("维护主管不断开设备电源。", "维护主管断开设备电源。"),
        ("核验调查暂无线索。", "核验调查已有线索。"),
    ],
)
def test_actual_negations_and_different_relation_remain_constrained(
    text: str, claim: str
) -> None:
    evidence, draft = _supported_draft(text, claim)
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_NEGATION_CHANGED"


def test_wireless_subject_cannot_be_replaced_by_another_device() -> None:
    evidence, draft = _supported_draft(
        "无线设备负责保存维护记录。", "有线设备负责保存维护记录。"
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_OBJECT_CHANGED"


@pytest.mark.parametrize(
    "action",
    [
        "牵头组织验收",
        "主要负责归档",
        "直接负责归档",
        "统一协调资源",
        "共同承担维护",
        "定期检查设备",
    ],
)
@pytest.mark.parametrize("subject", ["", "质量主管", "资料专员"])
def test_action_modifiers_are_not_part_of_the_subject(
    action: str, subject: str
) -> None:
    role = subject or "质量主管"
    evidence, draft = _supported_draft(
        f"{role}负责以下事项：{action}。", f"{subject}{action}。"
    )
    validate_grounded_draft(draft, evidence)


def _table_role_draft(
    claim: str,
    *,
    other_row: bool = False,
    other_table: bool = False,
    cite_role: bool = True,
) -> tuple[tuple[EvidenceItem, ...], AnswerDraft]:
    texts = (
        "质量主管",
        "牵头组织验收，定期检查设备。保存 14 天，不得销毁记录。",
    )
    items: list[EvidenceItem] = []
    for index, text in enumerate(texts):
        evidence, _ = _supported_draft(text, text)
        item = evidence[0]
        path = (
            "body",
            f"tbl:{int(other_table and index == 1)}",
            f"tr:{int(other_row and index == 1)}",
            f"tc:{index}",
            "p:0",
        )
        span = item.source_spans[0]
        assert span.source_anchor is not None
        span = span.model_copy(
            update={
                "structural_path": path,
                "source_anchor": span.source_anchor.model_copy(
                    update={"structural_path": path}
                ),
            }
        )
        items.append(
            item.model_copy(
                update={
                    "evidence_id": f"S{index + 1}",
                    "source_spans": (span,),
                    "table_context": True,
                    "table_locator": "public-table",
                    "document_version_id": "dver_" + "1" * 32,
                    "section_id": "public-section",
                }
            )
        )
    supports = tuple(
        ClaimSupport(support_id=item.support_id, quote=item.citation_text)
        for item in items
        if cite_role or item.support_id == "S2"
    )
    return tuple(items), AnswerDraft(
        text=claim,
        cited_evidence_ids=tuple(support.support_id for support in supports),
        claims=(AnswerClaim(text=claim, supports=supports),),
        generation_mode="llm",
    )


@pytest.mark.parametrize(
    "claim",
    [
        "质量主管牵头组织验收，定期检查设备。",
        "质量主管负责组织验收和检查设备。",
        "牵头组织验收。定期检查设备。",
    ],
)
def test_same_table_row_supports_natural_merged_and_separate_duties(
    claim: str,
) -> None:
    evidence, draft = _table_role_draft(claim)
    validate_grounded_draft(draft, evidence)


@pytest.mark.parametrize(
    "claim,code",
    [
        ("王某组织验收。", "CLAIM_OBJECT_CHANGED"),
        ("王某牵头组织验收。", "CLAIM_OBJECT_CHANGED"),
        ("其他负责人负责检查设备。", "CLAIM_OBJECT_CHANGED"),
        ("其他负责人定期检查设备。", "CLAIM_OBJECT_CHANGED"),
        ("临时主管主要负责组织验收。", "CLAIM_OBJECT_CHANGED"),
        ("质量主管批准专项采购经费。", "CLAIM_TEXT_UNSUPPORTED"),
        ("质量主管保存 15 天。", "CLAIM_NUMBER_UNSUPPORTED"),
        ("质量主管可以销毁记录。", "CLAIM_NEGATION_CHANGED"),
    ],
)
def test_role_duty_join_does_not_authorize_changed_facts(
    claim: str, code: str
) -> None:
    evidence, draft = _table_role_draft(claim)
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == code


@pytest.mark.parametrize("location", ["other_row", "other_table"])
def test_role_and_duty_cannot_join_different_table_rows(location: str) -> None:
    evidence, draft = _table_role_draft(
        "质量主管负责组织验收。", **{location: True}
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_SOURCE_MISMATCH"


def test_each_clause_can_bind_to_its_own_cited_source_group() -> None:
    """一条回答可串联多个独立事实，但每个分句必须有自己的完整来源。"""
    table_evidence, _ = _table_role_draft("质量主管负责组织验收。")
    paragraph_evidence, _ = _supported_draft(
        "需求发起方提交核心材料，完成项目报备登记。",
        "需求发起方完成项目报备登记。",
    )
    paragraph = paragraph_evidence[0].model_copy(update={"evidence_id": "S3"})
    supports = (
        *(
            ClaimSupport(
                support_id=item.support_id,
                quote=item.citation_text,
            )
            for item in table_evidence
        ),
        ClaimSupport(
            support_id=paragraph.support_id,
            quote=paragraph.citation_text,
        ),
    )
    draft = AnswerDraft(
        text=("质量主管负责组织验收；需求发起方完成项目报备登记。"),
        cited_evidence_ids=tuple(support.support_id for support in supports),
        claims=(
            AnswerClaim(
                text=("质量主管负责组织验收；需求发起方完成项目报备登记。"),
                supports=supports,
            ),
        ),
        generation_mode="llm",
    )

    validate_grounded_draft(draft, (*table_evidence, paragraph))


def test_complete_source_group_is_not_rejected_by_an_extra_citation() -> None:
    """多引一个来源不能推翻已由单一表格行完整支持的事实。"""
    table_evidence, _ = _table_role_draft("质量主管负责组织验收。")
    paragraph_evidence, _ = _supported_draft(
        "需求发起方完成项目报备登记。",
        "需求发起方完成项目报备登记。",
    )
    paragraph = paragraph_evidence[0].model_copy(update={"evidence_id": "S3"})
    supports = tuple(
        ClaimSupport(
            support_id=item.support_id,
            quote=item.citation_text,
        )
        for item in (*table_evidence, paragraph)
    )
    draft = AnswerDraft(
        text="质量主管负责组织验收。",
        cited_evidence_ids=tuple(support.support_id for support in supports),
        claims=(
            AnswerClaim(
                text="质量主管负责组织验收。",
                supports=supports,
            ),
        ),
        generation_mode="llm",
    )

    validate_grounded_draft(draft, (*table_evidence, paragraph))


def test_subject_and_action_cannot_be_borrowed_across_paragraphs() -> None:
    """同一章节的两个段落也不能分别借出对象和动作来拼事实。"""
    role_evidence, _ = _supported_draft("甲部门负责人。", "甲部门负责人。")
    action_evidence, _ = _supported_draft("负责归档。", "负责归档。")
    role = role_evidence[0].model_copy(update={"evidence_id": "S1"})
    action = action_evidence[0].model_copy(
        update={
            "evidence_id": "S2",
            "document_version_id": role.document_version_id,
            "section_id": role.section_id,
        }
    )
    supports = (
        ClaimSupport(support_id="S1", quote=role.citation_text),
        ClaimSupport(support_id="S2", quote=action.citation_text),
    )
    draft = AnswerDraft(
        text="甲部门负责人负责归档。",
        cited_evidence_ids=("S1", "S2"),
        claims=(
            AnswerClaim(
                text="甲部门负责人负责归档。",
                supports=supports,
            ),
        ),
        generation_mode="llm",
    )

    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, (role, action))
    assert error.value.code == "CLAIM_SOURCE_MISMATCH"


def test_uncited_role_is_not_borrowed_from_question_or_other_evidence() -> None:
    evidence, draft = _table_role_draft(
        "质量主管负责组织验收。", cite_role=False
    )
    with pytest.raises(ValidationFailed) as error:
        validate_grounded_draft(draft, evidence)
    assert error.value.code == "CLAIM_OBJECT_CHANGED"


def test_validation_upgrade_does_not_reuse_legacy_fallback_cache() -> None:
    settings = KnowledgeBaseModelSettings()
    legacy_identity = canonical_sha256(
        {
            "settings": settings.model_dump(),
            "prompt": "grounded-chat-v1",
            "rewrite": "bounded-rewrite-v3",
            "validation": "claim-support-v2",
        }
    )
    models = ProductModelSettings(Mock(), Mock())
    assert models.serving_identity(settings) != legacy_identity
    assert settings == KnowledgeBaseModelSettings()


def test_role_validation_upgrade_invalidates_previous_answer_cache() -> None:
    """对象主语与职责目标合同升级后不能复用上一版回答缓存。"""
    settings = KnowledgeBaseModelSettings()
    previous_identity = canonical_sha256(
        {
            "settings": settings.model_dump(),
            "prompt": "grounded-chat-v2",
            "interpret": "bounded-interpret-v1",
            "rewrite": "bounded-rewrite-v3",
            "validation": "claim-support-v4",
            "answer_selection": "shared-query-semantics-v1",
        }
    )

    models = ProductModelSettings(Mock(), Mock())

    assert models.serving_identity(settings) != previous_identity


def _supported_draft(
    text: str, claim: str
) -> tuple[tuple[EvidenceItem, ...], AnswerDraft]:
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(text)), RetrievalPolicy()
    )
    return evidence, AnswerDraft(
        text=claim,
        cited_evidence_ids=(evidence[0].support_id,),
        claims=(
            AnswerClaim(
                text=claim,
                supports=(
                    ClaimSupport(support_id=evidence[0].support_id, quote=text),
                ),
            ),
        ),
        generation_mode="llm",
    )


def test_changed_role_repairs_before_stream_publish() -> None:
    """首条职责偷换不能形成 partial 前缀，修复尝试仍可安全发布。"""
    wrong_source = (
        "协助总经理制定并落实各部门的质量方针和质量目标；"
        "负责贯彻总经理的各项决策，协调好各部门的工作，并对总经理负责。"
    )
    wrong_evidence, wrong_draft = _supported_draft(
        wrong_source,
        "总经理负责贯彻各项决策，协调各部门工作，并对总经理负责。",
    )
    correct_source = "4.1 总经理\n主持质量评审。"
    correct_evidence, _ = _supported_draft(
        correct_source,
        "总经理主持质量评审。",
    )
    correct_item = correct_evidence[0].model_copy(update={"evidence_id": "S2"})
    correct_claim = AnswerClaim(
        text="总经理主持质量评审。",
        supports=(ClaimSupport(support_id="S2", quote=correct_source),),
    )
    correct_draft = AnswerDraft(
        text=correct_claim.text,
        cited_evidence_ids=("S2",),
        claims=(correct_claim,),
        generation_mode="llm",
    )
    drafts = iter((wrong_draft, correct_draft))
    requests: list[GenerationRequest] = []

    def generate_stream(
        request: GenerationRequest,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: CancellationPort,
    ) -> AnswerDraft:
        assert not cancellation.is_cancelled()
        requests.append(request)
        draft = next(drafts)
        on_claim(draft.claims[0])
        return draft

    generator = Mock()
    generator.generate_stream.side_effect = generate_stream
    cancellation = Mock()
    cancellation.is_cancelled.return_value = False
    emitted: list[AnswerClaim] = []

    outcome = GroundedAnsweringService(generator).answer(
        "总经理干嘛的",
        (*wrong_evidence, correct_item),
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        analysis=_general_manager_duty_analysis(),
        on_claim=emitted.append,
        cancellation=cancellation,
    )

    assert emitted == [correct_claim]
    assert outcome.answer == "总经理主持质量评审。 [S2]"
    assert outcome.reason_code == "CLAIMS_VALIDATED"
    assert len(requests) == 2
    assert requests[0].repair_reason is None
    assert requests[1].repair_reason == "CLAIM_OBJECT_CHANGED"


def test_invalid_claim_gets_only_one_repair_and_preserves_both_calls() -> None:
    evidence, invalid = _supported_draft(
        "甲部门保存 14 天。", "甲部门保存 4 天。"
    )
    call = ProviderCall(
        provider_id="synthetic",
        operation="generation",
        call_count=1,
        retry_count=0,
        elapsed_ms=1,
    )
    invalid = invalid.model_copy(update={"provider_calls": (call,)})
    generator = Mock()
    generator.generate.return_value = invalid
    service = GroundedAnsweringService(generator)
    outcome = service.answer(
        "保存多久",
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
    )
    assert outcome.answer is None
    assert generator.generate.call_count == 2
    assert generator.generate.call_args_list[0].args[0].repair_reason is None
    assert (
        generator.generate.call_args_list[1].args[0].repair_reason
        == "CLAIM_NUMBER_UNSUPPORTED"
    )
    assert len(outcome.calls) == 2
    assert outcome.answer is None
    assert outcome.mode == "none"
    assert outcome.reason_code == "CLAIM_NUMBER_UNSUPPORTED"


def _fact_analysis() -> QueryAnalysis:
    """返回不携带私有内容的稳定 FACT 查询分析。"""
    return QueryAnalysis(
        original_query="合成设备的保管期限是多少？",
        normalized_query="合成设备的保管期限是多少?",
        semantics=QuerySemantics(
            target="合成设备",
            relation="保管期限",
            answer_type=RequestedAnswerType.FACT,
            source="RULE",
        ),
        conversation_fingerprint=canonical_sha256({"conversation": []}),
    )


def _verified_fact_evidence() -> tuple[EvidenceItem, ...]:
    """让公开事实夹具经过与生产一致的直接支持判定。"""
    analysis = _fact_analysis()
    return EvidenceAssembler().assemble(
        _candidates(_paragraph("合成设备的保管期限为 14 天。")),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
        ),
    )


def _abstained_draft() -> AnswerDraft:
    """构造已实际调用但没有形成 claim 的模型拒答。"""
    return AnswerDraft(
        text="现有资料不足以支持该问题的回答。",
        cited_evidence_ids=(),
        generation_mode="llm",
        reason_code="GENERATION_ABSTAINED",
    )


@pytest.mark.parametrize(
    ("outcome", "expected_reason", "expected_calls"),
    (
        (_abstained_draft(), "GENERATION_ABSTAINED", 1),
        (
            ProviderUnavailable(
                "上游超时。",
                stage="generation",
                code="PROVIDER_TIMEOUT",
            ),
            "PROVIDER_TIMEOUT",
            1,
        ),
        (
            ProviderRateLimited("上游限流。", stage="generation"),
            "PROVIDER_RATE_LIMITED",
            1,
        ),
        (
            PolicyDenied(
                "资料未获授权。",
                stage="generation.authorization",
                code="DATA_EGRESS_NOT_AUTHORIZED",
            ),
            "DATA_EGRESS_NOT_AUTHORIZED",
            1,
        ),
        (
            ProviderInvalidResponse(
                "模型 JSON 无效。",
                stage="generation",
                code="GENERATION_JSON_INVALID",
            ),
            "GENERATION_JSON_INVALID",
            2,
        ),
    ),
)
def test_model_failures_refuse_even_when_support_is_complete(
    outcome: AnswerDraft | Exception,
    expected_reason: str,
    expected_calls: int,
) -> None:
    """模型阻断或失败时，完整本地支持也不能绕过真实生成。"""
    evidence = _verified_fact_evidence()
    assert evidence
    generator = Mock()
    if isinstance(outcome, Exception):
        generator.generate.side_effect = outcome
    else:
        generator.generate.return_value = outcome
    service = GroundedAnsweringService(generator)

    result = service.answer(
        "合成设备的保管期限是多少？",
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        answer_support_set=evidence,
        analysis=_fact_analysis(),
    )

    assert result.mode == "none"
    assert result.reason_code == expected_reason
    assert result.answer is None
    assert result.published_support_ids == ()
    assert generator.generate.call_count == expected_calls
    request = generator.generate.call_args_list[0].args[0]
    assert request.typed_semantics == _fact_analysis().semantics
    assert request.answer_support_set == evidence
    assert request.model_evidence_candidates == evidence


def test_model_failure_refuses_when_support_set_is_incomplete() -> None:
    """相关候选不能在 Provider 失败后被本地 renderer 冒充为支持。"""
    candidates, _ = _supported_draft(
        "合成设备的维护手册已归档。",
        "合成设备的维护手册已归档。",
    )
    generator = Mock()
    generator.generate.side_effect = ProviderUnavailable(
        "上游超时。",
        stage="generation",
        code="PROVIDER_TIMEOUT",
    )
    service = GroundedAnsweringService(generator)

    result = service.answer(
        "合成设备的保管期限是多少？",
        candidates,
        ConfidenceDecision(
            status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
            score=0.0,
        ),
        answer_support_set=(),
        analysis=_fact_analysis(),
    )

    assert result.answer is None
    assert result.mode == "none"
    assert result.reason_code == "PROVIDER_TIMEOUT"
    assert result.published_support_ids == ()
    assert generator.generate.call_count == 1
