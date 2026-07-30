"""索引 GC 的只读规划、身份复核与 SQLite 文件集安全测试。"""

from __future__ import annotations

import gc
import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from qdrant_client import QdrantClient

from rag_app import cli
from rag_app.index.gc import (
    GarbageCollectionPlan,
    GarbageCollectorConfig,
    IndexGarbageCollector,
)
from rag_app.manifest import (
    ManifestRepository,
    ReadOnlyManifestRepository,
)
from rag_app.state import JobKind, StateStore
from rag_app.state.jobs import ReadOnlyJobStore
from rag_app.state.models import CollectionStateIdentity
from tests.test_index_gc import (
    _client,
    _create_index,
    _create_state,
    _pipeline,
    _state_path,
    _target_name,
)


@dataclass(frozen=True, slots=True)
class _SafetyScenario:
    """单 collection 的真实 Qdrant/SQLite GC 安全场景。"""

    collector: IndexGarbageCollector
    client: QdrantClient
    failed: str
    state_dir: Path


def _safety_scenario(
    tmp_path: Path,
    client: QdrantClient,
) -> _SafetyScenario:
    """创建只含一个 terminal failed collection 的最小场景。

    Args:
        tmp_path: pytest 临时目录。
        client: 模块级共享的真实 Qdrant 客户端。

    Returns:
        可规划 collection/state 删除的真实 GC 场景。

    """
    pipeline = _pipeline()
    fingerprint = pipeline.fingerprint()
    prefix = f"rag-gc-safety-{uuid.uuid4().hex}"
    state_dir = tmp_path / "indexes"
    control_path = tmp_path / "control.sqlite3"
    control = StateStore(control_path)
    control.initialize()
    failed_job = control.create_job(
        idempotency_key=f"gc-safety:{uuid.uuid4().hex}",
        kind=JobKind.FULL,
        pipeline_fingerprint=fingerprint,
    )
    claimed = control.claim_next_job(
        worker_id="gc-safety-worker",
        now=datetime.now(UTC),
        lease_seconds=60,
    )
    assert claimed is not None
    control.finish_job(
        job_id=failed_job.job_id,
        worker_id="gc-safety-worker",
        error_code="TEST_FAILED",
    )
    failed = _target_name(prefix, fingerprint, failed_job.job_id)
    identity = CollectionStateIdentity(
        control_job_id=failed_job.job_id,
        pipeline_fingerprint=fingerprint,
        base_manifest_sha256=None,
    )
    _create_index(
        client,
        collection_name=failed,
        pipeline=pipeline,
        control_job_id=failed_job.job_id,
    )
    _create_state(state_dir, failed, identity)
    manifest_path = tmp_path / "manifests.sqlite3"
    ManifestRepository(manifest_path).initialize()
    collector = IndexGarbageCollector(
        client=client,
        manifests=ReadOnlyManifestRepository(manifest_path),
        control=ReadOnlyJobStore(control_path),
        config=GarbageCollectorConfig(
            alias_name=f"{prefix}-active",
            index_state_dir=state_dir,
            collection_prefix=prefix,
            dense_dimension=pipeline.embedding_dimension,
            pipeline_fingerprint=fingerprint,
            index_revision=pipeline.index_revision,
        ),
    )
    gc.collect()
    return _SafetyScenario(
        collector=collector,
        client=client,
        failed=failed,
        state_dir=state_dir,
    )


def _cleanup_safety(scenario: _SafetyScenario) -> None:
    """删除专用测试 collection。

    Args:
        scenario: 单 collection 安全测试场景。

    """
    if scenario.client.collection_exists(scenario.failed):
        scenario.client.delete_collection(scenario.failed)


@pytest.fixture(scope="module")
def shared_qdrant_client() -> Iterator[QdrantClient]:
    """为本模块共享单个真实 Qdrant HTTP 客户端。

    Yields:
        指向专用本地测试 Qdrant 的客户端。

    """
    client = _client()
    try:
        yield client
    finally:
        client.close()


