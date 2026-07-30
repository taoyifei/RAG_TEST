"""从活动 Qdrant 索引生成可验证证据清单。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import (
    ChunkIdentity,
    ChunkRole,
    ChunkSourceSpan,
    Locator,
    SourceRecord,
    stable_chunk_id,
    validate_chunk_source_spans,
)
from rag_app.manifest import ManifestState, StoredManifest

__all__ = [
    "ActiveEvidenceExporter",
    "ActiveEvidenceManifest",
    "ActiveEvidenceRecord",
    "load_active_evidence_manifest",
    "write_active_evidence_manifest",
]

_ACTIVE_STATE = "active"
_SCHEMA_VERSION = "2"
_PAYLOAD_SCHEMA_VERSION = "2"


class ActiveEvidenceRecord(BaseModel):
    """活动索引中一条证据的规范化内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{32}$")
    source_path: str = Field(min_length=1)
    doc_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    section_id: str = Field(pattern=r"^section_[0-9a-f]{32}$")
    neighbor_group_id: str = Field(pattern=r"^group_[0-9a-f]{32}$")
    chunk_role: ChunkRole
    locator: str = Field(min_length=1)
    locators: tuple[Locator, ...] = Field(min_length=1)
    source_spans: tuple[ChunkSourceSpan, ...] = Field(min_length=1)
    text: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_content_identity(self) -> Self:
        """验证文本摘要与稳定 chunk ID。

        Args:
            无参数。

        Returns:
            已验证的证据记录。

        Raises:
            ValueError: 文本摘要不一致。

        """
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("活动证据 text 与 content_sha256 不一致。")
        validate_chunk_source_spans(
            self.text,
            self.locators,
            self.source_spans,
        )
        if any(
            locator.file_path != self.source_path
            for locator in self.locators
        ):
            raise ValueError("活动证据 locator 与 source_path 不一致。")
        return self


