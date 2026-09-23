"""有界资料生成、逐条来源核验与一次修复，不将引用 ID 当事实证明。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from rag_app.application.answering.atom_semantics import current_atom_analysis
from rag_app.application.answering.evidence_binding import (
    BoundClaim,
    EvidenceBindingError,
    bind_wire_claim,
    revalidate_bound_claim,
)
from rag_app.application.answering.natural_renderer import (
    MissingAtomReason,
    ValidatedNaturalClaim,
    render_natural_answer,
    resolve_conflict_support_ids,
)
from rag_app.application.answering.ocr_guard import (
    claim_pdf_visual_evidence,
    critical_ocr_atoms,
)
from rag_app.application.answering.plan_coverage import (
    AnswerPlanContractError,
    CompiledPlanCoverage,
    ValidatedPlanArtifact,
    reduce_plan_coverage,
)
from rag_app.application.answering.qualifier_evidence import evaluate_qualifier
from rag_app.application.answering.request_relation import (
    RequestRelationStatus,
    RequestRelationUndetermined,
    decide_request_relation,
    has_relation_review,
    record_relation_review,
    relation_review_key,
    relation_review_scope,
)
from rag_app.application.answering.semantic_validation import (
    SemanticValidationCandidate,
    SemanticValidationPayload,
    SemanticValidationRequest,
    SemanticValidationResponse,
    normalized_semantic_results,
)
from rag_app.application.answering.source_projection import (
    SourceProjectionError,
    project_bound_claim,
)
from rag_app.application.answering.target_coverage import (
    target_member_coverage,
)
from rag_app.core.errors import (
    ProviderInvalidResponse,
    QueryCancelled,
    RagError,
    StreamDeliveryError,
    ValidationFailed,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    AtomFactBinding,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    FieldResolutionStatus,
    GroundedWireDiagnostic,
    OcrVerificationState,
    PhysicalTableFact,
    ProviderCall,
    QualifierStatus,
    QueryAnalysis,
    RequestedAnswerType,
)
from rag_app.core.models.common import JsonObject, freeze_json_object
from rag_app.core.models.evidence_group import EvidenceGroup
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
    SourceContentRequirement,
)
from rag_app.core.models.relation_review import (
    RelationReviewCandidate,
    RelationReviewPayload,
    RelationReviewRequest,
    RelationReviewResponse,
    review_context_support_ids,
    validate_review_payload,
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
    named_table_label_in_query,
    normalize_catalog_label,
    normalize_role_owner_text,
    normalize_semantic_text,
    section_heading_path_owns_target,
)
from rag_app.core.source_compatibility import (
    certified_source_group,
    compatible_partitions,
    source_compatibility,
    source_group_contains,
    source_group_covered,
    source_group_keys,
    table_cell_coordinate,
)
from rag_app.core.source_scope import evidence_allowed_for_atom

if TYPE_CHECKING:
    from rag_app.application.answering.executor import AnswerExecutionResult
    from rag_app.application.retrieval.generation_evidence import (
        GenerationEvidencePack,
    )
    from rag_app.core.models.answer_plan import (
        CompiledAnswerPlan,
        ResolvedQueryView,
    )

_QUANTITY_UNIT_ATOM = (
    r"(?:%|％|万元|亿元|元|毫秒|分钟|小时|秒|天|周|个月|年|月|"
    r"毫米|厘米|千米|米|公斤|千克|毫克|克|吨|升|毫升|次|个|"
    r"台|件|人|双|套|副|只|张|支|瓶|组|批|份|条|顶|块|辆|"
    r"艘|架|门|床|℃|[A-Za-zμµΩ°]+)"
)
_TABLE_INTERSECTION_MIN_SPANS = 3
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
_BOUND_CONDITION = re.compile(
    r"(?:在|当)(?P<scope>[^，,。；;]{2,32}?)(?P<end>之前|之后|时|前|后)"
    r"(?=[，,]|可以|应|须|需|必|负责|完成|提交|启动|归档|可)"
)
_STAGE_SCOPE = re.compile(
    r"(?:^|[，,。；;：:\s]|在)"
    r"(?P<scope>[^，,。；;：:\s在]{1,20}?(?:阶段|期间|环节))"
)
_PARENTHETICAL_LEVEL = re.compile(
    r"[（(]\s*[IVXivxⅠⅡⅢⅣⅤⅥ\d一二三四五六七八九十]+\s*级\s*[）)]"
)
_EXPLICIT_LEVEL = re.compile(r"[IVXivxⅠⅡⅢⅣⅤⅥ一二三四五六七八九十]{1,4}级")
_MODALITY_CLASSES = (
    ("MUST", re.compile(r"必须|(?<!不)须(?!要)")),
    ("SHOULD", re.compile(r"应当|应予|应该|应(?=\s|[，。：；,;])")),
    ("NEED", re.compile(r"(?<!无)需要|(?<!无)需(?!求|方|量)")),
    ("MAY", re.compile(r"可以|允许|可(?=\s|[，。：；,;])")),
)
_YES_NO_ACTION_FOCUS = re.compile(
    r"(?:是否|需不需要|要不要|需要|应当|应该|必须|可以|要|能)"
    r"(?P<focus>[\u4e00-\u9fff]{2,8})(?:吗|么)[？?]?$"
)
_SOURCE_SENTENCE_END = re.compile(r"[。！？!?；;]")
_SHORT_QUERY_FOCUS_LENGTH = 2
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
_FALLBACK_MIN_TABLE_ITEMS = 2
_FALLBACK_MAX_ORDINARY_EXCERPTS = 3
_FALLBACK_TABLE_LABEL_MIN_CHARS = 3
_FALLBACK_TABLE_LABEL_MAX_CHARS = 24
_FALLBACK_TABLE_SHORT_NAME_MIN_CHARS = 4
_FALLBACK_TABLE_SHORT_NAME_SUFFIX_CHARS = 2
_FALLBACK_NODE_MIN_EXCERPTS = 2
_FALLBACK_PREDECESSOR_MAX_GAP = 4
_FALLBACK_PREDECESSOR_LIMIT = 2
_FALLBACK_MIN_QUESTION_ANCHOR_CHARS = 3
_FALLBACK_LONG_QUESTION_CHARS = 10
_FALLBACK_SHORT_QUESTION_CHARS = 14
_FALLBACK_SEQUENCE_MIN_PROCEDURE_MEMBERS = 2
_FALLBACK_SEQUENCE_MIN_NUMBERED_MEMBERS = 3
_FALLBACK_FOCUSED_MIN_CHARS = 6
_FALLBACK_TABLE_ACTION_MIN_CHARS = 12
_FALLBACK_TABLE_SUBJECT_MIN_CHARS = 3
_CONTEXT_SOURCE_MIN_MATCH_CHARS = 4
_CONTEXT_SOURCE_MIN_LEAD_CHARS = 2
_FALLBACK_DURATION = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个工作日|工作日|天|日|周|个月|月|年|小时|分钟)"
)
_FALLBACK_LIST_MARKER = re.compile(
    r"^[（(]?[一二三四五六七八九十\d]+[）).、]?$"
)
_FALLBACK_ORPHAN_HEADING = re.compile(r"^[、，:：]\s*[^，。；;！？!?]{1,24}$")
# 引用、对象、数字、频率与否定另有独立硬门。这里仅要求自然改写与
# 来源谓语保留基本词面联系，避免把同义概括误判成无支持事实。
_MIN_SUPPORTED_BIGRAM_RATIO = 0.20
_MIN_COMPLETE_FACT_BIGRAM_RATIO = 0.60
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
    r"(?:负责人|经理|主管|专员|工程师|部门|人员|设备|系统|模式|实验室|中心|部|组))"
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
_POST_SUBJECT_CONTEXT = r"(?:在[^，,。；;！？?]{1,24})?"
_MODAL_ACTION = re.compile(
    r"^(?:准备|整理|提交|上传|填报|填写|确认|补充|完成|检查|核对|"
    r"提供|记录|归档|执行)"
)
_ENTITY_SUBJECT = re.compile(
    r"^\s*((?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|实验室|用户|客户|人员|岗位|角色|小组|组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库|部)))"
    rf"\s*(?={_POST_SUBJECT_CONTEXT}(?:(?:应当|必须|可以|应|须|需|可|已)?"
    r"(?:不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|尚未|没有|未|无|不)?"
    rf"(?:{_ACTION_MODIFIER})?(?:{_DUTY_ACTION_VERB}))"
    r"|的(?:核心)?职责|的(?:维护)?周期)"
)
_STANDALONE_SUBJECT = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]某|"
    r"[\u4e00-\u9fff]{1,24}?(?:负责人|经理|主管|专员|工程师|部门|团队|"
    r"单位|机构|公司|中心|实验室|用户|客户|人员|岗位|角色|小组|组|委员会|平台|"
    r"服务|应用|模块|组件|设备|系统|模式|库|管代|部))"
)
_SECTION_NUMBER_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\s*")
_LEADING_LIST_MARKER = re.compile(
    r"^\s*(?:(?:[（(]?(?:\d+(?:\.\d+)*|[A-Za-z])\s*[.)、）]\s*)|"
    r"(?:[-*+•‣◦⁃●○▪▫–—]\s+))"
)
_LEADING_SECTION_MARKER = re.compile(
    r"^\s*[（(][一二三四五六七八九十百千\d]+[）)]\s*"
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


@dataclass(frozen=True, slots=True)
class ClaimRejectionDiagnostic:
    """不含正文的逐 Claim 原始拒绝诊断。"""

    atom_id: str
    raw_reason_code: str
    public_reason_code: str
    validator_stage: str
    validator: str
    selected_support_ids: tuple[str, ...]
    allowed_support_ids: tuple[str, ...]
    claim_sha256: str
    quote_sha256s: tuple[str, ...]
    origin_module: str | None = None
    origin_function: str | None = None
    origin_line: int | None = None


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
    relation_review_calls: int = 0
    relation_review_elapsed_ms: float = 0.0
    relation_review_skip_reason: str | None = None
    relation_review_results: tuple[tuple[str, str, str], ...] = ()
    semantic_review_facets: tuple[JsonObject, ...] = ()
    target_member_coverage: tuple[JsonObject, ...] = ()
    claim_rejection_codes: tuple[tuple[str, int], ...] = ()
    generated_claim_count: int = 0
    accepted_claim_count: int = 0
    published_claim_count: int = 0
    generation_gap_count: int = 0
    missing_atom_reasons: tuple[tuple[str, str], ...] = ()
    false_limited_detected: bool = False
    accepted_support_ids: tuple[str, ...] = ()
    claim_rejection_diagnostics: tuple[ClaimRejectionDiagnostic, ...] = ()
    extractive_fallback_reason: str | None = None
    prepared_packets: tuple[PreparedGenerationPacket, ...] = ()
    repair_attempted: bool = False
    repair_skip_reason: str | None = None
    raw_failures: tuple[tuple[str, str], ...] = ()
    recovery_results: tuple[tuple[str, str, str], ...] = ()
    wire_diagnostics: tuple[GroundedWireDiagnostic, ...] = ()
    source_projection_records: tuple[JsonObject, ...] = ()
    answer_plan_id: str | None = None
    answer_plan_revision: str | None = None
    answer_plan_records: tuple[JsonObject, ...] = ()
    answer_plan_coverage: tuple[JsonObject, ...] = ()


def _compiled_coverage_records(
    coverage: CompiledPlanCoverage,
) -> tuple[JsonObject, ...]:
    """把纯覆盖结果投影为不含正文的 SAFE 诊断。"""
    return tuple(
        freeze_json_object(
            {
                "obligation_id": item.obligation_id,
                "status": item.status,
                "covered_member_keys": item.covered_member_keys,
                "missing_member_keys": item.missing_member_keys,
                "satisfied_qualifier_ids": item.satisfied_qualifier_ids,
                "missing_qualifier_ids": item.missing_qualifier_ids,
                "satisfied_qualifier_keys": item.satisfied_qualifier_keys,
                "missing_qualifier_keys": item.missing_qualifier_keys,
                "source_closed": item.source_closed,
            }
        )
        for item in coverage.obligations
    )


def _compiled_plan_records(
    plan: CompiledAnswerPlan,
    execution: AnswerExecutionResult,
) -> tuple[JsonObject, ...]:
    """记录冻结图和确定性执行身份，不记录问题、来源或答案正文。"""
    compiled = freeze_json_object(
        {
            "event": "ANSWER_PLAN_COMPILED",
            "plan_id": plan.plan_id,
            "snapshot_id": plan.snapshot_id,
            "schema_revision": plan.schema_revision,
            "policy_revision": plan.policy_revision,
            "obligation_ids": tuple(
                item.obligation_id for item in plan.obligations
            ),
            "selection_digests": tuple(
                item.selection_digest for item in plan.selections
            ),
            "field_resolutions": tuple(
                (
                    item.selection_digest,
                    item.field_candidate_id,
                    item.field_resolution_status.value,
                    item.field_resolution_reason,
                    item.field_query_span,
                )
                for item in plan.selections
            ),
            "qualifier_requirements": tuple(
                (
                    *qualifier.qualifier_key,
                    qualifier.kind.value,
                    qualifier.subject,
                    qualifier.action,
                    qualifier.selected_member_refs,
                    qualifier.event_anchor,
                    qualifier.polarity,
                    qualifier.conditions,
                )
                for obligation in plan.obligations
                for qualifier in obligation.qualifiers
            ),
            "task_modes": tuple(
                item.mode.value for item in plan.physical_tasks
            ),
        }
    )
    executed = tuple(
        freeze_json_object(
            {
                "event": record.origin,
                "task_id": record.task_id,
                "plan_id": record.plan_id,
                "selection_digest": record.selection_digest,
                "source_digest": record.source_digest,
                "checked_support_ids": record.checked_support_ids,
                "published_member_keys": record.published_member_keys,
                "satisfied_qualifier_ids": record.satisfied_qualifier_ids,
                "satisfied_qualifier_keys": (record.satisfied_qualifier_keys),
                "qualifier_results": tuple(
                    (
                        *result.qualifier_key,
                        result.status.value,
                        result.covered_member_keys,
                        result.locator_support_ids,
                        tuple(
                            reference.support_id
                            for reference in result.proof_references
                        ),
                        result.source_conditions,
                        result.reason_code,
                    )
                    for result in record.qualifier_results
                ),
                "claim_sha256": record.claim_sha256,
            }
        )
        for record in execution.records
    )
    return (compiled, *executed)


def _generation_plan_artifacts(
    plan: CompiledAnswerPlan,
    claims: tuple[ValidatedNaturalClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    *,
    skip_claim_ids: frozenset[str],
) -> tuple[ValidatedPlanArtifact, ...]:
    """把已完成旧硬门的 G 路径事实绑定回冻结开放义务。"""
    registry = {item.support_id: item for item in evidence}
    artifacts: list[ValidatedPlanArtifact] = []
    usable_claims = tuple(
        item for item in claims if item.claim_id not in skip_claim_ids
    )
    for obligation in plan.obligations:
        if obligation.selection_ids:
            continue
        related = tuple(
            item
            for item in usable_claims
            if set(obligation.atom_ids) & set(item.atom_ids)
        )
        if not related:
            continue
        answer_claims = tuple(item.claim for item in related)
        covered_members = tuple(
            dependency.member_key
            for dependency in obligation.member_dependencies
            if dependency.support_ids
            and set(dependency.support_ids) <= registry.keys()
            and all(
                _source_fact_content_covered(
                    registry[support_id], answer_claims
                )
                for support_id in dependency.support_ids
            )
        )
        allowed_pairs = frozenset(
            (
                evidence_item.document_id,
                evidence_item.document_version_id,
            )
            for qualifier in obligation.qualifiers
            for support_id in qualifier.candidate_support_ids
            if (evidence_item := registry.get(support_id)) is not None
            and evidence_item.document_id is not None
            and evidence_item.document_version_id is not None
        )
        qualifier_results = tuple(
            evaluate_qualifier(
                qualifier,
                member_sources=tuple(
                    (dependency.member_key, dependency.support_ids)
                    for dependency in obligation.member_dependencies
                ),
                registry=registry,
                allowed_document_pairs=allowed_pairs,
            )
            for qualifier in obligation.qualifiers
        )
        satisfied_qualifier_keys = tuple(
            result.qualifier_key
            for result in qualifier_results
            if result.status is QualifierStatus.SUPPORTED
            and result.support_ids
            and set(result.support_ids) <= registry.keys()
            and all(
                _source_fact_content_covered(
                    registry[support_id], answer_claims
                )
                for support_id in result.support_ids
            )
        )
        for index, validated_claim in enumerate(related):
            artifacts.append(
                ValidatedPlanArtifact(
                    artifact_id=f"G{len(artifacts) + 1}",
                    plan_id=plan.plan_id,
                    obligation_ids=(obligation.obligation_id,),
                    selection_digests=(),
                    covered_member_keys=(covered_members if index == 0 else ()),
                    satisfied_qualifier_ids=(
                        tuple(
                            qualifier_key[1]
                            for qualifier_key in satisfied_qualifier_keys
                        )
                        if index == 0
                        else ()
                    ),
                    satisfied_qualifier_keys=(
                        satisfied_qualifier_keys if index == 0 else ()
                    ),
                    qualifier_results=(qualifier_results if index == 0 else ()),
                    source_closed=validated_claim.relation_complete,
                    origin="GROUNDED_GENERATION",
                    claim=validated_claim.claim,
                )
            )
    return tuple(artifacts)


def _resource_limited_plan_artifacts(
    plan: CompiledAnswerPlan,
    obligation_ids: tuple[str, ...],
) -> tuple[ValidatedPlanArtifact, ...]:
    """把真实输入预算失败标成资源终态，不伪装资料不足。"""
    known = {item.obligation_id for item in plan.obligations}
    return tuple(
        ValidatedPlanArtifact(
            artifact_id=f"R{index}",
            plan_id=plan.plan_id,
            obligation_ids=(obligation_id,),
            selection_digests=(),
            covered_member_keys=(),
            satisfied_qualifier_ids=(),
            source_closed=False,
            origin="GROUNDED_GENERATION",
            resource_limited=True,
        )
        for index, obligation_id in enumerate(obligation_ids, 1)
        if obligation_id in known
    )


@dataclass(frozen=True, slots=True)
class _ClaimSourceGroup:
    """一个不可跨越的来源组及服务端认证的展示语境。"""

    support_text: str
    trusted_subjects: frozenset[str]
    trusted_contexts: frozenset[str]
    trusted_term_contexts: frozenset[str] = frozenset()
    table_columns: tuple[str, ...] = ()


def _failed_generation_packets(
    error: RagError,
) -> tuple[PreparedGenerationPacket, ...]:
    """保留失败传输与兼容降级的发送记录，错误详情不携内部包。"""
    previous = getattr(error, "_previous_prepared_generation_packets", ())
    packets = (
        tuple(
            packet
            for packet in previous
            if isinstance(packet, PreparedGenerationPacket)
        )
        if isinstance(previous, tuple)
        else ()
    )
    current = getattr(error, "_prepared_generation_packet", None)
    return (
        (*packets, current)
        if isinstance(current, PreparedGenerationPacket)
        else packets
    )


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
        subject: str | None = None
        # 阶段与条件也是事实正文；只在 _subject 中跳过主语前缀。
        for segment in _split_enumeration_lead_in(sentence):
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
    return normalize_role_owner_text(left) == normalize_role_owner_text(right)


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
    if any(
        detected is not None and _same_subject(subject, detected)
        for clause, inherited in _clauses_with_subject(text)
        if (
            detected := inherited
            or _leading_explicit_subject(clause)
            or _subject(clause)
        )
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
    _validate_scalar_question_support(claim, analysis, cited_items)
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
        or _STANDALONE_SUBJECT.fullmatch(analysis.semantics.target.strip())
        is None
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


def _validate_scalar_question_support(
    claim: AnswerClaim,
    analysis: QueryAnalysis | None,
    units: tuple[EvidenceItem, ...],
) -> None:
    """最终数值事实必须回答所问对象属性，不能仅凭共同属性借值。"""
    # retrieval 包的兼容导出包含回答服务，运行时复用关系门避免导入环。
    from rag_app.application.retrieval.answer_support import (  # noqa: PLC0415
        SupportStatus,
        evaluate_span_support,
    )

    if analysis is None:
        return
    if (
        "CURRENT_ATOM_SEMANTICS" in analysis.reason_codes
        and not analysis.semantics.target
    ):
        # 未解析出对象时留给统一三态门，数字/单位/条件事实门仍逐项执行。
        return
    source = (
        _claim_source_text(claim, units)
        if units
        else "\n".join(support.quote for support in claim.supports)
    )
    proof = evaluate_span_support(analysis, source)
    if (
        analysis.semantics.relation in {"规定", "事实", "事实关系"}
        and analysis.semantics.target
        and analysis.semantics.target in source
        and analysis.semantics.answer_type is RequestedAnswerType.DURATION
        and _FALLBACK_DURATION.search(source)
    ):
        return
    if (
        proof.answer_type
        not in {
            "CONTACT",
            "MONEY",
            "AREA",
            "TEMPERATURE",
            "PRESSURE",
            "MASS",
            "RATIO",
            "DURATION",
            "TIME",
            "VERSION",
            "BRAND",
        }
        or proof.status is SupportStatus.SUPPORTED
    ):
        return
    decision = source_compatibility(units)
    if decision.compatible and decision.reason == "TABLE_INTERSECTION":
        certificates = [
            dict(item.metadata).get("answer_support") for item in units
        ]
        if certificates and all(
            isinstance(certificate, dict)
            and normalize_semantic_text(
                str(certificate.get("query_target") or "")
            )
            == normalize_semantic_text(proof.query_target)
            and normalize_semantic_text(
                str(certificate.get("requested_relation_or_attribute") or "")
            )
            == normalize_semantic_text(proof.requested_relation_or_attribute)
            for certificate in certificates
        ):
            # 结构门已核实唯一目标行、真实列头和值，此处仍验证属性和值类型。
            cells = tuple((item, table_cell_coordinate(item)) for item in units)
            row = next(
                coordinate[1]
                for item, coordinate in cells
                if coordinate is not None
                and normalize_semantic_text(item.citation_text)
                == normalize_semantic_text(proof.query_target)
            )
            header = " ".join(
                item.citation_text
                for item, coordinate in cells
                if coordinate is not None and coordinate[1] != row
            )
            if any(
                coordinate is not None
                and coordinate[1] == row
                and evaluate_span_support(
                    analysis,
                    item.citation_text,
                    table_relation=True,
                    table_header=header,
                ).status
                is SupportStatus.SUPPORTED
                for item, coordinate in cells
            ):
                return
    if (
        "CURRENT_ATOM_SEMANTICS" in analysis.reason_codes
        and decide_request_relation(analysis, source).status
        is not RequestRelationStatus.CONTRADICTED_OR_IRRELEVANT
    ):
        # 类型词法未命中先继续数字/quote硬门，最终三态门负责决定是否复核。
        return
    raise ValidationFailed(
        "引文没有证明所问对象的对应属性和值。",
        stage="answer.validate",
        code="CLAIM_QUERY_RELATION_UNSUPPORTED",
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
    """来源分组只使用统一合同核对过的真实身份与坐标。"""
    return source_group_keys(item)


def _table_cell_coordinate(
    item: EvidenceItem,
) -> tuple[tuple[object, ...], int, int] | None:
    """表格组合、恢复和摘录复用同一套严格坐标解析。"""
    return table_cell_coordinate(item)


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
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
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
    identities: dict[str, tuple[object, ...]] = {}
    for decision, members in compatible_partitions(
        tuple(units), trusted_groups=trusted_groups
    ):
        if not decision.compatible:
            raise ValidationFailed(
                "一个引用跨越不同来源结构。",
                stage="answer.validate",
                code="CLAIM_SOURCE_MISMATCH",
                details=(("validator", "_claim_source_groups"),),
            )
        for item in members:
            identities[item.support_id] = decision.proof_key
    for support, item in zip(claim.supports, units, strict=True):
        group = identities[item.support_id]
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
    trusted_groups: tuple[EvidenceGroup, ...] = (),
) -> None:
    """校验逐字支持、来源关系与关键事实，允许有词汇依据的自然概括。

    Args:
        draft: 模型的结构化事实草稿。
        evidence: 本次已通过范围筛选的有限证据。
        analysis: 可选的服务端查询语义，用于约束职责主体。
        complete: 增量 claim 校验时为 False；最终草稿必须检查完整列表。
        trusted_groups: 服务端真实成员与跨度映射，不能由模型证书替代。

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
        source_groups = _claim_source_groups(
            claim, units, analysis, trusted_groups=trusted_groups
        )
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
                    details=(("validator", "validate_grounded_draft"),),
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
        snapshot_id: str | None = None,
        resolved_query_view: ResolvedQueryView | None = None,
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
            snapshot_id: 请求开始时冻结的活动索引身份。
            resolved_query_view: 业务分析前冻结的问题视图。
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
                snapshot_id=snapshot_id,
                resolved_query_view=resolved_query_view,
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
        prepared_packets: list[PreparedGenerationPacket] = []
        request_id = uuid4().hex
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
                    request_id=request_id,
                    attempt_id=uuid4().hex,
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
                prepared_packets.extend(draft.previous_prepared_packets)
                if draft.prepared_packet is not None:
                    prepared_packets.append(draft.prepared_packet)
                    _validate_legacy_generation_packet(
                        generation_request, draft
                    )
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
                if rendered_claims != draft.claims:
                    validate_grounded_draft(
                        draft.model_copy(update={"claims": rendered_claims}),
                        evidence,
                        analysis=analysis,
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
                    prepared_packets=tuple(prepared_packets),
                )
            except QueryCancelled as error:
                error.provider_calls = (*calls, *error.provider_calls)
                raise
            except ValidationFailed as error:
                prepared_packets.extend(_failed_generation_packets(error))
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
                prepared_packets.extend(_failed_generation_packets(error))
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
            prepared_packets=tuple(prepared_packets),
        )

    @relation_review_scope
    def _answer_with_plan(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
        *,
        query_plan: QueryPlan,
        atom_support_matrix: AtomSupportMatrix,
        generation_evidence_pack: GenerationEvidencePack | None,
        snapshot_id: str | None,
        resolved_query_view: ResolvedQueryView | None,
        analysis: QueryAnalysis | None,
        on_claim: Callable[[AnswerClaim], None] | None,
        cancellation: CancellationPort | None,
    ) -> GroundedOutcome:
        """执行单次 Wire 生成、来源绑定和一次批量语义复核。"""
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
        compiled_plan: CompiledAnswerPlan | None = None
        strict_compiled_protocol = resolved_query_view is not None
        deterministic_execution: AnswerExecutionResult | None = None
        deterministic_coverage: CompiledPlanCoverage | None = None
        # 延迟导入以避开 retrieval.service -> grounded 的包初始化环。
        from rag_app.application.answering.executor import (  # noqa: PLC0415
            AnswerExecutionError,
            GenerationFailureDisposition,
            compiled_atom_coverage,
            execute_compiled_tasks,
            execute_generation_tasks,
            render_deterministic_answer,
        )

        if generation_evidence_pack is not None:
            from rag_app.application.answering.plan_compiler import (  # noqa: PLC0415
                AnswerPlanCompilationError,
                compile_answer_plan,
            )

            try:
                compiled_plan = compile_answer_plan(
                    query_plan,
                    generation_evidence_pack,
                    snapshot_id=(
                        snapshot_id or "legacy-unpinned-answer-snapshot"
                    ),
                    resolved_query_view=resolved_query_view,
                )
                deterministic_execution = execute_compiled_tasks(
                    compiled_plan,
                    generation_evidence_pack,
                    cancellation=cancellation,
                )
                deterministic_coverage = reduce_plan_coverage(
                    compiled_plan,
                    deterministic_execution.artifacts,
                )
            except (
                AnswerExecutionError,
                AnswerPlanCompilationError,
                AnswerPlanContractError,
            ) as error:
                return GroundedOutcome(
                    None,
                    "none",
                    reason_code=error.failure_code,
                )
            if not deterministic_execution.deferred_obligation_ids:
                answer = render_deterministic_answer(
                    compiled_plan,
                    deterministic_execution,
                    deterministic_coverage,
                )
                published_claims = tuple(
                    artifact.claim
                    for artifact in deterministic_execution.artifacts
                    if artifact.claim is not None
                )
                if answer is None:
                    return GroundedOutcome(
                        None,
                        "none",
                        reason_code="ANSWER_PLAN_NO_PUBLISHABLE_ARTIFACT",
                        answer_plan_id=compiled_plan.plan_id,
                        answer_plan_revision=compiled_plan.schema_revision,
                        answer_plan_records=_compiled_plan_records(
                            compiled_plan,
                            deterministic_execution,
                        ),
                        answer_plan_coverage=_compiled_coverage_records(
                            deterministic_coverage
                        ),
                    )
                for claim in published_claims:
                    _raise_if_cancelled(cancellation)
                    if on_claim is not None:
                        on_claim(claim)
                support_ids = tuple(
                    dict.fromkeys(
                        support.support_id
                        for claim in published_claims
                        for support in claim.supports
                    )
                )
                coverage_records = _compiled_coverage_records(
                    deterministic_coverage
                )
                return GroundedOutcome(
                    answer=answer,
                    mode="extractive",
                    reason_code=(
                        "DETERMINISTIC_ANSWER_PLAN"
                        if deterministic_coverage.complete
                        else "LIMITED_ANSWER"
                    ),
                    published_support_ids=support_ids,
                    atom_coverage=compiled_atom_coverage(
                        compiled_plan,
                        deterministic_coverage,
                    ),
                    relation_review_skip_reason="DETERMINISTIC_EXECUTION",
                    target_member_coverage=coverage_records,
                    accepted_claim_count=len(published_claims),
                    published_claim_count=len(published_claims),
                    accepted_support_ids=support_ids,
                    answer_plan_id=compiled_plan.plan_id,
                    answer_plan_revision=compiled_plan.schema_revision,
                    answer_plan_records=_compiled_plan_records(
                        compiled_plan,
                        deterministic_execution,
                    ),
                    answer_plan_coverage=coverage_records,
                )
        linked_ids = (
            dict(generation_evidence_pack.per_atom_candidate_support_ids)
            if generation_evidence_pack is not None
            else {}
        )
        by_id = {item.support_id: item for item in evidence}
        trusted_groups = (
            generation_evidence_pack.trusted_source_groups
            if generation_evidence_pack is not None
            else ()
        )

        def validate_source_excerpt(
            claim: AnswerClaim, atom_id: str | None = None
        ) -> bool:
            """服务端摘录也执行最终事实核验，不能借回退绕过主体边界。

            Args:
                claim: 即将发布的最终原文事实及逐字引用。
                atom_id: 交点摘录指定的当前 Atom；普通摘录保持原选择规则。

            Returns:
                至少一个被分配的 Atom 完整事实核验通过时为 True。

            """
            for atom in query_plan.atoms:
                if atom_id is not None and atom.atom_id != atom_id:
                    continue
                if not {
                    support.support_id for support in claim.supports
                } <= set(linked_ids.get(atom.atom_id, ())):
                    continue
                try:
                    _validated_natural_claim(
                        NaturalClaim(
                            atom_id=atom.atom_id,
                            text=claim.text,
                            supports=claim.supports,
                        ),
                        query_plan,
                        atom_support_matrix,
                        _atom_validation_evidence(
                            atom.atom_id,
                            evidence,
                            generation_evidence_pack.per_atom_source_certificates
                            if generation_evidence_pack is not None
                            else (),
                        ),
                        analysis,
                        trusted_groups=trusted_groups,
                        physical_table_facts=(
                            generation_evidence_pack.physical_table_facts
                            if generation_evidence_pack is not None
                            else ()
                        ),
                        atom_fact_bindings=(
                            generation_evidence_pack.atom_fact_bindings
                            if generation_evidence_pack is not None
                            else ()
                        ),
                    )
                    if analysis is not None and len(query_plan.atoms) == 1:
                        validate_grounded_draft(
                            AnswerDraft(
                                text=claim.text,
                                cited_evidence_ids=tuple(
                                    support.support_id
                                    for support in claim.supports
                                ),
                                claims=(claim,),
                                generation_mode="extractive",
                            ),
                            evidence,
                            analysis=analysis,
                            complete=False,
                            trusted_groups=trusted_groups,
                        )
                except (ValidationFailed, ValueError):
                    continue
                return True
            return False

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
        if compiled_plan is not None and deterministic_execution is not None:
            deferred_atoms = {
                atom_id
                for obligation in compiled_plan.obligations
                if obligation.obligation_id
                in deterministic_execution.deferred_obligation_ids
                for atom_id in obligation.atom_ids
            }
            eligible &= deferred_atoms
        blocked_field_atom_ids: set[str] = set()
        pending_field_atom_ids: set[str] = set()
        field_resolution_reason: str | None = None
        if (
            generation_evidence_pack is not None
            and generation_evidence_pack.field_resolution_active
        ):
            candidate_atom_ids = {
                item.atom_id
                for item in generation_evidence_pack.field_candidates
            }
            pending_field_atom_ids = set(
                generation_evidence_pack.field_resolution_pending_atom_ids
            )
            blocked_resolutions = tuple(
                item
                for item in generation_evidence_pack.field_resolutions
                if item.atom_id in candidate_atom_ids
                and item.atom_id not in pending_field_atom_ids
                and item.status
                in {
                    FieldResolutionStatus.AMBIGUOUS,
                    FieldResolutionStatus.RELATED_FIELD,
                }
            )
            blocked_field_atom_ids = {
                item.atom_id for item in blocked_resolutions
            } | pending_field_atom_ids
            eligible -= blocked_field_atom_ids
            if pending_field_atom_ids:
                field_resolution_reason = (
                    generation_evidence_pack.field_resolution_failure_reason
                    or "FIELD_RESOLUTION_PROVIDER_UNAVAILABLE"
                )
            elif any(
                item.status is FieldResolutionStatus.AMBIGUOUS
                for item in blocked_resolutions
            ):
                field_resolution_reason = "FIELD_RESOLUTION_AMBIGUOUS"
            elif blocked_resolutions:
                field_resolution_reason = "FIELD_RESOLUTION_RELATED_ONLY"
        calls: list[ProviderCall] = []
        deterministic_claim_ids: set[str] = set()
        accepted: list[ValidatedNaturalClaim] = []
        if compiled_plan is not None and deterministic_execution is not None:
            obligations = {
                item.obligation_id: item for item in compiled_plan.obligations
            }
            for artifact in deterministic_execution.artifacts:
                if artifact.claim is None:
                    continue
                atom_ids = tuple(
                    dict.fromkeys(
                        atom_id
                        for obligation_id in artifact.obligation_ids
                        for atom_id in obligations[obligation_id].atom_ids
                    )
                )
                claim_id = f"C{len(accepted) + 1}"
                deterministic_claim_ids.add(claim_id)
                accepted.append(
                    ValidatedNaturalClaim(
                        claim_id=claim_id,
                        atom_ids=atom_ids,
                        claim=artifact.claim,
                        render_origin="deterministic_execution",
                    )
                )
        claim_rejections: Counter[str] = Counter()
        claim_rejection_diagnostics: list[ClaimRejectionDiagnostic] = []
        rejected_atoms: Counter[str] = Counter()
        generated_claim_count = 0
        generation_returned = False
        reason: str | None = field_resolution_reason
        resource_limited_atom_ids: set[str] = set()
        repair_calls = 0
        relation_review_calls = 0
        relation_review_elapsed_ms = 0.0
        relation_review_skip_reason: str | None = "NO_BOUND_CLAIM"
        relation_review_results: list[tuple[str, str, str]] = []
        semantic_review_facets: list[JsonObject] = []
        pending_relations: list[BoundClaim] = []
        pending_legacy_relations: list[NaturalClaim] = []
        pending_legacy_diagnostics: dict[
            NaturalClaim, ClaimRejectionDiagnostic
        ] = {}
        wire_diagnostics: list[GroundedWireDiagnostic] = []
        legacy_protocol = False
        generation_started: float | None = None
        extractive_fallback_reason: str | None = None
        request_id = uuid4().hex
        active_request: GenerationRequest | None = None
        prepared_packets: list[PreparedGenerationPacket] = []
        attempt_linked_ids: dict[str, tuple[str, ...]] = {}
        raw_failures: list[tuple[str, str]] = []
        recovery_results: list[tuple[str, str, str]] = []
        source_projection_records: list[JsonObject] = []
        repair_skip_reason: str | None = "NO_MISSING_ATOM"

        def finalized_source_projection_records() -> tuple[JsonObject, ...]:
            """给每条 SAFE 投影补充真实发布终态，不记录任何正文。"""
            published_claim_ids = {item.claim_id for item in accepted}
            return tuple(
                freeze_json_object(
                    {
                        **dict(record),
                        "published": dict(record).get("claim_id")
                        in published_claim_ids,
                    }
                )
                for record in source_projection_records
            )

        def generate(
            repair_atom_ids: tuple[str, ...] = (),
            *,
            execution_atom_ids: tuple[str, ...] = (),
        ) -> AnswerDraft:
            """从准入证据中选出本次 Atom 的候选，不以发布许可过滤。"""
            # 延迟导入以避开 retrieval.service -> grounded 的包初始化环。
            from rag_app.application.retrieval.generation_evidence import (  # noqa: PLC0415
                project_evidence_read_units,
            )

            nonlocal active_request
            nonlocal generation_started
            if repair_atom_ids and execution_atom_ids:
                raise ValueError("生成批次不能同时是修复与初次执行。")
            if generation_started is None:
                generation_started = monotonic()
            requested = (
                set(repair_atom_ids)
                if repair_atom_ids
                else set(execution_atom_ids)
                if execution_atom_ids
                else eligible
            )
            available_bindings = tuple(
                binding
                for binding in (
                    generation_evidence_pack.atom_fact_bindings
                    if generation_evidence_pack is not None
                    else ()
                )
                if binding.atom_id in requested
            )
            available_fact_ids = {
                binding.fact_id for binding in available_bindings
            }
            available_facts = tuple(
                fact
                for fact in (
                    generation_evidence_pack.physical_table_facts
                    if generation_evidence_pack is not None
                    else ()
                )
                if fact.fact_id in available_fact_ids
            )
            allowed = {
                support_id
                for atom_id in requested
                for support_id in linked_ids.get(
                    atom_id,
                    atom_support_matrix.for_atom(atom_id).supporting_support_ids
                    or tuple(by_id),
                )
            }
            allowed.update(
                support_id
                for fact in available_facts
                for support_id in fact.all_support_ids
            )
            candidates = tuple(
                item for item in evidence if item.support_id in allowed
            )
            candidate_ids = {item.support_id for item in candidates}
            selected_facts = tuple(
                fact
                for fact in available_facts
                if set(fact.all_support_ids) <= candidate_ids
            )
            selected_fact_ids = {fact.fact_id for fact in selected_facts}
            selected_bindings = tuple(
                binding
                for binding in available_bindings
                if binding.fact_id in selected_fact_ids
            )

            def atom_candidate_ids(atom_id: str) -> tuple[str, ...]:
                """逐 Atom 加入完整事实依赖，不借给其他 Atom。"""
                ids = set(
                    linked_ids.get(
                        atom_id,
                        atom_support_matrix.for_atom(
                            atom_id
                        ).supporting_support_ids
                        or tuple(by_id),
                    )
                )
                bound_fact_ids = {
                    binding.fact_id
                    for binding in selected_bindings
                    if binding.atom_id == atom_id
                }
                ids.update(
                    support_id
                    for fact in selected_facts
                    if fact.fact_id in bound_fact_ids
                    for support_id in fact.all_support_ids
                )
                return tuple(
                    item.support_id
                    for item in candidates
                    if item.support_id in ids
                )

            per_atom_candidate_ids = tuple(
                (atom_id, atom_candidate_ids(atom_id))
                for atom_id in sorted(requested)
            )
            candidate_keys = {stable_support_key(item) for item in candidates}
            request = GenerationRequest(
                request_id=request_id,
                attempt_id=uuid4().hex,
                query=query,
                evidence=candidates,
                citation_protocol="support-id-v3-quoted-natural-claims",
                typed_semantics=None
                if analysis is None
                else analysis.semantics,
                model_evidence_candidates=candidates,
                query_plan=query_plan,
                atom_support_matrix=atom_support_matrix,
                per_atom_candidate_support_ids=per_atom_candidate_ids,
                execution_atom_ids=(
                    tuple(sorted(requested)) if not repair_atom_ids else ()
                ),
                repair_atom_ids=repair_atom_ids,
                accepted_claim_ids=tuple(item.claim_id for item in accepted),
                trusted_source_groups=trusted_groups,
                per_atom_source_certificates=(
                    generation_evidence_pack.per_atom_source_certificates
                    if generation_evidence_pack is not None
                    else ()
                ),
                priority_source_units=(
                    tuple(
                        (owner, keys)
                        for owner, keys in (
                            generation_evidence_pack.priority_source_units
                        )
                        if owner in requested
                        or (owner == "ROOT" and set(keys) <= candidate_keys)
                    )
                    if generation_evidence_pack is not None
                    else ()
                ),
                physical_table_facts=selected_facts,
                atom_fact_bindings=selected_bindings,
                evidence_read_units=project_evidence_read_units(
                    candidates, selected_facts
                ),
                repair_raw_failures=tuple(
                    (atom_id, raw)
                    for atom_id, raw in raw_failures
                    if atom_id in requested
                )
                if repair_atom_ids
                else (),
                repair_allowed_support_keys=tuple(
                    (
                        atom_id,
                        tuple(
                            stable_support_key(item)
                            for item in candidates
                            if item.support_id in atom_candidate_ids(atom_id)
                        ),
                    )
                    for atom_id in sorted(requested)
                )
                if repair_atom_ids
                else (),
            )
            active_request = request
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

        def validate_legacy_natural(
            natural: NaturalClaim,
        ) -> tuple[AnswerClaim, ...]:
            """仅为旧 Provider/测试替身保留 V8 校验链。"""
            _validate_natural_atom_support_scope(
                natural,
                query_plan,
                attempt_linked_ids,
            )
            validation_evidence = _atom_validation_evidence(
                natural.atom_id,
                evidence,
                generation_evidence_pack.per_atom_source_certificates
                if generation_evidence_pack is not None
                else (),
            )
            try:
                claim = _validated_natural_claim(
                    natural,
                    query_plan,
                    atom_support_matrix,
                    validation_evidence,
                    analysis,
                    trusted_groups=trusted_groups,
                    physical_table_facts=(
                        generation_evidence_pack.physical_table_facts
                        if generation_evidence_pack is not None
                        else ()
                    ),
                    atom_fact_bindings=(
                        generation_evidence_pack.atom_fact_bindings
                        if generation_evidence_pack is not None
                        else ()
                    ),
                )
            except (ValidationFailed, ValueError) as error:
                raw = (
                    error.code
                    if isinstance(error, ValidationFailed)
                    else "VALUE_ERROR"
                )
                raw_failures.append((natural.atom_id, raw))
                if generation_evidence_pack is not None and (
                    _natural_rejection_code(error)
                    == "CLAIM_SEMANTIC_SUPPORT_FAILED"
                    or raw == "CLAIM_FRAGMENT_INCOMPLETE"
                ):
                    recovered_claim = _validated_source_faithful_claim(
                        natural,
                        query_plan,
                        atom_support_matrix,
                        validation_evidence,
                        analysis,
                        trusted_groups=trusted_groups,
                        physical_table_facts=(
                            generation_evidence_pack.physical_table_facts
                            if generation_evidence_pack is not None
                            else ()
                        ),
                        atom_fact_bindings=(
                            generation_evidence_pack.atom_fact_bindings
                            if generation_evidence_pack is not None
                            else ()
                        ),
                    )
                    recovery_results.append(
                        (natural.atom_id, raw, "SOURCE_SENTENCE_VALIDATED")
                    )
                    return (recovered_claim,)
                if (
                    generation_evidence_pack is not None
                    and isinstance(error, ValidationFailed)
                    and error.code == "CLAIM_SOURCE_MISMATCH"
                    and (
                        recovered := _validated_source_group_claims(
                            natural,
                            query_plan,
                            atom_support_matrix,
                            validation_evidence,
                            analysis,
                            trusted_groups=trusted_groups,
                            on_undetermined=pending_legacy_relations.append,
                        )
                    )
                ):
                    recovery_results.append(
                        (
                            natural.atom_id,
                            raw,
                            "SOURCE_PARTITIONS_VALIDATED",
                        )
                    )
                    return recovered
                raise
            return (claim,)

        def consume_legacy(draft: AnswerDraft) -> None:
            """兼容旧 NaturalClaim 草稿；真实 V9 Provider 不进入此分支。"""
            nonlocal generated_claim_count, reason
            if active_request is None:
                raise ValueError("生成草稿缺少对应的请求身份。")
            generated_claim_count += len(draft.natural_claims)
            repair_scope = set(active_request.repair_atom_ids)
            if repair_scope and any(
                natural.atom_id not in repair_scope
                for natural in draft.natural_claims
            ):
                raise ValueError("REPAIR_ATOM_SCOPE_VIOLATION")
            if not draft.natural_claims:
                reason = draft.reason_code or "GENERATION_ABSTAINED"
                raw_failures.extend(
                    (atom_id, reason) for atom_id in attempt_linked_ids
                )
            for natural in draft.natural_claims:
                try:
                    validated_claims = validate_legacy_natural(natural)
                except (ValidationFailed, ValueError) as error:
                    pending_natural: NaturalClaim | None = None
                    if isinstance(error, RequestRelationUndetermined):
                        pending_natural = NaturalClaim(
                            atom_id=natural.atom_id,
                            text=error.claim.text,
                            supports=error.claim.supports,
                        )
                        pending_legacy_relations.append(pending_natural)
                    raw = (
                        error.code
                        if isinstance(error, ValidationFailed)
                        else "VALUE_ERROR"
                    )
                    if (natural.atom_id, raw) not in raw_failures:
                        raw_failures.append((natural.atom_id, raw))
                    public_reason = _natural_rejection_code(error)
                    claim_rejections[public_reason] += 1
                    allowed_support_ids = attempt_linked_ids.get(
                        natural.atom_id,
                        atom_support_matrix.for_atom(
                            natural.atom_id
                        ).supporting_support_ids
                        if natural.atom_id
                        in {atom.atom_id for atom in query_plan.atoms}
                        else (),
                    )
                    diagnostic = _claim_rejection_diagnostic(
                        natural,
                        error,
                        public_reason=public_reason,
                        allowed_support_ids=allowed_support_ids,
                    )
                    claim_rejection_diagnostics.append(diagnostic)
                    if pending_natural is not None:
                        pending_legacy_diagnostics[pending_natural] = diagnostic
                    rejected_atoms[natural.atom_id] += 1
                    reason = "CLAIM_NOT_SUPPORTED"
                    continue
                for claim in validated_claims:
                    if any(
                        item.atom_ids == (natural.atom_id,)
                        and item.claim == claim
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

        def consume(draft: AnswerDraft) -> None:  # noqa: PLR0912, PLR0915
            """逐项绑定 Wire Claim；语义发布许可留给一次批量复核。"""
            nonlocal generated_claim_count, generation_returned, reason
            nonlocal attempt_linked_ids, legacy_protocol
            if draft.generation_mode != "natural":
                raise ValidationFailed(
                    "类型化生成返回错误协议。",
                    stage="answer.validate",
                    code="GENERATION_CLAIMS_INVALID",
                )
            calls.extend(draft.provider_calls)
            if active_request is None:
                raise ValueError("生成草稿缺少对应的请求身份。")
            attempt_linked_ids = _generation_attempt_allowance(
                active_request, draft
            )
            if draft.prepared_packet is not None:
                prepared_packets.append(draft.prepared_packet)
            generation_returned = True
            if draft.natural_claims and not draft.wire_claims:
                if strict_compiled_protocol:
                    raise ValueError("ANSWER_PLAN_LEGACY_PROTOCOL_FORBIDDEN")
                legacy_protocol = True
                consume_legacy(draft)
                return
            if (
                draft.prepared_packet is None
                and not draft.wire_claims
                and not draft.wire_diagnostics
            ):
                if strict_compiled_protocol:
                    raise ValueError("ANSWER_PLAN_LEGACY_PROTOCOL_FORBIDDEN")
                legacy_protocol = True
                consume_legacy(draft)
                return
            packet = draft.prepared_packet
            if packet is None:
                raise ValueError("自然生成缺少实际发送包。")
            generated_claim_count += len(draft.wire_claims) + len(
                draft.wire_diagnostics
            )
            wire_diagnostics.extend(draft.wire_diagnostics)
            for diagnostic in draft.wire_diagnostics:
                if diagnostic.atom_id is not None:
                    raw_failures.append(
                        (diagnostic.atom_id, diagnostic.failure_code)
                    )
                    rejected_atoms[diagnostic.atom_id] += 1
                claim_rejections[diagnostic.failure_code] += 1

            sent_unit_ids = set(packet.sent_read_unit_ids)
            read_units = tuple(
                unit
                for unit in active_request.evidence_read_units
                if unit.unit_id in sent_unit_ids
            )
            wire_claims = list(draft.wire_claims)
            if not wire_claims:
                reason = draft.reason_code or "GENERATION_ABSTAINED"
                raw_failures.extend(
                    (atom_id, reason) for atom_id in attempt_linked_ids
                )
            allowed_units = {
                atom_id: frozenset(unit_ids)
                for atom_id, unit_ids in packet.per_atom_read_unit_ids
            }
            atoms_by_id = {atom.atom_id: atom for atom in query_plan.atoms}
            for index, wire_claim in enumerate(wire_claims, start=1):
                try:
                    atom = atoms_by_id.get(wire_claim.atom_id)
                    if atom is None:
                        raise EvidenceBindingError(
                            "UNKNOWN_ATOM", f"claim[{index - 1}].atom_id"
                        )
                    bound = bind_wire_claim(
                        wire_claim,
                        claim_id=f"C{index}",
                        read_units=read_units,
                        evidence=active_request.evidence,
                        allowed_unit_ids=allowed_units.get(
                            wire_claim.atom_id, frozenset()
                        ),
                        physical_table_facts=active_request.physical_table_facts,
                        atom_fact_bindings=active_request.atom_fact_bindings,
                        source_scope=atom.source_scope,
                    )
                    projected = project_bound_claim(
                        bound,
                        read_units=read_units,
                        evidence=active_request.evidence,
                        physical_table_facts=(
                            active_request.physical_table_facts
                        ),
                        atom_fact_bindings=active_request.atom_fact_bindings,
                        source_scope=atom.source_scope,
                    )
                except (EvidenceBindingError, SourceProjectionError) as error:
                    failure_code = error.failure_code
                    diagnostic = GroundedWireDiagnostic(
                        item_index=index - 1,
                        atom_id=wire_claim.atom_id,
                        failure_stage="evidence_binding",
                        failure_code=failure_code,
                        json_path=(
                            error.json_path
                            if isinstance(error, EvidenceBindingError)
                            else f"claim[{index - 1}]"
                        ),
                        expected_type="sent_read_unit",
                        observed_type="binding_mismatch",
                    )
                    wire_diagnostics.append(diagnostic)
                    raw_failures.append((wire_claim.atom_id, failure_code))
                    claim_rejections[failure_code] += 1
                    rejected_atoms[wire_claim.atom_id] += 1
                    reason = "EVIDENCE_BINDING_FAILED"
                    continue
                source_projection_records.append(
                    freeze_json_object(
                        {
                            "claim_id": projected.claim_id,
                            "atom_id": projected.atom_id,
                            "draft_text_sha256": projected.draft_text_sha256,
                            "published_text_sha256": (
                                projected.published_text_sha256
                            ),
                            "render_origin": projected.render_origin,
                            "selected_assertion_ids": (
                                projected.selected_assertion_ids
                            ),
                            "relation_gap": not projected.relation_complete,
                            "relation_gap_reason": (
                                projected.relation_gap_reason
                            ),
                            "scope_digest": (
                                atom.source_scope.scope_digest
                                if atom.source_scope is not None
                                else None
                            ),
                        }
                    )
                )
                candidate = (
                    projected
                    if projected.render_origin == "source_sentence"
                    else bound
                )
                if candidate not in pending_relations:
                    pending_relations.append(candidate)

        def review_legacy_pending() -> None:  # noqa: PLR0912, PLR0915
            """仅为旧 V8 协议保留原有关系复核行为。"""
            nonlocal relation_review_calls, relation_review_elapsed_ms
            nonlocal relation_review_skip_reason, reason
            if not pending_legacy_relations:
                return
            unique = tuple(dict.fromkeys(pending_legacy_relations))

            def observed(natural: NaturalClaim, status: str, code: str) -> None:
                relation_review_results.append(
                    (
                        hashlib.sha256(natural.text.encode()).hexdigest(),
                        status,
                        code,
                    )
                )

            review_method = getattr(
                type(self.generator), "review_relations", None
            )
            packet = prepared_packets[0] if prepared_packets else None
            timeout = getattr(
                self.generator, "supplement_timeout_seconds", None
            )
            if (
                not callable(review_method)
                or packet is None
                or packet.evidence_level != "TRANSPORT_SENT"
                or generation_started is None
                or not isinstance(timeout, (int, float))
            ):
                relation_review_skip_reason = (
                    "NO_SENT_PACKET_OR_PROVIDER_DEADLINE"
                )
                for natural in unique:
                    observed(
                        natural, "NOT_OBSERVED", relation_review_skip_reason
                    )
                return
            deadline = generation_started + timeout
            outer_deadline = getattr(cancellation, "deadline_monotonic", None)
            if isinstance(outer_deadline, (int, float)):
                deadline = min(deadline, outer_deadline)
            if deadline <= monotonic():
                relation_review_skip_reason = "DEADLINE_EXHAUSTED"
                for natural in unique:
                    observed(
                        natural, "NOT_OBSERVED", relation_review_skip_reason
                    )
                return
            atoms = {atom.atom_id: atom for atom in query_plan.atoms}
            candidates = tuple(
                RelationReviewCandidate(
                    claim_id=f"R{index}",
                    claim=natural,
                    atom=atoms[natural.atom_id],
                    analysis=_natural_atom_analysis(
                        atoms[natural.atom_id], analysis
                    ),
                )
                for index, natural in enumerate(unique, 1)
            )
            candidates = tuple(
                candidate.model_copy(
                    update={
                        "context_support_ids": review_context_support_ids(
                            candidate, evidence, packet, trusted_groups
                        )
                    }
                )
                for candidate in candidates
            )
            selected_ids = {
                support.support_id
                for natural in unique
                for support in natural.supports
            }
            selected_ids.update(
                support_id
                for candidate in candidates
                for support_id in candidate.context_support_ids
            )
            request = RelationReviewRequest(
                original_query=query_plan.original_query,
                candidates=candidates,
                evidence=tuple(
                    item for item in evidence if item.support_id in selected_ids
                ),
                trusted_source_groups=trusted_groups,
                sent_packet=packet,
                request_id=request_id,
                attempt_id=uuid4().hex,
                deadline_monotonic=deadline,
                generation_model=next(
                    (
                        call.model
                        for call in reversed(calls)
                        if call.operation == "generation"
                    ),
                    None,
                ),
            )
            _raise_if_cancelled(cancellation)
            relation_review_skip_reason = None
            started = monotonic()
            try:
                response = review_method(self.generator, request)
            except (RagError, ValueError) as error:
                review_reason = (
                    "RELATION_REVIEW_RESPONSE_INVALID"
                    if isinstance(error, ProviderInvalidResponse)
                    else error.code
                    if isinstance(error, RagError)
                    and error.code
                    in {
                        "RELATION_REVIEW_INPUT_BUDGET_EXCEEDED",
                        "RELATION_REVIEW_DEADLINE_EXHAUSTED",
                        "RELATION_REVIEW_RESPONSE_INVALID",
                    }
                    else "RELATION_REVIEW_PROVIDER_ERROR"
                )
                relation_review_skip_reason = review_reason
                reason = review_reason
                if isinstance(error, RagError):
                    failed_packets = _failed_generation_packets(error)
                    failed_calls = error.provider_calls or (
                        ()
                        if error.provider_call is None
                        else (error.provider_call,)
                    )
                    prepared_packets.extend(failed_packets)
                    calls.extend(failed_calls)
                    relation_review_calls = int(
                        any(
                            item.evidence_level == "TRANSPORT_SENT"
                            for item in failed_packets
                        )
                        or any(call.call_count for call in failed_calls)
                    )
                for candidate in request.candidates:
                    observed(candidate.claim, "NOT_OBSERVED", review_reason)
                return
            finally:
                relation_review_elapsed_ms = (monotonic() - started) * 1000
            if not isinstance(response, RelationReviewResponse):
                for candidate in request.candidates:
                    observed(
                        candidate.claim,
                        "NOT_OBSERVED",
                        "RELATION_REVIEW_RESPONSE_INVALID",
                    )
                raise ValueError("关系复核没有返回严格协议。")
            relation_review_calls = 1
            calls.append(response.call)
            prepared_packets.append(response.prepared_packet)
            _raise_if_cancelled(cancellation)
            validate_review_payload(
                RelationReviewPayload(results=response.results), request
            )
            if response.prepared_packet.evidence_level != "TRANSPORT_SENT":
                raise ValueError("复核缺少实际发送证据。")
            by_claim = {item.claim_id: item for item in request.candidates}
            for result in response.results:
                candidate = by_claim[result.claim_id]
                if result.status != "supported":
                    observed(
                        candidate.claim, result.status, "MODEL_NOT_SUPPORTED"
                    )
                    continue
                units = tuple(
                    by_id[s.support_id] for s in candidate.claim.supports
                )
                context_units = tuple(
                    by_id[support_id]
                    for support_id in candidate.context_support_ids
                )
                review_decision = decide_request_relation(
                    candidate.analysis,
                    "\n".join(
                        (
                            _claim_source_text(
                                AnswerClaim(
                                    text=candidate.claim.text,
                                    supports=candidate.claim.supports,
                                ),
                                units,
                            ),
                            *(item.citation_text for item in context_units),
                        )
                    ),
                )
                if (
                    review_decision.status
                    is RequestRelationStatus.CONTRADICTED_OR_IRRELEVANT
                ):
                    observed(
                        candidate.claim,
                        result.status,
                        "HARD_SCOPE_CONTRADICTION",
                    )
                    continue
                plain = AnswerClaim(
                    text=candidate.claim.text,
                    supports=candidate.claim.supports,
                )
                record_relation_review(
                    relation_review_key(candidate.atom, plain, units)
                )
                try:
                    claim = _validated_natural_claim(
                        candidate.claim,
                        query_plan,
                        atom_support_matrix,
                        _atom_validation_evidence(
                            candidate.atom.atom_id,
                            evidence,
                            generation_evidence_pack.per_atom_source_certificates
                            if generation_evidence_pack
                            else (),
                        ),
                        analysis,
                        trusted_groups=trusted_groups,
                        physical_table_facts=(
                            generation_evidence_pack.physical_table_facts
                            if generation_evidence_pack is not None
                            else ()
                        ),
                        atom_fact_bindings=(
                            generation_evidence_pack.atom_fact_bindings
                            if generation_evidence_pack is not None
                            else ()
                        ),
                    )
                except (ValidationFailed, ValueError):
                    observed(
                        candidate.claim,
                        result.status,
                        "HARD_VALIDATOR_REJECTED",
                    )
                    raise
                observed(
                    candidate.claim,
                    result.status,
                    "RELATION_REVIEW_VALIDATED",
                )
                accepted.append(
                    ValidatedNaturalClaim(
                        claim_id=f"C{len(accepted) + 1}",
                        atom_ids=(candidate.atom.atom_id,),
                        claim=claim,
                    )
                )
                pending_diagnostic = pending_legacy_diagnostics.get(
                    candidate.claim
                )
                if pending_diagnostic is not None:
                    code = pending_diagnostic.public_reason_code
                    claim_rejections[code] -= 1
                    if not claim_rejections[code]:
                        del claim_rejections[code]
                    rejected_atoms[candidate.atom.atom_id] -= 1
                    claim_rejection_diagnostics.remove(pending_diagnostic)
                recovery_results.append(
                    (
                        candidate.atom.atom_id,
                        "CLAIM_QUERY_RELATION_UNDETERMINED",
                        "RELATION_REVIEW_VALIDATED",
                    )
                )

        def review_pending(  # noqa: PLR0912, PLR0915
            bounds: tuple[BoundClaim, ...] | None = None,
            *,
            generation_request: GenerationRequest | None = None,
            sent_packet: PreparedGenerationPacket | None = None,
            allow_split: bool = True,
        ) -> None:
            """按真实 Claim 成本执行一个或至多两个互斥复核批次。"""
            nonlocal relation_review_calls, relation_review_elapsed_ms
            nonlocal relation_review_skip_reason, reason
            selected_bounds = (
                tuple(pending_relations) if bounds is None else bounds
            )
            if not selected_bounds:
                return
            unique = tuple(dict.fromkeys(selected_bounds))

            def observed(bound: BoundClaim, status: str, code: str) -> None:
                relation_review_results.append(
                    (
                        hashlib.sha256(bound.text.encode()).hexdigest(),
                        status,
                        code,
                    )
                )

            review_method = getattr(
                type(self.generator), "review_semantics", None
            )
            packet = sent_packet or (
                prepared_packets[0] if prepared_packets else None
            )
            timeout = getattr(
                self.generator, "supplement_timeout_seconds", None
            )
            if (
                not callable(review_method)
                or packet is None
                or packet.evidence_level != "TRANSPORT_SENT"
                or generation_started is None
                or not isinstance(timeout, (int, float))
            ):
                relation_review_skip_reason = (
                    "NO_SENT_PACKET_OR_PROVIDER_DEADLINE"
                )
                reason = "SEMANTIC_REVIEW_NOT_AVAILABLE"
                for bound in unique:
                    observed(bound, "NOT_OBSERVED", reason)
                return
            deadline = generation_started + timeout
            outer_deadline = getattr(cancellation, "deadline_monotonic", None)
            if isinstance(outer_deadline, (int, float)):
                deadline = min(deadline, outer_deadline)
            if deadline <= monotonic():
                relation_review_skip_reason = (
                    "SEMANTIC_REVIEW_DEADLINE_EXHAUSTED"
                )
                reason = relation_review_skip_reason
                for bound in unique:
                    observed(bound, "NOT_OBSERVED", reason)
                return
            generation_request = generation_request or active_request
            if generation_request is None:
                raise ValueError("语义复核缺少原生成请求。")
            atoms = {atom.atom_id: atom for atom in query_plan.atoms}
            generation_model = next(
                (
                    call.model
                    for call in reversed(calls)
                    if call.operation == "generation"
                ),
                None,
            )

            def build_request(
                bounds: tuple[BoundClaim, ...],
            ) -> SemanticValidationRequest:
                """为当前互斥事实集仅装入其真实选择的阅读单元。"""
                selected_unit_ids = {
                    unit_id
                    for bound in bounds
                    for unit_id in bound.selected_unit_ids
                }
                return SemanticValidationRequest(
                    original_query=query_plan.original_query,
                    candidates=tuple(
                        SemanticValidationCandidate(
                            claim=bound,
                            atom=atoms[bound.atom_id],
                        )
                        for bound in bounds
                    ),
                    read_units=tuple(
                        unit
                        for unit in generation_request.evidence_read_units
                        if unit.unit_id in selected_unit_ids
                    ),
                    sent_packet=packet,
                    request_id=request_id,
                    attempt_id=uuid4().hex,
                    deadline_monotonic=deadline,
                    generation_model=generation_model,
                )

            allowed_units = {
                atom_id: frozenset(unit_ids)
                for atom_id, unit_ids in packet.per_atom_read_unit_ids
            }
            supported_count = 0
            successful_batches = 0
            batch_failed = False
            split_attempted = False
            requests = [build_request(unique)]
            relation_review_skip_reason = None
            started = monotonic()
            index = 0
            while index < len(requests):
                request = requests[index]
                _raise_if_cancelled(cancellation)
                try:
                    response = review_method(self.generator, request)
                except (RagError, ValueError) as error:
                    review_reason = (
                        dict(error.details).get("reason_code")
                        if isinstance(error, ProviderInvalidResponse)
                        else error.code
                        if isinstance(error, RagError)
                        else "SEMANTIC_REVIEW_PROVIDER_ERROR"
                    )
                    if not isinstance(review_reason, str):
                        review_reason = "SEMANTIC_REVIEW_PROVIDER_ERROR"
                    failed_packets = (
                        _failed_generation_packets(error)
                        if isinstance(error, RagError)
                        else ()
                    )
                    failed_calls = (
                        error.provider_calls
                        or (
                            ()
                            if error.provider_call is None
                            else (error.provider_call,)
                        )
                        if isinstance(error, RagError)
                        else ()
                    )
                    prepared_packets.extend(failed_packets)
                    calls.extend(failed_calls)
                    transported = any(
                        item.evidence_level == "TRANSPORT_SENT"
                        for item in failed_packets
                    ) or any(call.call_count for call in failed_calls)
                    relation_review_calls += int(transported)
                    if (
                        review_reason == "SEMANTIC_REVIEW_INPUT_BUDGET_EXCEEDED"
                        and not transported
                        and allow_split
                        and not split_attempted
                        and len(request.candidates) > 1
                    ):
                        midpoint = (len(request.candidates) + 1) // 2
                        left = tuple(
                            item.claim for item in request.candidates[:midpoint]
                        )
                        right = tuple(
                            item.claim for item in request.candidates[midpoint:]
                        )
                        requests = [build_request(left), build_request(right)]
                        split_attempted = True
                        relation_review_skip_reason = None
                        reason = None
                        index = 0
                        continue
                    batch_failed = True
                    relation_review_skip_reason = review_reason
                    reason = review_reason
                    if review_reason == (
                        "SEMANTIC_REVIEW_INPUT_BUDGET_EXCEEDED"
                    ):
                        resource_limited_atom_ids.update(
                            candidate.atom.atom_id
                            for candidate in request.candidates
                        )
                    for candidate in request.candidates:
                        observed(
                            candidate.claim,
                            "NOT_OBSERVED",
                            review_reason,
                        )
                    index += 1
                    continue
                if not isinstance(response, SemanticValidationResponse):
                    raise ValueError("语义复核没有返回严格协议。")
                successful_batches += 1
                relation_review_calls += 1
                calls.append(response.call)
                prepared_packets.append(response.prepared_packet)
                _raise_if_cancelled(cancellation)
                if response.prepared_packet.evidence_level != "TRANSPORT_SENT":
                    raise ValueError("语义复核缺少实际发送证据。")
                by_claim = {
                    candidate.claim.claim_id: candidate
                    for candidate in request.candidates
                }
                results = normalized_semantic_results(
                    SemanticValidationPayload(results=response.results),
                    request,
                )
                for result in results:
                    candidate = by_claim[result.claim_id]
                    bound = candidate.claim.with_semantic_status(result.status)
                    semantic_review_facets.append(
                        freeze_json_object(
                            {
                                "claim_sha256": canonical_sha256(bound.text),
                                "atom_id": bound.atom_id,
                                "source_support": result.source_support,
                                "question_relevance": (
                                    result.question_relevance
                                ),
                                "qualifier_fidelity": (
                                    result.qualifier_fidelity
                                ),
                                "overall_status": result.status,
                            }
                        )
                    )
                    if result.status != "supported":
                        rejected_atoms[bound.atom_id] += 1
                        claim_rejections[
                            f"SEMANTIC_{result.status.upper()}"
                        ] += 1
                        observed(bound, result.status, "MODEL_NOT_SUPPORTED")
                        continue
                    try:
                        rebound = revalidate_bound_claim(
                            bound,
                            packet=packet,
                            read_units=generation_request.evidence_read_units,
                            evidence=generation_request.evidence,
                            allowed_unit_ids=allowed_units.get(
                                bound.atom_id, frozenset()
                            ),
                            physical_table_facts=(
                                generation_request.physical_table_facts
                            ),
                            atom_fact_bindings=(
                                generation_request.atom_fact_bindings
                            ),
                            source_scope=candidate.atom.source_scope,
                        )
                        rebound = project_bound_claim(
                            rebound,
                            read_units=(
                                generation_request.evidence_read_units
                            ),
                            evidence=generation_request.evidence,
                            physical_table_facts=(
                                generation_request.physical_table_facts
                            ),
                            atom_fact_bindings=(
                                generation_request.atom_fact_bindings
                            ),
                            source_scope=candidate.atom.source_scope,
                            semantic_relation_supported=(
                                result.source_support == "supported"
                                and result.question_relevance == "answered"
                                and result.qualifier_fidelity == "faithful"
                            ),
                            question_fragment=(
                                candidate.atom.original_fragment or ""
                            ),
                            original_query=query_plan.original_query,
                        )
                    except SourceProjectionError as error:
                        if error.failure_code == (
                            "QUESTION_TABLE_ROW_LEVEL_CONFLICT"
                        ):
                            rejected_atoms[bound.atom_id] += 1
                            claim_rejections[error.failure_code] += 1
                            observed(bound, result.status, error.failure_code)
                            continue
                        observed(
                            bound,
                            result.status,
                            "HARD_BINDING_REVALIDATION_FAILED",
                        )
                        raise ValueError(
                            "语义通过后的来源身份重验失败。"
                        ) from error
                    except EvidenceBindingError:
                        observed(
                            bound,
                            result.status,
                            "HARD_BINDING_REVALIDATION_FAILED",
                        )
                        raise ValueError(
                            "语义通过后的来源身份重验失败。"
                        ) from None
                    observed(
                        bound,
                        result.status,
                        "SEMANTIC_REVIEW_VALIDATED",
                    )
                    for record_index in range(
                        len(source_projection_records) - 1, -1, -1
                    ):
                        record = dict(source_projection_records[record_index])
                        if (
                            record.get("claim_id") == rebound.claim_id
                            and record.get("draft_text_sha256")
                            == rebound.draft_text_sha256
                        ):
                            source_projection_records[record_index] = (
                                freeze_json_object(
                                    {
                                        **record,
                                        "published_text_sha256": (
                                            rebound.published_text_sha256
                                        ),
                                        "render_origin": rebound.render_origin,
                                        "selected_assertion_ids": (
                                            rebound.selected_assertion_ids
                                        ),
                                        "relation_gap": (
                                            not rebound.relation_complete
                                        ),
                                        "relation_gap_reason": (
                                            rebound.relation_gap_reason
                                        ),
                                    }
                                )
                            )
                            break
                    supported_count += 1
                    accepted.append(
                        ValidatedNaturalClaim(
                            claim_id=rebound.claim_id,
                            atom_ids=(rebound.atom_id,),
                            claim=rebound.answer_claim,
                            render_origin=rebound.render_origin,
                            selected_assertion_ids=(
                                rebound.selected_assertion_ids
                            ),
                            relation_complete=rebound.relation_complete,
                            relation_gap_reason=rebound.relation_gap_reason,
                        )
                    )
                    recovery_results.append(
                        (
                            rebound.atom_id,
                            "SEMANTIC_REVIEW_REQUIRED",
                            "SEMANTIC_REVIEW_VALIDATED",
                        )
                    )
                index += 1
            relation_review_elapsed_ms = (monotonic() - started) * 1000
            if supported_count == 0 and successful_batches and not batch_failed:
                reason = "SEMANTIC_REVIEW_NO_SUPPORTED_CLAIM"

        def classify_generation_failure(
            error: RagError,
        ) -> GenerationFailureDisposition:
            """无副作用地判断失败能否在未传输前按任务重编排。"""
            failed_packets = _failed_generation_packets(error)
            failed_calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            transported = any(
                packet.evidence_level == "TRANSPORT_SENT"
                for packet in failed_packets
            ) or any(call.call_count for call in failed_calls)
            raw_reason = error.code
            if isinstance(error, ProviderInvalidResponse):
                detailed_reason = dict(error.details).get("reason_code")
                if isinstance(detailed_reason, str):
                    raw_reason = detailed_reason
            return GenerationFailureDisposition(
                reason_code=raw_reason,
                transported=transported,
            )

        def record_generation_failure(
            error: RagError,
            atom_ids: tuple[str, ...],
            terminal: bool,
            disposition: GenerationFailureDisposition,
        ) -> None:
            """保存真实失败包；只有最终失败才改变义务终态。"""
            nonlocal attempt_linked_ids, reason
            failed_packets = _failed_generation_packets(error)
            failed_calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            prepared_packets.extend(failed_packets)
            calls.extend(failed_calls)
            sent_packet = next(
                (
                    packet
                    for packet in reversed(failed_packets)
                    if packet.evidence_level == "TRANSPORT_SENT"
                ),
                None,
            )
            if sent_packet is not None:
                attempt_linked_ids = dict(sent_packet.per_atom_support_ids)
            if terminal:
                reason = disposition.reason_code
                raw_failures.extend(
                    (atom_id, disposition.reason_code)
                    for atom_id in atom_ids
                    if (atom_id, disposition.reason_code) not in raw_failures
                )
                if disposition.reason_code == (
                    "GENERATION_INPUT_BUDGET_EXCEEDED"
                ):
                    resource_limited_atom_ids.update(atom_ids)

        def record_invalid_generation(atom_ids: tuple[str, ...]) -> None:
            """把响应合同错误限定到实际执行的 G 义务。"""
            nonlocal reason
            reason = "GENERATION_OUTPUT_INVALID"
            raw_failures.extend(
                (atom_id, reason)
                for atom_id in atom_ids
                if (atom_id, reason) not in raw_failures
            )

        def review_latest_batch(
            pending_start: int,
            *,
            allow_split: bool,
        ) -> None:
            """用该生成批自己的实际发送包复核其新增 Claim。"""
            if legacy_protocol or len(pending_relations) <= pending_start:
                return
            if active_request is None or not prepared_packets:
                raise ValueError("生成批次缺少可复核的实际发送包。")
            review_pending(
                tuple(pending_relations[pending_start:]),
                generation_request=active_request,
                sent_packet=prepared_packets[-1],
                allow_split=allow_split,
            )

        scheduled_generation_atom_ids = (
            tuple(
                atom_id
                for atom_id in deterministic_execution.deferred_atom_ids
                if atom_id in eligible
            )
            if deterministic_execution is not None
            else tuple(sorted(eligible))
        )

        def run_generation_batch(
            atom_ids: tuple[str, ...],
            allow_review_split: bool,
        ) -> None:
            """执行一个冻结 G 批次并只复核该批新增的真实 Claim。"""
            pending_start = len(pending_relations)
            consume(generate(execution_atom_ids=atom_ids))
            review_latest_batch(
                pending_start,
                allow_split=allow_review_split,
            )

        if scheduled_generation_atom_ids:
            try:
                _raise_if_cancelled(cancellation)
                execute_generation_tasks(
                    scheduled_generation_atom_ids,
                    run_batch=run_generation_batch,
                    classify_failure=classify_generation_failure,
                    record_failure=record_generation_failure,
                    record_invalid=record_invalid_generation,
                    cancellation=cancellation,
                )
                if legacy_protocol:
                    # 旧协议只为兼容既有离线 Provider；V9 线上路径不修补。
                    omitted = tuple(
                        atom.atom_id
                        for atom in query_plan.atoms
                        if atom.atom_id in eligible
                        and (
                            linked_ids.get(atom.atom_id)
                            or (
                                generation_evidence_pack is None
                                and atom_support_matrix.for_atom(
                                    atom.atom_id
                                ).status
                                is AtomStatus.SUPPORTED
                            )
                        )
                        and not _natural_atom_complete(
                            atom,
                            atom_support_matrix,
                            tuple(accepted),
                            evidence,
                            analysis,
                            generation_evidence_pack=generation_evidence_pack,
                        )
                    )
                    repairable = tuple(
                        atom_id
                        for atom_id in omitted
                        if _can_repair_atom(
                            atom_id,
                            evidence,
                            linked_ids.get(
                                atom_id,
                                atom_support_matrix.for_atom(
                                    atom_id
                                ).supporting_support_ids,
                            ),
                            tuple(raw_failures),
                        )
                    )
                    repair_skip_reason = (
                        None
                        if repairable
                        else "NON_RECOVERABLE_OR_NO_CITABLE_SOURCE"
                        if omitted
                        else "NO_MISSING_ATOM"
                    )
                    pending_atom_ids = {
                        natural.atom_id for natural in pending_legacy_relations
                    }
                    repair_first = tuple(
                        atom_id
                        for atom_id in repairable
                        if atom_id not in pending_atom_ids
                    )
                    if repair_first:
                        if pending_legacy_relations:
                            relation_review_skip_reason = (
                                "SUPPLEMENT_SLOT_RESERVED_FOR_ATOM_REPAIR"
                            )
                        _raise_if_cancelled(cancellation)
                        repair_calls = 1
                        consume(generate(repair_first))
                    elif pending_legacy_relations:
                        review_legacy_pending()
                        if (
                            repairable
                            and not relation_review_calls
                            and relation_review_skip_reason
                            == "RELATION_REVIEW_INPUT_BUDGET_EXCEEDED"
                        ):
                            _raise_if_cancelled(cancellation)
                            repair_calls = 1
                            consume(generate(repairable))
                        elif repairable and relation_review_calls:
                            repair_skip_reason = (
                                "SUPPLEMENT_SLOT_USED_BY_RELATION_REVIEW"
                            )
                    elif repairable:
                        _raise_if_cancelled(cancellation)
                        repair_calls = 1
                        consume(generate(repairable))
                else:
                    repair_skip_reason = "AUTOMATIC_GENERATION_REPAIR_DISABLED"
            except QueryCancelled as error:
                error.provider_calls = (*calls, *error.provider_calls)
                raise
            except RagError as error:
                prepared_packets.extend(_failed_generation_packets(error))
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
            and legacy_protocol
        ):
            fallback_diagnostics: list[str] = []
            fallback = _safe_extractive_fallback(
                query_plan,
                evidence,
                linked_ids,
                generation_evidence_pack.complete_group_ids,
                diagnostic_reasons=fallback_diagnostics,
                validate_claim=validate_source_excerpt,
                validate_atom_claim=validate_source_excerpt,
            )
            extractive_fallback_reason = (
                fallback_diagnostics[-1]
                if fallback_diagnostics
                else "EXTRACTIVE_FALLBACK_SELECTED"
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
                    relation_review_calls=relation_review_calls,
                    relation_review_elapsed_ms=relation_review_elapsed_ms,
                    relation_review_skip_reason=relation_review_skip_reason,
                    relation_review_results=tuple(relation_review_results),
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
                    claim_rejection_diagnostics=tuple(
                        claim_rejection_diagnostics
                    ),
                    extractive_fallback_reason=extractive_fallback_reason,
                    prepared_packets=tuple(prepared_packets),
                    repair_attempted=repair_calls > 0,
                    repair_skip_reason=repair_skip_reason,
                    raw_failures=tuple(raw_failures),
                    recovery_results=tuple(recovery_results),
                    wire_diagnostics=tuple(wire_diagnostics),
                    source_projection_records=(
                        finalized_source_projection_records()
                    ),
                )

        covered = {atom_id for item in accepted for atom_id in item.atom_ids}
        coverage: list[tuple[str, str]] = []
        missing: dict[str, MissingAtomReason] = {}
        final_plan_coverage: CompiledPlanCoverage | None = None
        use_compiled_coverage = False
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
            elif atom.atom_id in pending_field_atom_ids:
                final = AtomStatus.MISSING
                missing[atom.atom_id] = (
                    MissingAtomReason.SYSTEM_DEPENDENCY_FAILED
                )
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
                relation_incomplete = not any(
                    item.relation_complete
                    for item in accepted
                    if atom.atom_id in item.atom_ids
                )
                if relation_incomplete:
                    missing[atom.atom_id] = (
                        MissingAtomReason.EVIDENCE_NOT_DIRECT
                    )
                elif (
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
        if compiled_plan is not None and deterministic_execution is not None:
            limited_obligation_ids = tuple(
                obligation.obligation_id
                for obligation in compiled_plan.obligations
                if set(obligation.atom_ids) & resource_limited_atom_ids
            )
            resource_limited_artifacts = (
                _resource_limited_plan_artifacts(
                    compiled_plan,
                    limited_obligation_ids
                    or deterministic_execution.deferred_obligation_ids,
                )
                if reason
                in {
                    "GENERATION_INPUT_BUDGET_EXCEEDED",
                    "SEMANTIC_REVIEW_INPUT_BUDGET_EXCEEDED",
                }
                else ()
            )
            final_plan_coverage = reduce_plan_coverage(
                compiled_plan,
                (
                    *deterministic_execution.artifacts,
                    *_generation_plan_artifacts(
                        compiled_plan,
                        tuple(accepted),
                        evidence,
                        skip_claim_ids=frozenset(deterministic_claim_ids),
                    ),
                    *resource_limited_artifacts,
                ),
            )
            use_compiled_coverage = (
                strict_compiled_protocol
                or bool(deterministic_execution.artifacts)
                or any(
                    obligation.required_member_keys
                    for obligation in compiled_plan.obligations
                )
                or all(
                    atom.answer_shape is AtomAnswerShape.FACT
                    for atom in query_plan.atoms
                )
            )
            if use_compiled_coverage:
                coverage = list(
                    compiled_atom_coverage(
                        compiled_plan,
                        final_plan_coverage,
                    )
                )
                coverage_by_obligation = {
                    item.obligation_id: item
                    for item in final_plan_coverage.obligations
                }
                missing = {
                    atom_id: (
                        MissingAtomReason.EVIDENCE_NOT_DIRECT
                        if any(
                            not item.relation_complete
                            for item in accepted
                            if atom_id in item.atom_ids
                        )
                        or any(
                            coverage_by_obligation[
                                obligation.obligation_id
                            ].missing_qualifier_ids
                            for obligation in compiled_plan.obligations
                            if atom_id in obligation.atom_ids
                        )
                        else MissingAtomReason.GENERATION_INCOMPLETE
                    )
                    for atom_id, status in coverage
                    if status != AtomStatus.SUPPORTED.value
                }
                for atom_id in pending_field_atom_ids:
                    if atom_id in dict(coverage) and dict(coverage)[
                        atom_id
                    ] != (AtomStatus.SUPPORTED.value):
                        missing[atom_id] = (
                            MissingAtomReason.SYSTEM_DEPENDENCY_FAILED
                        )
        answer = render_natural_answer(
            query_plan,
            atom_support_matrix,
            tuple(accepted),
            evidence,
            missing_atoms=missing,
        )
        if final_plan_coverage is not None and use_compiled_coverage:
            target_records = _compiled_coverage_records(final_plan_coverage)
        else:
            target_records = _target_coverage_records(
                query_plan,
                evidence,
                tuple(accepted),
                analysis,
                generation_evidence_pack,
            )
        answer_plan_records = (
            _compiled_plan_records(compiled_plan, deterministic_execution)
            if compiled_plan is not None and deterministic_execution is not None
            else ()
        )
        answer_plan_coverage = (
            _compiled_coverage_records(final_plan_coverage)
            if final_plan_coverage is not None
            else ()
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
                target_member_coverage=target_records,
                repair_calls=repair_calls,
                relation_review_calls=relation_review_calls,
                relation_review_elapsed_ms=relation_review_elapsed_ms,
                relation_review_skip_reason=relation_review_skip_reason,
                relation_review_results=tuple(relation_review_results),
                semantic_review_facets=tuple(semantic_review_facets),
                claim_rejection_codes=tuple(sorted(claim_rejections.items())),
                generated_claim_count=generated_claim_count,
                accepted_claim_count=len(accepted),
                generation_gap_count=generation_gap_count,
                missing_atom_reasons=tuple(
                    (atom_id, value.value) for atom_id, value in missing.items()
                ),
                false_limited_detected=false_limited_detected,
                accepted_support_ids=accepted_support_ids,
                claim_rejection_diagnostics=tuple(claim_rejection_diagnostics),
                extractive_fallback_reason=extractive_fallback_reason,
                prepared_packets=tuple(prepared_packets),
                repair_attempted=repair_calls > 0,
                repair_skip_reason=repair_skip_reason,
                raw_failures=tuple(raw_failures),
                recovery_results=tuple(recovery_results),
                wire_diagnostics=tuple(wire_diagnostics),
                source_projection_records=(
                    finalized_source_projection_records()
                ),
                answer_plan_id=(
                    compiled_plan.plan_id if compiled_plan is not None else None
                ),
                answer_plan_revision=(
                    compiled_plan.schema_revision
                    if compiled_plan is not None
                    else None
                ),
                answer_plan_records=answer_plan_records,
                answer_plan_coverage=answer_plan_coverage,
            )
        published = list(accepted_support_ids)
        for matrix_atom in atom_support_matrix.atoms:
            published.extend(
                resolve_conflict_support_ids(matrix_atom, evidence)
            )
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
            answer=answer,
            mode=(
                "llm"
                if any(
                    item.claim_id not in deterministic_claim_ids
                    for item in accepted
                )
                else "extractive"
                if deterministic_claim_ids
                else "none"
            ),
            calls=tuple(calls),
            reason_code="CONTRADICTORY_EVIDENCE"
            if has_conflict
            else field_resolution_reason
            if pending_field_atom_ids
            else "LIMITED_ANSWER"
            if missing
            else "CLAIMS_VALIDATED",
            published_support_ids=published_ids,
            ocr_verification_states=verification_states,
            atom_coverage=tuple(coverage),
            target_member_coverage=target_records,
            repair_calls=repair_calls,
            relation_review_calls=relation_review_calls,
            relation_review_elapsed_ms=relation_review_elapsed_ms,
            relation_review_skip_reason=relation_review_skip_reason,
            relation_review_results=tuple(relation_review_results),
            semantic_review_facets=tuple(semantic_review_facets),
            claim_rejection_codes=tuple(sorted(claim_rejections.items())),
            generated_claim_count=generated_claim_count,
            accepted_claim_count=len(accepted),
            published_claim_count=published_claim_count,
            generation_gap_count=generation_gap_count,
            missing_atom_reasons=tuple(
                (atom_id, value.value) for atom_id, value in missing.items()
            ),
            false_limited_detected=false_limited_detected,
            accepted_support_ids=accepted_support_ids,
            claim_rejection_diagnostics=tuple(claim_rejection_diagnostics),
            extractive_fallback_reason=extractive_fallback_reason,
            prepared_packets=tuple(prepared_packets),
            repair_attempted=repair_calls > 0,
            repair_skip_reason=repair_skip_reason,
            raw_failures=tuple(raw_failures),
            recovery_results=tuple(recovery_results),
            wire_diagnostics=tuple(wire_diagnostics),
            source_projection_records=finalized_source_projection_records(),
            answer_plan_id=(
                compiled_plan.plan_id if compiled_plan is not None else None
            ),
            answer_plan_revision=(
                compiled_plan.schema_revision
                if compiled_plan is not None
                else None
            ),
            answer_plan_records=answer_plan_records,
            answer_plan_coverage=answer_plan_coverage,
        )


