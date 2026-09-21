"""OpenAI-compatible 结构化输出连接的共享能力合同。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models.common import FrozenModel

StructuredOutputMode = Literal[
    "response_format", "structured_outputs", "guided_json"
]

_KNOWN_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "description",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "if",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "properties",
        "required",
        "then",
        "title",
        "type",
        "uniqueItems",
    }
)
_DEFAULT_ALLOWED_KEYWORDS = (
    "$defs",
    "$ref",
    "additionalProperties",
    "default",
    "description",
    "enum",
    "items",
    "maxItems",
    "maxLength",
    "minItems",
    "minLength",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
)
_REGISTERED_SCHEMA_FAMILIES = (
    "wb08r-field-resolution-v2",
    "wb08r-query-plan-v11",
    "wb08r-grounded-wire-v9",
    "wb08r-semantic-validation-v3",
    "wb08r-relation-review-v4",
)


class StructuredSchemaFamilyQualification(FrozenModel):
    """一个已按形状边界验证的 schema 家族，不绑定动态 enum hash。"""

    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    max_schema_bytes: StrictInt = Field(gt=0, le=1_048_576)
    max_atoms: StrictInt | None = Field(default=None, ge=1, le=32)
    max_candidates_per_atom: StrictInt | None = Field(
        default=None, ge=1, le=256
    )
    max_candidate_id_length: StrictInt | None = Field(
        default=None, ge=1, le=256
    )
    max_query_fragment_length: StrictInt | None = Field(
        default=None, ge=1, le=4096
    )


class StructuredOutputCapabilityProfile(FrozenModel):
    """固定服务身份、模式、后端和已验证 schema 家族的资格。"""

    profile_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    service_identity_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=200)
    chat_template_revision: str = Field(min_length=1, max_length=160)
    mode: StructuredOutputMode
    grammar_backend: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$"
    )
    allowed_keywords: tuple[str, ...] = Field(min_length=1, max_length=64)
    local_constraint_transfers: tuple[str, ...] = Field(
        default=(), max_length=16
    )
    families: tuple[StructuredSchemaFamilyQualification, ...] = Field(
        min_length=1, max_length=16
    )
    deadline_ms: StrictInt = Field(gt=0, le=30_000)
    field_resolution_output_tokens: StrictInt = Field(ge=32, le=1536)
    qualification_evidence_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        if len(self.allowed_keywords) != len(set(self.allowed_keywords)):
            raise ValueError("结构化输出 keyword 不允许重复。")
        if len(self.local_constraint_transfers) != len(
            set(self.local_constraint_transfers)
        ):
            raise ValueError("本地约束移交项不允许重复。")
        revisions = tuple(item.schema_revision for item in self.families)
        if len(revisions) != len(set(revisions)):
            raise ValueError("结构化输出 schema 家族不允许重复。")
        unknown = set(self.allowed_keywords) - _KNOWN_SCHEMA_KEYWORDS
        if unknown:
            raise ValueError("能力合同包含未知 JSON Schema keyword。")
        transfers = set(self.local_constraint_transfers)
        if not transfers <= _KNOWN_SCHEMA_KEYWORDS:
            raise ValueError("本地约束移交包含未知 JSON Schema keyword。")
        if transfers & set(self.allowed_keywords):
            raise ValueError("同一约束不能同时声明后端执行和本地移交。")
        return self

    @property
    def profile_sha256(self) -> str:
        """返回随服务、模板、后端和资格证据变化的完整身份。"""
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def grammar_backend_fingerprint(self) -> str:
        """返回不含 URL 或凭据的 grammar 后端身份。"""
        return canonical_sha256(
            {
                "service": self.service_identity_sha256,
                "model": self.model,
                "template": self.chat_template_revision,
                "mode": self.mode,
                "backend": self.grammar_backend,
                "profile": self.profile_revision,
            }
        )

    def family(
        self, schema_revision: str
    ) -> StructuredSchemaFamilyQualification:
        """返回已登记家族；未登记即拒绝发送。"""
        for family in self.families:
            if family.schema_revision == schema_revision:
                return family
        raise ValueError("STRUCTURED_SCHEMA_FAMILY_NOT_QUALIFIED")

    def supports_keyword(self, keyword: str) -> bool:
        """判断固定后端资格是否允许某个 JSON Schema keyword。"""
        return keyword in self.allowed_keywords

    def assert_schema(  # noqa: PLR0913
        self,
        schema_revision: str,
        schema: Mapping[str, object],
        *,
        atom_count: int | None = None,
        max_candidates_per_atom: int | None = None,
        max_candidate_id_length: int | None = None,
        max_query_fragment_length: int | None = None,
    ) -> None:
        """校验家族、keyword、序列化大小和动态形状上界。"""
        family = self.family(schema_revision)
        keywords = _schema_keywords(schema)
        unsupported = keywords - set(self.allowed_keywords)
        if unsupported:
            raise ValueError("STRUCTURED_SCHEMA_KEYWORD_NOT_QUALIFIED")
        if (
            len(canonical_json(schema).encode("utf-8"))
            > family.max_schema_bytes
        ):
            raise ValueError("STRUCTURED_SCHEMA_SIZE_NOT_QUALIFIED")
        _within_optional_bound(atom_count, family.max_atoms, "ATOM")
        _within_optional_bound(
            max_candidates_per_atom,
            family.max_candidates_per_atom,
            "CANDIDATE",
        )
        _within_optional_bound(
            max_candidate_id_length,
            family.max_candidate_id_length,
            "CANDIDATE_ID",
        )
        _within_optional_bound(
            max_query_fragment_length,
            family.max_query_fragment_length,
            "QUERY_FRAGMENT",
        )


def wb08r_structured_output_profile(  # noqa: PLR0913
    *,
    profile_revision: str,
    service_identity_sha256: str,
    model: str,
    chat_template_revision: str,
    mode: StructuredOutputMode,
    grammar_backend: str,
    deadline_ms: int,
    field_resolution_output_tokens: int,
    qualification_evidence_sha256: str,
    allow_unique_items: bool = False,
) -> StructuredOutputCapabilityProfile:
    """构造登记 03 全部实际 schema 家族的固定能力合同。"""
    allowed: tuple[str, ...] = (*_DEFAULT_ALLOWED_KEYWORDS,)
    transfers: tuple[str, ...] = ("uniqueItems",)
    if allow_unique_items:
        allowed = (*allowed, "uniqueItems")
        transfers = ()
    families = tuple(
        StructuredSchemaFamilyQualification(
            schema_revision=revision,
            max_schema_bytes=262_144,
            # 字段协议的逻辑请求仍可包含四个 Atom，但一次真实 HTTP
            # 只资格化一个 Atom；组合与部分失败隔离由应用层负责。
            max_atoms=1 if revision == "wb08r-field-resolution-v2" else None,
            max_candidates_per_atom=(
                16 if revision == "wb08r-field-resolution-v2" else None
            ),
            max_candidate_id_length=(
                16 if revision == "wb08r-field-resolution-v2" else None
            ),
            max_query_fragment_length=(
                160 if revision == "wb08r-field-resolution-v2" else None
            ),
        )
        for revision in _REGISTERED_SCHEMA_FAMILIES
    )
    return StructuredOutputCapabilityProfile(
        profile_revision=profile_revision,
        service_identity_sha256=service_identity_sha256,
        model=model,
        chat_template_revision=chat_template_revision,
        mode=mode,
        grammar_backend=grammar_backend,
        allowed_keywords=allowed,
        local_constraint_transfers=transfers,
        families=families,
        deadline_ms=deadline_ms,
        field_resolution_output_tokens=field_resolution_output_tokens,
        qualification_evidence_sha256=qualification_evidence_sha256,
    )


def _schema_keywords(value: object) -> set[str]:
    """只识别规范 keyword，不把动态 property 名误判成能力。"""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _KNOWN_SCHEMA_KEYWORDS:
                found.add(key)
            if key == "properties" and isinstance(item, Mapping):
                for property_schema in item.values():
                    found.update(_schema_keywords(property_schema))
            elif key == "$defs" and isinstance(item, Mapping):
                for definition in item.values():
                    found.update(_schema_keywords(definition))
            else:
                found.update(_schema_keywords(item))
    elif isinstance(value, (tuple, list)):
        for item in value:
            found.update(_schema_keywords(item))
    return found


def _within_optional_bound(
    actual: int | None, maximum: int | None, label: str
) -> None:
    if actual is None:
        return
    if maximum is None or not 0 <= actual <= maximum:
        raise ValueError(f"STRUCTURED_SCHEMA_{label}_SHAPE_NOT_QUALIFIED")


__all__ = [
    "StructuredOutputCapabilityProfile",
    "StructuredOutputMode",
    "StructuredSchemaFamilyQualification",
    "wb08r_structured_output_profile",
]
