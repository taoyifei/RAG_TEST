"""在双存储之上生成有界、可复现的 History + Trace 支持包。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Literal

from rag_app.core.errors import NotFound
from rag_app.product.feedback import normalize_trace_id
from rag_app.product.query_history import (
    HistoryExportSnapshot,
    HistorySnapshotLimitError,
    ProductQueryHistory,
)
from rag_app.product.trace_coordinator import (
    OperationalTracePayloadLimitError,
    OperationalTraceSnapshot,
    ProductTraceCoordinator,
)
from rag_app.tracing.store import (
    ArtifactExpiredError,
    ArtifactNotFoundError,
    TraceArtifactLimitError,
)

MAX_HISTORY_TRACE_EXPORT_COUNT = 100
MAX_HISTORY_TRACE_MEMBER_COUNT = 201
MAX_HISTORY_TRACE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_HISTORY_TRACE_ITEM_BYTES = 24 * 1024 * 1024
MAX_HISTORY_TRACE_TOTAL_BYTES = 32 * 1024 * 1024
_SPOOL_MEMORY_BYTES = 2 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_TRACE_ID_PATTERN = re.compile(r"^(?:trace_)?[0-9a-f]{32}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_LimitReason = Literal[
    "EXPORT_MEMBER_LIMIT_EXCEEDED",
    "EXPORT_MEMBER_BYTES_EXCEEDED",
    "EXPORT_ITEM_BYTES_EXCEEDED",
    "EXPORT_TOTAL_BYTES_EXCEEDED",
    "EXPORT_COMPRESSED_BYTES_EXCEEDED",
]


class HistoryTraceExportLimitError(ValueError):
    """支持包命中公开硬上限，且尚未向调用方发送截断内容。"""

    def __init__(
        self,
        reason_code: _LimitReason,
    ) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(slots=True)
class HistoryTraceArchive:
    """由响应消费并在迭代完成或取消时关闭的临时 ZIP。"""

    stream: IO[bytes]
    filename: str
    size_bytes: int
    sha256: str

    def close(self) -> None:
        """幂等释放可能已落盘的临时文件描述符。

        Args:
            无参数；关闭当前归档流。

        Returns:
            无返回值。

        """
        self.stream.close()

    def iter_bytes(self) -> Iterator[bytes]:
        """从头分块读取，并在正常、异常或取消时释放文件描述符。

        Args:
            无参数；读取当前归档流。

        Returns:
            按固定块大小读取归档的迭代器。

        Yields:
            固定上限的连续 ZIP 字节块。

        """
        try:
            self.stream.seek(0)
            while chunk := self.stream.read(_STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            self.close()


@dataclass(frozen=True, slots=True)
class _Member:
    """写入 ZIP 前已完成上限与摘要校验的成员。"""

    path: str
    payload: bytes
    sha256: str
    compressed_bytes: int

    @property
    def bytes(self) -> int:
        """返回成员未压缩字节数。

        Args:
            无参数；读取当前成员。

        Returns:
            成员 payload 的字节数。

        """
        return len(self.payload)


class HistoryTraceExportService:
    """协调 History 快照与兼容 Operational Trace 快照。"""

    def __init__(
        self,
        history: ProductQueryHistory,
        traces: ProductTraceCoordinator,
        *,
        source_revision: str,
    ) -> None:
        if not source_revision:
            raise ValueError("支持包 source_revision 不能为空。")
        self._history = history
        self._traces = traces
        self._source_revision = source_revision

    def export(
        self,
        trace_ids: Sequence[str],
        *,
        include_history_body: bool,
        body_authorized: bool,
        generated_at: datetime | None = None,
    ) -> HistoryTraceArchive:
        """生成完整 ZIP；任何缺失或超限都在返回响应前失败。

        Args:
            trace_ids: 去重前数量为 1 到 100 的新式或旧式 Trace ID。
            include_history_body: 是否显式请求解密后的问答正文。
            body_authorized: 当前主体是否获准导出问答正文。
            generated_at: 可选冻结时钟；相同快照与时钟产生相同字节。

        Returns:
            已写完且通过实际 ZIP 大小校验的临时归档。

        Raises:
            ValueError: ID 数量、格式、重复项或时钟无效。
            NotFound: 任一 ID 在 History、Trace 和 flat events 中都缺失。
            HistoryTraceExportLimitError: 任一公开容量上限被触发。

        """
        requested = tuple(trace_ids)
        _validate_trace_ids(requested)
        frozen_at = generated_at or datetime.now(UTC)
        if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
            raise ValueError("支持包 generated_at 必须带时区。")
        frozen_at = frozen_at.astimezone(UTC)
        canonical_ids = tuple(sorted(requested))

        try:
            with (
                self._traces.export_guard(canonical_ids),
                self._history.export_snapshots(
                    canonical_ids,
                    include_body=include_history_body,
                    body_authorized=body_authorized,
                    now=frozen_at,
                    max_item_bytes=MAX_HISTORY_TRACE_MEMBER_BYTES,
                    max_total_bytes=MAX_HISTORY_TRACE_TOTAL_BYTES,
                ) as histories,
                self._traces.export_snapshots(
                    canonical_ids,
                    max_total_payload_bytes=MAX_HISTORY_TRACE_TOTAL_BYTES,
                ) as operations,
            ):
                missing = [
                    snapshot.trace_id
                    for snapshot in operations
                    if snapshot.state == "missing"
                    and histories[snapshot.trace_id].history_status == "MISSING"
                ]
                if missing:
                    raise NotFound(
                        "部分支持包记录不存在。",
                        stage="history_trace.export",
                        details={"missing_trace_ids": missing},
                    )
                return self._build_archive(
                    canonical_ids,
                    histories=histories,
                    operations=operations,
                    generated_at=frozen_at,
                )
        except HistorySnapshotLimitError as error:
            reason: _LimitReason = (
                "EXPORT_TOTAL_BYTES_EXCEEDED"
                if error.reason_code == "EXPORT_TOTAL_BYTES_EXCEEDED"
                else "EXPORT_MEMBER_BYTES_EXCEEDED"
            )
            raise HistoryTraceExportLimitError(reason) from error
        except OperationalTracePayloadLimitError as error:
            raise HistoryTraceExportLimitError(
                "EXPORT_TOTAL_BYTES_EXCEEDED"
            ) from error
        except TraceArtifactLimitError as error:
            raise HistoryTraceExportLimitError(
                "EXPORT_MEMBER_BYTES_EXCEEDED"
            ) from error
        except (ArtifactExpiredError, ArtifactNotFoundError) as error:
            raise NotFound(
                "支持包所需 Trace Artifact 不存在或已过期。",
                stage="history_trace.export",
            ) from error

    def _build_archive(
        self,
        trace_ids: tuple[str, ...],
        *,
        histories: dict[str, HistoryExportSnapshot],
        operations: tuple[OperationalTraceSnapshot, ...],
        generated_at: datetime,
    ) -> HistoryTraceArchive:
        """先验证成员，再一次写入临时 ZIP 并复核实际压缩大小。"""
        members: list[_Member] = []
        manifest_items: list[dict[str, object]] = []
        operation_by_id = {item.trace_id: item for item in operations}
        for trace_id in trace_ids:
            history = histories[trace_id]
            operation = operation_by_id[trace_id]
            if operation.payload is None:
                raise TypeError("已确认存在的 Trace 快照缺少导出 payload。")
            history_member = _member(
                f"items/{trace_id}/history.json",
                _canonical_json_bytes(history.payload),
            )
            trace_member = _member(
                f"items/{trace_id}/operational-trace.json",
                operation.payload,
            )
            item_bytes = history_member.bytes + trace_member.bytes
            if item_bytes > MAX_HISTORY_TRACE_ITEM_BYTES:
                raise HistoryTraceExportLimitError("EXPORT_ITEM_BYTES_EXCEEDED")
            members.extend((history_member, trace_member))
            manifest_items.append(
                {
                    "trace_id": trace_id,
                    "history_path": history_member.path,
                    "operational_trace_path": trace_member.path,
                    "history_status": history.history_status,
                    "body_included": history.body_included,
                    "body_unavailable_reason": (
                        history.body_unavailable_reason
                    ),
                    "operational_trace_schema": operation.schema_version,
                    "capture_complete": operation.capture_complete,
                    "missing_reason": (
                        operation.missing_reason
                        if operation.state == "history-only"
                        else None
                    ),
                }
            )

        if len(members) + 1 > MAX_HISTORY_TRACE_MEMBER_COUNT:
            raise HistoryTraceExportLimitError("EXPORT_MEMBER_LIMIT_EXCEEDED")
        total_uncompressed = sum(item.bytes for item in members)
        if total_uncompressed > MAX_HISTORY_TRACE_TOTAL_BYTES:
            raise HistoryTraceExportLimitError("EXPORT_TOTAL_BYTES_EXCEEDED")
        total_compressed = sum(item.compressed_bytes for item in members)
        if total_compressed > MAX_HISTORY_TRACE_TOTAL_BYTES:
            raise HistoryTraceExportLimitError(
                "EXPORT_COMPRESSED_BYTES_EXCEEDED"
            )
        manifest = {
            "schema_version": "history-trace-support-v1",
            "generated_at": generated_at.isoformat(),
            "source_revision": self._source_revision,
            "item_count": len(trace_ids),
            "member_count": len(members) + 1,
            "manifest_path": "MANIFEST.json",
            "requested_trace_ids": list(trace_ids),
            "canonical_order": list(trace_ids),
            "canonical_order_rule": "trace_id_ascending",
            "size_accounting": "item_members_excluding_manifest",
            "total_uncompressed_bytes": total_uncompressed,
            "total_compressed_bytes": total_compressed,
            "members": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                    "compressed_bytes": item.compressed_bytes,
                }
                for item in members
            ],
            "items": manifest_items,
        }
        manifest_member = _member(
            "MANIFEST.json", _canonical_json_bytes(manifest)
        )
        ordered_members = (manifest_member, *members)
        # 文件所有权转交给 HistoryTraceArchive.iter_bytes，不能在此提前关闭。
        stream = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=_SPOOL_MEMORY_BYTES,
            mode="w+b",
        )
        try:
            with zipfile.ZipFile(
                stream,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=False,
            ) as archive:
                for member in ordered_members:
                    _write_member(archive, member)
                for member in ordered_members:
                    if (
                        archive.getinfo(member.path).compress_size
                        != member.compressed_bytes
                    ):
                        raise RuntimeError(
                            "ZIP 压缩大小与 canonical 预计算不一致。"
                        )
            stream.seek(0, 2)
            archive_bytes = stream.tell()
            if archive_bytes > MAX_HISTORY_TRACE_TOTAL_BYTES:
                raise HistoryTraceExportLimitError(
                    "EXPORT_COMPRESSED_BYTES_EXCEEDED"
                )
            stream.seek(0)
            digest = hashlib.sha256()
            while chunk := stream.read(_STREAM_CHUNK_BYTES):
                digest.update(chunk)
            filename = (
                f"history-trace-{trace_ids[0]}.zip"
                if len(trace_ids) == 1
                else "history-traces.zip"
            )
            stream.seek(0)
            return HistoryTraceArchive(
                stream=stream,
                filename=filename,
                size_bytes=archive_bytes,
                sha256=digest.hexdigest(),
            )
        except Exception:
            stream.close()
            raise


def _validate_trace_ids(trace_ids: tuple[str, ...]) -> None:
    """在任何路径或 journal 写入前验证数量、重复项和字符集。"""
    if not 1 <= len(trace_ids) <= MAX_HISTORY_TRACE_EXPORT_COUNT:
        raise ValueError("支持包 Trace ID 数量必须在 1 到 100 之间。")
    if len(trace_ids) != len(set(trace_ids)):
        raise ValueError("支持包不能包含重复 Trace ID。")
    if any(_TRACE_ID_PATTERN.fullmatch(value) is None for value in trace_ids):
        raise ValueError("Trace ID 必须是新式或旧式 32 位十六进制。")
    normalized = tuple(normalize_trace_id(value) for value in trace_ids)
    if len(normalized) != len(set(normalized)):
        raise ValueError("支持包不能包含等价的新旧重复 Trace ID。")


def _member(path: str, payload: bytes) -> _Member:
    if len(payload) > MAX_HISTORY_TRACE_MEMBER_BYTES:
        raise HistoryTraceExportLimitError("EXPORT_MEMBER_BYTES_EXCEEDED")
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
    )
    compressed = compressor.compress(payload) + compressor.flush()
    return _Member(
        path=path,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        compressed_bytes=len(compressed),
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_member(archive: zipfile.ZipFile, member: _Member) -> None:
    """固定成员时间、权限、平台和压缩算法。"""
    info = zipfile.ZipInfo(member.path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    info.internal_attr = 0
    archive.writestr(info, member.payload, compresslevel=9)


__all__ = [
    "MAX_HISTORY_TRACE_EXPORT_COUNT",
    "HistoryTraceArchive",
    "HistoryTraceExportLimitError",
    "HistoryTraceExportService",
]