def _validate_legacy_generation_packet(
    request: GenerationRequest, draft: AnswerDraft
) -> None:
    """兼容协议同样核对发送集合；已计账的传输降级有独立 attempt。"""
    packet = draft.prepared_packet
    if packet is None:
        return
    chain = (*draft.previous_prepared_packets, packet)
    by_id = {item.support_id: item for item in request.evidence}
    if (
        chain[0].attempt_id != request.attempt_id
        or any(value.request_id != request.request_id for value in chain)
        or len({value.attempt_id for value in chain}) != len(chain)
        or any(
            alias not in by_id or stable_support_key(by_id[alias]) != key
            for value in chain
            for alias, key in value.alias_to_support_key
        )
    ):
        raise ValidationFailed(
            "生成包身份不属于当前请求与证据集合。",
            stage="answer.validate",
            code="GENERATION_PACKET_IDENTITY_MISMATCH",
        )
    if any(
        support.support_id not in packet.sent_support_ids
        for claim in draft.claims
        for support in claim.supports
    ):
        raise ValidationFailed(
            "事实引用没有进入本次实际发送包。",
            stage="answer.validate",
            code="CLAIM_SUPPORT_NOT_SENT",
        )


def _generation_attempt_allowance(
    request: GenerationRequest, draft: AnswerDraft
) -> dict[str, tuple[str, ...]]:
    """本次实际发送和 Atom 阅读许可取交集，拒绝跨请求别名污染。"""
    requested = dict(request.per_atom_candidate_support_ids)
    packet = draft.prepared_packet
    if packet is None:
        # 兼容离线固定 Provider；真实 adapter 必须返回发送包身份。
        return requested
    by_id = {item.support_id: item for item in request.evidence}
    if (
        packet.request_id != request.request_id
        or packet.attempt_id != request.attempt_id
        or any(
            alias not in by_id or stable_support_key(by_id[alias]) != key
            for alias, key in packet.alias_to_support_key
        )
        or any(
            atom not in requested or not set(ids) <= set(requested[atom])
            for atom, ids in packet.per_atom_support_ids
        )
    ):
        raise ValidationFailed(
            "生成包身份不属于当前请求与证据集合。",
            stage="answer.validate",
            code="GENERATION_PACKET_IDENTITY_MISMATCH",
        )
    sent = set(packet.sent_support_ids)
    return {
        atom: tuple(alias for alias in ids if alias in sent)
        for atom, ids in packet.per_atom_support_ids
    }


