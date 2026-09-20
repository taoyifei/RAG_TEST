"""把来源、结构选择和回答义务编译为唯一冻结计划。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from rag_app.application.answering.atom_semantics import current_atom_analysis
from rag_app.application.answering.target_coverage import (
    TargetMemberCoverage,
    target_member_coverage,
)
from rag_app.application.retrieval.generation_evidence import (
    GenerationEvidencePack,
)
from rag_app.application.retrieval.source_scope import build_resolved_query_view
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.answer_plan import (
    ANSWER_PLAN_POLICY_REVISION,
    ANSWER_PLAN_SCHEMA_REVISION,
    AnswerMemberDependency,
    AnswerObligation,
    AnswerOperation,
    AnswerQualifier,
    AnswerQualifierKind,
    AnswerTaskMode,
    CompiledAnswerPlan,
    EvidenceSelection,
    PhysicalTask,
)
from rag_app.core.models.evidence_group import EvidenceGroupKind
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    QueryAtom,
    QueryPlan,
    SourceResolution,
)
from rag_app.core.models.retrieval import (
    AtomFactBinding,
    EvidenceItem,
    PhysicalTableFact,
)
from rag_app.core.query_text import (
    normalize_document_label,
    table_axis_label_in_query,
)
from rag_app.core.source_scope import source_identity_allowed

_QUALIFIER_PATTERNS: tuple[tuple[AnswerQualifierKind, re.Pattern[str]], ...] = (
    (
        AnswerQualifierKind.BEFORE,
        re.compile(r"之前|开始前|事前|预先"),
    ),
    (
        AnswerQualifierKind.AFTER,
        re.compile(r"之后|完成后|结束后|事后"),
    ),
    (
        AnswerQualifierKind.MUST,
        re.compile(r"必须|应当|应该|须要|须先"),
    ),
    (
        AnswerQualifierKind.PROHIBITED,
        re.compile(r"不得|禁止|严禁"),
    ),
)


class AnswerPlanCompilationError(ValueError):
    """统一计划无法满足内部来源或结构合同。"""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


def _ordered_text(
    support_ids: Iterable[str], registry: dict[str, EvidenceItem]
) -> str:
    """按物理事实登记顺序连接来源文本并稳定去重。"""
    try:
        return " / ".join(
            dict.fromkeys(
                registry[support_id].citation_text.strip()
                for support_id in support_ids
                if registry[support_id].citation_text.strip()
            )
        )
    except KeyError as error:
        raise AnswerPlanCompilationError(
            "ANSWER_PLAN_SOURCE_MISSING"
        ) from error


def _header_text(
    fact: PhysicalTableFact, registry: dict[str, EvidenceItem]
) -> str:
    """保留多级规范表头顺序，不把兄弟列并入当前事实。"""
    return " / ".join(
        dict.fromkeys(
            text
            for header in fact.headers
            if (
                text := "".join(
                    registry[support_id].citation_text
                    for support_id in header.support_ids
                ).strip()
            )
        )
    )


def _source_scope_digest(plan: QueryPlan) -> str:
    """把全部逐 Atom 来源决定合成为请求级冻结摘要。"""
    return canonical_sha256(
        {
            "source_scopes": [
                atom.source_scope.scope_digest
                if atom.source_scope is not None
                else None
                for atom in plan.atoms
            ]
        }
    )


def _source_resolved(atom: QueryAtom) -> bool:
    """OPEN 是合法全库范围；显式来源必须唯一解析。"""
    scope = atom.source_scope
    return scope is None or scope.resolution in {
        SourceResolution.OPEN,
        SourceResolution.RESOLVED,
    }


def _fact_allowed(atom: QueryAtom, fact: PhysicalTableFact) -> bool:
    """结构选择复用已经签发的文档身份许可。"""
    return source_identity_allowed(
        atom.source_scope,
        document_id=fact.document_id,
        document_version_id=fact.document_version_id,
    )


def _label_matches(value: str, label: str) -> bool:
    """只允许规范标签的完整包含关系，不使用业务白名单。"""
    normalized_value = normalize_document_label(value)
    normalized_label = normalize_document_label(label)
    return bool(normalized_value and normalized_label) and (
        normalized_value in normalized_label
        or normalized_label in normalized_value
    )


def _qualifier_specs(query: str) -> tuple[tuple[AnswerQualifierKind, str], ...]:
    """仅提取需要来源单独证明的强关系限定。"""
    result: list[tuple[AnswerQualifierKind, str]] = []
    seen: set[AnswerQualifierKind] = set()
    for kind, pattern in _QUALIFIER_PATTERNS:
        match = pattern.search(query)
        if match is None or kind in seen:
            continue
        seen.add(kind)
        result.append((kind, match.group(0)))
    return tuple(result)


def _qualifier_source_ids(
    kind: AnswerQualifierKind,
    fact: PhysicalTableFact,
    registry: dict[str, EvidenceItem],
) -> tuple[str, ...]:
    """限定必须在当前事实的正向依赖中逐字出现。"""
    pattern = dict(_QUALIFIER_PATTERNS)[kind]
    return tuple(
        support_id
        for support_id in fact.all_support_ids
        if pattern.search(registry[support_id].citation_text)
    )


def _qualifier_support_ids(
    kind: AnswerQualifierKind,
    support_ids: Iterable[str],
    registry: dict[str, EvidenceItem],
) -> tuple[str, ...]:
    """在给定正向依赖中寻找限定的逐字来源。"""
    pattern = dict(_QUALIFIER_PATTERNS)[kind]
    return tuple(
        support_id
        for support_id in dict.fromkeys(support_ids)
        if support_id in registry
        and pattern.search(registry[support_id].citation_text)
    )


def _fact_complete(
    fact: PhysicalTableFact, registry: dict[str, EvidenceItem]
) -> bool:
    """确定性执行必须拥有可发布且跨度闭合的全部事实依赖。"""
    if not set(fact.all_support_ids) <= registry.keys():
        return False
    return all(
        item.publishable
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
        for item in (
            registry[support_id] for support_id in fact.all_support_ids
        )
    )


def _binding_matches_fact(
    binding: AtomFactBinding,
    fact: PhysicalTableFact,
    registry: dict[str, EvidenceItem],
) -> bool:
    """核对兼容 Atom 的目标和字段确实属于当前物理事实。"""
    target = _ordered_text(fact.row_label_support_ids, registry)
    relation = _header_text(fact, registry)
    return _label_matches(binding.requested_target, target) and _label_matches(
        binding.requested_relation, relation
    )


def _binding_target_matches_fact(
    binding: AtomFactBinding,
    fact: PhysicalTableFact,
    registry: dict[str, EvidenceItem],
) -> bool:
    """危险限定拆出的 Atom 可归回同一目标，但不借给其它目标。"""
    target = _ordered_text(fact.row_label_support_ids, registry)
    return _label_matches(binding.requested_target, target)


def _select_fact_bindings(
    *,
    explicit_axes: tuple[bool, bool],
    has_qualifiers: bool,
    supported: tuple[AtomFactBinding, ...],
    semantic: tuple[AtomFactBinding, ...],
    same_target: tuple[AtomFactBinding, ...],
) -> tuple[tuple[AtomFactBinding, ...], str]:
    """只把已证明属于当前字段的 Atom 绑定进物理事实义务。"""
    explicit_row, explicit_field = explicit_axes
    certified = tuple(dict.fromkeys((*semantic, *supported)))
    if explicit_row and explicit_field and certified:
        return (
            tuple(
                dict.fromkeys(
                    (*certified, *(same_target if has_qualifiers else ()))
                )
            ),
            "EXPLICIT_SCHEMA_AXIS",
        )
    if supported:
        return (
            tuple(
                dict.fromkeys(
                    (*supported, *(same_target if has_qualifiers else ()))
                )
            ),
            "CERTIFIED_ATOM_BINDING",
        )
    if explicit_row and semantic:
        return semantic, "SEMANTIC_SCHEMA_AXIS"
    if has_qualifiers and semantic:
        return (
            tuple(dict.fromkeys((*semantic, *same_target))),
            "QUALIFIED_BASE_FACT",
        )
    return (), ""


def _candidate_facts(
    plan: QueryPlan,
    pack: GenerationEvidencePack,
    business_query: str,
    registry: dict[str, EvidenceItem],
) -> tuple[
    tuple[
        PhysicalTableFact,
        tuple[AtomFactBinding, ...],
        str,
    ],
    ...,
]:
    """只从真实表格 schema 绑定字段，不重新发明来源成员。"""
    qualifiers = _qualifier_specs(business_query)
    bindings_by_fact: dict[str, list[AtomFactBinding]] = defaultdict(list)
    for binding in pack.atom_fact_bindings:
        bindings_by_fact[binding.fact_id].append(binding)
    atoms = {atom.atom_id: atom for atom in plan.atoms}
    raw: list[
        tuple[
            PhysicalTableFact,
            tuple[AtomFactBinding, ...],
            bool,
            bool,
            str,
        ]
    ] = []
    for fact in pack.physical_table_facts:
        bindings = tuple(
            binding
            for binding in bindings_by_fact.get(fact.fact_id, ())
            if (atom := atoms.get(binding.atom_id)) is not None
            and _fact_allowed(atom, fact)
        )
        if not bindings or not _fact_complete(fact, registry):
            continue
        target = _ordered_text(fact.row_label_support_ids, registry)
        field = _header_text(fact, registry)
        explicit_row = table_axis_label_in_query(business_query, target)
        explicit_field = table_axis_label_in_query(business_query, field)
        supported = tuple(
            binding
            for binding in bindings
            if binding.relation_status == "SUPPORTED"
        )
        semantic = tuple(
            binding
            for binding in bindings
            if _binding_matches_fact(binding, fact, registry)
        )
        same_target = tuple(
            binding
            for binding in bindings
            if _binding_target_matches_fact(binding, fact, registry)
        )
        selected_bindings, basis = _select_fact_bindings(
            explicit_axes=(explicit_row, explicit_field),
            has_qualifiers=bool(qualifiers),
            supported=supported,
            semantic=semantic,
            same_target=same_target,
        )
        if selected_bindings:
            raw.append(
                (
                    fact,
                    selected_bindings,
                    explicit_row,
                    explicit_field,
                    basis,
                )
            )

    basis_priority = {
        "QUALIFIED_BASE_FACT": 1,
        "SEMANTIC_SCHEMA_AXIS": 2,
        "CERTIFIED_ATOM_BINDING": 3,
        "EXPLICIT_SCHEMA_AXIS": 4,
    }
    best_priority_by_atom: dict[str, int] = defaultdict(int)
    for _fact, bindings, _row, _field, basis in raw:
        for binding in bindings:
            best_priority_by_atom[binding.atom_id] = max(
                best_priority_by_atom[binding.atom_id], basis_priority[basis]
            )
    raw = [
        (fact, preferred, row, field, basis)
        for fact, bindings, row, field, basis in raw
        if (
            preferred := tuple(
                binding
                for binding in bindings
                if basis_priority[basis]
                == best_priority_by_atom[binding.atom_id]
            )
        )
    ]
    facts_by_atom: dict[str, set[str]] = defaultdict(set)
    for fact, bindings, _row, _field, _basis in raw:
        for binding in bindings:
            facts_by_atom[binding.atom_id].add(fact.fact_id)
    ambiguous_atom_ids = {
        atom_id
        for atom_id, fact_ids in facts_by_atom.items()
        if len(fact_ids) > 1
    }
    raw = [
        (fact, unambiguous, row, field, basis)
        for fact, bindings, row, field, basis in raw
        if (
            unambiguous := tuple(
                binding
                for binding in bindings
                if binding.atom_id not in ambiguous_atom_ids
            )
        )
    ]
    unique: dict[
        str,
        tuple[PhysicalTableFact, tuple[AtomFactBinding, ...], str],
    ] = {}
    for fact, bindings, _row, _field, basis in raw:
        unique.setdefault(fact.fact_id, (fact, bindings, basis))
    return tuple(
        unique[fact_id]
        for fact_id in sorted(
            unique,
            key=lambda item: (
                unique[item][0].document_version_id,
                unique[item][0].table_key,
                unique[item][0].row_index,
                unique[item][0].value_column_index,
            ),
        )
    )


def _selection_scope_digest(atom_ids: tuple[str, ...], plan: QueryPlan) -> str:
    """按语义来源许可签名，消除等价 Atom 拆分带来的身份漂移。"""
    atoms = {atom.atom_id: atom for atom in plan.atoms}
    contracts = {
        canonical_sha256(
            {
                "source_intent": scope.source_intent.value,
                "resolution": scope.resolution.value,
                "allowed_documents": [
                    item.model_dump(mode="json")
                    for item in scope.allowed_documents
                ],
                "required_content": scope.required_content.value,
                "mention_sha256": scope.mention_sha256,
                "registry_revision": scope.registry_revision,
            }
        )
        for atom_id in atom_ids
        if (scope := atoms[atom_id].source_scope) is not None
    }
    return canonical_sha256({"source_contracts": sorted(contracts)})


def _structured_member_scope(
    atom: QueryAtom,
    pack: GenerationEvidencePack,
) -> TargetMemberCoverage | None:
    """从当前 Atom 已关联的 canonical 组冻结非表格成员集合。"""
    if not _source_resolved(atom) or atom.answer_shape.value not in {
        "ENUMERATION",
        "PROCEDURE",
        "DUTIES",
    }:
        return None
    candidate_ids = set(
        dict(pack.per_atom_candidate_support_ids).get(atom.atom_id, ())
    )
    group_ids = {
        entry.source_group_id
        for entry in pack.entries
        if entry.support_id in candidate_ids
        and entry.source_group_id is not None
    }
    groups = tuple(
        group
        for group in pack.trusted_source_groups
        if group.group_id in group_ids
        and source_identity_allowed(
            atom.source_scope,
            document_id=group.document_id,
            document_version_id=group.document_version_id,
        )
        and group.kind
        in {
            EvidenceGroupKind.LIST_GROUP,
            EvidenceGroupKind.PROCEDURE_GROUP,
            EvidenceGroupKind.SECTION_GROUP,
        }
    )
    if not groups:
        return None
    evidence = tuple(
        item for item in pack.evidence if item.support_id in candidate_ids
    )
    base_semantics = current_atom_analysis(atom, None).semantics
    from rag_app.application.retrieval.semantics import (  # noqa: PLC0415
        parse_query_semantics,
    )

    parsed = parse_query_semantics(atom.original_fragment or atom.search_text)
    semantics = base_semantics.model_copy(
        update={
            "ordinal": parsed.ordinal,
            "expected_count": parsed.expected_count,
            "context_qualifier": (
                parsed.context_qualifier or base_semantics.context_qualifier
            ),
        }
    )
    result = target_member_coverage(
        atom,
        evidence,
        (),
        trusted_groups=groups,
        semantics=semantics,
        fact_covered=lambda _item, _claims: False,
    )
    return result if result.required_member_keys else None


def compile_answer_plan(
    query_plan: QueryPlan,
    pack: GenerationEvidencePack,
    *,
    snapshot_id: str,
) -> CompiledAnswerPlan:
    """把兼容查询计划和可信来源结构编译为唯一执行合同。

    Args:
        query_plan: 已签发来源身份的兼容 Root/Atom 计划。
        pack: 当前请求的有限、已授权来源和物理表格事实。
        snapshot_id: 请求开始时冻结的活动索引身份。

    Returns:
        下游不得再次从自然语言推导 required set 的冻结计划。

    """
    query_view = build_resolved_query_view(query_plan)
    scope_digest = _source_scope_digest(query_plan)
    registry = {item.support_id: item for item in pack.evidence}
    if len(registry) != len(pack.evidence):
        raise AnswerPlanCompilationError("ANSWER_PLAN_DUPLICATE_SUPPORT_ID")
    candidates = _candidate_facts(
        query_plan,
        pack,
        query_view.business_query,
        registry,
    )
    selections: list[EvidenceSelection] = []
    obligations: list[AnswerObligation] = []
    selected_atom_ids: set[str] = set()
    qualifier_specs = _qualifier_specs(query_view.business_query)
    for index, (fact, bindings, basis) in enumerate(candidates, 1):
        atom_ids = tuple(dict.fromkeys(binding.atom_id for binding in bindings))
        selected_atom_ids.update(atom_ids)
        selection_id = f"S{index}"
        selection_scope_digest = _selection_scope_digest(
            atom_ids,
            query_plan,
        )
        selection_payload = {
            "fact_id": fact.fact_id,
            "document_id": fact.document_id,
            "document_version_id": fact.document_version_id,
            "table_key": fact.table_key,
            "row": fact.row_index,
            "column": fact.value_column_index,
            "dependencies": [
                stable_support_key(registry[support_id])
                for support_id in fact.all_support_ids
            ],
            "scope": selection_scope_digest,
            "basis": basis,
        }
        selections.append(
            EvidenceSelection(
                selection_id=selection_id,
                selection_digest=canonical_sha256(selection_payload),
                fact_id=fact.fact_id,
                document_id=fact.document_id,
                document_version_id=fact.document_version_id,
                table_key=fact.table_key,
                row_index=fact.row_index,
                value_column_index=fact.value_column_index,
                target_label=_ordered_text(
                    fact.row_label_support_ids, registry
                ),
                field_label=_header_text(fact, registry),
                member_keys=(fact.fact_id,),
                value_support_ids=fact.value_support_ids,
                dependency_support_ids=fact.all_support_ids,
                scope_digest=selection_scope_digest,
                binding_basis=basis,
            )
        )
        qualifiers: list[AnswerQualifier] = []
        for qualifier_index, (kind, text) in enumerate(qualifier_specs, 1):
            support_ids = _qualifier_source_ids(
                kind,
                fact,
                registry,
            )
            qualifiers.append(
                AnswerQualifier(
                    qualifier_id=f"Q{qualifier_index}",
                    kind=kind,
                    text=text,
                    supported=bool(support_ids),
                    support_ids=support_ids,
                )
            )
        atoms = tuple(
            atom for atom in query_plan.atoms if atom.atom_id in atom_ids
        )
        obligations.append(
            AnswerObligation(
                obligation_id=f"O{len(obligations) + 1}",
                operation=AnswerOperation.FIELD_LOOKUP,
                atom_ids=atom_ids,
                selection_ids=(selection_id,),
                required_member_keys=(fact.fact_id,),
                member_dependencies=(
                    AnswerMemberDependency(
                        member_key=fact.fact_id,
                        support_ids=fact.value_support_ids,
                    ),
                ),
                qualifiers=tuple(qualifiers),
                source_resolved=all(_source_resolved(atom) for atom in atoms),
                source_closed=True,
            )
        )

    for atom in query_plan.atoms:
        if atom.atom_id in selected_atom_ids:
            continue
        member_scope = _structured_member_scope(atom, pack)
        if member_scope is None:
            continue
        selected_atom_ids.add(atom.atom_id)
        dependencies = tuple(
            AnswerMemberDependency(
                member_key=member_key,
                support_ids=support_ids,
            )
            for member_key, support_ids in member_scope.member_support_ids
        )
        dependency_ids = tuple(
            dict.fromkeys(
                support_id
                for item in dependencies
                for support_id in item.support_ids
            )
        )
        structured_qualifiers = tuple(
            AnswerQualifier(
                qualifier_id=f"Q{index}",
                kind=kind,
                text=text,
                supported=bool(
                    support_ids := _qualifier_support_ids(
                        kind,
                        dependency_ids,
                        registry,
                    )
                ),
                support_ids=support_ids,
            )
            for index, (kind, text) in enumerate(qualifier_specs, 1)
        )
        obligations.append(
            AnswerObligation(
                obligation_id=f"O{len(obligations) + 1}",
                operation=AnswerOperation.OPEN_TEXT,
                atom_ids=(atom.atom_id,),
                required_member_keys=member_scope.required_member_keys,
                member_dependencies=dependencies,
                qualifiers=structured_qualifiers,
                source_resolved=_source_resolved(atom),
                source_closed=member_scope.source_complete,
            )
        )

    for atom in query_plan.atoms:
        if atom.atom_id in selected_atom_ids:
            continue
        obligations.append(
            AnswerObligation(
                obligation_id=f"O{len(obligations) + 1}",
                operation=AnswerOperation.OPEN_TEXT,
                atom_ids=(atom.atom_id,),
                source_resolved=_source_resolved(atom),
                source_closed=False,
            )
        )

    tasks: list[PhysicalTask] = []
    deterministic = tuple(
        obligation
        for obligation in obligations
        if obligation.operation is AnswerOperation.FIELD_LOOKUP
    )
    if deterministic:
        selected_ids = {
            selection_id
            for obligation in deterministic
            for selection_id in obligation.selection_ids
        }
        selected = tuple(
            item for item in selections if item.selection_id in selected_ids
        )
        task_payload = {
            "mode": AnswerTaskMode.DETERMINISTIC.value,
            "obligations": [item.obligation_id for item in deterministic],
            "selections": [item.selection_digest for item in selected],
        }
        tasks.append(
            PhysicalTask(
                task_id=f"T{len(tasks) + 1}",
                task_digest=canonical_sha256(task_payload),
                mode=AnswerTaskMode.DETERMINISTIC,
                obligation_ids=tuple(
                    item.obligation_id for item in deterministic
                ),
                atom_ids=tuple(
                    dict.fromkeys(
                        atom_id
                        for item in deterministic
                        for atom_id in item.atom_ids
                    )
                ),
                selection_ids=tuple(item.selection_id for item in selected),
                dependency_support_ids=tuple(
                    dict.fromkeys(
                        support_id
                        for item in selected
                        for support_id in item.dependency_support_ids
                    )
                ),
            )
        )
    generated = tuple(
        obligation
        for obligation in obligations
        if obligation.operation is AnswerOperation.OPEN_TEXT
    )
    if generated:
        dependency_support_ids = tuple(
            dict.fromkeys(
                support_id
                for item in generated
                for dependency in item.member_dependencies
                for support_id in dependency.support_ids
            )
        )
        task_payload = {
            "mode": AnswerTaskMode.GROUNDED_GENERATION.value,
            "obligations": [item.obligation_id for item in generated],
            "atoms": [
                atom_id for item in generated for atom_id in item.atom_ids
            ],
            "dependencies": [
                stable_support_key(registry[support_id])
                for support_id in dependency_support_ids
                if support_id in registry
            ],
        }
        tasks.append(
            PhysicalTask(
                task_id=f"T{len(tasks) + 1}",
                task_digest=canonical_sha256(task_payload),
                mode=AnswerTaskMode.GROUNDED_GENERATION,
                obligation_ids=tuple(item.obligation_id for item in generated),
                atom_ids=tuple(
                    dict.fromkeys(
                        atom_id
                        for item in generated
                        for atom_id in item.atom_ids
                    )
                ),
                dependency_support_ids=dependency_support_ids,
            )
        )

    plan_payload = {
        "schema": ANSWER_PLAN_SCHEMA_REVISION,
        "policy": ANSWER_PLAN_POLICY_REVISION,
        "snapshot": snapshot_id,
        "scope": scope_digest,
        "query_view": query_view.model_dump(mode="json"),
        "obligations": [item.model_dump(mode="json") for item in obligations],
        "selections": [item.model_dump(mode="json") for item in selections],
        "tasks": [item.model_dump(mode="json") for item in tasks],
    }
    return CompiledAnswerPlan(
        plan_id=canonical_sha256(plan_payload),
        snapshot_id=snapshot_id,
        scope_digest=scope_digest,
        query_view=query_view,
        obligations=tuple(obligations),
        selections=tuple(selections),
        physical_tasks=tuple(tasks),
    )


__all__ = [
    "AnswerPlanCompilationError",
    "compile_answer_plan",
]