def _settings(tmp_path: Path) -> SimpleNamespace:
    """构造不会触达真实外部资源的最小 GC 设置。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        具备 GC 入口所需字段的设置对象。

    """
    return SimpleNamespace(
        release_revision="a" * 40,
        pipeline_path=tmp_path / "pipeline.json",
        qdrant_url="http://127.0.0.1:1",
        qdrant_api_key=SecretStr("test-only"),
        qdrant_alias="rag-docx-active",
        state_database=tmp_path / "control.sqlite3",
        manifest_database=tmp_path / "manifest.sqlite3",
        index_state_dir=tmp_path / "indexes",
    )


def _logical_state_paths(main_path: Path) -> tuple[Path, Path, Path]:
    """返回 SQLite 主库、WAL 与 SHM 的逻辑文件集。

    Args:
        main_path: SQLite 主库路径。

    Returns:
        固定顺序的三个逻辑路径。

    """
    return (
        main_path,
        Path(f"{main_path}-wal"),
        Path(f"{main_path}-shm"),
    )


def _file_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    """冻结目录内普通文件的相对路径与内容摘要。

    Args:
        root: 待冻结目录。

    Returns:
        稳定排序的相对路径和 SHA256。

    """
    return tuple(
        (
            path.relative_to(root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _result_statuses(
    plan: GarbageCollectionPlan,
    scenario: _SafetyScenario,
) -> dict[str, str]:
    """执行计划并按 stable ID 返回状态。

    Args:
        plan: 已冻结 GC 计划。
        scenario: 测试场景。

    Returns:
        stable ID 到执行状态的映射。

    """
    return {
        result.stable_id: result.status
        for result in scenario.collector.apply(plan).results
    }


def test_revision_mismatch_fails_before_pipeline_qdrant_or_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 release revision 是 GC 的首个资源前置条件。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 替换工具。

    """
    settings = _settings(tmp_path)
    calls: list[str] = []

    def reject_revision(_: object) -> None:
        calls.append("revision")
        raise ValueError("revision mismatch")

    def unexpected_pipeline(_: Path) -> None:
        calls.append("pipeline")
        raise AssertionError("pipeline must not load")

    monkeypatch.setattr(cli, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "require_release_revision",
        reject_revision,
        raising=False,
    )
    monkeypatch.setattr(cli, "load_pipeline", unexpected_pipeline)

    with pytest.raises(ValueError, match="revision mismatch"):
        cli._run_index_gc(apply=False, collection_prefix="rag-docx")

    assert calls == ["revision"]
    assert not tuple(tmp_path.rglob("*.sqlite3*"))


def test_missing_control_or_manifest_fails_without_creating_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明缺失数据库在 pipeline 和 Qdrant 前失败且零创建。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 替换工具。

    """
    settings = _settings(tmp_path)
    calls: list[str] = []

    def accept_revision(_: object) -> None:
        calls.append("revision")

    def unexpected_pipeline(_: Path) -> None:
        calls.append("pipeline")
        raise AssertionError("pipeline must not load")

    monkeypatch.setattr(cli, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "require_release_revision",
        accept_revision,
        raising=False,
    )
    monkeypatch.setattr(cli, "load_pipeline", unexpected_pipeline)

    with pytest.raises(FileNotFoundError, match="SQLite"):
        cli._run_index_gc(apply=False, collection_prefix="rag-docx")

    assert calls == ["revision"]
    for database in (
        settings.state_database,
        settings.manifest_database,
    ):
        assert not database.exists()
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()


@pytest.mark.parametrize(
    "database_field",
    ("state_database", "manifest_database"),
)
def test_control_and_manifest_database_symlinks_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_field: str,
) -> None:
    """证明 control/manifest 任一路径为 symlink 时资源前置失败。

    Args:
        tmp_path: pytest 临时目录。
        monkeypatch: pytest 替换工具。
        database_field: 替换为 symlink 的设置字段。

    """
    settings = _settings(tmp_path)
    settings.state_database.touch()
    settings.manifest_database.touch()
    unsafe_path = getattr(settings, database_field)
    unsafe_path.unlink()
    real_path = tmp_path / f"{database_field}-real.sqlite3"
    real_path.touch()
    unsafe_path.symlink_to(real_path)
    calls: list[str] = []

    def accept_revision(_: object) -> None:
        calls.append("revision")

    def unexpected_pipeline(_: Path) -> None:
        calls.append("pipeline")
        raise AssertionError("pipeline must not load")

    monkeypatch.setattr(cli, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(cli, "require_release_revision", accept_revision)
    monkeypatch.setattr(cli, "load_pipeline", unexpected_pipeline)

    with pytest.raises(FileNotFoundError, match="普通文件"):
        cli._run_index_gc(apply=False, collection_prefix="rag-docx")

    assert calls == ["revision"]
    assert not Path(f"{unsafe_path}-wal").exists()
    assert not Path(f"{unsafe_path}-shm").exists()


def test_cli_constructs_only_readonly_gc_repositories() -> None:
    """证明 GC CLI 不再把可写 store 注入规划器。"""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    function = source[
        source.index("def _run_index_gc"):
        source.index("def _print_json")
    ]

    assert "ReadOnlyManifestRepository" in function
    assert "ReadOnlyJobStore" in function
    assert "manifests=ManifestRepository(" not in function
    assert "control=StateStore(" not in function


def test_readonly_snapshot_reads_committed_wal_without_source_changes(
    tmp_path: Path,
) -> None:
    """证明隔离只读副本读取 WAL 且不触碰源三件套。

    Args:
        tmp_path: pytest 临时目录。

    """
    database_path = tmp_path / "state.sqlite3"
    state = StateStore(database_path)
    state.initialize()
    state.bind_collection_identity(
        control_job_id="job_original",
        pipeline_fingerprint=_pipeline().fingerprint(),
        base_manifest_sha256=None,
    )
    gc.collect()
    writer = sqlite3.connect(database_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """
            UPDATE collection_identity
            SET control_job_id = 'job_replacement'
            WHERE singleton = 1
            """
        )
        writer.commit()
        wal_path = Path(f"{database_path}-wal")
        assert wal_path.stat().st_size > 0
        before = _file_snapshot(tmp_path)

        identity = ReadOnlyJobStore(database_path).collection_identity()

        assert identity.control_job_id == "job_replacement"
        assert _file_snapshot(tmp_path) == before
    finally:
        writer.close()


def test_dry_run_preserves_database_file_set_and_digests(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明直接规划不创建或修改 control、manifest、state 文件。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    try:
        before = _file_snapshot(tmp_path)

        scenario.collector.plan()

        assert _file_snapshot(tmp_path) == before
    finally:
        _cleanup_safety(scenario)


def test_dry_run_works_with_readonly_local_database_tree(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明 GC 规划不依赖 SQLite 目录或文件可写。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    files = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    directories = tuple(
        sorted(
            (path for path in tmp_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
    )
    try:
        before = _file_snapshot(tmp_path)
        for path in files:
            path.chmod(0o444)
        for path in directories:
            path.chmod(0o555)
        tmp_path.chmod(0o555)

        scenario.collector.plan()

        assert _file_snapshot(tmp_path) == before
    finally:
        tmp_path.chmod(0o755)
        for path in reversed(directories):
            path.chmod(0o755)
        for path in files:
            if path.exists():
                path.chmod(0o644)
        _cleanup_safety(scenario)


def test_apply_deletes_state_main_wal_and_shm_then_is_idempotent(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明 state 三件套在 collection 删除后完整清理。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    try:
        state_paths = _logical_state_paths(
            _state_path(scenario.state_dir, scenario.failed)
        )
        state_paths[1].write_bytes(b"")
        state_paths[2].write_bytes(b"")
        plan = scenario.collector.plan()

        first = _result_statuses(plan, scenario)
        second = _result_statuses(plan, scenario)

        assert first[f"collection:{scenario.failed}"] == "deleted"
        assert first[f"state:{scenario.failed}"] == "deleted"
        assert all(not path.exists() for path in state_paths)
        assert second[f"collection:{scenario.failed}"] == "already_absent"
        assert second[f"state:{scenario.failed}"] == "already_absent"
    finally:
        _cleanup_safety(scenario)


def test_apply_rejects_sidecar_symlink(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明计划后出现的 WAL symlink 不会被当作安全 state 删除。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    try:
        plan = scenario.collector.plan()
        main_path = _state_path(scenario.state_dir, scenario.failed)
        outside = tmp_path / "outside"
        outside.write_text("preserve", encoding="utf-8")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{main_path}{suffix}")
            sidecar.symlink_to(outside)
            statuses = _result_statuses(plan, scenario)
            assert (
                statuses[f"collection:{scenario.failed}"]
                == "unsafe_state"
            )
            assert sidecar.is_symlink()
            sidecar.unlink()
        assert outside.read_text(encoding="utf-8") == "preserve"
        assert main_path.is_file()
    finally:
        _cleanup_safety(scenario)


def test_incomplete_state_deletion_is_not_reported_deleted(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明任一 sidecar 删除失败会保留失败状态。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。
        monkeypatch: pytest 替换工具。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    original_unlink = Path.unlink
    try:
        main_path = _state_path(scenario.state_dir, scenario.failed)
        wal_path = Path(f"{main_path}-wal")
        shm_path = Path(f"{main_path}-shm")
        wal_path.write_bytes(b"")
        shm_path.write_bytes(b"")
        plan = scenario.collector.plan()

        def fail_shm(path: Path, missing_ok: bool = False) -> None:
            if path == shm_path:
                raise PermissionError("test sidecar failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_shm)
        statuses = _result_statuses(plan, scenario)

        assert statuses[f"state:{scenario.failed}"] == "delete_failed"
        assert shm_path.exists()
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)
        _cleanup_safety(scenario)


def test_apply_rejects_same_name_collection_replacement(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明同名 Qdrant collection 换身份后不会按旧计划删除。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    try:
        plan = scenario.collector.plan()
        assert scenario.client.delete_collection(scenario.failed)
        _create_index(
            scenario.client,
            collection_name=scenario.failed,
            pipeline=_pipeline(),
            control_job_id="job_replacement",
        )

        statuses = _result_statuses(plan, scenario)

        assert (
            statuses[f"collection:{scenario.failed}"]
            == "identity_changed"
        )
        assert scenario.client.collection_exists(scenario.failed)
    finally:
        _cleanup_safety(scenario)


def test_apply_rejects_same_name_state_replacement(
    tmp_path: Path,
    shared_qdrant_client: QdrantClient,
) -> None:
    """证明同名 SQLite 换身份后 collection 与 state 都不会删除。

    Args:
        tmp_path: pytest 临时目录。
        shared_qdrant_client: 模块级共享的真实 Qdrant 客户端。

    """
    scenario = _safety_scenario(tmp_path, shared_qdrant_client)
    try:
        plan = scenario.collector.plan()
        main_path = _state_path(scenario.state_dir, scenario.failed)
        main_path.unlink()
        replacement = StateStore(main_path)
        replacement.initialize()
        replacement.bind_collection_identity(
            control_job_id="job_replacement",
            pipeline_fingerprint=_pipeline().fingerprint(),
            base_manifest_sha256=None,
        )

        statuses = _result_statuses(plan, scenario)

        assert (
            statuses[f"collection:{scenario.failed}"]
            == "identity_changed"
        )
        assert statuses[f"state:{scenario.failed}"] == "still_referenced"
        assert scenario.client.collection_exists(scenario.failed)
        assert main_path.is_file()
    finally:
        _cleanup_safety(scenario)