def _atom_validation_evidence(
    atom_id: str,
    evidence: tuple[EvidenceItem, ...],
    certificates: tuple[tuple[str, str, JsonObject], ...],
) -> tuple[EvidenceItem, ...]:
    """同一来源身份可以有不同 Atom 证书，核验时只应用当前关系。"""
    if not certificates:
        return evidence
    by_key = {
        key: certificate
        for certificate_atom, key, certificate in certificates
        if certificate_atom == atom_id
    }
    result: list[EvidenceItem] = []
    for item in evidence:
        metadata = dict(item.metadata)
        metadata.pop("answer_support", None)
        certificate = by_key.get(stable_support_key(item))
        if certificate is not None:
            metadata["answer_support"] = dict(certificate)
        result.append(
            item.model_copy(update={"metadata": freeze_json_object(metadata)})
        )
    return tuple(result)


def _can_repair_atom(
    atom_id: str,
    evidence: tuple[EvidenceItem, ...],
    allowed_support_ids: tuple[str, ...],
    failures: tuple[tuple[str, str], ...],
) -> bool:
    """只修组织缺项和低风险语义组织错误，硬边界失败不再试探。"""
    recoverable = {
        "GENERATION_ABSTAINED",
        "GENERATION_CLAIMS_INVALID",
        "GENERATION_INCOMPLETE",
        "CLAIM_TEXT_UNSUPPORTED",
        "CLAIM_FRAGMENT_INCOMPLETE",
        "CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
        "CLAIM_QUERY_RELATION_UNDETERMINED",
    }
    if any(
        atom == atom_id and reason not in recoverable
        for atom, reason in failures
    ):
        return False
    return any(
        item.support_id in allowed_support_ids
        and item.publishable
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
        and source_compatibility((item,)).compatible
        for item in evidence
    )


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
        score = 0
        for item, sentence in items:
            coordinate = _table_cell_coordinate(item)
            if coordinate is None or coordinate[1] == 0 or coordinate[2] != 0:
                continue
            label, separator, remainder = sentence.partition("|")
            if separator and remainder.strip():
                continue
            label = _STOP.sub("", label.casefold())
            if not (
                _FALLBACK_TABLE_LABEL_MIN_CHARS
                <= len(label)
                <= _FALLBACK_TABLE_LABEL_MAX_CHARS
            ):
                continue
            if label in query:
                score = max(score, len(label))
            elif (
                len(label) >= _FALLBACK_TABLE_SHORT_NAME_MIN_CHARS
                and label[-_FALLBACK_TABLE_SHORT_NAME_SUFFIX_CHARS:] in query
            ):
                # 口语简称只在命中行名尾部且本次唯一时使用。
                score = max(score, _FALLBACK_TABLE_SHORT_NAME_SUFFIX_CHARS)
        if score:
            matches.append((score, items))
    if not matches:
        return []
    best = max(score for score, _items in matches)
    winners = [items for score, items in matches if score == best]
    return winners[0] if len(winners) == 1 else []


