"""SQLite 来源版本与 OCR 状态存储。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from rag_app.contracts import allocate_source_id, content_doc_version
from rag_app.state.jobs import JobStore, _utc_now_text
from rag_app.state.models import (
    ActiveSource,
    CollectionStateIdentity,
    MediaReference,
    OcrResult,
    SourceVersion,
    VersionState,
    _require_row,
    _version_from_row,
)
from rag_app.state.schema import SCHEMA

__all__ = ["StateStore"]

_SOURCE_ID_LENGTH = 36
_SHA256_HEX_LENGTH = 64
_PIPELINE_FINGERPRINT_LENGTH = 71


class StateStore(JobStore):
    """用短事务管理任务、来源版本与 OCR 缓存。"""

    @classmethod
    def clone_collection_state(
        cls,
        *,
        source_path: Path,
        target_path: Path,
        identity: CollectionStateIdentity,
        expected_sources: tuple[ActiveSource, ...],
    ) -> StateStore:
        """用 SQLite backup API 建立独立 collection state。

        Args:
            source_path: 当前活动 collection 的只读状态数据库。
            target_path: 新物理 collection 的独立状态数据库。
            identity: 创建 target 的 control job、pipeline 与基线摘要。
            expected_sources: 活动 manifest 声明的完整来源列表。

        Returns:
            已校验完整性和来源列表的 target StateStore。

        Raises:
            FileNotFoundError: 活动状态数据库不存在。
            FileExistsError: target 在原子发布时由其他主体创建。
            ValueError: 数据库、来源列表或 staging 身份不一致。

        """
        _require_identity_values(
            identity.control_job_id,
            identity.pipeline_fingerprint,
            identity.base_manifest_sha256,
        )
        if identity.base_manifest_sha256 is None:
            raise ValueError("增量 clone 必须绑定 base manifest SHA256。")
        if target_path.exists():
            store = cls(target_path)
            store.require_collection_identity(
                control_job_id=identity.control_job_id,
                pipeline_fingerprint=identity.pipeline_fingerprint,
                base_manifest_sha256=identity.base_manifest_sha256,
            )
            return store
        if (
            not source_path.is_file()
            or source_path.is_symlink()
            or target_path.is_symlink()
        ):
            raise FileNotFoundError("活动 collection state 不存在或不安全。")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target_path.parent,
                prefix=f".{target_path.name}.clone-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            _backup_state_database(
                source_path=source_path,
                target_path=temporary_path,
                identity=identity,
                expected_sources=expected_sources,
            )
            os.link(temporary_path, target_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return cls(target_path)

    def bind_collection_identity(
        self,
        *,
        control_job_id: str,
        pipeline_fingerprint: str,
        base_manifest_sha256: str | None,
    ) -> None:
        """幂等绑定物理 collection state 的 staging 身份。

        Args:
            control_job_id: 创建该 state 的 control job。
            pipeline_fingerprint: 该 state 对应的索引 pipeline。
            base_manifest_sha256: 增量基线摘要；full 首发时为 None。

        Returns:
            无返回值。

        Raises:
            ValueError: 现有 state 已绑定其他 job、pipeline 或基线。

        """
        _require_identity_values(
            control_job_id,
            pipeline_fingerprint,
            base_manifest_sha256,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO collection_identity (
                    singleton, control_job_id, pipeline_fingerprint,
                    base_manifest_sha256
                ) VALUES (1, ?, ?, ?)
                """,
                (
                    control_job_id,
                    pipeline_fingerprint,
                    base_manifest_sha256,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_identity WHERE singleton = 1"
            ).fetchone()
        _require_matching_identity(
            _require_row(row),
            control_job_id,
            pipeline_fingerprint,
            base_manifest_sha256,
        )

    def require_collection_identity(
        self,
        *,
        control_job_id: str,
        pipeline_fingerprint: str,
        base_manifest_sha256: str | None,
    ) -> None:
        """要求既有 target state 属于同一 control job 和基线。

        Args:
            control_job_id: 预期 control job。
            pipeline_fingerprint: 预期索引 pipeline。
            base_manifest_sha256: 预期增量基线；full 首发时为 None。

        Returns:
            无返回值。

        Raises:
            ValueError: 身份缺失或任一字段不一致。

        """
        _require_identity_values(
            control_job_id,
            pipeline_fingerprint,
            base_manifest_sha256,
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collection_identity WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ValueError("target collection state 缺少 staging 身份。")
        _require_matching_identity(
            row,
            control_job_id,
            pipeline_fingerprint,
            base_manifest_sha256,
        )

    def stage_source_version(
        self,
        *,
        job_id: str,
        source_path: str,
        content_sha256: str,
        pipeline_fingerprint: str,
        source_id_hint: str | None = None,
    ) -> SourceVersion:
        """创建不影响当前活动版本的 staging 版本。

        Args:
            job_id: 创建该版本的任务。
            source_path: 当前相对路径。
            content_sha256: DOCX 内容摘要。
            pipeline_fingerprint: 解析和索引 pipeline 指纹。
            source_id_hint: full planner 可靠继承的可选来源身份。

        Returns:
            staging 或已存在的幂等版本。

        """
        doc_version = content_doc_version(content_sha256)
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_id = self._resolve_source_id(
                connection,
                source_path,
                content_sha256,
                source_id_hint,
            )
            conflicting = connection.execute(
                """
                SELECT pipeline_fingerprint FROM source_versions
                WHERE source_id = ? AND doc_version = ?
                  AND pipeline_fingerprint <> ?
                LIMIT 1
                """,
                (source_id, doc_version, pipeline_fingerprint),
            ).fetchone()
            if conflicting is not None:
                connection.rollback()
                raise ValueError(
                    "相同内容已属于另一 pipeline；"
                    "必须在新 collection 的独立 staging 状态库全量重建。"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO sources (
                    source_id, current_path, state, updated_at
                ) VALUES (?, ?, 'staging', ?)
                """,
                (source_id, source_path, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_versions (
                    source_id, doc_version, content_sha256, source_path,
                    pipeline_fingerprint, state, job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    doc_version,
                    content_sha256,
                    source_path,
                    pipeline_fingerprint,
                    VersionState.STAGING.value,
                    job_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE source_versions
                SET source_path = ?, pipeline_fingerprint = ?, state = ?,
                    job_id = ?, error_code = NULL, created_at = ?,
                    activated_at = NULL
                WHERE source_id = ? AND doc_version = ?
                  AND state IN (?, ?)
                """,
                (
                    source_path,
                    pipeline_fingerprint,
                    VersionState.STAGING.value,
                    job_id,
                    now,
                    source_id,
                    doc_version,
                    VersionState.RETIRED.value,
                    VersionState.FAILED.value,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM source_versions
                WHERE source_id = ? AND doc_version = ?
                """,
                (source_id, doc_version),
            ).fetchone()
            connection.commit()
        return _version_from_row(_require_row(row))

    def activate_source_version(
        self,
        source_id: str,
        doc_version: str,
    ) -> None:
        """原子激活完整版本并停用旧版本。

        Args:
            source_id: 持久来源标识。
            doc_version: 已完成外部索引写入的内容版本。

        Returns:
            无返回值。

        Raises:
            LookupError: staging 版本不存在。

        """
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM source_versions
                WHERE source_id = ? AND doc_version = ?
                """,
                (source_id, doc_version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("待激活来源版本不存在。")
            if row["state"] == VersionState.ACTIVE.value:
                connection.commit()
                return
            if row["state"] != VersionState.STAGING.value:
                connection.rollback()
                raise LookupError("只有 staging 来源版本可以激活。")
            connection.execute(
                """
                UPDATE source_versions SET state = ?
                WHERE source_id = ? AND state = ?
                """,
                (
                    VersionState.RETIRED.value,
                    source_id,
                    VersionState.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                UPDATE source_versions
                SET state = ?, activated_at = ?
                WHERE source_id = ? AND doc_version = ?
                """,
                (
                    VersionState.ACTIVE.value,
                    now,
                    source_id,
                    doc_version,
                ),
            )
            connection.execute(
                """
                UPDATE sources
                SET current_path = ?, current_content_sha256 = ?,
                    active_doc_version = ?, state = 'active', updated_at = ?
                WHERE source_id = ?
                """,
                (
                    row["source_path"],
                    row["content_sha256"],
                    doc_version,
                    now,
                    source_id,
                ),
            )
            connection.commit()

    def record_staged_chunk_count(
        self,
        source_id: str,
        doc_version: str,
        chunk_count: int,
    ) -> None:
        """记录完整 staging 版本应有的 Qdrant 点数。

        Args:
            source_id: 持久来源标识。
            doc_version: 内容版本。
            chunk_count: 大于零的唯一 chunk 数。

        Returns:
            无返回值。

        Raises:
            ValueError: chunk_count 不为正数。
            LookupError: 版本不处于 staging。

        """
        if chunk_count <= 0:
            raise ValueError("chunk_count 必须为正数。")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE source_versions SET chunk_count = ?
                WHERE source_id = ? AND doc_version = ? AND state = ?
                """,
                (
                    chunk_count,
                    source_id,
                    doc_version,
                    VersionState.STAGING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("待记录点数的 staging 版本不存在。")

    def fail_source_version(
        self,
        source_id: str,
        doc_version: str,
        *,
        error_code: str,
    ) -> None:
        """标记 staging 失败且不触碰旧活动版本。

        Args:
            source_id: 持久来源标识。
            doc_version: 失败的内容版本。
            error_code: 可聚合且不含原文的错误码。

        Returns:
            无返回值。

        """
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE source_versions
                SET state = ?, error_code = ?
                WHERE source_id = ? AND doc_version = ? AND state = ?
                """,
                (
                    VersionState.FAILED.value,
                    error_code,
                    source_id,
                    doc_version,
                    VersionState.STAGING.value,
                ),
            )

    def get_active_source(self, source_id: str) -> ActiveSource | None:
        """读取当前活动来源。

        Args:
            source_id: 持久来源标识。

        Returns:
            活动来源；没有活动版本时返回 None。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_id, current_path, current_content_sha256,
                       active_doc_version
                FROM sources
                WHERE source_id = ? AND state = 'active'
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return ActiveSource(
            source_id=str(row["source_id"]),
            current_path=str(row["current_path"]),
            content_sha256=str(row["current_content_sha256"]),
            doc_version=str(row["active_doc_version"]),
        )

    def get_active_source_by_path(
        self,
        source_path: str,
    ) -> ActiveSource | None:
        """按当前路径读取活动来源。

        Args:
            source_path: manifest 中的当前相对路径。

        Returns:
            活动来源；路径未激活时返回 None。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_id FROM sources
                WHERE current_path = ? AND state = 'active'
                """,
                (source_path,),
            ).fetchone()
        if row is None:
            return None
        return self.get_active_source(str(row["source_id"]))

    def list_active_sources(self) -> tuple[ActiveSource, ...]:
        """列出全部活动来源。

        Args:
            无参数。

        Returns:
            按当前路径排序的活动来源快照。

        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, current_path, current_content_sha256,
                       active_doc_version
                FROM sources
                WHERE state = 'active'
                ORDER BY current_path
                """
            ).fetchall()
        return tuple(
            ActiveSource(
                source_id=str(row["source_id"]),
                current_path=str(row["current_path"]),
                content_sha256=str(row["current_content_sha256"]),
                doc_version=str(row["active_doc_version"]),
            )
            for row in rows
        )

    def mark_source_deleted(self, source_id: str) -> None:
        """在外部索引停用成功后原子记录来源删除。

        Args:
            source_id: 持久来源标识。

        Returns:
            无返回值。

        Raises:
            LookupError: 来源不存在。

        """
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("待删除来源不存在。")
            connection.execute(
                """
                UPDATE source_versions SET state = ?
                WHERE source_id = ? AND state = ?
                """,
                (
                    VersionState.RETIRED.value,
                    source_id,
                    VersionState.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                UPDATE sources
                SET state = 'deleted', active_doc_version = NULL,
                    updated_at = ?
                WHERE source_id = ?
                """,
                (now, source_id),
            )
            connection.commit()

    def get_source_version(
        self,
        source_id: str,
        doc_version: str,
    ) -> SourceVersion:
        """读取指定来源版本。

        Args:
            source_id: 持久来源标识。
            doc_version: 内容版本。

        Returns:
            来源版本快照。

        Raises:
            LookupError: 版本不存在。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM source_versions
                WHERE source_id = ? AND doc_version = ?
                """,
                (source_id, doc_version),
            ).fetchone()
        if row is None:
            raise LookupError("来源版本不存在。")
        return _version_from_row(row)

    def apply_rename_if_unique(
        self,
        *,
        new_path: str,
        content_sha256: str,
    ) -> str | None:
        """仅在活动内容摘要唯一时应用纯重命名。

        Args:
            new_path: 新相对路径。
            content_sha256: 未改变的内容摘要。

        Returns:
            重命名后的 source ID；无法唯一识别时返回 None。

        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT source_id FROM sources
                WHERE state = 'active' AND current_content_sha256 = ?
                """,
                (content_sha256,),
            ).fetchall()
            if len(rows) != 1:
                connection.commit()
                return None
            source_id = str(rows[0]["source_id"])
            connection.execute(
                """
                UPDATE sources SET current_path = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (new_path, _utc_now_text(), source_id),
            )
            connection.commit()
        return source_id

    def record_ocr_result(
        self,
        result: OcrResult,
    ) -> None:
        """按媒体摘要与 revision 幂等保存 OCR 结果。

        Args:
            result: 包含媒体摘要、revision、状态与结果的不可变快照。

        Returns:
            无返回值。

        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ocr_results (
                    media_sha256, ocr_revision, state, text,
                    confidence, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_sha256, ocr_revision) DO UPDATE SET
                    state = excluded.state,
                    text = excluded.text,
                    confidence = excluded.confidence,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    result.media_sha256,
                    result.ocr_revision,
                    result.state,
                    result.text,
                    result.confidence,
                    result.error_code,
                    _utc_now_text(),
                ),
            )

    def record_media_reference(self, reference: MediaReference) -> None:
        """幂等保存一次图片引用及其 OCR 状态。

        Args:
            reference: 带来源版本、稳定元素 ID 和媒体摘要的图片引用。

        Returns:
            无返回值。

        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_references (
                    source_id, doc_version, element_id, media_sha256,
                    media_type, media_name, locator, ocr_revision,
                    state, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    source_id, doc_version, element_id, ocr_revision
                ) DO UPDATE SET
                    media_sha256 = excluded.media_sha256,
                    media_type = excluded.media_type,
                    media_name = excluded.media_name,
                    locator = excluded.locator,
                    state = excluded.state,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    reference.source_id,
                    reference.doc_version,
                    reference.element_id,
                    reference.media_sha256,
                    reference.media_type,
                    reference.media_name,
                    reference.locator,
                    reference.ocr_revision,
                    reference.state,
                    reference.error_code,
                    _utc_now_text(),
                ),
            )

    def count_media_references(
        self,
        *,
        ocr_revision: str,
        state: str | None = None,
        media_type: str | None = None,
    ) -> int:
        """统计一个 OCR revision 下的逐图片引用状态。

        Args:
            ocr_revision: OCR 模型与运行时 revision。
            state: 可选生命周期状态。
            media_type: 可选 MIME 类型。

        Returns:
            满足条件的图片引用数量，不按媒体摘要去重。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM media_references
                WHERE ocr_revision = ?
                  AND (? IS NULL OR state = ?)
                  AND (? IS NULL OR media_type = ?)
                """,
                (
                    ocr_revision,
                    state,
                    state,
                    media_type,
                    media_type,
                ),
            ).fetchone()
        return int(_require_row(row)[0])

    def get_ocr_result(
        self,
        media_sha256: str,
        ocr_revision: str,
    ) -> OcrResult | None:
        """读取精确 OCR revision 的缓存结果。

        Args:
            media_sha256: 原始媒体内容摘要。
            ocr_revision: OCR 模型与运行时 revision。

        Returns:
            命中的 OCR 结果；未命中时返回 None。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ocr_results
                WHERE media_sha256 = ? AND ocr_revision = ?
                """,
                (media_sha256, ocr_revision),
            ).fetchone()
        if row is None:
            return None
        result = OcrResult(
            media_sha256=str(row["media_sha256"]),
            ocr_revision=str(row["ocr_revision"]),
            state=str(row["state"]),
            text=None if row["text"] is None else str(row["text"]),
            confidence=(
                None if row["confidence"] is None else float(row["confidence"])
            ),
            error_code=(
                None if row["error_code"] is None else str(row["error_code"])
            ),
        )
        if (
            result.state == "failed"
            and result.error_code == "OCR_SERVICE_UNAVAILABLE"
        ):
            return None
        return result

    def count_ocr_results(
        self,
        *,
        ocr_revision: str,
        state: str | None = None,
        error_code: str | None = None,
    ) -> int:
        """统计一个 OCR revision 下满足状态条件的唯一媒体。

        Args:
            ocr_revision: OCR 模型与运行时 revision。
            state: 可选的生命周期状态。
            error_code: 可选的不含原文错误码。

        Returns:
            以媒体 SHA256 去重后的结果数。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM ocr_results
                WHERE ocr_revision = ?
                  AND (? IS NULL OR state = ?)
                  AND (? IS NULL OR error_code = ?)
                """,
                (
                    ocr_revision,
                    state,
                    state,
                    error_code,
                    error_code,
                ),
            ).fetchone()
        return int(_require_row(row)[0])

    def _resolve_source_id(
        self,
        connection: sqlite3.Connection,
        source_path: str,
        content_sha256: str,
        source_id_hint: str | None,
    ) -> str:
        """按显式提示、既有路径和唯一内容匹配解析来源身份。

        显式提示具有最高优先级，但不得覆盖其他路径身份。没有提示时优先
        复用当前路径；仅有一个活动来源共享内容摘要时将其视为 rename，
        否则为新来源分配稳定标识。

        Args:
            connection: 当前来源版本事务使用的 SQLite 连接。
            source_path: 待索引来源的稳定路径。
            content_sha256: 待索引内容摘要。
            source_id_hint: 同步计划冻结的来源标识；未提供时为 None。

        Returns:
            可用于 staging 来源版本的稳定来源标识。

        Raises:
            ValueError: 提示格式无效，或与既有路径、来源身份冲突。

        """
        row = connection.execute(
            "SELECT source_id FROM sources WHERE current_path = ?",
            (source_path,),
        ).fetchone()
        if source_id_hint is not None:
            _require_source_id(source_id_hint)
            if row is not None and str(row["source_id"]) != source_id_hint:
                raise ValueError("source ID hint 与现有路径身份冲突。")
            hinted = connection.execute(
                """
                SELECT current_path FROM sources WHERE source_id = ?
                """,
                (source_id_hint,),
            ).fetchone()
            if (
                hinted is not None
                and str(hinted["current_path"]) != source_path
            ):
                raise ValueError("source ID hint 已绑定其他来源路径。")
            return source_id_hint
        if row is not None:
            return str(row["source_id"])
        matching = connection.execute(
            """
            SELECT source_id FROM sources
            WHERE state = 'active' AND current_content_sha256 = ?
            """,
            (content_sha256,),
        ).fetchall()
        if len(matching) == 1:
            return str(matching[0]["source_id"])
        return allocate_source_id(source_path, content_sha256)


def _require_source_id(value: str) -> None:
    if (
        len(value) != _SOURCE_ID_LENGTH
        or not value.startswith("src_")
        or any(character not in "0123456789abcdef" for character in value[4:])
    ):
        raise ValueError("source ID hint 格式无效。")


def _backup_state_database(
    *,
    source_path: Path,
    target_path: Path,
    identity: CollectionStateIdentity,
    expected_sources: tuple[ActiveSource, ...],
) -> None:
    """克隆只读活动状态库，并把副本绑定到新的 staging 身份。

    克隆后会核对活动来源快照和 SQLite 完整性，再替换副本中的 collection
    身份。源数据库始终以只读、query-only 模式打开。

    Args:
        source_path: 活动 collection 的状态数据库。
        target_path: 接收完整副本的临时数据库路径。
        identity: 新 target 应绑定的控制任务、pipeline 与基线。
        expected_sources: 活动 manifest 冻结的来源快照。

    Returns:
        无返回值。

    Raises:
        FileNotFoundError: 活动状态数据库在克隆前消失。
        ValueError: 来源快照不一致或克隆后的数据库完整性校验失败。
        sqlite3.Error: SQLite backup、schema 或身份写入失败。

    """
    encoded = quote(source_path.resolve(strict=True).as_posix(), safe="/:")
    with sqlite3.connect(
        f"file:{encoded}?mode=ro",
        uri=True,
        timeout=10.0,
    ) as source:
        source.execute("PRAGMA query_only=ON")
        with sqlite3.connect(target_path, timeout=10.0) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            target.executescript(SCHEMA)
            actual_sources = _active_sources_from_connection(target)
            if actual_sources != tuple(
                sorted(expected_sources, key=lambda item: item.current_path)
            ):
                raise ValueError(
                    "活动 collection state 来源列表与 manifest 不一致。"
                )
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise ValueError("克隆后的 collection state 完整性校验失败。")
            target.execute("DELETE FROM collection_identity")
            target.execute(
                """
                INSERT INTO collection_identity (
                    singleton, control_job_id, pipeline_fingerprint,
                    base_manifest_sha256
                ) VALUES (1, ?, ?, ?)
                """,
                (
                    identity.control_job_id,
                    identity.pipeline_fingerprint,
                    identity.base_manifest_sha256,
                ),
            )
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")


def _active_sources_from_connection(
    connection: sqlite3.Connection,
) -> tuple[ActiveSource, ...]:
    rows = connection.execute(
        """
        SELECT source_id, current_path, current_content_sha256,
               active_doc_version
        FROM sources
        WHERE state = 'active'
        ORDER BY current_path
        """
    ).fetchall()
    return tuple(
        ActiveSource(
            source_id=str(row["source_id"]),
            current_path=str(row["current_path"]),
            content_sha256=str(row["current_content_sha256"]),
            doc_version=str(row["active_doc_version"]),
        )
        for row in rows
    )


def _require_identity_values(
    control_job_id: str,
    pipeline_fingerprint: str,
    base_manifest_sha256: str | None,
) -> None:
    if not control_job_id:
        raise ValueError("control_job_id 不能为空。")
    if (
        not pipeline_fingerprint.startswith("sha256:")
        or len(pipeline_fingerprint) != _PIPELINE_FINGERPRINT_LENGTH
    ):
        raise ValueError("collection state pipeline 指纹格式无效。")
    if base_manifest_sha256 is not None and (
        len(base_manifest_sha256) != _SHA256_HEX_LENGTH
        or any(
            character not in "0123456789abcdef"
            for character in base_manifest_sha256
        )
    ):
        raise ValueError("base manifest SHA256 格式无效。")


def _require_matching_identity(
    row: sqlite3.Row,
    control_job_id: str,
    pipeline_fingerprint: str,
    base_manifest_sha256: str | None,
) -> None:
    actual_base = (
        None
        if row["base_manifest_sha256"] is None
        else str(row["base_manifest_sha256"])
    )
    if (
        str(row["control_job_id"]) != control_job_id
        or str(row["pipeline_fingerprint"]) != pipeline_fingerprint
        or actual_base != base_manifest_sha256
    ):
        raise ValueError("target collection state staging 身份不一致。")