class ActiveEvidenceManifest(BaseModel):
    """可重算摘要的活动证据传输清单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^2$")
    collection_name: str = Field(min_length=1)
    index_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    point_count: int = Field(ge=0)
    records_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[ActiveEvidenceRecord, ...]

    @classmethod
    def create(
        cls,
        *,
        collection_name: str,
        index_manifest_sha256: str,
        pipeline_fingerprint: str,
        records: tuple[ActiveEvidenceRecord, ...],
    ) -> ActiveEvidenceManifest:
        """由已验证记录创建规范清单。

        Args:
            collection_name: 活动物理 collection。
            index_manifest_sha256: SQLite 活动索引 manifest 摘要。
            pipeline_fingerprint: 活动 pipeline 指纹。
            records: 现场导出的活动证据。

        Returns:
            记录有序且全部摘要已计算的清单。

        """
        ordered = tuple(sorted(records, key=lambda item: item.chunk_id))
        records_sha256 = _records_sha256(ordered)
        manifest_sha256 = _manifest_sha256(
            collection_name=collection_name,
            index_manifest_sha256=index_manifest_sha256,
            pipeline_fingerprint=pipeline_fingerprint,
            point_count=len(ordered),
            records_sha256=records_sha256,
        )
        return cls(
            schema_version=_SCHEMA_VERSION,
            collection_name=collection_name,
            index_manifest_sha256=index_manifest_sha256,
            pipeline_fingerprint=pipeline_fingerprint,
            point_count=len(ordered),
            records_sha256=records_sha256,
            manifest_sha256=manifest_sha256,
            records=ordered,
        )

    @model_validator(mode="after")
    def _validate_canonical_digests(self) -> Self:
        """拒绝重复、乱序、计数或摘要被篡改的清单。

        Args:
            无参数。

        Returns:
            已完成完整性验证的清单。

        Raises:
            ValueError: 清单不规范或任一摘要不匹配。

        """
        identifiers = tuple(record.chunk_id for record in self.records)
        if identifiers != tuple(sorted(identifiers)):
            raise ValueError("活动证据 records 必须按 chunk_id 排序。")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("活动证据清单含重复 chunk_id。")
        if self.point_count != len(self.records):
            raise ValueError("活动证据 point_count 与 records 不一致。")
        records_sha256 = _records_sha256(self.records)
        if self.records_sha256 != records_sha256:
            raise ValueError("活动证据 records_sha256 不一致。")
        manifest_sha256 = _manifest_sha256(
            collection_name=self.collection_name,
            index_manifest_sha256=self.index_manifest_sha256,
            pipeline_fingerprint=self.pipeline_fingerprint,
            point_count=self.point_count,
            records_sha256=self.records_sha256,
        )
        if self.manifest_sha256 != manifest_sha256:
            raise ValueError("活动证据 manifest_sha256 不一致。")
        return self


class _ManifestReader(Protocol):
    def get_active(self) -> StoredManifest | None:
        """读取唯一活动 manifest。

        Args:
            无参数。

        Returns:
            当前活动记录；不存在时返回 None。

        """
        ...


@dataclass(frozen=True, slots=True)
class _LiveState:
    collection_name: str
    manifest_sha256: str
    pipeline_fingerprint: str
    collection_metadata_sha256: str
    exact_active_count: int


class ActiveEvidenceExporter:
    """从活动 alias 和 SQLite manifest 导出可信证据。"""

    def __init__(
        self,
        client: QdrantClient,
        repository: _ManifestReader,
        *,
        alias_name: str,
        page_size: int = 256,
    ) -> None:
        """保存活动索引现场依赖。

        Args:
            client: 目标 Qdrant 客户端。
            repository: 独立 SQLite manifest 仓库。
            alias_name: 查询运行时使用的活动 alias。
            page_size: 单次 scroll 的最大记录数。

        Returns:
            无返回值。

        Raises:
            ValueError: alias 为空或分页大小无效。

        """
        if not alias_name:
            raise ValueError("alias_name 不能为空。")
        if page_size <= 0:
            raise ValueError("page_size 必须为正数。")
        self._client = client
        self._repository = repository
        self._alias_name = alias_name
        self._page_size = page_size

    def export(self) -> ActiveEvidenceManifest:
        """从当前活动现场生成一次不可回灌的证据快照。

        Args:
            无参数。

        Returns:
            已绑定 alias、manifest、pipeline 和来源版本的审计清单。

        Raises:
            LookupError: 没有活动 manifest 或 alias。
            ValueError: 跨存储状态、payload 或计数不一致。

        """
        stored = _active_manifest(self._repository)
        before = self._read_live_state(stored)
        records = self._scroll_records(
            before.collection_name,
            stored,
        )
        after_stored = _active_manifest(self._repository)
        after = self._read_live_state(after_stored)
        if before != after:
            raise ValueError("活动证据扫描前后现场状态发生变化。")
        if before.exact_active_count != len(records):
            raise ValueError("活动证据分页结果与 Qdrant 精确计数不一致。")
        return ActiveEvidenceManifest.create(
            collection_name=before.collection_name,
            index_manifest_sha256=stored.manifest_sha256,
            pipeline_fingerprint=stored.manifest.pipeline_fingerprint,
            records=records,
        )

    def _read_live_state(self, stored: StoredManifest) -> _LiveState:
        """读取并交叉校验当前 alias、collection 与 manifest。

        Args:
            stored: SQLite 中唯一的活动索引 manifest。

        Returns:
            可用于扫描前后并发变更检测的现场状态。

        Raises:
            LookupError: 活动 alias 不存在。
            ValueError: alias、collection 元数据或 pipeline 不一致。

        """
        collection_name = _active_collection(
            self._client,
            self._alias_name,
        )
        if collection_name != stored.manifest.collection_name:
            raise ValueError(
                "活动 alias 与 index manifest collection 不一致。"
            )
        metadata = _collection_metadata(
            self._client,
            collection_name,
        )
        pipeline = metadata.get("pipeline_fingerprint")
        if pipeline != stored.manifest.pipeline_fingerprint:
            raise ValueError(
                "Qdrant collection metadata pipeline 不一致。"
            )
        if (
            metadata.get("payload_schema_version")
            != _PAYLOAD_SCHEMA_VERSION
        ):
            raise ValueError("Qdrant collection payload schema 不是 v2。")
        exact_count = self._client.count(
            collection_name=collection_name,
            count_filter=_active_filter(),
            exact=True,
        ).count
        return _LiveState(
            collection_name=collection_name,
            manifest_sha256=stored.manifest_sha256,
            pipeline_fingerprint=stored.manifest.pipeline_fingerprint,
            collection_metadata_sha256=_canonical_sha256(metadata),
            exact_active_count=exact_count,
        )

    def _scroll_records(
        self,
        collection_name: str,
        stored: StoredManifest,
    ) -> tuple[ActiveEvidenceRecord, ...]:
        """分页读取活动 point 并拒绝重复页或无效证据。

        Args:
            collection_name: 当前活动物理 collection 名称。
            stored: 用于校验证据来源与 pipeline 的活动 manifest。

        Returns:
            按 Qdrant 分页结果收集的已验证证据记录。

        Raises:
            ValueError: point、分页 offset 或证据来源不满足活动契约。

        """
        sources = {
            source.source_id: source
            for source in stored.manifest.sources
            if source.active
        }
        records: list[ActiveEvidenceRecord] = []
        point_ids: set[str] = set()
        offsets: set[str] = set()
        offset: models.ExtendedPointId | None = None
        while True:
            page, next_offset = self._client.scroll(
                collection_name=collection_name,
                scroll_filter=_active_filter(),
                limit=self._page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in page:
                point_id = str(point.id)
                if point_id in point_ids:
                    raise ValueError("Qdrant scroll 返回重复物理 point ID。")
                point_ids.add(point_id)
                records.append(
                    _record_from_payload(
                        point.payload,
                        sources=sources,
                        pipeline_fingerprint=(
                            stored.manifest.pipeline_fingerprint
                        ),
                    )
                )
            if next_offset is None:
                break
            offset_key = str(next_offset)
            if offset_key in offsets:
                raise ValueError("Qdrant scroll offset 重复，拒绝无限分页。")
            offsets.add(offset_key)
            offset = next_offset
        return tuple(records)


def load_active_evidence_manifest(path: Path) -> ActiveEvidenceManifest:
    """读取并重算活动证据清单的传输完整性。

    Args:
        path: UTF-8 JSON 清单路径。

    Returns:
        仅完成自身摘要验证、尚不能用于生产评分的清单。

    """
    return ActiveEvidenceManifest.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def write_active_evidence_manifest(
    evidence: ActiveEvidenceManifest,
    path: Path,
) -> str:
    """原子写出只用于审计留痕的证据清单。

    Args:
        evidence: 当前进程现场扫描产生的证据清单。
        path: 审计清单输出路径。

    Returns:
        实际文件字节的 SHA256。

    """
    payload = (evidence.model_dump_json(indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return hashlib.sha256(payload).hexdigest()


def _record_from_payload(
    payload: dict[str, object] | None,
    *,
    sources: dict[str, SourceRecord],
    pipeline_fingerprint: str,
) -> ActiveEvidenceRecord:
    """把活动 point payload 转换为可验证证据记录。

    Args:
        payload: Qdrant point 携带的原始 payload。
        sources: 当前 manifest 中按 source ID 索引的活动来源。
        pipeline_fingerprint: 当前活动索引的 pipeline 指纹。

    Returns:
        已通过字段、来源、定位和稳定 ID 校验的证据记录。

    Raises:
        ValueError: payload 缺失、格式无效或与活动索引契约不一致。

    """
    if payload is None:
        raise ValueError("活动 Qdrant point 缺少 payload。")
    chunk_id = _required_string(payload, "chunk_id")
    source_id = _required_string(payload, "source_id")
    source_path = _required_string(payload, "source_path")
    doc_version = _required_string(payload, "doc_version")
    section_id = _required_string(payload, "section_id")
    neighbor_group_id = _required_string(payload, "neighbor_group_id")
    raw_chunk_role = _required_string(payload, "chunk_role")
    text = _required_string(payload, "text")
    content_sha256 = _required_string(payload, "content_sha256")
    point_pipeline = _required_string(payload, "pipeline_fingerprint")
    version_state = _required_string(payload, "version_state")
    if version_state != _ACTIVE_STATE:
        raise ValueError("证据导出只允许 active point。")
    if point_pipeline != pipeline_fingerprint:
        raise ValueError("活动 point pipeline 与 index manifest 不一致。")
    raw_locators = payload.get("locators")
    if not isinstance(raw_locators, list) or not raw_locators:
        raise ValueError("活动 point 缺少 locator 列表。")
    required_locator_fields = {"heading_index", "segment_index"}
    if any(
        not isinstance(item, dict)
        or not required_locator_fields.issubset(item)
        for item in raw_locators
    ):
        raise ValueError("活动 point locator 不是当前持久契约。")
    locators = tuple(Locator.model_validate(item) for item in raw_locators)
    if any(locator.file_path != source_path for locator in locators):
        raise ValueError("活动 point locator 与 source_path 不一致。")
    raw_source_spans = payload.get("source_spans")
    if not isinstance(raw_source_spans, list) or not raw_source_spans:
        raise ValueError("活动 point 缺少 source span 列表。")
    source_spans = tuple(
        ChunkSourceSpan.model_validate(item) for item in raw_source_spans
    )
    validate_chunk_source_spans(text, locators, source_spans)
    if any(
        span.locator.file_path != source_path for span in source_spans
    ):
        raise ValueError("活动 point source span 与 source_path 不一致。")
    try:
        chunk_role = ChunkRole(raw_chunk_role)
    except ValueError as error:
        raise ValueError("活动 point chunk_role 无效。") from error
    expected_chunk_id = stable_chunk_id(
        source_id,
        ChunkIdentity(
            section_id=section_id,
            neighbor_group_id=neighbor_group_id,
            chunk_role=chunk_role,
            source_spans=source_spans,
        ),
        text,
    )
    if chunk_id != expected_chunk_id:
        raise ValueError("活动 point chunk_id 与 locator/text 不一致。")
    record = ActiveEvidenceRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        source_path=source_path,
        doc_version=doc_version,
        section_id=section_id,
        neighbor_group_id=neighbor_group_id,
        chunk_role=chunk_role,
        locator=locators[0].display(),
        locators=locators,
        source_spans=source_spans,
        text=text,
        content_sha256=content_sha256,
    )
    _validate_record_source(record, sources)
    return record


def _validate_record_source(
    record: ActiveEvidenceRecord,
    sources: dict[str, SourceRecord],
) -> None:
    source = sources.get(record.source_id)
    if source is None:
        raise ValueError("活动 point source_id 不在当前 index manifest。")
    if source.doc_version != record.doc_version:
        raise ValueError("活动 point doc_version 与 index manifest 不一致。")
    if source.current_path != record.source_path:
        raise ValueError("活动 point source_path 与 index manifest 不一致。")


def _active_manifest(repository: _ManifestReader) -> StoredManifest:
    stored = repository.get_active()
    if stored is None:
        raise LookupError("没有活动 index manifest。")
    if stored.state != ManifestState.ACTIVE:
        raise ValueError("现场读取只接受 ACTIVE index manifest。")
    return stored


def _active_collection(client: QdrantClient, alias_name: str) -> str:
    targets = [
        alias.collection_name
        for alias in client.get_aliases().aliases
        if alias.alias_name == alias_name
    ]
    if not targets:
        raise LookupError("活动 Qdrant alias 不存在。")
    if len(targets) != 1:
        raise ValueError("活动 Qdrant alias 指向多个 collection。")
    return targets[0]


def _collection_metadata(
    client: QdrantClient,
    collection_name: str,
) -> dict[str, object]:
    info = client.get_collection(collection_name)
    metadata = info.config.metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError("Qdrant collection metadata schema 无效。")
    return metadata


def _active_filter() -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="version_state",
                match=models.MatchValue(value=_ACTIVE_STATE),
            )
        ]
    )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"活动 point 缺少字符串字段 {key}。")
    return value


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _records_sha256(records: tuple[ActiveEvidenceRecord, ...]) -> str:
    payload = [record.model_dump(mode="json") for record in records]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _manifest_sha256(
    *,
    collection_name: str,
    index_manifest_sha256: str,
    pipeline_fingerprint: str,
    point_count: int,
    records_sha256: str,
) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "collection_name": collection_name,
        "index_manifest_sha256": index_manifest_sha256,
        "pipeline_fingerprint": pipeline_fingerprint,
        "point_count": point_count,
        "records_sha256": records_sha256,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