def _fallback_named_table_cells(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    related: set[str],
) -> list[tuple[EvidenceItem, str]]:
    """按明确行名取同一表格行的原文；合并单元格只展示重复原文。"""
    labels: list[tuple[tuple[object, ...], int, EvidenceItem]] = []
    for item in evidence:
        coordinate = _table_cell_coordinate(item)
        if (
            item.support_id in related
            and coordinate is not None
            and coordinate[1] > 0
            and coordinate[2] == 0
            and item.source_spans
            and all(span.is_citable for span in item.source_spans)
            and named_table_label_in_query(
                plan.resolved_root_query, item.citation_text.strip(" |")
            )
        ):
            labels.append((coordinate[0], coordinate[1], item))
    rows = {(table, row) for table, row, _ in labels}
    if len(rows) != 1:
        return []
    table, row = next(iter(rows))
    members = [
        (item, item.citation_text.strip())
        for item in evidence
        if item.support_id in related
        and (coordinate := _table_cell_coordinate(item)) is not None
        and coordinate[0] == table
        and coordinate[1] == row
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
    ]
    if not members:
        return []
    # 被纵向合并的单元格可能位于上一行，只有同一 canonical Chunk
    # 明确复用该原文时才把它作为本行的共同说明。
    member_chunks = {item.chunk_id for item, _ in members}
    shared_nodes = {
        span.node_id
        for item in evidence
        if item.chunk_id in member_chunks
        for span in item.source_spans
        if span.is_repeated and span.node_id
    }
    repeated = [
        (item, item.citation_text.strip())
        for item in evidence
        if item.support_id in related
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
        and any(
            span.is_repeated and span.node_id in shared_nodes
            for span in item.source_spans
        )
        and (coordinate := _table_cell_coordinate(item)) is not None
        and coordinate[0] == table
    ]
    selected: list[tuple[EvidenceItem, str]] = []
    seen_ids: set[str] = set()
    for item, excerpt in (*repeated, *members):
        if item.support_id not in seen_ids:
            selected.append((item, excerpt))
            seen_ids.add(item.support_id)
    if (
        len(plan.atoms) == 1
        and plan.atoms[0].answer_shape is AtomAnswerShape.DURATION
    ):
        label_ids = {label.support_id for _, _, label in labels}
        selected = [
            pair
            for pair in selected
            if pair[0].support_id in label_ids
            or _FALLBACK_DURATION.search(pair[1])
        ]
        if not any(
            _FALLBACK_DURATION.search(excerpt)
            and item.support_id not in label_ids
            for item, excerpt in selected
        ):
            return []
    return selected


