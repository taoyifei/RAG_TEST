"""只读导出生产公共资料，并将既有索引和文件复制到独立 8289 测试栈。

脚本在 60 服务器运行，只使用标准库、Docker 和容器内 psql。
默认生成清单并校验源文件；只有 --apply 才写入目标文件目录和目标库。
目标公共知识库、目标用户和模型必须先由新版原生服务创建。
"""

from __future__ import annotations

# SQL 表/列使用固定白名单，值经 UUID/int 校验和 _literal 转义；
# COPY 数据由 _copy_value 转义，psql 不支持 DB-API 的占位参数。
# ruff: noqa: S608
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID, uuid4

SOURCE_CONTAINER = "wst_weknora_candidate_20260925-postgres-1"
SOURCE_DATABASE = "wst_weknora_candidate"
SOURCE_USER = "wst_candidate"
SOURCE_KB = "e6e800f1-f27d-41a5-9622-4e3ebebb7e73"
SOURCE_FILES = Path("/srv/wanshitong-data/files")
TARGET_PROJECT = "wxd_v2_083_8289"
TARGET_DATA_ROOT = Path("/srv/wanshitong-data/wxd-v2-test")
TABLES = (
    "knowledge_bases",
    "knowledge_tags",
    "knowledges",
    "chunks",
    "embeddings",
    "resources",
    "resource_bindings",
    "knowledge_tag_relations",
)
INSERT_TABLES = TABLES[1:]
OMIT_COLUMNS = {
    "knowledge_tags": {"seq_id"},
    "chunks": {"seq_id"},
    "embeddings": {"id"},
}
KB_CONFIG_COLUMNS = (
    "description",
    "chunking_config",
    "image_processing_config",
    "extract_config",
    "faq_config",
    "question_generation_config",
    "auto_tag_config",
    "profile_config",
    "generated_profile",
    "indexing_strategy",
)
RESOURCE_REFERENCE = re.compile(
    r"resource://([A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])"
)
LOCAL_REFERENCE = re.compile(
    r"(?:storage://[A-Za-z0-9-]+/)?local://[^\s\"<>\[\]()]+"
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _docker(
    arguments: list[str],
    *,
    input_text: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("docker")
    if binary is None:
        raise RuntimeError("找不到 docker；请在 60 服务器运行。")
    return subprocess.run(  # noqa: S603 - 固定程序和参数列表，不经 shell。
        [binary, *arguments],
        check=True,
        text=True,
        input=input_text,
        stdin=input_stream,
        stdout=subprocess.PIPE if capture_output else output_stream,
        stderr=subprocess.PIPE,
    )


@dataclass(frozen=True)
class Database:
    """容器内数据库连接；生产连接始终强制只读。"""

    container: str
    database: str
    user: str

    def command(self, *, read_only: bool = True) -> list[str]:
        """返回 psql 命令参数，不包含密码或 shell 插值。"""
        arguments = ["exec", "-i"]
        if read_only:
            arguments.extend(
                ["-e", "PGOPTIONS=-c default_transaction_read_only=on"]
            )
        elif self.container == SOURCE_CONTAINER:
            raise ValueError("禁止写入生产数据库。")
        return [
            *arguments,
            self.container,
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self.user,
            "-d",
            self.database,
        ]

    def query(self, sql: str) -> Any:  # noqa: ANN401 - psql 返回异构 JSON。
        """只读执行一个 JSON 查询。"""
        result = _docker(self.command(), input_text=sql, capture_output=True)
        return json.loads(result.stdout) if result.stdout.strip() else None


def _check_target(args: argparse.Namespace) -> Database:
    """核对目标 Compose 身份和文件卷，拒绝复用生产位置。"""
    if not args.target_container.startswith(TARGET_PROJECT + "-"):
        raise ValueError("目标容器必须属于 wxd_v2_083_8289。")
    target = Database(args.target_container, "wxd_v2_test", "wxd_test")
    inspection = json.loads(
        _docker(["inspect", target.container], capture_output=True).stdout
    )[0]
    labels = inspection["Config"].get("Labels") or {}
    if (
        labels.get("com.docker.compose.project") != TARGET_PROJECT
        or labels.get("com.docker.compose.service") != "postgres"
    ):
        raise ValueError("目标不是独立测试项目的 PostgreSQL 容器。")
    target_files = args.target_files.resolve(strict=True)
    expected = TARGET_DATA_ROOT / "files"
    if target_files != expected or target_files == SOURCE_FILES.resolve():
        raise ValueError(f"目标文件目录必须为 {expected}，且不能与生产重叠。")
    volume = json.loads(
        _docker(
            ["volume", "inspect", TARGET_PROJECT + "_native-files"],
            capture_output=True,
        ).stdout
    )[0]
    device = Path((volume.get("Options") or {}).get("device", ""))
    if device.resolve() != target_files:
        raise ValueError("测试 native-files 卷与 --target-files 不一致。")
    UUID(args.target_kb_id)
    UUID(args.target_owner_id)
    if args.source_tenant_id <= 0 or args.target_tenant_id <= 0:
        raise ValueError("租户 ID 必须为正整数。")
    return target


def _export_sql(source_tenant_id: int) -> str:
    """只选公开资料和资料所引用的资源，不读取身份、会话或密钥表。"""
    safe_config = ", ".join("kb." + column for column in KB_CONFIG_COLUMNS)
    unions = [
        "SELECT json_build_object('table','knowledge_bases',"
        "'row',row_to_json(k)) "
        f"FROM (SELECT kb.id, kb.embedding_model_id, kb.storage_backend_id, "
        f"{safe_config} FROM knowledge_bases kb "
        f"WHERE kb.id={_literal(SOURCE_KB)} "
        f"AND kb.tenant_id={source_tenant_id} AND kb.deleted_at IS NULL) k"
    ]
    selections = {
        "knowledge_tags": "SELECT t.* FROM knowledge_tags t WHERE "
        f"t.knowledge_base_id={_literal(SOURCE_KB)} "
        f"AND t.tenant_id={source_tenant_id} AND t.deleted_at IS NULL",
        "knowledges": "SELECT * FROM selected_knowledges",
        "chunks": "SELECT * FROM selected_chunks",
        "embeddings": "SELECT e.* FROM embeddings e JOIN selected_chunks c "
        "ON c.id=e.chunk_id WHERE "
        f"e.knowledge_base_id={_literal(SOURCE_KB)}",
        "resources": "SELECT r.* FROM resources r WHERE "
        f"r.tenant_id={source_tenant_id} AND r.deleted_at IS NULL "
        "AND r.state='active' AND (r.id IN "
        "(SELECT resource_id FROM selected_bindings) OR r.handle IN "
        "(SELECT handle FROM referenced_handles))",
        "resource_bindings": "SELECT * FROM selected_bindings",
        "knowledge_tag_relations": "SELECT r.* FROM knowledge_tag_relations r "
        "JOIN selected_knowledges k ON k.id=r.knowledge_id "
        "JOIN knowledge_tags t ON t.id=r.tag_id WHERE t.deleted_at IS NULL",
    }
    unions.extend(
        f"SELECT json_build_object('table',{_literal(table)},'row',"
        f"row_to_json(t)) FROM ({selection}) t"
        for table, selection in selections.items()
    )
    return (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        "WITH selected_knowledges AS MATERIALIZED ("
        f"SELECT * FROM knowledges WHERE tenant_id={source_tenant_id} "
        f"AND knowledge_base_id={_literal(SOURCE_KB)} AND deleted_at IS NULL), "
        "selected_chunks AS MATERIALIZED (SELECT c.* FROM chunks c "
        "JOIN selected_knowledges k ON k.id=c.knowledge_id "
        "WHERE c.deleted_at IS NULL), "
        "selected_bindings AS MATERIALIZED ("
        "SELECT b.* FROM resource_bindings b "
        "JOIN selected_knowledges k ON k.id=b.owner_id "
        f"WHERE b.owner_type='knowledge' AND b.tenant_id={source_tenant_id}), "
        "referenced_handles AS (SELECT DISTINCT "
        "(regexp_matches(body,'resource://([A-Za-z0-9_-]{22})','g'))[1] handle "
        "FROM (SELECT row_to_json(k)::text body FROM selected_knowledges k "
        "UNION ALL SELECT row_to_json(c)::text "
        "FROM selected_chunks c) text_rows)\n"
        + "\nUNION ALL\n".join(unions)
        + ";\nCOMMIT;\n"
    )


def _load_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {table: [] for table in TABLES}
    with path.open(encoding="utf-8") as snapshot:
        for line in snapshot:
            entry = json.loads(line)
            rows[entry["table"]].append(entry["row"])
    return rows


def _json_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)


def _local_relative(reference: str) -> Path:
    if reference.startswith("storage://"):
        reference = reference.split("/", 3)[3]
    if reference.startswith("local://"):
        relative = Path(reference.removeprefix("local://"))
    elif reference.startswith("/data/files/"):
        relative = Path(reference.removeprefix("/data/files/"))
    else:
        raise ValueError(f"只支持生产本地文件资源：{reference[:120]}")
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("文件资源路径超出本地文件目录。")
    return relative


def _file_manifest(
    rows: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """核对每个原件和图片的引用，只列实际使用的本地文件。"""
    by_handle = {row["handle"]: row for row in rows["resources"]}
    paths: dict[str, str] = {}
    for table in ("knowledges", "chunks"):
        for row in rows[table]:
            for text in _json_strings(row):
                for handle in RESOURCE_REFERENCE.findall(text):
                    if handle not in by_handle:
                        raise ValueError(f"公开资料引用了缺失资源：{handle}")
                for reference in LOCAL_REFERENCE.findall(text):
                    paths[_local_relative(reference).as_posix()] = ""
    for row in rows["resources"]:
        if row["provider"] != "local":
            raise ValueError("公开库存在非 local 资源，不能直接复制文件字节。")
        relative = _local_relative(row["physical_path"])
        paths[relative.as_posix()] = row.get("content_hash") or ""
    files = []
    for name, expected_hash in sorted(paths.items()):
        source = (SOURCE_FILES / name).resolve(strict=True)
        if (
            not source.is_relative_to(SOURCE_FILES.resolve())
            or not source.is_file()
        ):
            raise ValueError("源文件不在生产文件目录中或不是普通文件。")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            expected_hash
            and len(expected_hash) == hashlib.sha256().digest_size * 2
            and digest != expected_hash
        ):
            raise ValueError(f"源文件与资源 SHA256 不一致：{name}")
        files.append(
            {"path": name, "size": source.stat().st_size, "sha256": digest}
        )
    return files


def _rewrite(
    value: Any,  # noqa: ANN401 - 递归保留异构 JSON 的原始类型。
    old_backend: str,
    new_backend: str,
    model_map: Mapping[str, str],
) -> Any:  # noqa: ANN401 - 返回与输入相同的 JSON 数据类型。
    if isinstance(value, str):
        rewritten = value.replace(
            f"storage://{old_backend}/", f"storage://{new_backend}/"
        )
        return model_map.get(rewritten, rewritten)
    if isinstance(value, dict):
        return {
            key: _rewrite(child, old_backend, new_backend, model_map)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite(child, old_backend, new_backend, model_map)
            for child in value
        ]
    return value


def _prepare_rows(
    rows: dict[str, list[dict[str, Any]]],
    args: argparse.Namespace,
    target_kb: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    source_kb = rows["knowledge_bases"][0]
    old_backend = source_kb["storage_backend_id"]
    new_backend = target_kb["storage_backend_id"]
    if not old_backend or not new_backend:
        raise ValueError("源和目标知识库必须绑定有效 local storage backend。")
    model_map: dict[str, str] = json.loads(args.model_map_json)
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in model_map.items()
    ):
        raise ValueError("model-map-json 必须是模型 ID 的字符串映射。")
    expected_model = model_map.get(
        source_kb["embedding_model_id"], source_kb["embedding_model_id"]
    )
    if target_kb["embedding_model_id"] != expected_model:
        raise ValueError(
            "目标 embedding 模型 ID 不一致，禁止复用不匹配的向量。"
        )
    result = {table: [] for table in TABLES}
    for table, source_rows in rows.items():
        for source in source_rows:
            row = _rewrite(source, old_backend, new_backend, model_map)
            if "tenant_id" in row:
                row["tenant_id"] = args.target_tenant_id
            if "knowledge_base_id" in row:
                row["knowledge_base_id"] = args.target_kb_id
            if table == "resources":
                row["storage_backend_id"] = new_backend
                row["location_hash"] = hashlib.sha256(
                    row["physical_path"].encode("utf-8")
                ).hexdigest()
            if table == "chunks" and row.get("last_editor_id"):
                row["last_editor_id"] = args.target_owner_id
            result[table].append(row)
    return result


def _copy_files(files: list[dict[str, Any]], target_files: Path) -> None:
    """按相对路径复制并校验，不覆盖不同内容，不修改生产文件。"""
    for item in files:
        destination = target_files / item["path"]
        if not destination.resolve().is_relative_to(target_files):
            raise ValueError("目标文件路径越界。")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_name(
                f".{destination.name}.{uuid4().hex}.migrating"
            )
            try:
                with (
                    (SOURCE_FILES / item["path"]).open("rb") as source,
                    partial.open("xb") as target,
                ):
                    shutil.copyfileobj(source, target)
                with partial.open("rb") as copied:
                    if (
                        hashlib.file_digest(copied, "sha256").hexdigest()
                        != item["sha256"]
                    ):
                        raise ValueError(f"复制中文件发生变化：{item['path']}")
                partial.chmod(0o644)
                destination.hardlink_to(partial)
            finally:
                partial.unlink(missing_ok=True)
        with destination.open("rb") as target:
            digest = hashlib.file_digest(target, "sha256").hexdigest()
        if (
            digest != item["sha256"]
            or destination.stat().st_size != item["size"]
        ):
            raise ValueError(
                f"目标文件校验失败，不继续写数据库：{item['path']}"
            )


def _copy_value(value: object) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _fix_target_file_ownership() -> None:
    """只在固定测试 app 的独立文件卷上恢复运行用户的写权限。"""
    container = TARGET_PROJECT + "-app-1"
    inspection = json.loads(
        _docker(["inspect", container], capture_output=True).stdout
    )[0]
    labels = inspection["Config"].get("Labels") or {}
    if (
        inspection["Name"] != "/" + container
        or labels.get("com.docker.compose.project") != TARGET_PROJECT
        or labels.get("com.docker.compose.service") != "app"
    ):
        raise ValueError("文件权限修复只允许在独立测试 app 上执行。")
    files_mount = next(
        (
            mount
            for mount in inspection.get("Mounts", [])
            if mount.get("Destination") == "/data/files"
        ),
        None,
    )
    if (
        files_mount is None
        or files_mount.get("Type") != "volume"
        or files_mount.get("Name") != TARGET_PROJECT + "_native-files"
        or files_mount.get("RW") is not True
    ):
        raise ValueError("测试 app 的文件挂载与独立 native-files 卷不匹配。")
    _docker(
        [
            "exec",
            "-u",
            "0",
            container,
            "chown",
            "-R",
            "appuser:appuser",
            "/data/files",
        ],
        capture_output=True,
    )


def _write_import(
    stream: TextIO,
    rows: dict[str, list[dict[str, Any]]],
    schema: Mapping[str, list[str]],
    args: argparse.Namespace,
) -> None:
    """显式列导入；新版本新增列使用数据库默认值。"""
    stream.write("BEGIN;\nSET LOCAL standard_conforming_strings=on;\n")
    stream.write(
        "DO $wxd$ BEGIN IF NOT EXISTS (SELECT 1 FROM knowledge_bases WHERE "
        f"id={_literal(args.target_kb_id)} "
        f"AND tenant_id={args.target_tenant_id} AND deleted_at IS NULL) THEN "
        "RAISE EXCEPTION '目标知识库不属于指定测试租户'; END IF; "
        "IF EXISTS (SELECT 1 FROM knowledges WHERE "
        f"knowledge_base_id={_literal(args.target_kb_id)}) THEN "
        "RAISE EXCEPTION '目标公共库非空，停止迁移'; END IF; END $wxd$;\n"
    )
    for table in INSERT_TABLES:
        table_rows = rows[table]
        if not table_rows:
            continue
        columns = [
            column
            for column in schema[table]
            if column in table_rows[0]
            and column not in OMIT_COLUMNS.get(table, set())
        ]
        stream.write(f"COPY {table} ({','.join(columns)}) FROM STDIN;\n")
        for row in table_rows:
            stream.write(
                "\t".join(_copy_value(row[column]) for column in columns) + "\n"
            )
        stream.write("\\.\n")
    configuration = rows["knowledge_bases"][0]
    assignments = []
    for column in KB_CONFIG_COLUMNS:
        if (
            column not in schema["knowledge_bases"]
            or column not in configuration
        ):
            continue
        value = configuration[column]
        text = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else value
        )
        assignments.append(
            f"{column}=" + ("NULL" if text is None else _literal(str(text)))
        )
    assignments.append(f"creator_id={_literal(args.target_owner_id)}")
    stream.write(
        "UPDATE knowledge_bases SET "
        + ",".join(assignments)
        + f" WHERE id={_literal(args.target_kb_id)} "
        f"AND tenant_id={args.target_tenant_id};\n"
    )
    stream.write(
        "UPDATE tenants SET storage_used=(SELECT COALESCE(SUM(storage_size),0) "
        f"FROM knowledges WHERE tenant_id={args.target_tenant_id} "
        "AND deleted_at IS NULL) "
        f"WHERE id={args.target_tenant_id};\nCOMMIT;\n"
    )


