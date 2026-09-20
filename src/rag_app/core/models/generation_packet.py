"""生成尝试的来源身份与最终发送合同；不属于公开问答 Schema。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)

if TYPE_CHECKING:
    from rag_app.core.models.retrieval import EvidenceItem

EVIDENCE_IDENTITY_REVISION = "wb08r-source-key-v1"
PREPARED_PACKET_REVISION = "wb08r-prepared-packet-v3"
GENERATION_BUDGET_REVISION = "wb08r-generation-budget-v3"


class EvidenceReadUnit(FrozenModel):
    """模型可读的短编号来源单元及其服务端来源映射。"""

    unit_id: str = Field(pattern=r"^E[1-9][0-9]*$")
    kind: Literal["paragraph", "list_item", "table_fact", "catalog_entry"]
    text: str = Field(min_length=1, max_length=12_000, repr=False)
    source_context: JsonObject = ()
    support_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    fact_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_complete: bool

    @field_validator("source_context", mode="before")
    @classmethod
    def _freeze_source_context(cls, value: object) -> JsonObject:
        """冻结不含正文的可读来源语境。"""
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _validate_identity(self) -> EvidenceReadUnit:
        if len(self.support_ids) != len(set(self.support_ids)):
            raise ValueError("阅读单元的来源 ID 不允许重复。")
        if (self.kind == "table_fact") != (self.fact_id is not None):
            raise ValueError("只有物理表格阅读单元必须绑定 fact_id。")
        return self


def stable_read_unit_digest(unit: EvidenceReadUnit) -> str:
    """摘要本次可读投影，防止复核阶段替换正文或可信语境。"""
    return canonical_sha256(unit.model_dump(mode="json"))


def safe_support_source(item: EvidenceItem) -> dict[str, object]:
    """为授权来源提供可审计的定位；不包含正文、标题或端点。

    Args:
        item: 已通过权限及版本边界的原文引用单元。

    Returns:
        稳定来源 key、原文摘要及实际节点跨度的 SAFE 投影。

    """
    return {
        "support_key": stable_support_key(item),
        "support_id": item.support_id,
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "document_version_id": item.document_version_id,
        "quote_sha256": canonical_sha256(item.citation_text),
        "spans": [
            {
                "node_id": span.node_id,
                "part_uri": span.source_anchor.part_uri
                if span.source_anchor
                else None,
                "story_kind": span.source_anchor.story_kind.value
                if span.source_anchor
                else None,
                "structural_path": span.structural_path,
                "source_start_char": span.source_start_char,
                "source_end_char": span.source_end_char,
            }
            for span in item.source_spans
        ],
    }


def stable_support_key(item: EvidenceItem) -> str:
    """以真实来源定位和原文摘要认定身份，不使用展示别名或语义证书。

    Args:
        item: 携带授权范围、活动版本、原文定位及逐字引用的证据。

    Returns:
        与排序、别名和逐 Atom 关系认证无关的来源身份摘要。

    """
    metadata = dict(item.metadata)
    return canonical_sha256(
        {
            "revision": EVIDENCE_IDENTITY_REVISION,
            "scope": item.source_identity_scope
            or [
                metadata.get("project_id"),
                metadata.get("knowledge_base_id"),
                metadata.get("index_revision_id"),
            ],
            "document_id": item.document_id,
            "document_version_id": item.document_version_id,
            "fallback_chunk": item.chunk_id if not item.source_spans else None,
            "spans": [
                {
                    "node_id": span.node_id,
                    "source_start": span.source_start_char,
                    "source_end": span.source_end_char,
                    "kind": span.span_type.value,
                    "anchor": (
                        span.source_anchor.model_dump(mode="json")
                        if span.source_anchor is not None
                        else None
                    ),
                    "path": span.structural_path,
                }
                for span in item.source_spans
            ],
            "quote_sha256": canonical_sha256(item.citation_text),
        }
    )


class PreparedGenerationPacket(FrozenModel):
    """仅保存 SAFE 身份；实际证据仍由请求持有且不直接公开序列化。"""

    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    packet_id: str = Field(min_length=1)
    packet_revision: str = PREPARED_PACKET_REVISION
    evidence_identity_revision: str = EVIDENCE_IDENTITY_REVISION
    budget_revision: str = GENERATION_BUDGET_REVISION
    schema_revision: str
    evidence_level: Literal[
        "PREPARATION_REJECTED", "TRANSPORT_PREPARED", "TRANSPORT_SENT"
    ]
    alias_to_support_key: tuple[tuple[str, str], ...]
    read_unit_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    read_unit_sha256s: tuple[tuple[str, str], ...] = ()
    per_atom_read_unit_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    support_sources: tuple[dict[str, object], ...] = ()
    per_atom_support_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    protected_support_keys: tuple[str, ...] = ()
    retained_source_units: tuple[tuple[str, tuple[str, ...]], ...] = ()
    retained_table_fact_ids: tuple[str, ...] = ()
    preparation_failure: str | None = None
    original_support_keys: tuple[str, ...]
    removed_support_keys: tuple[tuple[str, str], ...] = ()
    complete_group_ids: tuple[str, ...] = ()
    partial_group_ids: tuple[str, ...] = ()
    node_cell_coverage: Literal["NOT_CERTIFIED"] = "NOT_CERTIFIED"
    answer_coverage: Literal["NOT_CERTIFIED"] = "NOT_CERTIFIED"
    messages_sha256: str
    transport_body_sha256: str | None = None
    estimated_input_tokens: StrictInt = Field(ge=0)
    schema_tokens: StrictInt = Field(default=0, ge=0)
    max_input_tokens: StrictInt = Field(gt=0)
    reserved_output_tokens: StrictInt = Field(ge=0)
    safety_margin_tokens: StrictInt = Field(ge=0)
    observed_prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    estimate_error_tokens: int | None = None

    @property
    def sent_support_ids(self) -> tuple[str, ...]:
        """返回本次包唯一的模型别名集合。

        Args:
            无参数；读取当前包的别名登记表。

        Returns:
            按本次发送证据顺序排列的别名，不跨 attempt 复用。

        """
        return tuple(alias for alias, _ in self.alias_to_support_key)

    @property
    def support_key_to_alias(self) -> dict[str, str]:
        """生成反向映射，禁止用其他 attempt 的 S 编号恢复来源。

        Args:
            无参数；读取当前包的稳定来源身份。

        Returns:
            当前包的稳定来源 key 到模型别名的独立映射。

        """
        return {key: alias for alias, key in self.alias_to_support_key}

    @property
    def sent_read_unit_ids(self) -> tuple[str, ...]:
        """返回本次实际发送的阅读单元短编号。"""
        return tuple(unit_id for unit_id, _keys in self.read_unit_bindings)

    @model_validator(mode="after")
    def _validate_registry(self) -> Self:
        aliases = self.sent_support_ids
        keys = tuple(key for _, key in self.alias_to_support_key)
        if len(set(aliases)) != len(aliases) or len(set(keys)) != len(keys):
            raise ValueError("PREPARED_PACKET_DUPLICATE_IDENTITY")
        read_unit_ids = self.sent_read_unit_ids
        if len(read_unit_ids) != len(set(read_unit_ids)) or any(
            not members or not set(members) <= set(keys)
            for _unit_id, members in self.read_unit_bindings
        ):
            raise ValueError("PREPARED_PACKET_INVALID_READ_UNIT_BINDING")
        digest_ids = [unit_id for unit_id, _digest in self.read_unit_sha256s]
        if self.read_unit_sha256s and (
            len(digest_ids) != len(set(digest_ids))
            or set(digest_ids) != set(read_unit_ids)
        ):
            raise ValueError("PREPARED_PACKET_INVALID_READ_UNIT_DIGEST")
        atom_ids = [atom_id for atom_id, _units in self.per_atom_read_unit_ids]
        if len(atom_ids) != len(set(atom_ids)) or any(
            not set(unit_ids) <= set(read_unit_ids)
            for _atom_id, unit_ids in self.per_atom_read_unit_ids
        ):
            raise ValueError("PREPARED_PACKET_READ_UNIT_OUTSIDE_SENT")
        if any(
            not set(allowed) <= set(aliases)
            for _, allowed in self.per_atom_support_ids
        ):
            raise ValueError("PREPARED_PACKET_ALLOWANCE_OUTSIDE_SENT")
        if not set(self.protected_support_keys) <= set(keys):
            raise ValueError("PREPARED_PACKET_PROTECTED_SUPPORT_MISSING")
        if any(
            not set(members) <= set(self.protected_support_keys)
            for _, members in self.retained_source_units
        ):
            raise ValueError("PREPARED_PACKET_PRIORITY_UNIT_NOT_PROTECTED")
        if self.evidence_level == "PREPARATION_REJECTED" and (
            not self.preparation_failure
            or self.transport_body_sha256 is not None
            or self.observed_prompt_tokens is not None
        ):
            raise ValueError("REJECTED_PREPARATION_CANNOT_CLAIM_TRANSPORT")
        return self