def _fallback_partial_table_row(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    related: set[str],
    scoped_versions: frozenset[str] | None,
    question_terms: set[str],
) -> list[tuple[EvidenceItem, str]]:
    """只摘录唯一表格块中可引用的原句与独立数值单元格。"""
    by_row: dict[
        tuple[str | None, str, int], list[tuple[EvidenceItem, str]]
    ] = {}
    for item in evidence:
        if (
            item.support_id not in related
            or not item.table_context
            or (
                scoped_versions is not None
                and item.document_version_id not in scoped_versions
            )
            or not item.source_spans
            or any(not span.is_citable for span in item.source_spans)
        ):
            continue
        metadata = dict(item.metadata)
        table_node = metadata.get("table_logical_node_id")
        row_index = metadata.get("table_logical_row_index")
        if (
            not isinstance(table_node, str)
            or not isinstance(row_index, int)
            or isinstance(row_index, bool)
        ):
            continue
        excerpt = item.citation_text.strip()
        if excerpt.endswith(("。", "；", ";")) or _NUMBER.fullmatch(excerpt):
            key = (item.document_version_id, table_node, row_index)
            by_row.setdefault(key, []).append((item, excerpt))
    eligible: list[list[tuple[EvidenceItem, str]]] = []
    query = " ".join((plan.original_query, plan.resolved_root_query))
    for items in by_row.values():
        if not (
            _FALLBACK_MIN_TABLE_ITEMS
            <= len(items)
            <= _FALLBACK_MAX_ORDINARY_EXCERPTS
        ):
            continue
        values = [pair for pair in items if _NUMBER.fullmatch(pair[1])]
        sentences = [
            pair for pair in items if pair[1].endswith(("。", "；", ";"))
        ]
        if len(values) != 1 or not sentences:
            continue
        if not any(
            not span.is_repeated
            for item, _ in items
            for span in item.source_spans
        ):
            continue
        title = str(
            dict(items[0][0].metadata).get("document_title")
            or items[0][0].display_name
            or ""
        )
        if (
            _longest_common_han_run(query, title)
            < _CONTEXT_SOURCE_MIN_MATCH_CHARS
        ):
            continue
        if (
            len(
                question_terms
                & _terms(" ".join(excerpt for _, excerpt in items))
            )
            < _FALLBACK_MIN_BIGRAM_OVERLAP
        ):
            continue
        eligible.append(items)
    # 行名未闭合时不以排名猜测多个候选行的关系。
    return eligible[0] if len(eligible) == 1 else []


