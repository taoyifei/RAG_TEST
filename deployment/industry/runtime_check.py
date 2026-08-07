"""控制 Industry full index 并执行不泄露正文的运行时验收。"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OWNER_ONLY_MODE = 0o600
_ROLLBACK_FIELDS = {
    "alias",
    "current_revision",
    "manifest_snapshot",
    "previous_collection",
    "previous_manifest_sha256",
    "previous_pipeline_fingerprint",
    "schema_version",
}


class RuntimeCheckError(RuntimeError):
    """表示 Industry 运行时身份或查询验收失败。"""


def create_full_job(base_url: str, idempotency_key: str, token: str) -> str:
    """通过真实管理 API 幂等创建 full index job。

    Args:
        base_url: Industry app 本机 URL。
        idempotency_key: 绑定 release/corpus 的稳定幂等键。
        token: Industry admin token。

    Returns:
        API 返回的 job ID。

    Raises:
        RuntimeCheckError: HTTP 或响应 schema 无效。

    """
    payload = _request_json(
        f"{base_url.rstrip('/')}/api/index/jobs",
        token=token,
        payload={"idempotency_key": idempotency_key, "kind": "full"},
    )
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeCheckError("创建索引任务未返回 job_id。")
    return job_id


def get_job_state(base_url: str, job_id: str, token: str) -> str:
    """读取索引任务当前状态而不等待。

    Args:
        base_url: Industry app 本机 URL。
        job_id: 已创建的 job ID。
        token: Industry admin token。

    Returns:
        pending、running、succeeded 或 failed。

    Raises:
        RuntimeCheckError: HTTP、任务身份或状态无效。

    """
    payload = _request_json(
        f"{base_url.rstrip('/')}/api/index/jobs/{job_id}",
        token=token,
        method="GET",
    )
    state = payload.get("state")
    if payload.get("job_id") != job_id or state not in {
        "pending",
        "running",
        "succeeded",
        "failed",
    }:
        raise RuntimeCheckError("索引任务身份或状态无效。")
    return str(state)


def wait_job(base_url: str, job_id: str, token: str) -> dict[str, object]:
    """轮询 full index job 直到成功或失败。

    Args:
        base_url: Industry app 本机 URL。
        job_id: 已创建的 job ID。
        token: Industry admin token。

    Returns:
        不含 token 和业务内容的成功任务身份。

    Raises:
        RuntimeCheckError: 超时、失败或响应 schema 无效。

    """
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        payload = _request_json(
            f"{base_url.rstrip('/')}/api/index/jobs/{job_id}",
            token=token,
            method="GET",
        )
        state = payload.get("state")
        if state == "succeeded":
            return {
                "attempt": payload.get("attempt"),
                "job_id": job_id,
                "kind": payload.get("kind"),
                "pipeline_fingerprint": payload.get(
                    "pipeline_fingerprint"
                ),
                "state": state,
            }
        if state == "failed":
            raise RuntimeCheckError(
                f"full index job 失败：{payload.get('error_code')}"
            )
        if state not in {"pending", "running"}:
            raise RuntimeCheckError("索引任务返回未知状态。")
        time.sleep(2)
    raise RuntimeCheckError("full index job 轮询超时。")


def verify_index_state(expected_path: Path) -> dict[str, object]:
    """在 worker 网络与 state mount 中验证 alias、manifest 和点数。

    Args:
        expected_path: release 内 expected-corpus.json。

    Returns:
        不含 source path 正文的索引身份计数。

    Raises:
        RuntimeCheckError: alias、manifest、source exact set 或点数无效。

    """
    from qdrant_client import QdrantClient  # noqa: PLC0415

    from rag_app.manifest import ReadOnlyManifestRepository  # noqa: PLC0415

    expected = _load_object(expected_path)
    active_names = expected.get("active_documents")
    if not isinstance(active_names, list) or any(
        not isinstance(item, str) for item in active_names
    ):
        raise RuntimeCheckError("expected corpus active_documents 无效。")
    active = ReadOnlyManifestRepository(
        Path(os.environ["RAG_MANIFEST_DATABASE"])
    ).get_active()
    if active is None:
        raise RuntimeCheckError("Industry 没有 active manifest。")
    source_paths = {
        source.current_path
        for source in active.manifest.sources
        if source.active
    }
    if source_paths != set(active_names):
        raise RuntimeCheckError("Industry manifest source exact set 不一致。")
    alias_name = os.environ["RAG_QDRANT_ALIAS"]
    if alias_name != "rag-industry-active":
        raise RuntimeCheckError("Industry alias 身份无效。")
    qdrant_credential = os.environ["RAG_QDRANT_API_KEY"]
    client = QdrantClient(
        url=os.environ["RAG_QDRANT_URL"],
        api_key=qdrant_credential,
        timeout=30,
        check_compatibility=False,
    )
    try:
        aliases = client.get_aliases().aliases
        targets = {
            item.collection_name
            for item in aliases
            if item.alias_name == alias_name
        }
        collection = active.manifest.collection_name
        if targets != {collection}:
            raise RuntimeCheckError(
                "Industry alias 与 manifest collection 不一致。"
            )
        collection_info = client.get_collection(collection)
        point_count = collection_info.points_count
    finally:
        client.close()
    if not isinstance(point_count, int) or point_count <= 0:
        raise RuntimeCheckError("Industry collection point count 必须非零。")
    if not active.snapshot_name or not active.snapshot_checksum:
        raise RuntimeCheckError("Industry active manifest 缺少 snapshot 身份。")
    return {
        "active_source_count": len(source_paths),
        "alias": alias_name,
        "collection": active.manifest.collection_name,
        "pipeline_fingerprint": active.manifest.pipeline_fingerprint,
        "point_count": point_count,
        "snapshot_recorded": True,
    }


def capture_index_rollback(snapshot_path: Path) -> dict[str, object]:
    """在 full index 前保存 manifest SQLite 与当前 alias 身份。

    Args:
        snapshot_path: 不存在的 owner-only SQLite 快照绝对路径。

    Returns:
        不含正文和凭据的前一活动索引身份。

    Raises:
        RuntimeCheckError: 路径、SQLite、alias 或 manifest 状态不一致。

    """
    from rag_app.manifest import ReadOnlyManifestRepository  # noqa: PLC0415

    _require_snapshot_destination(snapshot_path)
    database = Path(os.environ["RAG_MANIFEST_DATABASE"])
    if not database.is_file() or database.is_symlink():
        raise RuntimeCheckError("Industry manifest database 不可快照。")
    alias = os.environ["RAG_QDRANT_ALIAS"]
    if alias != "rag-industry-active":
        raise RuntimeCheckError("Industry alias 身份无效。")
    active = ReadOnlyManifestRepository(database).get_active()
    client = _qdrant_client()
    try:
        target = _alias_target(client, alias)
    finally:
        client.close()
    expected_target = (
        None if active is None else active.manifest.collection_name
    )
    if target != expected_target:
        raise RuntimeCheckError("索引前 alias 与 active manifest 不一致。")
    try:
        _sqlite_backup(database, snapshot_path)
        snapshot_path.chmod(0o600)
        _apply_requested_owner(snapshot_path)
    except (OSError, RuntimeCheckError, sqlite3.Error) as error:
        snapshot_path.unlink(missing_ok=True)
        raise RuntimeCheckError(
            "Industry manifest SQLite 快照失败。"
        ) from error
    return {
        "alias": alias,
        "manifest_snapshot": snapshot_path.name,
        "previous_collection": expected_target,
        "previous_manifest_sha256": (
            None if active is None else active.manifest_sha256
        ),
        "previous_pipeline_fingerprint": (
            None if active is None else active.manifest.pipeline_fingerprint
        ),
        "schema_version": "1",
    }


def describe_index_rollback(snapshot_path: Path) -> dict[str, object]:
    """从已完成的 manifest 快照恢复回滚描述。

    用于 worker 已成功、但发布 index report 前中断的确定性续跑。

    Args:
        snapshot_path: 已存在的 owner-only manifest SQLite 快照。

    Returns:
        不含当前 revision 的回滚描述。

    Raises:
        RuntimeCheckError: 快照路径、权限或 SQLite 内容无效。

    """
    from rag_app.manifest import ReadOnlyManifestRepository  # noqa: PLC0415

    if (
        not snapshot_path.is_absolute()
        or snapshot_path.name
        != snapshot_path.as_posix().rsplit("/", maxsplit=1)[-1]
        or snapshot_path.suffix != ".sqlite3"
        or not snapshot_path.is_file()
        or snapshot_path.is_symlink()
        or snapshot_path.stat().st_mode & 0o777 != _OWNER_ONLY_MODE
    ):
        raise RuntimeCheckError("manifest rollback snapshot 无效。")
    active = ReadOnlyManifestRepository(snapshot_path).get_active()
    return {
        "alias": "rag-industry-active",
        "manifest_snapshot": snapshot_path.name,
        "previous_collection": (
            None if active is None else active.manifest.collection_name
        ),
        "previous_manifest_sha256": (
            None if active is None else active.manifest_sha256
        ),
        "previous_pipeline_fingerprint": (
            None if active is None else active.manifest.pipeline_fingerprint
        ),
        "schema_version": "1",
    }


def restore_index_rollback(  # noqa: PLR0915
    descriptor_path: Path,
) -> dict[str, object]:
    """恢复索引前 Industry alias 与 manifest SQLite 状态。

    调用方必须先停止 Industry app，且只能在 Industry worker 网络内执行。

    Args:
        descriptor_path: `run-index.sh` 原子发布的回滚描述文件。

    Returns:
        仅含 alias 与是否恢复物理 collection 的结果。

    Raises:
        RuntimeCheckError: 描述、快照、Qdrant 或原子恢复失败。

    """
    from rag_app.manifest import ReadOnlyManifestRepository  # noqa: PLC0415

    descriptor = _load_object(descriptor_path)
    _validate_rollback_descriptor(descriptor)
    snapshot = descriptor_path.parent / str(descriptor["manifest_snapshot"])
    if not snapshot.is_file() or snapshot.is_symlink():
        raise RuntimeCheckError("manifest rollback snapshot 缺失。")
    database = Path(os.environ["RAG_MANIFEST_DATABASE"])
    if not database.is_file() or database.is_symlink():
        raise RuntimeCheckError("Industry manifest database 不可恢复。")
    alias = str(descriptor["alias"])
    database_owner = (database.stat().st_uid, database.stat().st_gid)
    previous = descriptor["previous_collection"]
    previous_target = previous if isinstance(previous, str) else None
    client = _qdrant_client()
    current_target = _alias_target(client, alias)
    current_backup = _temporary_database(database.parent, ".manifest-current-")
    restore_stage = _temporary_database(database.parent, ".manifest-restore-")
    current_captured = False
    try:
        _sqlite_backup(database, current_backup)
        current_captured = True
        _sqlite_backup(snapshot, restore_stage)
        if previous_target is not None:
            client.get_collection(previous_target)
        _switch_alias(client, alias, previous_target)
        _remove_sqlite_sidecars(database)
        restore_stage.replace(database)
        database.chmod(0o600)
        os.chown(database, *database_owner)
        active = ReadOnlyManifestRepository(database).get_active()
        restored = None if active is None else active.manifest.collection_name
        restored_sha = None if active is None else active.manifest_sha256
        if (
            restored != previous_target
            or restored_sha != descriptor["previous_manifest_sha256"]
            or _alias_target(client, alias) != previous_target
        ):
            raise RuntimeCheckError("回滚后 alias/manifest 身份不一致。")
    except Exception as error:
        if not current_captured:
            raise RuntimeCheckError("INDEX_ROLLBACK_PREPARE_FAILED") from error
        try:
            _switch_alias(client, alias, current_target)
            _remove_sqlite_sidecars(database)
            current_backup.replace(database)
            database.chmod(0o600)
            os.chown(database, *database_owner)
        except Exception as recovery_error:
            raise RuntimeCheckError(
                "INDEX_ROLLBACK_FAILED_AND_RECOVERY_FAILED"
            ) from recovery_error
        if isinstance(error, RuntimeCheckError):
            raise
        raise RuntimeCheckError("INDEX_ROLLBACK_FAILED") from error
    finally:
        client.close()
        current_backup.unlink(missing_ok=True)
        restore_stage.unlink(missing_ok=True)
    return {"alias": alias, "collection_restored": previous_target is not None}


def _qdrant_client() -> QdrantClient:
    from qdrant_client import QdrantClient  # noqa: PLC0415

    qdrant_credential = os.environ["RAG_QDRANT_API_KEY"]
    return QdrantClient(
        url=os.environ["RAG_QDRANT_URL"],
        api_key=qdrant_credential,
        timeout=30,
        check_compatibility=False,
    )


def _alias_target(client: QdrantClient, alias: str) -> str | None:
    targets = {
        item.collection_name
        for item in client.get_aliases().aliases
        if item.alias_name == alias
    }
    if len(targets) > 1:
        raise RuntimeCheckError("Industry alias 指向多个 collection。")
    return next(iter(targets), None)


def _switch_alias(
    client: QdrantClient,
    alias: str,
    target: str | None,
) -> None:
    from qdrant_client.http import models  # noqa: PLC0415

    operations: list[
        models.CreateAliasOperation | models.DeleteAliasOperation
    ] = []
    if _alias_target(client, alias) is not None:
        operations.append(
            models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=alias)
            )
        )
    if target is not None:
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    alias_name=alias,
                    collection_name=target,
                )
            )
        )
    if operations:
        client.update_collection_aliases(operations)


def _require_snapshot_destination(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.name != path.as_posix().rsplit("/", maxsplit=1)[-1]
        or path.suffix != ".sqlite3"
        or path.exists()
        or path.is_symlink()
        or not path.parent.is_dir()
        or path.parent.is_symlink()
    ):
        raise RuntimeCheckError("manifest snapshot 输出路径无效。")


def _validate_rollback_descriptor(value: dict[str, Any]) -> None:
    previous = value.get("previous_collection")
    previous_sha = value.get("previous_manifest_sha256")
    fingerprint = value.get("previous_pipeline_fingerprint")
    snapshot = value.get("manifest_snapshot")
    if (
        set(value) != _ROLLBACK_FIELDS
        or value.get("schema_version") != "1"
        or value.get("alias") != "rag-industry-active"
        or not isinstance(value.get("current_revision"), str)
        or _FULL_REVISION.fullmatch(str(value["current_revision"])) is None
        or not isinstance(snapshot, str)
        or Path(snapshot).name != snapshot
        or not snapshot.endswith(".sqlite3")
        or (previous is not None and not isinstance(previous, str))
        or (previous is None) != (previous_sha is None)
        or (previous is None) != (fingerprint is None)
        or (
            previous_sha is not None
            and (
                not isinstance(previous_sha, str)
                or _SHA256.fullmatch(previous_sha) is None
            )
        )
        or (fingerprint is not None and not isinstance(fingerprint, str))
    ):
        raise RuntimeCheckError("index rollback descriptor 无效。")


def _temporary_database(parent: Path, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=parent,
        prefix=prefix,
        suffix=".sqlite3",
        delete=False,
    ) as temporary:
        path = Path(temporary.name)
    path.chmod(0o600)
    return path


def _sqlite_backup(source: Path, destination: Path) -> None:
    uri = f"{source.resolve(strict=True).as_uri()}?mode=ro"
    with (
        sqlite3.connect(uri, uri=True, timeout=10) as source_connection,
        sqlite3.connect(destination, timeout=10) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _remove_sqlite_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        database.with_name(f"{database.name}{suffix}").unlink(missing_ok=True)


def _apply_requested_owner(path: Path) -> None:
    uid = os.environ.get("RAG_ROLLBACK_OWNER_UID")
    gid = os.environ.get("RAG_ROLLBACK_OWNER_GID")
    if uid is None and gid is None:
        return
    if (
        uid is None
        or gid is None
        or not uid.isdigit()
        or not gid.isdigit()
    ):
        raise RuntimeCheckError("rollback snapshot owner 无效。")
    os.chown(path, int(uid), int(gid))


def verify_smoke(base_url: str, token: str, smoke_path: Path) -> dict[str, int]:
    """执行工业正向与 training 负向隔离查询。

    Args:
        base_url: Industry app 本机 URL。
        token: Industry query token。
        smoke_path: 不含答案正文的 smoke JSONL。

    Returns:
        仅包含通过数与正负用例数的报告。

    Raises:
        RuntimeCheckError: NDJSON、状态或 citation 来源不符合数据集。

    """
    cases = _load_jsonl(smoke_path)
    positive = 0
    negative = 0
    for case in cases:
        case_id = _required_string(case, "id")
        question = _required_string(case, "question")
        expected_outcome = _required_string(case, "expected_outcome")
        response = _chat(
            base_url,
            token,
            conversation_id=(
                f"industry-{case_id}-{secrets.token_hex(8)}"
            ),
            question=question,
        )
        final = [item for item in response if item.get("type") == "final"]
        if len(final) != 1:
            raise RuntimeCheckError("查询流必须恰有一个 final。")
        locators = _claim_locators(final[0])
        expected_patterns = _string_list(case, "expected_source_patterns")
        forbidden_patterns = _string_list(case, "forbidden_source_patterns")
        if any(
            pattern in locator
            for pattern in forbidden_patterns
            for locator in locators
        ):
            raise RuntimeCheckError(
                "Industry citation 命中 training 禁止来源。"
            )
        if expected_outcome == "answered_or_partial":
            positive += 1
            if final[0].get("status") != "answered":
                raise RuntimeCheckError("工业正向问题未 answered。")
            if any(
                not any(pattern in locator for locator in locators)
                for pattern in expected_patterns
            ):
                raise RuntimeCheckError("工业正向问题缺少预期 citation。")
        elif expected_outcome == "not_found_or_refused":
            negative += 1
            if final[0].get("status") != "refused" or locators:
                raise RuntimeCheckError("training 负向问题未安全拒答。")
        else:
            raise RuntimeCheckError("smoke expected_outcome 无效。")
    return {"negative": negative, "passed": len(cases), "positive": positive}


def _chat(
    base_url: str,
    token: str,
    *,
    conversation_id: str,
    question: str,
) -> list[dict[str, Any]]:
    request = urllib.request.Request(  # noqa: S310
        f"{base_url.rstrip('/')}/api/chat",
        data=json.dumps(
            {"conversation_id": conversation_id, "question": question},
            ensure_ascii=False,
        ).encode(),
        headers={
            "Accept": "application/x-ndjson",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=300,
        ) as response:
            lines = response.read().decode().splitlines()
        values = [json.loads(line) for line in lines]
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
    ) as error:
        raise RuntimeCheckError("Industry NDJSON 查询失败。") from error
    if any(not isinstance(item, dict) for item in values):
        raise RuntimeCheckError("Industry NDJSON 事件 schema 无效。")
    return values


def _claim_locators(final: dict[str, Any]) -> tuple[str, ...]:
    claims = final.get("claims")
    if not isinstance(claims, list):
        raise RuntimeCheckError("final claims 不是数组。")
    locators: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(
            claim.get("supports"), list
        ):
            raise RuntimeCheckError("final claim supports schema 无效。")
        for support in claim["supports"]:
            if not isinstance(support, dict) or not isinstance(
                support.get("locator"), str
            ):
                raise RuntimeCheckError("final support locator schema 无效。")
            locators.append(support["locator"])
    return tuple(locators)


def _request_json(
    url: str,
    *,
    token: str,
    method: str = "POST",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=30,
        ) as response:
            value = json.loads(response.read())
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
    ) as error:
        raise RuntimeCheckError("Industry admin API 请求失败。") from error
    if not isinstance(value, dict):
        raise RuntimeCheckError("Industry admin API JSON 顶层无效。")
    return value


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeCheckError("JSON 文件无效。") from error
    if not isinstance(value, dict):
        raise RuntimeCheckError("JSON 顶层必须是对象。")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise RuntimeCheckError("smoke JSONL 行不是对象。")
            values.append(item)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeCheckError("smoke JSONL 无效。") from error
    if not values:
        raise RuntimeCheckError("smoke JSONL 不能为空。")
    return values


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeCheckError(f"smoke {key} 必须是非空字符串。")
    return item


def _string_list(value: dict[str, Any], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or any(
        not isinstance(entry, str) for entry in item
    ):
        raise RuntimeCheckError(f"smoke {key} 必须是字符串数组。")
    return tuple(item)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-job")
    create.add_argument("base_url")
    create.add_argument("idempotency_key")
    wait = commands.add_parser("wait-job")
    wait.add_argument("base_url")
    wait.add_argument("job_id")
    job_state = commands.add_parser("job-state")
    job_state.add_argument("base_url")
    job_state.add_argument("job_id")
    state = commands.add_parser("index-state")
    state.add_argument("expected", type=Path)
    capture = commands.add_parser("capture-index-rollback")
    capture.add_argument("snapshot", type=Path)
    describe = commands.add_parser("describe-index-rollback")
    describe.add_argument("snapshot", type=Path)
    restore = commands.add_parser("restore-index-rollback")
    restore.add_argument("descriptor", type=Path)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("base_url")
    smoke.add_argument("dataset", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行索引 API 控制或运行时验收。

    Returns:
        成功返回 0；异常由调用方获得非零退出码。

    """
    arguments = _arguments()
    runtime_credential = os.environ.get("RAG_RUNTIME_CHECK_TOKEN", "")
    if arguments.command == "create-job":
        print(
            create_full_job(
                arguments.base_url,
                arguments.idempotency_key,
                runtime_credential,
            )
        )
    elif arguments.command == "wait-job":
        print(
            json.dumps(
                wait_job(
                    arguments.base_url,
                    arguments.job_id,
                    runtime_credential,
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "job-state":
        print(
            get_job_state(
                arguments.base_url,
                arguments.job_id,
                runtime_credential,
            )
        )
    elif arguments.command == "index-state":
        print(
            json.dumps(
                verify_index_state(arguments.expected),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "capture-index-rollback":
        print(
            json.dumps(
                capture_index_rollback(arguments.snapshot),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "describe-index-rollback":
        print(
            json.dumps(
                describe_index_rollback(arguments.snapshot),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "restore-index-rollback":
        print(
            json.dumps(
                restore_index_rollback(arguments.descriptor),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            json.dumps(
                verify_smoke(
                    arguments.base_url,
                    runtime_credential,
                    arguments.dataset,
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
