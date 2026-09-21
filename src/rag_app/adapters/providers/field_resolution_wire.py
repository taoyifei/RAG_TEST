"""V14.1 C5 字段解析的单一业务合同、传输 schema 与本地验证。"""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal, Self

from pydantic import Field, StrictStr, ValidationError, model_validator

from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    FieldCandidate,
    FieldResolution,
    FieldResolutionStatus,
)
from rag_app.core.models.answer_plan import ResolvedQueryView
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import QueryAtom

FIELD_RESOLUTION_SCHEMA_REVISION = "wb08r-field-resolution-v2"
_MAX_ATOMS = 4
_MAX_CANDIDATES_PER_ATOM = 16
_MAX_QUERY_FRAGMENT = 160


class FieldResolutionWireError(ValueError):
    """只携带稳定原因码的字段 Wire 合同失败。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class FieldResolutionContractContext(FrozenModel):
    """构造字段合同所需的受信问题视图、Atom 与真实候选。"""

    query_view: ResolvedQueryView
    atoms: tuple[QueryAtom, ...] = Field(min_length=1, max_length=_MAX_ATOMS)
    candidates: tuple[FieldCandidate, ...] = Field(
        min_length=1,
        max_length=_MAX_ATOMS * _MAX_CANDIDATES_PER_ATOM,
    )


class FieldResolutionAtomContract(FrozenModel):
    """一个 Atom 的候选集合及可验证业务问题片段范围。"""

    atom_id: str = Field(pattern=r"^A[1-4]$")
    original_fragment: str | None = Field(default=None, max_length=320)
    trusted_span_start: int | None = Field(default=None, ge=0)
    trusted_span_end: int | None = Field(default=None, gt=0)
    candidate_ids: tuple[str, ...] = Field(
        min_length=1, max_length=_MAX_CANDIDATES_PER_ATOM
    )

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("Atom 候选 ID 不允许重复。")
        if (self.trusted_span_start is None) != (self.trusted_span_end is None):
            raise ValueError("Atom 受信问题范围必须同时存在或同时为空。")
        if (
            self.trusted_span_start is not None
            and self.trusted_span_end is not None
            and self.trusted_span_end <= self.trusted_span_start
        ):
            raise ValueError("Atom 受信问题范围不能倒置。")
        return self


class FieldResolutionContract(FrozenModel):
    """同时驱动请求、传输 schema 与响应验证的完整业务声明。"""

    schema_revision: Literal["wb08r-field-resolution-v2"] = (
        "wb08r-field-resolution-v2"
    )
    query_view_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    query_view: ResolvedQueryView = Field(exclude=True, repr=False)
    business_query: str = Field(min_length=1, max_length=8000, repr=False)
    atoms: tuple[FieldResolutionAtomContract, ...] = Field(
        min_length=1, max_length=_MAX_ATOMS
    )
    candidates: tuple[FieldCandidate, ...] = Field(
        min_length=1,
        max_length=_MAX_ATOMS * _MAX_CANDIDATES_PER_ATOM,
        repr=False,
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        atom_ids = tuple(item.atom_id for item in self.atoms)
        if len(atom_ids) != len(set(atom_ids)):
            raise ValueError("字段合同 Atom 不允许重复。")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("字段合同候选 ID 不允许重复。")
        allowed = {
            (atom.atom_id, candidate_id)
            for atom in self.atoms
            for candidate_id in atom.candidate_ids
        }
        actual = {
            (candidate.atom_id, candidate.candidate_id)
            for candidate in self.candidates
        }
        if actual != allowed:
            raise ValueError("字段合同候选与 Atom 声明不一致。")
        return self

    @property
    def contract_sha256(self) -> str:
        """返回包含动态候选和业务问题视图的逐请求审计身份。"""
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def max_candidates_per_atom(self) -> int:
        """返回本请求的真实最大候选形状。"""
        return max(len(item.candidate_ids) for item in self.atoms)

    @property
    def max_candidate_id_length(self) -> int:
        """返回动态候选 ID 的真实最大长度。"""
        return max(
            len(candidate_id)
            for item in self.atoms
            for candidate_id in item.candidate_ids
        )


class _WireChoice(FrozenModel):
    """模型可签发的字段状态，不包含确定性专属 EXACT。"""

    a: StrictStr = Field(pattern=r"^A[1-4]$")
    s: Literal[
        "SUPPORTED_PARAPHRASE",
        "RELATED_FIELD",
        "AMBIGUOUS",
        "NOT_FOUND",
    ]
    c: tuple[StrictStr, ...] = Field(
        default=(), max_length=_MAX_CANDIDATES_PER_ATOM
    )
    q: StrictStr = Field(min_length=1, max_length=_MAX_QUERY_FRAGMENT)


class _WirePayload(FrozenModel):
    """字段解析 Wire 的唯一根外壳。"""

    r: tuple[_WireChoice, ...] = Field(min_length=1, max_length=_MAX_ATOMS)


def build_field_resolution_contract(
    context: FieldResolutionContractContext,
) -> FieldResolutionContract:
    """从同一受信上下文建立请求和响应共用的字段合同。"""
    candidates_by_atom: dict[str, list[FieldCandidate]] = defaultdict(list)
    for candidate in context.candidates:
        candidates_by_atom[candidate.atom_id].append(candidate)
    context_atom_ids = {atom.atom_id for atom in context.atoms}
    if not set(candidates_by_atom) <= context_atom_ids:
        raise FieldResolutionWireError("FIELD_CONTRACT_UNKNOWN_ATOM")
    expected_atoms = tuple(
        atom for atom in context.atoms if atom.atom_id in candidates_by_atom
    )
    if not expected_atoms:
        raise FieldResolutionWireError("FIELD_CONTRACT_EMPTY")
    single_atom = len(expected_atoms) == 1
    atom_contracts: list[FieldResolutionAtomContract] = []
    for atom in expected_atoms:
        span = (
            (0, len(context.query_view.business_query))
            if single_atom
            else _trusted_atom_span(
                context.query_view.business_query, atom.original_fragment
            )
        )
        candidates = tuple(candidates_by_atom[atom.atom_id])
        atom_contracts.append(
            FieldResolutionAtomContract(
                atom_id=atom.atom_id,
                original_fragment=atom.original_fragment,
                trusted_span_start=None if span is None else span[0],
                trusted_span_end=None if span is None else span[1],
                candidate_ids=tuple(item.candidate_id for item in candidates),
            )
        )
    return FieldResolutionContract(
        query_view_digest=canonical_sha256(
            context.query_view.model_dump(mode="json")
        ),
        query_view=context.query_view,
        business_query=context.query_view.business_query,
        atoms=tuple(atom_contracts),
        candidates=tuple(
            candidate
            for atom in expected_atoms
            for candidate in candidates_by_atom[atom.atom_id]
        ),
    )


def render_wire_schema(
    contract: FieldResolutionContract,
    capability_profile: StructuredOutputCapabilityProfile,
) -> dict[str, object]:
    """按固定后端能力渲染传输子集，未表达唯一性仍由本地强制。"""
    atom_ids = [item.atom_id for item in contract.atoms]
    candidate_ids = sorted(
        candidate_id
        for item in contract.atoms
        for candidate_id in item.candidate_ids
    )
    candidate_array: dict[str, object] = {
        "type": "array",
        "maxItems": _MAX_CANDIDATES_PER_ATOM,
        "items": {"type": "string", "enum": candidate_ids},
    }
    if capability_profile.supports_keyword("uniqueItems"):
        candidate_array["uniqueItems"] = True
    elif "uniqueItems" not in capability_profile.local_constraint_transfers:
        raise FieldResolutionWireError("FIELD_CONTRACT_UNIQUENESS_NOT_ENFORCED")
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["r"],
        "properties": {
            "r": {
                "type": "array",
                "minItems": len(atom_ids),
                "maxItems": len(atom_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["a", "s", "c", "q"],
                    "properties": {
                        "a": {"type": "string", "enum": atom_ids},
                        "s": {
                            "type": "string",
                            "enum": [
                                "SUPPORTED_PARAPHRASE",
                                "RELATED_FIELD",
                                "AMBIGUOUS",
                                "NOT_FOUND",
                            ],
                        },
                        "c": candidate_array,
                        "q": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_QUERY_FRAGMENT,
                        },
                    },
                },
            }
        },
    }
    capability_profile.assert_schema(
        contract.schema_revision,
        schema,
        atom_count=len(contract.atoms),
        max_candidates_per_atom=contract.max_candidates_per_atom,
        max_candidate_id_length=contract.max_candidate_id_length,
        max_query_fragment_length=_MAX_QUERY_FRAGMENT,
    )
    return schema


def validate_field_response(
    content: str,
    contract: FieldResolutionContract,
) -> tuple[FieldResolution, ...]:
    """拒绝重复键并严格验证 Atom、候选、状态组合和业务问句坐标。"""
    try:
        raw = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, FieldResolutionWireError):
        raise FieldResolutionWireError("FIELD_RESPONSE_INVALID_JSON") from None
    try:
        payload = _WirePayload.model_validate(raw)
    except ValidationError:
        raise FieldResolutionWireError(
            "FIELD_RESPONSE_SCHEMA_INVALID"
        ) from None
    expected_atom_ids = tuple(item.atom_id for item in contract.atoms)
    actual_atom_ids = tuple(item.a for item in payload.r)
    if len(actual_atom_ids) != len(expected_atom_ids):
        raise FieldResolutionWireError("FIELD_RESPONSE_ATOM_COUNT_MISMATCH")
    if len(actual_atom_ids) != len(set(actual_atom_ids)):
        raise FieldResolutionWireError("FIELD_RESPONSE_DUPLICATE_ATOM")
    if set(actual_atom_ids) != set(expected_atom_ids):
        raise FieldResolutionWireError("FIELD_RESPONSE_ATOM_SET_MISMATCH")
    atoms = {item.atom_id: item for item in contract.atoms}
    choices = {item.a: item for item in payload.r}
    resolutions: list[FieldResolution] = []
    for atom_id in expected_atom_ids:
        atom = atoms[atom_id]
        choice = choices[atom_id]
        if len(choice.c) != len(set(choice.c)):
            raise FieldResolutionWireError("FIELD_RESPONSE_DUPLICATE_CANDIDATE")
        if not set(choice.c) <= set(atom.candidate_ids):
            raise FieldResolutionWireError(
                "FIELD_RESPONSE_CANDIDATE_OUT_OF_SCOPE"
            )
        span = _unique_query_span(contract, atom, choice.q)
        original_span = _original_query_span(contract, choice.q)
        try:
            resolutions.append(
                FieldResolution(
                    atom_id=atom_id,
                    status=FieldResolutionStatus(choice.s),
                    candidate_ids=choice.c,
                    query_span_start=span[0],
                    query_span_end=span[1],
                    query_fragment=choice.q,
                    query_view_digest=contract.query_view_digest,
                    span_basis="BUSINESS_QUERY",
                    original_query_span_start=(
                        None if original_span is None else original_span[0]
                    ),
                    original_query_span_end=(
                        None if original_span is None else original_span[1]
                    ),
                    reason_code="SCHEMA_AWARE_INTERPRETATION_V2",
                )
            )
        except ValueError:
            raise FieldResolutionWireError(
                "FIELD_RESPONSE_STATUS_COMBINATION_INVALID"
            ) from None
    return tuple(resolutions)


def request_payload(contract: FieldResolutionContract) -> dict[str, object]:
    """渲染供模型判断的私有业务输入，候选身份只来自同一合同。"""
    return {
        "question": contract.business_query,
        "query_view_digest": contract.query_view_digest,
        "atoms": [
            {
                "atom": atom.atom_id,
                "original_fragment": atom.original_fragment,
                "candidate_ids": list(atom.candidate_ids),
            }
            for atom in contract.atoms
        ],
        "candidates": [
            {
                "id": item.candidate_id,
                "atom": item.atom_id,
                "target": item.target_label,
                "field": item.field_label,
                "value_preview": item.value_preview,
            }
            for item in contract.candidates
        ],
    }


def _trusted_atom_span(
    business_query: str, fragment: str | None
) -> tuple[int, int] | None:
    if fragment is None or not fragment:
        return None
    positions = _occurrences(business_query, fragment)
    if len(positions) != 1:
        return None
    start = positions[0]
    return start, start + len(fragment)


def _unique_query_span(
    contract: FieldResolutionContract,
    atom: FieldResolutionAtomContract,
    query_fragment: str,
) -> tuple[int, int]:
    if atom.trusted_span_start is None or atom.trusted_span_end is None:
        raise FieldResolutionWireError("FIELD_RESPONSE_ATOM_SCOPE_UNDETERMINED")
    trusted = contract.business_query[
        atom.trusted_span_start : atom.trusted_span_end
    ]
    relative_positions = _occurrences(trusted, query_fragment)
    if len(relative_positions) != 1:
        raise FieldResolutionWireError("FIELD_RESPONSE_QUERY_SPAN_NOT_UNIQUE")
    start = atom.trusted_span_start + relative_positions[0]
    end = start + len(query_fragment)
    if contract.business_query[start:end] != query_fragment:
        raise FieldResolutionWireError("FIELD_RESPONSE_QUERY_SPAN_INVALID")
    return start, end


def _occurrences(text: str, fragment: str) -> tuple[int, ...]:
    if not fragment:
        return ()
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(fragment, start)
        if position < 0:
            return tuple(positions)
        positions.append(position)
        start = position + 1


def _original_query_span(
    contract: FieldResolutionContract, query_fragment: str
) -> tuple[int, int] | None:
    """从 NFKC 坐标恢复唯一原始跨度，并再次验证逐字规范化切片。"""
    view = contract.query_view
    positions = _occurrences(view.normalized_query, query_fragment)
    if len(positions) != 1:
        return None
    normalized_start = positions[0]
    normalized_end = normalized_start + len(query_fragment)
    covered = tuple(
        span
        for span in view.normalized_offsets
        if span.normalized_end > normalized_start
        and span.normalized_start < normalized_end
    )
    if not covered:
        return None
    original_start = min(span.original_start for span in covered)
    original_end = max(span.original_end for span in covered)
    original_slice = view.original_query[original_start:original_end]
    if unicodedata.normalize("NFKC", original_slice) != query_fragment:
        return None
    return original_start, original_end


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, object]],
) -> Mapping[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise FieldResolutionWireError("FIELD_RESPONSE_DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> None:
    del value
    raise FieldResolutionWireError("FIELD_RESPONSE_NON_JSON_NUMBER")


__all__ = [
    "FIELD_RESOLUTION_SCHEMA_REVISION",
    "FieldResolutionContract",
    "FieldResolutionContractContext",
    "FieldResolutionWireError",
    "build_field_resolution_contract",
    "render_wire_schema",
    "request_payload",
    "validate_field_response",
]