def _fallback_source_node(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    related: set[str],
) -> list[tuple[EvidenceItem, str]]:
    """把同一原文段落分块后的步骤重新按来源位置展示。"""
    query_terms = _terms(plan.resolved_root_query)
    nodes: dict[tuple[str | None, str], list[tuple[EvidenceItem, str]]] = {}
    for item in evidence:
        if (
            item.support_id not in related
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
        if node_id is None:
            continue
        nodes.setdefault((item.document_version_id, node_id), []).append(
            (item, sentence)
        )
    candidates = [
        (len(_terms(" ".join(text for _, text in items)) & query_terms), items)
        for items in nodes.values()
        if len({text for _, text in items}) >= _FALLBACK_NODE_MIN_EXCERPTS
        and source_compatibility(
            tuple(item for item, _text in items)
        ).compatible
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


def _fallback_prior_stage_excerpts(
    selected: list[tuple[EvidenceItem, str]],
    grouped: dict[str, list[tuple[EvidenceItem, str]]],
    complete_ids: frozenset[str],
    question_terms: set[str],
) -> list[tuple[EvidenceItem, str]]:
    """仅补入同文档章节、紧邻且已闭合的前序列表来源。"""
    selected_groups = {
        group_id
        for item, _ in selected
        if (group_id := dict(item.metadata).get("evidence_group_id"))
        in complete_ids
        and dict(item.metadata).get("evidence_group_type")
        not in {"TABLE_ROW_GROUP", "CATALOG_ENTRY"}
    }
    if len(selected_groups) != 1:
        return selected
    origins = {
        (item.document_version_id, item.section_id) for item, _ in selected
    }
    if len(origins) != 1 or None in next(iter(origins)):
        return selected
    origin = next(iter(origins))

    def source_ordinal(item: EvidenceItem) -> int | None:
        return min(
            (
                span.source_anchor.ordinal
                for span in item.source_spans
                if span.is_citable and span.source_anchor is not None
            ),
            default=None,
        )

    first = min(
        (
            ordinal
            for item, _ in selected
            if (ordinal := source_ordinal(item)) is not None
        ),
        default=None,
    )
    if first is None:
        return selected
    by_chunk: dict[str, tuple[EvidenceItem, str]] = {}
    for group_id, items in grouped.items():
        if group_id not in complete_ids or group_id in selected_groups:
            continue
        for item, sentence in items:
            ordinal = source_ordinal(item)
            if (
                (item.document_version_id, item.section_id) != origin
                or dict(item.metadata).get("evidence_group_type")
                in {"TABLE_ROW_GROUP", "CATALOG_ENTRY"}
                or ordinal is None
                or not 0 < first - ordinal <= _FALLBACK_PREDECESSOR_MAX_GAP
                or len(_terms(sentence) & question_terms)
                < _FALLBACK_MIN_BIGRAM_OVERLAP
                or _FALLBACK_LIST_MARKER.fullmatch(sentence)
            ):
                continue
            previous = by_chunk.get(item.chunk_id)
            if previous is None or len(sentence) > len(previous[1]):
                by_chunk[item.chunk_id] = (item, sentence)
    preceding = sorted(
        by_chunk.values(),
        key=lambda pair: -(source_ordinal(pair[0]) or 0),
    )[:_FALLBACK_PREDECESSOR_LIMIT]
    return [*preceding, *selected]


def _fallback_continues_fragment(
    previous: EvidenceItem,
    current: EvidenceItem,
    previous_excerpt: str,
    current_sentence: str,
    _complete_ids: frozenset[str],
) -> bool:
    """只有统一来源合同证明首尾相接时才恢复截断原句。"""
    if not previous_excerpt.endswith(("，", "、")):
        return False
    if _LEADING_SECTION_MARKER.match(current_sentence):
        return False
    decision = source_compatibility((previous, current))
    return bool(
        decision.compatible
        and decision.reason == "CONTIGUOUS_NODE"
        and len(previous.source_spans) == len(current.source_spans) == 1
        and previous.source_spans[0].source_end_char
        == current.source_spans[0].source_start_char
    )


def _fallback_complete_selected_nodes(
    selected: list[tuple[EvidenceItem, str]],
    evidence: tuple[EvidenceItem, ...],
) -> list[tuple[EvidenceItem, str]]:
    """仅沿同一原文节点的连续 SourceSpan 补全已选片段。"""
    nodes: dict[
        tuple[str | None, str, tuple[str, ...]],
        list[tuple[EvidenceItem, str, int, int]],
    ] = {}
    for item in evidence:
        if len(item.source_spans) != 1:
            continue
        span = item.source_spans[0]
        if (
            not span.is_citable
            or span.node_id is None
            or type(span.source_start_char) is not int
            or type(span.source_end_char) is not int
        ):
            continue
        key = (item.document_version_id, span.node_id, span.structural_path)
        nodes.setdefault(key, []).append(
            (
                item,
                item.citation_text.strip(),
                span.source_start_char,
                span.source_end_char,
            )
        )
    completed = list(selected)
    seen = {item.support_id for item, _ in selected}
    for item, _ in selected:
        if len(item.source_spans) != 1:
            continue
        span = item.source_spans[0]
        if span.node_id is None:
            continue
        key = (item.document_version_id, span.node_id, span.structural_path)
        ordered = sorted(nodes.get(key, ()), key=lambda row: (row[2], row[3]))
        if len(ordered) < _FALLBACK_NODE_MIN_EXCERPTS:
            continue
        current = next(
            (
                index
                for index, row in enumerate(ordered)
                if row[0].support_id == item.support_id
            ),
            None,
        )
        if current is None:
            continue
        left = current
        right = current
        while (
            left > 0
            and ordered[left - 1][3] == ordered[left][2]
            and source_compatibility(
                (ordered[left - 1][0], ordered[left][0])
            ).compatible
        ):
            left -= 1
        while (
            right + 1 < len(ordered)
            and ordered[right][3] == ordered[right + 1][2]
            and source_compatibility(
                (ordered[right][0], ordered[right + 1][0])
            ).compatible
        ):
            right += 1
        for peer, excerpt, _, _ in ordered[left : right + 1]:
            if peer.support_id not in seen:
                completed.append((peer, excerpt))
                seen.add(peer.support_id)
    return completed


def _certified_table_excerpts(  # noqa: PLR0912
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    linked_ids: dict[str, tuple[str, ...]],
    validate_atom_claim: Callable[[AnswerClaim, str], bool] | None,
) -> tuple[str, tuple[str, ...], frozenset[str]] | None:
    """原有交点证书的三个成员必须一起进入最终事实门。"""
    if validate_atom_claim is None:
        return None
    lines: list[str] = []
    ids: list[str] = []
    covered: set[str] = set()
    for atom in plan.atoms:
        semantics = _natural_atom_analysis(atom, None).semantics
        allowed = set(linked_ids.get(atom.atom_id, ()))
        seen: set[tuple[str, ...]] = set()
        for item in evidence:
            certificate = dict(item.metadata).get("answer_support")
            if not (
                isinstance(certificate, dict)
                and certificate.get("status") == "SUPPORTED"
                and certificate.get("support_reason") == "TABLE_INTERSECTION"
                and semantics.target
                and semantics.relation
                and normalize_semantic_text(
                    str(certificate.get("query_target") or "")
                )
                == normalize_semantic_text(semantics.target)
                and normalize_semantic_text(
                    str(
                        certificate.get("requested_relation_or_attribute") or ""
                    )
                )
                == normalize_semantic_text(semantics.relation)
            ):
                continue
            nodes = certificate.get("supporting_span_ids")
            if not isinstance(nodes, list) or not all(
                isinstance(node, str) for node in nodes
            ):
                continue
            identity = tuple(node for node in nodes if isinstance(node, str))
            if (
                len(set(identity)) < _TABLE_INTERSECTION_MIN_SPANS
                or len(identity) != len(set(identity))
                or identity in seen
            ):
                continue
            seen.add(identity)
            units = tuple(
                unit
                for unit in evidence
                if unit.support_id in allowed
                and dict(unit.metadata).get("answer_support") == certificate
                and len(unit.source_spans) == 1
                and unit.source_spans[0].node_id in identity
            )
            if len(units) != len(identity):
                continue
            try:
                _validate_table_claim_certificate(units)
            except ValidationFailed:
                continue
            if source_compatibility(units).reason != "TABLE_INTERSECTION":
                continue
            values = tuple(
                unit
                for unit in units
                if (coordinate := table_cell_coordinate(unit)) is not None
                and coordinate[1] > 0
                and coordinate[2] > 0
            )
            if len(values) != 1:
                continue
            claim = AnswerClaim(
                text=values[0].citation_text,
                supports=tuple(
                    ClaimSupport(
                        support_id=unit.support_id, quote=unit.citation_text
                    )
                    for unit in units
                ),
            )
            try:
                _validate_natural_request_support(atom, claim, units, None)
            except ValidationFailed:
                continue
            if not validate_atom_claim(claim, atom.atom_id):
                continue
            refs = " ".join(f"[{unit.support_id}]" for unit in units)
            lines.append(f"- {claim.text} {refs}")
            ids.extend(unit.support_id for unit in units)
            covered.add(atom.atom_id)
    if not lines:
        return None
    return (
        "资料中与该问题直接相关的规定如下：\n" + "\n".join(lines),
        tuple(dict.fromkeys(ids)),
        frozenset(covered),
    )


def _safe_extractive_fallback(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    linked_ids: dict[str, tuple[str, ...]],
    complete_group_ids: tuple[str, ...] = (),
    *,
    require_named_row: bool = False,
    diagnostic_reasons: list[str] | None = None,
    validate_claim: Callable[[AnswerClaim], bool] | None = None,
    validate_atom_claim: Callable[[AnswerClaim, str], bool] | None = None,
) -> tuple[str, tuple[str, ...], frozenset[str]] | None:
    """模型未形成可发布事实时，仅展示相关且可引用的来源原句。

    Args:
        plan: 当前查询计划。
        evidence: 已准入的模型证据。
        linked_ids: 每个 Atom 允许使用的 Support ID。
        complete_group_ids: 已确认完整的来源组。
        require_named_row: 是否只允许带明确行名的完整表格行。
        diagnostic_reasons: 可选的 SAFE 失败原因接收列表，不含正文。
        validate_claim: 应用最终事实门；未通过的摘录不得进入渲染器。
        validate_atom_claim: 交点摘录必须指定当前 Atom 并保留完整根问题约束。

    Returns:
        可发布原句、Support ID 与覆盖 Atom；无安全结果时返回 ``None``。

    """

    def reject(reason: str) -> None:
        """记录最后一个稳定失败分支，同时保持原返回合同。"""
        if diagnostic_reasons is not None:
            diagnostic_reasons.append(reason)

    if not linked_ids:
        reject("NO_LINKED_SUPPORTS")
        return None
    certified_excerpt = _certified_table_excerpts(
        plan, evidence, linked_ids, validate_atom_claim
    )
    if certified_excerpt is not None:
        return certified_excerpt
    scoped_versions = _contextual_source_versions(plan, evidence)
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
        if (
            scoped_versions is not None
            and item.document_version_id not in scoped_versions
        ):
            continue
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
            or not item.source_spans
            or any(not span.is_citable for span in item.source_spans)
            or _FALLBACK_ORPHAN_HEADING.fullmatch(item.citation_text.strip())
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
        if not sentences and (
            structured
            or item.citation_text.rstrip().endswith(("；", ";"))
            or (
                item.table_context
                and _FALLBACK_DURATION.search(item.citation_text)
            )
            or (
                item.table_context
                and len(item.citation_text.strip())
                >= _FALLBACK_TABLE_ACTION_MIN_CHARS
                and any(
                    (
                        len(normalize_semantic_text(atom.target))
                        >= _FALLBACK_TABLE_SUBJECT_MIN_CHARS
                        and normalize_semantic_text(atom.target)
                        in normalize_semantic_text(item.citation_text)
                    )
                    or _longest_common_han_run(
                        plan.original_query, item.citation_text
                    )
                    >= _FALLBACK_MIN_QUESTION_ANCHOR_CHARS + 1
                    for atom in plan.atoms
                    if atom.answer_shape
                    in {
                        AtomAnswerShape.DUTIES,
                        AtomAnswerShape.ENUMERATION,
                        AtomAnswerShape.PROCEDURE,
                    }
                )
            )
        ):
            sentences = (item.citation_text.strip(),)
        if not sentences:
            continue
        matched = max(
            sentences,
            key=lambda sentence: len(_terms(sentence) & question_terms),
        )
        if (
            structured
            and isinstance(group_id, str)
            and group_id in complete_ids
            and metadata.get("evidence_group_type")
            in {"LIST_GROUP", "PROCEDURE_GROUP"}
        ):
            matched = item.citation_text.strip()
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
            and (
                original_trigrams & sentence_trigrams
                or (
                    len(_han_text(plan.original_query))
                    <= _FALLBACK_SHORT_QUESTION_CHARS
                    and overlap >= _FALLBACK_MIN_BIGRAM_OVERLAP
                )
            )
            and (not direct_duration or _FALLBACK_DURATION.search(matched))
        ):
            ordinary.append((overlap, index, item, matched))
    selected: list[tuple[EvidenceItem, str]] = []
    named_row_selected = False
    partial_table_selected = False
    selected = _fallback_named_table_cells(plan, evidence, related)
    named_row_selected = bool(selected)
    if not selected and direct_duration and ordinary:
        _, _, item, sentence = max(ordinary, key=lambda row: (row[0], -row[1]))
        selected = [(item, sentence)]
    multi_part = len(plan.atoms) > 1 or any(
        atom.answer_shape
        in {
            AtomAnswerShape.ENUMERATION,
            AtomAnswerShape.PROCEDURE,
            AtomAnswerShape.DUTIES,
        }
        for atom in plan.atoms
    )
    if not selected and multi_part:
        selected = _fallback_table_row(plan, grouped)
        named_row_selected = bool(selected)
    if require_named_row and not named_row_selected:
        reject("NAMED_ROW_REQUIRED")
        return None
    if not selected and multi_part:
        selected = _fallback_partial_table_row(
            plan, evidence, related, scoped_versions, question_terms
        )
        partial_table_selected = bool(selected)
    ordinary_selection = [
        (item, sentence)
        for _, _, item, sentence in sorted(
            ordinary, key=lambda row: (-row[0], row[1])
        )[:_FALLBACK_MAX_ORDINARY_EXCERPTS]
    ]
    if not selected and not multi_part:
        selected = ordinary_selection
    if not selected:
        scoped_evidence = tuple(
            item
            for item in evidence
            if scoped_versions is None
            or item.document_version_id in scoped_versions
        )
        selected = _fallback_source_node(plan, scoped_evidence, related)
        if any(
            atom.answer_shape
            in {AtomAnswerShape.PROCEDURE, AtomAnswerShape.DUTIES}
            for atom in plan.atoms
        ):
            # 只有流程或职责问题需要把已命中段落扩展到同组后续步骤。
            source_groups = {
                group_id
                for item, _ in selected
                if isinstance(
                    group_id := dict(item.metadata).get("evidence_group_id"),
                    str,
                )
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
    if (
        not selected
        and len(plan.atoms) == 1
        and _FALLBACK_FOCUSED_MIN_CHARS
        <= len(_han_text(plan.original_query))
        <= _FALLBACK_SHORT_QUESTION_CHARS
        and grouped
    ):
        focused = max(
            (
                (len(_terms(sentence) & question_terms), -index, item, sentence)
                for index, item in enumerate(evidence)
                for members in grouped.values()
                for member, sentence in members
                if member.support_id == item.support_id
            ),
            key=lambda row: row[:2],
            default=None,
        )
        if focused is not None and focused[0] >= _FALLBACK_MIN_BIGRAM_OVERLAP:
            selected = [(focused[2], focused[3])]
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
        if (
            len(
                question_terms
                & _terms(" ".join(item.citation_text for item, _ in best_group))
            )
            >= _FALLBACK_MIN_BIGRAM_OVERLAP
        ):
            factual = all(
                atom.answer_shape
                not in {
                    AtomAnswerShape.ENUMERATION,
                    AtomAnswerShape.PROCEDURE,
                    AtomAnswerShape.DUTIES,
                }
                for atom in plan.atoms
            )

            def focused_members(
                members: list[tuple[EvidenceItem, str]],
                uncovered: set[str],
            ) -> list[tuple[EvidenceItem, str]]:
                if not factual or len(plan.atoms) == 1:
                    return list(members)
                return [
                    max(
                        members,
                        key=lambda pair: (
                            len(_terms(pair[1]) & uncovered),
                            len(_terms(pair[1]) & question_terms),
                        ),
                    )
                ]

            selected = focused_members(best_group, question_terms)
            if len(plan.atoms) > 1 and factual:
                # 复合事实问句可取同一章节内第二组原句；要求它有完整
                # 句尾，避免把半句或相邻标题当成额外答案。
                source = (
                    best_group[0][0].document_version_id,
                    best_group[0][0].section_id,
                )
                for _, members in ranked_groups[1:]:
                    if (
                        (
                            members[0][0].document_version_id,
                            members[0][0].section_id,
                        )
                        != source
                        or not any(
                            sentence.rstrip().endswith(("。", "；", ";"))
                            for _, sentence in members
                        )
                        or len(
                            question_terms
                            & _terms(" ".join(text for _, text in members))
                        )
                        < _FALLBACK_MIN_BIGRAM_OVERLAP
                    ):
                        continue
                    uncovered = question_terms - _terms(
                        " ".join(sentence for _, sentence in selected)
                    )
                    selected.extend(focused_members(members, uncovered))
                    break
    if not selected:
        selected = ordinary_selection
    if not selected:
        reject("NO_SAFE_EXCERPT")
        return None
    requested_levels = {
        unicodedata.normalize("NFKC", match.group()).casefold()
        for match in _EXPLICIT_LEVEL.finditer(plan.resolved_root_query)
    }
    selected_levels = {
        unicodedata.normalize("NFKC", match.group()).casefold()
        for _, sentence in selected
        for match in _EXPLICIT_LEVEL.finditer(sentence)
    }
    if requested_levels and selected_levels - requested_levels:
        reject("SOURCE_LEVEL_MISMATCH")
        return None
    selected = _fallback_complete_selected_nodes(selected, evidence)
    if (
        len(plan.atoms) > 1
        and all(
            atom.answer_shape
            not in {
                AtomAnswerShape.ENUMERATION,
                AtomAnswerShape.PROCEDURE,
                AtomAnswerShape.DUTIES,
            }
            for atom in plan.atoms
        )
        and grouped
        and not (named_row_selected or partial_table_selected)
    ):
        # 复合事实可从同版资料中的邻近完整条款补一条尚未覆盖的问意。
        # 保持独立摘录和引用，不跨来源组拼成单句。
        source_version = selected[0][0].document_version_id
        selected_positions = [
            span.source_anchor.ordinal
            for item, _ in selected
            for span in item.source_spans
            if span.source_anchor is not None
        ]
        uncovered = question_terms - _terms(
            " ".join(sentence for _, sentence in selected)
        )
        existing_ids = {item.support_id for item, _ in selected}
        existing_groups = {
            dict(item.metadata).get("evidence_group_id") for item, _ in selected
        }
        complements = (
            (
                len(_terms(sentence) & uncovered),
                len(_terms(sentence) & question_terms),
                item,
                sentence,
            )
            for members in grouped.values()
            for item, sentence in members
            if item.support_id not in existing_ids
            and item.document_version_id == source_version
            and dict(item.metadata).get("evidence_group_id")
            not in existing_groups
            and selected_positions
            and any(
                abs(span.source_anchor.ordinal - position)
                <= _FALLBACK_PREDECESSOR_MAX_GAP * 2
                for span in item.source_spans
                if span.source_anchor is not None
                for position in selected_positions
            )
            and sentence.rstrip().endswith(("。", "；", ";"))
        )
        complement = max(complements, key=lambda row: row[:2], default=None)
        if complement is not None and complement[0] >= 1:
            selected.append((complement[2], complement[3]))
    sequence_requested = any(
        atom.answer_shape in {AtomAnswerShape.PROCEDURE, AtomAnswerShape.DUTIES}
        for atom in plan.atoms
    ) or any(term in plan.original_query for term in ("步骤", "流程", "阶段"))
    if grouped and sequence_requested:
        selected = _fallback_prior_stage_excerpts(
            selected, grouped, complete_ids, question_terms
        )
    complete_selected_groups: set[str] = set()
    for item, _ in selected:
        metadata = dict(item.metadata)
        group_id = metadata.get("evidence_group_id")
        if (
            not isinstance(group_id, str)
            or group_id not in complete_ids
            or metadata.get("evidence_group_type")
            in {"TABLE_ROW_GROUP", "CATALOG_ENTRY"}
            or group_id not in grouped
        ):
            continue
        members = grouped[group_id]
        numbered_members = sum(
            bool(
                _LEADING_LIST_MARKER.match(sentence)
                or _LEADING_SECTION_MARKER.match(sentence)
            )
            for _, sentence in members
        )
        if (
            sequence_requested
            and numbered_members >= _FALLBACK_SEQUENCE_MIN_PROCEDURE_MEMBERS
        ) or (
            len(_han_text(plan.original_query))
            <= _FALLBACK_SHORT_QUESTION_CHARS
            and numbered_members >= _FALLBACK_SEQUENCE_MIN_NUMBERED_MEMBERS
        ):
            complete_selected_groups.add(group_id)
    if complete_selected_groups:
        selected_ids = {item.support_id for item, _ in selected}
        for group_id in complete_selected_groups:
            for item, sentence in grouped[group_id]:
                if item.support_id not in selected_ids:
                    selected.append((item, sentence))
                    selected_ids.add(item.support_id)
    focus_match = (
        _YES_NO_ACTION_FOCUS.search(plan.original_query.strip())
        if len(plan.atoms) == 1
        else None
    )
    if (
        focus_match is not None
        and selected
        and not any(
            _query_focus_in_source(focus_match["focus"], sentence)
            for _, sentence in selected
        )
    ):
        # 是非问已有同版、同动作原句时，不用仅同主题的摘录代答。
        selected_versions = {item.document_version_id for item, _ in selected}
        focused_sources = [
            (item, sentence.strip())
            for item in evidence
            if item.document_version_id in selected_versions
            and item.source_spans
            and all(span.is_citable for span in item.source_spans)
            for sentence in re.findall(
                r"[^。！？!?；;]+[。！？!?；;]?", item.citation_text
            )
            if _query_focus_in_source(focus_match["focus"], sentence)
        ]
        unique_focused = {
            (item.document_version_id, sentence)
            for item, sentence in focused_sources
        }
        if len(unique_focused) == 1:
            selected = [focused_sources[0]]
    selected.sort(
        key=lambda pair: min(
            (
                (
                    span.source_anchor.ordinal,
                    span.source_start_char or 0,
                )
                for span in pair[0].source_spans
                if span.source_anchor is not None
            ),
            default=(2**31 - 1, 2**31 - 1),
        ),
    )
    lines = ["资料中与该问题直接相关的规定如下："]
    ids: list[str] = []
    seen_excerpts: set[tuple[str | None, str]] = set()
    pending_item: EvidenceItem | None = None
    pending_excerpt = ""
    pending_ids: list[str] = []
    pending_supports: list[ClaimSupport] = []

    def emit_excerpt() -> bool:
        """先冻结最终事实与引文，再执行校验并只做格式化。

        Args:
            无参数；读取当前有界原文及独立引用。

        Returns:
            摘录通过核验并进入格式化结果时为 True。

        """
        if not pending_ids or pending_excerpt.endswith(("，", "、")):
            return False
        claim = AnswerClaim(
            text=pending_excerpt, supports=tuple(pending_supports)
        )
        if validate_claim is not None and not validate_claim(claim):
            reject("FALLBACK_CLAIM_NOT_SUPPORTED")
            return False
        refs = " ".join(f"[{support_id}]" for support_id in pending_ids)
        lines.append(f"- {claim.text} {refs}")
        return True

    for item, sentence in selected:
        excerpt = _LEADING_SECTION_MARKER.sub("", sentence).strip()
        excerpt_key = (item.document_version_id, excerpt)
        if not excerpt or excerpt_key in seen_excerpts:
            continue
        seen_excerpts.add(excerpt_key)
        if pending_item is not None and _fallback_continues_fragment(
            pending_item, item, pending_excerpt, sentence, complete_ids
        ):
            pending_excerpt += excerpt
            pending_ids.append(item.support_id)
            pending_supports.append(
                ClaimSupport(support_id=item.support_id, quote=sentence)
            )
        else:
            if not emit_excerpt():
                ids = [
                    support_id
                    for support_id in ids
                    if support_id not in pending_ids
                ]
            pending_excerpt = excerpt
            pending_ids = [item.support_id]
            pending_supports = [
                ClaimSupport(support_id=item.support_id, quote=sentence)
            ]
        pending_item = item
        ids.append(item.support_id)
    if not emit_excerpt():
        ids = [
            support_id for support_id in ids if support_id not in pending_ids
        ]
    if not ids:
        reject("NO_CITABLE_COMPLETE_EXCERPT")
        return None
    if not (
        named_row_selected or partial_table_selected
    ) and not _fallback_has_question_anchor(plan.original_query, selected):
        reject("QUESTION_ANCHOR_MISSING")
        return None
    covered_atoms = frozenset(
        atom_id
        for atom_id, support_ids in linked_ids.items()
        if any(support_id in ids for support_id in support_ids)
    )
    return "\n".join(lines), tuple(ids), covered_atoms


def _han_text(text: str) -> str:
    """只保留汉字，供来源标题和问题作保守的连续字面匹配。"""
    return "".join(char for char in text if "\u4e00" <= char <= "\u9fff")


def _longest_common_han_run(left: str, right: str) -> int:
    """返回两个短文本之间最长的连续汉字交集长度。"""
    left, right = _han_text(left), _han_text(right)
    previous = [0] * (len(right) + 1)
    best = 0
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, 1):
            if char == other:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def _contextual_source_versions(
    plan: QueryPlan, evidence: tuple[EvidenceItem, ...]
) -> frozenset[str] | None:
    """短追问的历史语境若唯一指向资料标题，则约束发布来源。"""
    normalized_original = unicodedata.normalize(
        "NFKC", plan.original_query
    ).strip()
    if (
        plan.context_resolution_mode != "RULE_CONTEXT"
        or not plan.resolved_root_query.endswith(normalized_original)
    ):
        return None
    context = plan.resolved_root_query.removesuffix(normalized_original).strip()
    if not context:
        return None
    scores: dict[str, int] = {}
    for item in evidence:
        if item.document_version_id is None or not item.display_name:
            continue
        scores[item.document_version_id] = max(
            scores.get(item.document_version_id, 0),
            _longest_common_han_run(context, item.display_name),
        )
    if not scores:
        return None
    best = max(scores.values())
    runner_up = max(
        (score for score in scores.values() if score != best), default=0
    )
    winners = frozenset(
        version for version, score in scores.items() if score == best
    )
    if (
        best < _CONTEXT_SOURCE_MIN_MATCH_CHARS
        or len(winners) != 1
        or best - runner_up < _CONTEXT_SOURCE_MIN_LEAD_CHARS
    ):
        return None
    return winners


def _fallback_has_question_anchor(
    question: str, selected: list[tuple[EvidenceItem, str]]
) -> bool:
    """长问题回退至少命中一个连续问句锚点，防止泛词串答。"""
    if len(_han_text(question)) < _FALLBACK_LONG_QUESTION_CHARS:
        return True
    source = " ".join(
        f"{item.display_name or ''} {sentence}" for item, sentence in selected
    )
    return (
        _longest_common_han_run(question, source)
        >= _FALLBACK_MIN_QUESTION_ANCHOR_CHARS
    )


def _validate_contextual_source_scope(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    units: tuple[EvidenceItem, ...],
) -> None:
    """历史语境唯一锚定资料时，拒绝相似但不同来源的事实。"""
    scoped_versions = _contextual_source_versions(plan, evidence)
    if scoped_versions is not None and any(
        item.document_version_id not in scoped_versions for item in units
    ):
        raise ValidationFailed(
            "短追问引用了与历史所指资料不同的来源。",
            stage="answer.validate",
            code="CLAIM_SOURCE_SCOPE_MISMATCH",
        )


def _query_focus_in_source(focus: str, text: str) -> bool:
    """只用问题动作的字面近邻匹配，避免引用同主题但答非所问的句子。"""
    normalized = "".join(
        character for character in text if "\u4e00" <= character <= "\u9fff"
    )
    if focus in normalized:
        return True
    return (
        len(focus) == _SHORT_QUERY_FOCUS_LENGTH
        and re.search(
            rf"{re.escape(focus[0])}.{{0,2}}{re.escape(focus[1])}",
            normalized,
        )
        is not None
    )


def _validate_short_question_source_anchor(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    units: tuple[EvidenceItem, ...],
) -> None:
    """短单问有更贴题的同版原文时，拒绝泛主题片段冒充答案。"""
    if (
        len(plan.atoms) != 1
        or plan.atoms[0].answer_shape is AtomAnswerShape.DURATION
        or len(_han_text(plan.original_query)) > _FALLBACK_SHORT_QUESTION_CHARS
    ):
        return
    action = _YES_NO_ACTION_FOCUS.search(plan.original_query.strip())
    if action is not None and any(
        _query_focus_in_source(action["focus"], item.citation_text)
        for item in units
    ):
        return
    versions = {item.document_version_id for item in units}
    if len(versions) != 1 or None in versions:
        return
    question_terms = _terms(plan.original_query)
    cited_overlap = max(
        (len(_terms(item.citation_text) & question_terms) for item in units),
        default=0,
    )
    if cited_overlap >= _FALLBACK_MIN_BIGRAM_OVERLAP:
        return
    best_overlap = max(
        (
            len(_terms(item.citation_text) & question_terms)
            for item in evidence
            if item.document_version_id in versions
            and item.publishable
            and item.source_spans
            and all(span.is_citable for span in item.source_spans)
        ),
        default=0,
    )
    if best_overlap >= _FALLBACK_MIN_BIGRAM_OVERLAP:
        raise ValidationFailed(
            "引用片段未命中短问中的关键对象，且同版资料存在更直接的原文。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNSUPPORTED",
        )


def _validate_yes_no_source_focus(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    units: tuple[EvidenceItem, ...],
) -> None:
    """当包中有问题动作的直接原文时，拒绝仅同主题的旁支来源。"""
    focus_match = _YES_NO_ACTION_FOCUS.search(plan.resolved_root_query.strip())
    if focus_match is None:
        return
    focus = focus_match["focus"]
    if any(
        _query_focus_in_source(focus, item.citation_text) for item in evidence
    ) and not any(
        _query_focus_in_source(focus, item.citation_text) for item in units
    ):
        raise ValidationFailed(
            "事实引用的来源没有回答是非问题所问的动作。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNSUPPORTED",
        )


def _validate_natural_support_structure(
    units: tuple[EvidenceItem, ...],
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
) -> None:
    """结构相容不表示事实支持；语义和 Atom 边界仍须另行核验。"""
    decision = source_compatibility(units, trusted_groups=trusted_groups)
    if not decision.compatible:
        raise ValidationFailed(
            "单条事实不能拼接互不相属的证据。",
            stage="answer.validate",
            code="CLAIM_SOURCE_MISMATCH",
            details=(
                ("validator", "_validate_natural_support_structure"),
                ("source_reason", decision.reason),
            ),
        )


def _complete_source_sentence(
    source: str,
    quote: str,
    *,
    quote_start: int | None = None,
) -> str:
    """在已证明的位置恢复有界原句，完整句末和段落边界保持幂等。"""
    positions = tuple(
        match.start() for match in re.finditer(re.escape(quote), source)
    )
    if (
        not quote
        or not positions
        or (
            quote_start is not None
            and (type(quote_start) is not int or quote_start not in positions)
        )
    ):
        raise ValidationFailed(
            "原句恢复缺少准确逐字引文位置。",
            stage="answer.validate",
            code="CLAIM_QUOTE_INVALID",
        )
    if quote_start is None and len(positions) != 1:
        raise ValidationFailed(
            "重复引文缺少来源位置证明。",
            stage="answer.validate",
            code="CLAIM_QUOTE_AMBIGUOUS",
        )
    position = positions[0] if quote_start is None else quote_start
    boundary = re.compile(r"[。！？!?；;][\u201d\u2019\"'）)】\]]*|[\r\n]+")
    previous = tuple(boundary.finditer(source, 0, position))
    start = previous[-1].end() if previous else 0
    quoted_end = position + len(quote.rstrip())
    already_ended = re.search(
        r"[。！？!?；;][\u201d\u2019\"'）)】\]]*$", quote.rstrip()
    )
    if already_ended is not None or quote.endswith(("\n", "\r")):
        end = quoted_end
        closing = re.match(r"[\u201d\u2019\"'）)】\]]+", source[end:])
        if closing is not None:
            end += closing.end()
    else:
        ending = boundary.search(source, quoted_end)
        end = ending.end() if ending else len(source)
    return source[start:end].strip()


def _source_faithful_claim(
    claim: AnswerClaim, units: tuple[EvidenceItem, ...]
) -> AnswerClaim:
    """构造待重新核验的原句草稿；本函数的结果不得直接发布。"""
    supports = tuple(
        ClaimSupport(
            support_id=support.support_id,
            quote=_complete_source_sentence(item.citation_text, support.quote),
        )
        for support, item in zip(claim.supports, units, strict=True)
    )
    excerpts = tuple(dict.fromkeys(support.quote for support in supports))
    return claim.model_copy(
        update={"text": "\n".join(excerpts), "supports": supports}
    )


def _validate_natural_atom_support_scope(
    natural: NaturalClaim,
    plan: QueryPlan,
    linked_ids: dict[str, tuple[str, ...]],
) -> None:
    """模型只能为当前 Atom 选择证据包明确分配的 Support ID。"""
    if natural.atom_id not in {atom.atom_id for atom in plan.atoms}:
        raise ValidationFailed(
            "自然事实引用未知 Atom。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_ATOM",
        )
    support_ids = {support.support_id for support in natural.supports}
    if not support_ids <= set(linked_ids.get(natural.atom_id, ())):
        raise ValidationFailed(
            "自然事实引用了未分配给当前 Atom 的来源。",
            stage="answer.validate",
            code="CLAIM_SUPPORT_OUTSIDE_ATOM",
            details=(("validator", "_validate_natural_atom_support_scope"),),
        )


def _validated_source_faithful_claim(  # noqa: PLR0913
    natural: NaturalClaim,
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
) -> AnswerClaim:
    """只为低风险语义改写恢复原句，并重新执行全部安全校验。"""
    by_id = {item.support_id: item for item in evidence}
    support_ids = tuple(support.support_id for support in natural.supports)
    if not support_ids or not set(support_ids) <= by_id.keys():
        raise ValidationFailed(
            "自然事实引用未知 Support ID。",
            stage="answer.validate",
            code="CLAIM_UNKNOWN_SUPPORT",
        )
    source_claim = _source_faithful_claim(
        AnswerClaim(text=natural.text, supports=natural.supports),
        tuple(by_id[support_id] for support_id in support_ids),
    )
    validated = _validated_natural_claim(
        NaturalClaim(
            atom_id=natural.atom_id,
            text=source_claim.text,
            supports=source_claim.supports,
        ),
        plan,
        matrix,
        evidence,
        analysis,
        trusted_groups=trusted_groups,
        physical_table_facts=physical_table_facts,
        atom_fact_bindings=atom_fact_bindings,
    )
    return validated


def _validated_source_group_claims(  # noqa: PLR0913
    natural: NaturalClaim,
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
    on_undetermined: Callable[[NaturalClaim], None] | None = None,
) -> tuple[AnswerClaim, ...]:
    """把模型混合的来源组拆开，各自恢复原句并独立核验。"""
    by_id = {item.support_id: item for item in evidence}
    selected: list[EvidenceItem] = []
    for support in natural.supports:
        item = by_id.get(support.support_id)
        if item is None:
            return ()
        selected.append(item)
    recovered: list[AnswerClaim] = []
    for decision, units in compatible_partitions(
        tuple(selected), trusted_groups=trusted_groups
    ):
        if not decision.compatible:
            continue
        ids = {item.support_id for item in units}
        supports = tuple(
            support for support in natural.supports if support.support_id in ids
        )
        candidate = natural.model_copy(update={"supports": tuple(supports)})
        try:
            claim = _validated_source_faithful_claim(
                candidate,
                plan,
                matrix,
                evidence,
                analysis,
                trusted_groups=trusted_groups,
            )
        except RequestRelationUndetermined as error:
            if on_undetermined is not None:
                on_undetermined(
                    NaturalClaim(
                        atom_id=natural.atom_id,
                        text=error.claim.text,
                        supports=error.claim.supports,
                    )
                )
            continue
        except (ValidationFailed, ValueError):
            continue
        if claim not in recovered:
            recovered.append(claim)
    return tuple(recovered)


def _physical_fact_for_claim(
    atom_id: str,
    units: tuple[EvidenceItem, ...],
    facts: tuple[PhysicalTableFact, ...],
    bindings: tuple[AtomFactBinding, ...],
) -> PhysicalTableFact | None:
    """只接受服务端已绑定且完整展开全部依赖的物理事实。"""
    selected_ids = {item.support_id for item in units}
    bound_ids = {
        binding.fact_id for binding in bindings if binding.atom_id == atom_id
    }
    return next(
        (
            fact
            for fact in facts
            if fact.fact_id in bound_ids
            and set(fact.all_support_ids) == selected_ids
        ),
        None,
    )


def _physical_binding_proves_relation(
    atom_id: str,
    fact: PhysicalTableFact | None,
    bindings: tuple[AtomFactBinding, ...],
) -> bool:
    """只复用当前 Atom 对当前事实的独立确定性关系证明。"""
    return fact is not None and any(
        binding.atom_id == atom_id
        and binding.fact_id == fact.fact_id
        and binding.relation_status == "SUPPORTED"
        for binding in bindings
    )


def _validate_physical_table_fact(
    fact: PhysicalTableFact, units: tuple[EvidenceItem, ...]
) -> None:
    """重新核对事实登记的表、行、列和规范表头来源。"""
    by_id = {item.support_id: item for item in units}
    if set(by_id) != set(fact.all_support_ids):
        raise ValidationFailed(
            "表格事实没有完整引用登记的物理来源。",
            stage="answer.validate",
            code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
        )

    def coordinate(support_id: str) -> tuple[tuple[object, ...], int, int]:
        item = by_id[support_id]
        cell = table_cell_coordinate(item)
        node = dict(item.metadata).get("table_logical_node_id")
        if cell is None or node != fact.table_node_id:
            raise ValidationFailed(
                "表格事实来源缺少规范坐标。",
                stage="answer.validate",
                code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
            )
        table, row, column = cell
        table_key = canonical_sha256(
            {
                "revision": "wb08r-physical-table-v1",
                "identity": (
                    *item.source_identity_scope,
                    *table[:4],
                    node,
                    table[-1],
                ),
            }
        )
        if table_key != fact.table_key:
            raise ValidationFailed(
                "表格事实来源跨越不同物理表。",
                stage="answer.validate",
                code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
            )
        return table, row, column

    if any(
        coordinate(support_id)[1:]
        != (fact.row_index, fact.row_label_column_index)
        for support_id in fact.row_label_support_ids
    ) or any(
        coordinate(support_id)[1:] != (fact.row_index, fact.value_column_index)
        for support_id in fact.value_support_ids
    ):
        raise ValidationFailed(
            "表格事实的行名或值不在登记坐标。",
            stage="answer.validate",
            code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
        )
    for header in fact.headers:
        if fact.value_column_index not in header.column_indexes or any(
            coordinate(support_id)[1] != header.row_index
            for support_id in header.support_ids
        ):
            raise ValidationFailed(
                "表格事实的规范表头没有覆盖值列。",
                stage="answer.validate",
                code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
            )


def _validated_natural_claim(  # noqa: PLR0912, PLR0913, PLR0915
    natural: NaturalClaim,
    plan: QueryPlan,
    matrix: AtomSupportMatrix,
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
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
    atom = atoms[natural.atom_id]
    if any(
        not evidence_allowed_for_atom(
            atom.source_scope,
            item,
            atom.source_scope.required_content
            if atom.source_scope is not None
            else SourceContentRequirement.BODY,
        )
        for item in units
    ):
        raise ValidationFailed(
            "自然事实的来源不属于用户指定文档。",
            stage="answer.validate",
            code="CLAIM_DOCUMENT_SCOPE_MISMATCH",
        )
    _validate_contextual_source_scope(plan, evidence, units)
    physical_fact = _physical_fact_for_claim(
        natural.atom_id,
        units,
        physical_table_facts,
        atom_fact_bindings,
    )
    if physical_fact is None:
        _validate_natural_support_structure(
            units, trusted_groups=trusted_groups
        )
    else:
        _validate_physical_table_fact(physical_fact, units)
    claim = AnswerClaim(
        text=natural.text,
        supports=natural.supports,
    )
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
    if (
        natural.text.strip() == source_text.strip()
        and not _SOURCE_SENTENCE_END.search(natural.text)
        and not re.search(_ACTION_VERB, natural.text)
        and not _number_tokens(natural.text)
        and any(
            support.quote != item.citation_text
            for support, item in zip(natural.supports, units, strict=True)
        )
    ):
        raise ValidationFailed(
            "模型只返回不能独立构成事实的原句片段。",
            stage="answer.validate",
            code="CLAIM_FRAGMENT_INCOMPLETE",
        )
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
    _validate_table_claim_certificate(atom_units, physical_fact=physical_fact)
    direct_relation = any(
        isinstance(
            certificate := dict(item.metadata).get("answer_support"), dict
        )
        and certificate.get("status") == "SUPPORTED"
        for item in atom_units
    )
    certified_group = certified_source_group(atom_units, trusted_groups)
    group_certified = bool(
        not direct_relation
        and certified_group is not None
        and certified_group.group_id in support.relation_certified_group_ids
    )
    if group_certified and (
        not any(_structural_lead_in(item) for item in atom_units)
        or not any(not _structural_lead_in(item) for item in atom_units)
    ):
        raise ValidationFailed(
            "结构事实缺少同组关系导语的引用。",
            stage="answer.validate",
            code="CLAIM_RELATION_UNSUPPORTED",
        )
    semantic_review_reasons: list[str] = []
    if _validate_natural_entailment(
        claim, source_text=_claim_source_text(claim, units)
    ):
        semantic_review_reasons.append("ACTION_LEXEME_NOT_PROOF")
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
        trusted_groups=trusted_groups,
    )
    _validate_natural_request_support(
        atom,
        claim,
        units,
        atom_analysis,
        relation_proved=(
            _matrix_proves_source_relation(matrix, atom, units)
            or _physical_binding_proves_relation(
                natural.atom_id, physical_fact, atom_fact_bindings
            )
        ),
        contextual_source_proved=(
            atom.answer_shape is AtomAnswerShape.DURATION
            and _contextual_source_versions(plan, evidence) is not None
        ),
        trusted_groups=trusted_groups,
        semantic_review_reason=(
            "+".join(semantic_review_reasons)
            if semantic_review_reasons
            else None
        ),
    )
    return claim


def _validate_natural_request_support(  # noqa: PLR0913
    atom: QueryAtom,
    claim: AnswerClaim,
    units: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    relation_proved: bool = False,
    contextual_source_proved: bool = False,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
    semantic_review_reason: str | None = None,
) -> None:
    """逐字事实还须回答当前 Atom，来源分区与集合完整都不能替代此门。"""
    atom_analysis = _natural_atom_analysis(atom, analysis)
    source = _claim_source_text(claim, units)
    request_stages = {
        match["scope"] for match in _STAGE_SCOPE.finditer(atom.search_text)
    }
    source_stages = {
        match["scope"]
        for match in _STAGE_SCOPE.finditer(
            "\n".join(
                (
                    source,
                    *(
                        heading
                        for item in units
                        for heading in item.heading_path
                    ),
                )
            )
        )
    }
    if (
        request_stages
        and source_stages
        and request_stages.isdisjoint(source_stages)
    ):
        raise ValidationFailed(
            "原文明确属于不同的请求阶段。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNSUPPORTED",
            details={
                "validator": "request_relation",
                "reason": "EXPLICIT_DIFFERENT_STAGE",
            },
        )
    source_qualifier = atom_analysis.semantics.source_qualifier
    if source_qualifier and not all(
        normalize_semantic_text(source_qualifier)
        in normalize_semantic_text(
            " ".join(
                (item.source_label, item.display_name or "", *item.heading_path)
            )
        )
        for item in units
    ):
        raise ValidationFailed(
            "当前 Atom 的受信来源限制与引用不一致。",
            stage="answer.validate",
            code="CLAIM_SOURCE_SCOPE_MISMATCH",
        )
    decision = decide_request_relation(atom_analysis, source)
    if decision.status is RequestRelationStatus.CONTRADICTED_OR_IRRELEVANT:
        raise ValidationFailed(
            "所引原文明确属于不同问题对象。",
            stage="answer.validate",
            code="CLAIM_QUERY_RELATION_UNSUPPORTED",
            details={
                "validator": decision.validator,
                "reason": decision.reason,
            },
        )
    review_key = relation_review_key(atom, claim, units)
    if semantic_review_reason and not has_relation_review(review_key):
        raise RequestRelationUndetermined(claim, semantic_review_reason)
    if relation_proved:
        return
    if source_compatibility(units).reason == "TABLE_INTERSECTION" and all(
        isinstance(
            certificate := dict(item.metadata).get("answer_support"), dict
        )
        and certificate.get("status") == "SUPPORTED"
        and certificate.get("support_reason") == "TABLE_INTERSECTION"
        and normalize_semantic_text(str(certificate.get("query_target") or ""))
        == normalize_semantic_text(atom_analysis.semantics.target or "")
        and normalize_semantic_text(
            str(certificate.get("requested_relation_or_attribute") or "")
        )
        == normalize_semantic_text(atom_analysis.semantics.relation or "")
        for item in units
    ):
        _validate_table_claim_certificate(units)
        return
    if contextual_source_proved and _FALLBACK_DURATION.search(source):
        return
    if has_relation_review(review_key):
        return
    # 结构上下文只用于所属来源组；不能把两组的对象词拼成关系。
    for group in _claim_source_groups(
        claim, list(units), atom_analysis, trusted_groups=trusted_groups
    ):
        if (
            atom.answer_shape is AtomAnswerShape.DUTIES
            and any(
                normalize_semantic_text(atom_analysis.semantics.target or "")
                == normalize_semantic_text(subject)
                for subject in group.trusted_subjects
            )
            and re.search(_DUTY_ACTION_VERB, group.support_text)
        ):
            return
    if decision.status is RequestRelationStatus.SUPPORTED:
        return
    raise RequestRelationUndetermined(claim, decision.reason)


def _matrix_proves_source_relation(
    matrix: AtomSupportMatrix,
    atom: QueryAtom,
    units: tuple[EvidenceItem, ...],
) -> bool:
    """仅复用当前 Atom 的独立支持证明，稳定来源键优先于旧展示编号。"""
    support = matrix.for_atom(atom.atom_id)
    if support.status is not AtomStatus.SUPPORTED or not units:
        return False
    if support.supporting_support_keys:
        return all(
            stable_support_key(item) in support.supporting_support_keys
            for item in units
        )
    return all(
        item.support_id in support.supporting_support_ids for item in units
    )


def _validate_table_claim_certificate(
    units: tuple[EvidenceItem, ...],
    *,
    physical_fact: PhysicalTableFact | None = None,
) -> None:
    """表格 Claim 必须引用证书声明的全部真实结构依赖。"""
    if physical_fact is not None:
        _validate_physical_table_fact(physical_fact, units)
        return
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
            or len(required) < _TABLE_INTERSECTION_MIN_SPANS
            or len(required) != len(set(required))
            or not all(
                isinstance(node_id, str) and node_id for node_id in required
            )
        ):
            raise ValidationFailed(
                "表格事实缺少完整交点来源。",
                stage="answer.validate",
                code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
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
                code="CLAIM_TABLE_DEPENDENCY_INCOMPLETE",
            )


