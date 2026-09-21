"""自然问答模型线的唯一轻量协议与安全逐项诊断。"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.retrieval import (
    GroundedWireClaim,
    GroundedWireDiagnostic,
)

_MAX_CLAIMS = 24
_EXPECTED_TYPES = {
    "atom_id": "string",
    "text": "string",
    "refs": "array",
}
_COMPLETE_JSON_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```\Z",
    re.IGNORECASE,
)


class GroundedWirePayload(FrozenModel):
    """供 Provider structured output 使用的唯一根 Schema。"""

    claims: tuple[GroundedWireClaim, ...] = Field(max_length=_MAX_CLAIMS)


_GROUNDED_WIRE_SCHEMA_TEMPLATE = GroundedWirePayload.model_json_schema()
_KNOWN_ATOM_IDS = frozenset({"A1", "A2", "A3", "A4"})


def _schema_object(value: object, path: str) -> dict[str, object]:
    """读取受控 Schema 节点并在内部结构漂移时立即失败。"""
    if not isinstance(value, dict):
        raise RuntimeError(f"Grounded Wire Schema 节点无效：{path}")
    return value


def _local_schema_target(
    schema: dict[str, object], reference: str
) -> dict[str, object]:
    """解析同一 Schema 内的 JSON Pointer，避免误改 `$ref` 外壳。"""
    if not reference.startswith("#/"):
        raise RuntimeError("Grounded Wire Schema 只允许本地引用。")
    target: object = schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        target = _schema_object(target, reference).get(part)
    return _schema_object(target, reference)


def grounded_wire_schema(
    allowed_atom_ids: Collection[str],
) -> dict[str, object]:
    """生成只允许本次真实发送 Atom 的独立输出 Schema。

    Args:
        allowed_atom_ids: 本次发送消息中存在的 Atom ID 集合。

    Returns:
        深复制的 Schema；`atom_id` enum 位于 Claim 的真实 `$ref` 目标。

    Raises:
        ValueError: 集合为空或包含协议外 Atom ID。
        RuntimeError: Pydantic 生成的固定 Schema 结构发生不可兼容漂移。

    """
    atom_ids = tuple(sorted(set(allowed_atom_ids)))
    if not atom_ids or not set(atom_ids) <= _KNOWN_ATOM_IDS:
        raise ValueError("Grounded Wire Schema 的 Atom 集合无效。")
    schema = deepcopy(_GROUNDED_WIRE_SCHEMA_TEMPLATE)
    properties = _schema_object(schema.get("properties"), "properties")
    claims = _schema_object(properties.get("claims"), "properties.claims")
    items = _schema_object(claims.get("items"), "properties.claims.items")
    reference = items.get("$ref")
    if not isinstance(reference, str):
        raise RuntimeError("Grounded Wire Claim 缺少本地 `$ref`。")
    claim_schema = _local_schema_target(schema, reference)
    claim_properties = _schema_object(
        claim_schema.get("properties"), f"{reference}.properties"
    )
    atom_schema = _schema_object(
        claim_properties.get("atom_id"), f"{reference}.properties.atom_id"
    )
    atom_schema["enum"] = list(atom_ids)
    return schema


class GroundedWireError(ValueError):
    """根级协议错误；字段只描述形状，不保存模型正文或字段值。"""

    def __init__(
        self,
        *,
        failure_stage: Literal["json_decode", "wire_schema"],
        failure_code: str,
        json_path: str,
        expected_type: str | None = None,
        observed_type: str | None = None,
    ) -> None:
        self.failure_stage = failure_stage
        self.failure_code = failure_code
        self.json_path = json_path
        self.expected_type = expected_type
        self.observed_type = observed_type
        super().__init__(failure_code)

    @property
    def safe_details(self) -> dict[str, str]:
        """返回可进入 Trace 的无正文诊断。"""
        details = {
            "failure_stage": self.failure_stage,
            "failure_code": self.failure_code,
            "json_path": self.json_path,
        }
        if self.expected_type is not None:
            details["expected_type"] = self.expected_type
        if self.observed_type is not None:
            details["observed_type"] = self.observed_type
        return details


@dataclass(frozen=True, slots=True)
class GroundedWireParseResult:
    """根合法后保留有效条目并逐项拒绝互不依赖的坏条目。"""

    claims: tuple[GroundedWireClaim, ...]
    diagnostics: tuple[GroundedWireDiagnostic, ...]


def _unique_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GroundedWireError(
                failure_stage="json_decode",
                failure_code="DUPLICATE_OBJECT_KEY",
                json_path="$",
                expected_type="unique_object_keys",
                observed_type="duplicate_key",
            )
        result[key] = value
    return result


def _json_value(content: str) -> object:
    stripped = content.strip()
    if match := _COMPLETE_JSON_FENCE.fullmatch(stripped):
        stripped = match["body"]
    try:
        return json.loads(stripped, object_pairs_hook=_unique_object)
    except GroundedWireError:
        raise
    except (json.JSONDecodeError, TypeError):
        raise GroundedWireError(
            failure_stage="json_decode",
            failure_code="JSON_DECODE_FAILED",
            json_path="$",
            expected_type="json_object",
            observed_type="text",
        ) from None


def _observed_type(raw: object, location: tuple[object, ...]) -> str:
    value = raw
    for part in location:
        if isinstance(part, str) and isinstance(value, Mapping):
            if part not in value:
                return "missing"
            value = value[part]
        elif isinstance(part, int) and isinstance(value, (list, tuple)):
            if not 0 <= part < len(value):
                return "missing"
            value = value[part]
        else:
            return type(value).__name__
    return type(value).__name__


def _item_diagnostic(
    index: int, raw: object, error: ValidationError
) -> GroundedWireDiagnostic:
    detail = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[0]
    location = tuple(detail.get("loc", ()))
    field = location[0] if location and isinstance(location[0], str) else None
    error_type = str(detail.get("type") or "schema_error")
    if error_type == "missing":
        code = "FIELD_REQUIRED"
    elif error_type == "extra_forbidden":
        code = "UNKNOWN_FIELD"
    elif error_type.endswith("_type"):
        code = "FIELD_TYPE"
    elif "too_" in error_type or error_type.startswith("string_pattern"):
        code = "FIELD_CONSTRAINT"
    else:
        code = "FIELD_VALUE"
    raw_atom_id = raw.get("atom_id") if isinstance(raw, Mapping) else None
    atom_id = (
        raw_atom_id
        if isinstance(raw, Mapping) and raw_atom_id in {"A1", "A2", "A3", "A4"}
        else None
    )
    path = f"claims[{index}]"
    if location:
        path += "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in location
        )
    return GroundedWireDiagnostic(
        item_index=index,
        atom_id=atom_id,
        failure_stage="wire_schema",
        failure_code=code,
        json_path=path,
        expected_type=_EXPECTED_TYPES.get(field) if field is not None else None,
        observed_type=_observed_type(raw, location),
    )


def parse_grounded_wire(
    content: str,
    *,
    allowed_atom_ids: frozenset[str],
    allowed_refs_by_atom: Mapping[str, frozenset[str]],
) -> GroundedWireParseResult:
    """严格解析根对象，并让单条字段或引用错误只影响该条。

    Args:
        content: Provider 返回的完整 content。
        allowed_atom_ids: 当前尝试实际发送的 Atom。
        allowed_refs_by_atom: 每个 Atom 在同一发送包内可引用的短编号。

    Returns:
        独立有效的 Wire Claim 与不含正文的逐项诊断。

    Raises:
        GroundedWireError: 根 JSON 或根 Schema 不可安全解释。

    """
    payload = _json_value(content)
    if not isinstance(payload, dict):
        raise GroundedWireError(
            failure_stage="wire_schema",
            failure_code="ROOT_TYPE",
            json_path="$",
            expected_type="object",
            observed_type=type(payload).__name__,
        )
    if set(payload) != {"claims"}:
        raise GroundedWireError(
            failure_stage="wire_schema",
            failure_code="ROOT_FIELDS",
            json_path="$",
            expected_type="claims_only",
            observed_type="object",
        )
    raw_claims = payload["claims"]
    if not isinstance(raw_claims, list):
        raise GroundedWireError(
            failure_stage="wire_schema",
            failure_code="CLAIMS_TYPE",
            json_path="claims",
            expected_type="array",
            observed_type=type(raw_claims).__name__,
        )
    if len(raw_claims) > _MAX_CLAIMS:
        raise GroundedWireError(
            failure_stage="wire_schema",
            failure_code="CLAIMS_COUNT",
            json_path="claims",
            expected_type=f"array_max_{_MAX_CLAIMS}",
            observed_type="array",
        )

    accepted: list[GroundedWireClaim] = []
    diagnostics: list[GroundedWireDiagnostic] = []
    for index, raw in enumerate(raw_claims):
        try:
            claim = GroundedWireClaim.model_validate(raw)
        except ValidationError as error:
            diagnostics.append(_item_diagnostic(index, raw, error))
            continue
        if claim.atom_id not in allowed_atom_ids:
            diagnostics.append(
                GroundedWireDiagnostic(
                    item_index=index,
                    atom_id=claim.atom_id,
                    failure_stage="wire_schema",
                    failure_code="UNKNOWN_ATOM",
                    json_path=f"claims[{index}].atom_id",
                    expected_type="sent_atom_id",
                    observed_type="string",
                )
            )
            continue
        if not set(claim.refs) <= allowed_refs_by_atom.get(
            claim.atom_id, frozenset()
        ):
            diagnostics.append(
                GroundedWireDiagnostic(
                    item_index=index,
                    atom_id=claim.atom_id,
                    failure_stage="evidence_binding",
                    failure_code="UNKNOWN_OR_OUT_OF_SCOPE_REF",
                    json_path=f"claims[{index}].refs",
                    expected_type="sent_read_unit_ids",
                    observed_type="array",
                )
            )
            continue
        if claim in accepted:
            diagnostics.append(
                GroundedWireDiagnostic(
                    item_index=index,
                    atom_id=claim.atom_id,
                    failure_stage="wire_schema",
                    failure_code="DUPLICATE_ITEM",
                    json_path=f"claims[{index}]",
                    expected_type="unique_claim",
                    observed_type="duplicate_item",
                )
            )
            continue
        accepted.append(claim)
    return GroundedWireParseResult(tuple(accepted), tuple(diagnostics))