def _counts_sql(
    rows: Mapping[str, list[dict[str, Any]]], target_kb_id: str
) -> str:
    """在目标库中按本批文档和资源 ID 核对全部迁移表的数量。"""
    kb_filter = f"knowledge_base_id={_literal(target_kb_id)}"
    conditions = dict.fromkeys(
        ("knowledge_tags", "knowledges", "chunks", "embeddings"), kb_filter
    )
    for table in ("resources", "resource_bindings"):
        ids = ",".join(_literal(row["id"]) for row in rows[table])
        conditions[table] = f"id IN ({ids})" if ids else "FALSE"
    document_ids = ",".join(_literal(row["id"]) for row in rows["knowledges"])
    conditions["knowledge_tag_relations"] = (
        f"knowledge_id IN ({document_ids})" if document_ids else "FALSE"
    )
    pairs = [
        f"{_literal(table)},(SELECT COUNT(*) FROM {table} WHERE {condition})"
        for table, condition in conditions.items()
    ]
    return "SELECT json_build_object(" + ",".join(pairs) + ");"


def _arguments() -> argparse.Namespace:
    """只接受本次公共库迁移需要的目标参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tenant-id", type=int, required=True)
    parser.add_argument("--target-tenant-id", type=int, required=True)
    parser.add_argument("--target-kb-id", required=True)
    parser.add_argument("--target-owner-id", required=True)
    parser.add_argument("--target-container", required=True)
    parser.add_argument("--target-files", type=Path, required=True)
    parser.add_argument("--expected-documents", type=int, default=99)
    parser.add_argument("--model-map-json", default="{}")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    """先导出和校验，再按显式参数写入独立测试项目。"""
    args = _arguments()
    target = _check_target(args)
    target_info = target.query(
        "SELECT row_to_json(kb) FROM knowledge_bases kb WHERE "
        f"id={_literal(args.target_kb_id)} "
        f"AND tenant_id={args.target_tenant_id} "
        "AND deleted_at IS NULL;"
    )
    if target_info is None:
        raise ValueError("目标公共知识库不存在。")
    owner = target.query(
        "SELECT COUNT(*) FROM users WHERE "
        f"id={_literal(args.target_owner_id)} "
        f"AND tenant_id={args.target_tenant_id} "
        "AND deleted_at IS NULL;"
    )
    if owner != 1:
        raise ValueError("目标知识库所有者不是目标租户中的有效用户。")
    existing = target.query(
        "SELECT COUNT(*) FROM knowledges WHERE "
        f"knowledge_base_id={_literal(args.target_kb_id)};"
    )
    if existing:
        raise ValueError("目标公共库非空，停止迁移以保留已有资料。")
    output = TARGET_DATA_ROOT / "public-migration"
    output.mkdir(mode=0o700, exist_ok=True)
    snapshot = output / "snapshot.ndjson"
    source = Database(SOURCE_CONTAINER, SOURCE_DATABASE, SOURCE_USER)
    with snapshot.open("w", encoding="utf-8") as stream:
        _docker(
            source.command(),
            input_text=_export_sql(args.source_tenant_id),
            output_stream=stream,
        )
    rows = _load_snapshot(snapshot)
    if (
        len(rows["knowledge_bases"]) != 1
        or len(rows["knowledges"]) != args.expected_documents
    ):
        raise ValueError("源公共知识库或文档数量与确认清单不一致，停止迁移。")
    if any(row["parse_status"] != "completed" for row in rows["knowledges"]):
        raise ValueError("源资料仍在解析或失败，不能直接复制索引。")
    files = _file_manifest(rows)
    prepared = _prepare_rows(rows, args, target_info)
    names = ",".join(_literal(table) for table in TABLES)
    schema = target.query(
        "SELECT json_object_agg(table_name,columns) FROM "
        "(SELECT table_name,"
        "json_agg(column_name ORDER BY ordinal_position) columns "
        "FROM information_schema.columns WHERE table_schema='public' "
        f"AND table_name IN ({names}) GROUP BY table_name) t;"
    )
    manifest = {
        "source_kb": SOURCE_KB,
        "target_kb": args.target_kb_id,
        "source_tenant": args.source_tenant_id,
        "target_tenant": args.target_tenant_id,
        "counts": {table: len(values) for table, values in rows.items()},
        "files": files,
        "applied": False,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sql_path = output / "import.sql"
    with sql_path.open("w", encoding="utf-8") as stream:
        _write_import(stream, prepared, schema, args)
    print(
        json.dumps(
            {
                "counts": manifest["counts"],
                "files": len(files),
                "manifest": str(manifest_path),
                "apply": args.apply,
            },
            ensure_ascii=False,
        )
    )
    if not args.apply:
        return
    _copy_files(files, args.target_files.resolve())
    _fix_target_file_ownership()
    with sql_path.open(encoding="utf-8") as stream:
        _docker(target.command(read_only=False), input_stream=stream)
    actual = target.query(_counts_sql(prepared, args.target_kb_id))
    for table, count in actual.items():
        if count != manifest["counts"][table]:
            raise ValueError(f"导入后 {table} 数量不一致。")
    manifest["applied"] = True
    manifest["verified_counts"] = actual
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": "imported", "counts": actual, "files": len(files)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