def _modality_class(text: str) -> str | None:
    """保留强制、应当、需要和许可四种不同义务强度。"""
    return next(
        (name for name, pattern in _MODALITY_CLASSES if pattern.search(text)),
        None,
    )


def _claim_source_text(
    claim: AnswerClaim, units: tuple[EvidenceItem, ...]
) -> str:
    """有原文位置证明时按源序核验半句，引用仍保留各自的 Support。"""
    decision = source_compatibility(units)
    supports = {support.support_id: support for support in claim.supports}
    by_id = {item.support_id: item for item in units}
    if decision.reason != "CONTIGUOUS_NODE" or any(
        supports[item.support_id].quote != item.citation_text for item in units
    ):
        return "\n".join(support.quote for support in claim.supports)
    parts: list[str] = []
    previous_end = 0
    for support_id in decision.ordered_support_ids:
        item = by_id[support_id]
        span = item.source_spans[0]
        if span.source_start_char is None or span.source_end_char is None:
            return "\n".join(support.quote for support in claim.supports)
        overlap = max(0, previous_end - span.source_start_char) if parts else 0
        parts.append(supports[support_id].quote[overlap:])
        previous_end = max(previous_end, span.source_end_char)
    return "".join(parts)


def _condition_labels(text: str) -> set[str]:
    """保留条件正文和前后顺序，统一等价的长短时间连接词。"""
    labels = {
        match["scope"] or match["temporal"]
        for match in _CONDITION_SCOPE.finditer(text)
    }
    labels.update(
        match["scope"]
        + match["end"].replace("之前", "前").replace("之后", "后")
        for match in _BOUND_CONDITION.finditer(text)
    )
    return {normalize_semantic_text(label) for label in labels}


def _validate_bound_scope(text: str, source: str) -> None:
    """同一段内不同阶段和条件的动作不能互借，条件也不能被删去。"""
    source_sentences = tuple(
        value.strip()
        for value in re.split(r"[。；;！？!?\n]", source)
        if value.strip()
    )
    for sentence in re.split(r"[。；;！？!?\n]", text):
        if not sentence.strip():
            continue
        stages = {match["scope"] for match in _STAGE_SCOPE.finditer(sentence)}
        if stages:
            scoped = tuple(
                candidate
                for candidate in source_sentences
                if all(stage in candidate for stage in stages)
            )
            if not scoped:
                raise ValidationFailed(
                    "事实阶段没有对应来源。",
                    stage="answer.validate",
                    code="CLAIM_STAGE_UNSUPPORTED",
                )
            for clause, subject in _clauses_with_subject(sentence):
                if not re.search(_ACTION_VERB, clause):
                    continue
                try:
                    content = _terms(
                        re.sub(r"负责|承担|包括|包含", "", _predicate(clause))
                    )
                    scoped_content = set().union(
                        *(
                            _terms(
                                re.sub(
                                    r"负责|承担|包括|包含",
                                    "",
                                    _predicate(value),
                                )
                            )
                            for candidate in scoped
                            for value, _owner in _clauses_with_subject(
                                candidate
                            )
                            if re.search(_ACTION_VERB, value)
                        )
                    )
                    if content and not content.intersection(scoped_content):
                        raise ValidationFailed(
                            "阶段中没有对应动作内容。",
                            stage="answer.validate",
                            code="CLAIM_STAGE_UNSUPPORTED",
                        )
                    _validate_clause_support(
                        clause,
                        subject,
                        _ClaimSourceGroup(
                            "\n".join(scoped), frozenset(), frozenset()
                        ),
                    )
                except ValidationFailed as error:
                    raise ValidationFailed(
                        "事实借用了另一阶段的动作。",
                        stage="answer.validate",
                        code="CLAIM_STAGE_UNSUPPORTED",
                    ) from error
        relevant = _best_negation_sources(sentence, list(source_sentences))
        if not relevant:
            continue
        required = set.intersection(
            *(_condition_labels(value) for value in relevant)
        )
        normalized = (
            normalize_semantic_text(sentence)
            .replace("之后", "后")
            .replace("之前", "前")
        )
        if any(condition not in normalized for condition in required):
            raise ValidationFailed(
                "事实遗漏了对应动作的适用条件或先后顺序。",
                stage="answer.validate",
                code="CLAIM_CONDITION_UNSUPPORTED",
            )


def _validate_owned_action_content(
    text: str,
    claim_subject: str,
    source_clauses: list[tuple[str, str | None]],
) -> None:
    """共同的“负责/包括”不能替代每个主体独有的动作内容。"""
    owned_content = set().union(
        *(
            _terms(re.sub(r"负责|承担|包括|包含", "", _predicate(clause)))
            for clause, subject in source_clauses
            if subject and _same_subject(subject, claim_subject)
        )
    )
    claim_content = _terms(re.sub(r"负责|承担|包括|包含", "", _predicate(text)))
    if claim_content and not claim_content.intersection(owned_content):
        raise ValidationFailed(
            "事实动作内容仅属于另一个主体。",
            stage="answer.validate",
            code="CLAIM_RELATION_UNSUPPORTED",
        )


def _validate_natural_entailment(
    claim: AnswerClaim,
    *,
    source_text: str | None = None,
) -> bool:
    """核对确定性边界，并返回是否需要统一语义复核。

    动作词集合仅是诊断特征。同义改写或表格列头承载关系时，动作词不相同
    不能证明事实错误；明确跨主体借用同一个来源动作仍然直接拒绝。

    Returns:
        动作词面不足以证明 Claim 与来源关系时为 True。

    """
    text = claim.text
    source = source_text or "\n".join(item.quote for item in claim.supports)
    _validate_bound_scope(text, source)
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
    for level in _PARENTHETICAL_LEVEL.findall(text):
        normalized_level = "".join(
            unicodedata.normalize("NFKC", level).casefold().split()
        )
        if normalized_level not in normalized_source:
            raise ValidationFailed(
                "事实新增了引用原文没有的事件或对象等级。",
                stage="answer.validate",
                code="CLAIM_CONDITION_UNSUPPORTED",
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
    semantic_review_required = bool(claim_actions - supported_actions)
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
        shared_actions = claim_actions & supported_actions
        if shared_actions - owned_actions:
            raise ValidationFailed(
                "事实借用了另一个主体的动作关系。",
                stage="answer.validate",
                code="CLAIM_RELATION_UNSUPPORTED",
            )
        if shared_actions:
            _validate_owned_action_content(
                text, claim_subject, source_with_subjects
            )
    matched = _best_negation_sources(text, source_clauses) or source_clauses
    claim_modality = _modality_class(text)
    if claim_modality is not None and not any(
        _modality_class(clause) == claim_modality for clause in matched
    ):
        source_modalities = {
            modality
            for clause in source_clauses
            if (modality := _modality_class(clause)) is not None
        }
        if source_modalities != {claim_modality}:
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
    return semantic_review_required


def _natural_rejection_code(error: ValidationFailed | ValueError) -> str:
    """把既有验证器的细分失败映射到逐 Claim 诊断合同。"""
    if not isinstance(error, ValidationFailed):
        return "CLAIM_SEMANTIC_SUPPORT_FAILED"
    mapping = {
        "CLAIM_QUERY_RELATION_UNSUPPORTED": "CLAIM_RELATION_UNSUPPORTED",
        "CLAIM_QUERY_RELATION_UNDETERMINED": "CLAIM_RELATION_UNSUPPORTED",
        "CLAIM_TABLE_DEPENDENCY_INCOMPLETE": "CLAIM_RELATION_UNSUPPORTED",
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


def _claim_rejection_diagnostic(
    natural: NaturalClaim,
    error: ValidationFailed | ValueError,
    *,
    public_reason: str,
    allowed_support_ids: tuple[str, ...],
) -> ClaimRejectionDiagnostic:
    """保留可重放身份，不把 Claim 或 Quote 正文写入 SAFE Trace。"""
    if isinstance(error, ValidationFailed):
        raw_reason = error.code
        validator_stage = error.stage
        details = dict(error.details)
        validator = details.get("validator")
    else:
        raw_reason = "VALUE_ERROR"
        validator_stage = "answer.validate"
        validator = None
    if not isinstance(validator, str):
        validator = "_validated_natural_claim"
    # 只记录仓库代码位置；不读取异常正文、局部变量或业务内容。
    origin_module = None
    origin_function = None
    origin_line = None
    traceback = error.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module, str) and module.startswith("rag_app."):
            origin_module = module
            origin_function = traceback.tb_frame.f_code.co_name
            origin_line = traceback.tb_lineno
        traceback = traceback.tb_next
    return ClaimRejectionDiagnostic(
        atom_id=natural.atom_id,
        raw_reason_code=raw_reason,
        public_reason_code=public_reason,
        validator_stage=validator_stage,
        validator=validator,
        origin_module=origin_module,
        origin_function=origin_function,
        origin_line=origin_line,
        selected_support_ids=tuple(
            support.support_id for support in natural.supports
        ),
        allowed_support_ids=allowed_support_ids,
        claim_sha256=hashlib.sha256(natural.text.encode("utf-8")).hexdigest(),
        quote_sha256s=tuple(
            hashlib.sha256(support.quote.encode("utf-8")).hexdigest()
            for support in natural.supports
        ),
    )


def _natural_atom_analysis(
    atom: QueryAtom,
    analysis: QueryAnalysis | None,
) -> QueryAnalysis:
    """统一派生当前 Atom 的语义，所有下游校验消费同一合同。"""
    return current_atom_analysis(atom, analysis)


def _target_coverage_records(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    claims: tuple[ValidatedNaturalClaim, ...],
    analysis: QueryAnalysis | None,
    pack: GenerationEvidencePack | None,
) -> tuple[JsonObject, ...]:
    """SAFE Trace 仅保存任务成员身份和实际覆盖，排除正文。"""
    if pack is None:
        return ()
    records = []
    for atom in plan.atoms:
        coverage = target_member_coverage(
            atom,
            evidence,
            tuple(
                item.claim for item in claims if atom.atom_id in item.atom_ids
            ),
            trusted_groups=pack.trusted_source_groups,
            semantics=_natural_atom_analysis(atom, analysis).semantics,
            fact_covered=_source_fact_content_covered,
        )
        records.append(
            freeze_json_object(
                {
                    "atom_id": atom.atom_id,
                    "required_member_keys": coverage.required_member_keys,
                    "covered_member_keys": coverage.covered_member_keys,
                    "missing_member_keys": coverage.missing_member_keys,
                    "source_complete": coverage.source_complete,
                    "complete": coverage.complete,
                    "reason_codes": coverage.reason_codes,
                }
            )
        )
    return tuple(records)


def _natural_atom_complete(  # noqa: PLR0911, PLR0912, PLR0913
    atom: QueryAtom,
    matrix: AtomSupportMatrix,
    claims: tuple[ValidatedNaturalClaim, ...],
    evidence: tuple[EvidenceItem, ...],
    analysis: QueryAnalysis | None,
    *,
    generation_evidence_pack: GenerationEvidencePack | None = None,
) -> bool:
    """列表和流程还须通过来源集合完整性门，不能只看相关 Claim。"""
    if generation_evidence_pack is not None:
        evidence = _atom_validation_evidence(
            atom.atom_id,
            evidence,
            generation_evidence_pack.per_atom_source_certificates,
        )
    atom_claim_items = tuple(
        item for item in claims if atom.atom_id in item.atom_ids
    )
    atom_claims = tuple(item.claim for item in atom_claim_items)
    if not atom_claims:
        return False
    if not any(item.relation_complete for item in atom_claim_items):
        return False
    by_id = {item.support_id: item for item in evidence}
    if generation_evidence_pack is not None:
        selected_fact_ids: set[str] = set()
        all_claims_are_physical_facts = True
        for claim in atom_claims:
            try:
                units = tuple(
                    by_id[support.support_id] for support in claim.supports
                )
            except KeyError:
                return False
            fact = _physical_fact_for_claim(
                atom.atom_id,
                units,
                generation_evidence_pack.physical_table_facts,
                generation_evidence_pack.atom_fact_bindings,
            )
            if fact is None:
                all_claims_are_physical_facts = False
                break
            try:
                _validate_physical_table_fact(fact, units)
            except ValidationFailed:
                return False
            selected_fact_ids.add(fact.fact_id)
        if all_claims_are_physical_facts:
            required_fact_ids = {
                binding.fact_id
                for binding in generation_evidence_pack.atom_fact_bindings
                if binding.atom_id == atom.atom_id
                and binding.relation_status == "SUPPORTED"
            }
            if required_fact_ids:
                return required_fact_ids <= selected_fact_ids
    for claim in atom_claims:
        try:
            units = tuple(
                by_id[support.support_id] for support in claim.supports
            )
            _validate_natural_request_support(
                atom,
                claim,
                units,
                _natural_atom_analysis(atom, analysis),
                relation_proved=_matrix_proves_source_relation(
                    matrix, atom, units
                ),
                trusted_groups=(
                    generation_evidence_pack.trusted_source_groups
                    if generation_evidence_pack is not None
                    else ()
                ),
            )
        except (ValidationFailed, KeyError):
            return False
    if generation_evidence_pack is not None:
        target_coverage = target_member_coverage(
            atom,
            evidence,
            atom_claims,
            trusted_groups=generation_evidence_pack.trusted_source_groups,
            semantics=_natural_atom_analysis(atom, analysis).semantics,
            fact_covered=_source_fact_content_covered,
        )
        if target_coverage.required_member_keys:
            return target_coverage.complete
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
    group_complete = _structural_member_coverage(
        atom_evidence,
        atom_claims,
        trusted_groups=(
            generation_evidence_pack.trusted_source_groups
            if generation_evidence_pack is not None
            else ()
        ),
    )
    if (
        generation_evidence_pack is not None
        and group_complete is not True
        and not _certified_node_answer_complete(
            atom, atom_evidence, atom_claims
        )
    ):
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
    *,
    trusted_groups: tuple[EvidenceGroup, ...] = (),
) -> bool | None:
    """组成员、可引用跨度和所问事实分别核对，不相信 group_complete。"""
    by_id = {item.support_id: item for item in evidence}
    cited_support_ids = {
        item.support_id
        for claim in claims
        for support in claim.supports
        if (item := by_id.get(support.support_id)) is not None
    }
    if not trusted_groups:
        return (
            False
            if any(
                dict(item.metadata).get("evidence_group_id")
                for item in evidence
            )
            else None
        )
    cited = tuple(
        item for item in evidence if item.support_id in cited_support_ids
    )
    for group in trusted_groups:
        if not source_group_covered(
            evidence, group
        ) or not source_group_covered(cited, group):
            continue
        required = tuple(
            item
            for item in evidence
            if source_group_contains(item, group)
            and not _structural_lead_in(item)
        )
        if required and all(
            _source_fact_content_covered(item, claims) for item in required
        ):
            return True
    return False


def _source_fact_content_covered(
    item: EvidenceItem, claims: tuple[AnswerClaim, ...]
) -> bool:
    """完整性反向核对每个原文事实，不能用整段引用掩盖只回答一项。"""
    cited_claims = tuple(
        claim
        for claim in claims
        if any(
            support.support_id == item.support_id
            and support.quote == item.citation_text
            for support in claim.supports
        )
    )
    if not cited_claims:
        return False
    answer = "\n".join(claim.text for claim in cited_claims)
    for source_clause, _subject in _clauses_with_subject(item.citation_text):
        clause = _LEADING_LIST_MARKER.sub("", source_clause)
        terms = _terms(_predicate(clause))
        if (
            terms
            and len(terms & _terms(answer)) / len(terms)
            < _MIN_COMPLETE_FACT_BIGRAM_RATIO
        ) or not _number_tokens(clause) <= _number_tokens(answer):
            return False
        if set(re.findall(_DUTY_ACTION_VERB, clause)) - set(
            re.findall(_DUTY_ACTION_VERB, answer)
        ):
            return False
    return True


def _certified_node_answer_complete(
    atom: QueryAtom,
    evidence: tuple[EvidenceItem, ...],
    claims: tuple[AnswerClaim, ...],
) -> bool:
    """无组的完整节点须有独立问题范围证书，不能自动代表整份职责表。"""
    if not evidence or not all(
        _source_fact_content_covered(item, claims) for item in evidence
    ):
        return False
    nodes = {span.node_id for item in evidence for span in item.source_spans}
    for item in evidence:
        certificate = dict(item.metadata).get("answer_support")
        if not isinstance(certificate, dict):
            return False
        required = certificate.get("supporting_span_ids")
        if (
            certificate.get("status") != "SUPPORTED"
            or certificate.get("query_target") != atom.target
            or certificate.get("answer_type") != atom.answer_shape.value
            or certificate.get("support_reason")
            not in {
                "SECTION_HEADING_BODY",
                "TABLE_ROW_CONTENT",
                "STRUCTURED_LIST_RELATION",
            }
            or not isinstance(required, list)
            or not required
            or set(required) != nodes
        ):
            return False
    for node in nodes:
        units = tuple(
            item
            for item in evidence
            if any(span.node_id == node for span in item.source_spans)
        )
        if not source_compatibility(units).compatible:
            return False
        spans = tuple(span for item in units for span in item.source_spans)
        original_ends = {
            span.source_anchor.source_end_char
            for span in spans
            if span.source_anchor is not None
        }
        starts = tuple(
            span.source_start_char
            for span in spans
            if type(span.source_start_char) is int
        )
        ends = tuple(
            span.source_end_char
            for span in spans
            if type(span.source_end_char) is int
        )
        if (
            None in original_ends
            or len(original_ends) != 1
            or not all(
                type(span.source_start_char) is int
                and type(span.source_end_char) is int
                for span in spans
            )
            or not starts
            or not ends
            or min(starts) != 0
            or max(ends) != next(iter(original_ends))
        ):
            return False
    return True


def _structural_lead_in(item: EvidenceItem) -> bool:
    """只豁免完整组的引导句，不豁免编号事实成员。"""
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
