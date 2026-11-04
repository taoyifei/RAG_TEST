"""资料用途、原件健康和版本关系；不复制检索算法或扩大原生权限。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)

DocumentKind = Literal["policy", "illustration", "template"]


def infer_kind(title: str) -> DocumentKind:
    """截图整理文档只标为图示，不因名称含模板就称作可填写文件。"""
    if "模板与图示" in title or "操作图示" in title:
        return "illustration"
    return "policy"


def guide_group(title: str) -> str:
    """识别既有拆分件的共同主题，保持各原件及内容不变。"""
    match = re.match(r"^03(?:0[2-8])_湾区研究院业务招待费", title)
    return "业务招待费报账完整指引" if match else ""


class DocumentCatalogue:
    """只保存已经过 LibraryService 授权的资料关联与本地附件元数据。"""

    def __init__(self, database_path: Path) -> None:
        self._connections = SqliteConnectionFactory(database_path)
        self.asset_root = database_path.parent / "library-assets"
        with self._connections.transaction(write=True) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS library_catalogue ("
                "native_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,"
                "state TEXT NOT NULL DEFAULT 'current',"
                "replaces_id TEXT NOT NULL DEFAULT '',"
                "kind TEXT NOT NULL DEFAULT '',group_name TEXT "
                "NOT NULL DEFAULT '',"
                "created_at TEXT NOT NULL,published_at TEXT,health_json TEXT);"
                "CREATE INDEX IF NOT EXISTS library_catalogue_replaces "
                "ON library_catalogue(replaces_id);"
                "CREATE TABLE IF NOT EXISTS library_assets ("
                "id TEXT PRIMARY KEY,filename TEXT NOT NULL,size "
                "INTEGER NOT NULL,"
                "sha256 TEXT NOT NULL,relation TEXT NOT "
                "NULL,created_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS library_asset_links ("
                "native_id TEXT NOT NULL,asset_id TEXT NOT NULL,"
                "PRIMARY KEY(native_id,asset_id),"
                "FOREIGN KEY(asset_id) REFERENCES library_assets(id));"
            )

    def entry(self, native_id: str) -> dict[str, Any]:
        """未参与版本更新的既有文档默认仍为当前可用版本。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM library_catalogue WHERE native_id=?",
                (native_id,),
            ).fetchone()
        return dict(row) if row else {}

    def searchable(self, native_id: str) -> bool:
        """暂存及已归档版本不得进入新检索。"""
        return bool(self.entry(native_id).get("state", "current") == "current")

    def adopt(self, job_id: str, native_id: str) -> None:
        """个人队列拿到原生 ID 后原子迁移暂存状态。"""
        if job_id == native_id:
            return
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE library_catalogue SET native_id=? WHERE native_id=?",
                (native_id, job_id),
            )

    def pending(self, previous_id: str) -> bool:
        """同一当前版本一次只允许准备一个新版本。"""
        with self._connections.transaction() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM library_catalogue WHERE "
                    "replaces_id=? AND state='staging'",
                    (previous_id,),
                ).fetchone()
                is not None
            )

    def has_exclusions(self, kb_ids: tuple[str, ...]) -> bool:
        """查询所选资料库是否存在暂存/归档记录。"""
        with self._connections.transaction() as connection:
            return any(
                row["kb_id"] in kb_ids
                for row in connection.execute(
                    "SELECT kb_id FROM library_catalogue WHERE state!='current'"
                )
            )

    def stage(self, native_id: str, kb_id: str, replaces_id: str) -> None:
        """新版本先登记为待发布，旧版本保持可检索。"""
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT 1 FROM library_catalogue WHERE replaces_id=? "
                "AND state='staging'",
                (replaces_id,),
            ).fetchone()
            if existing:
                raise ValueError("已有待发布版本，请先处理或删除该版本。")
            connection.execute(
                "INSERT INTO library_catalogue "
                "(native_id,kb_id,state,replaces_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    native_id,
                    kb_id,
                    "staging",
                    replaces_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def publish(self, native_id: str, kb_id: str, previous_id: str) -> None:
        """同一事务切换当前版本，保留旧引用可追溯的原件。"""
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM library_catalogue WHERE native_id=?",
                (native_id,),
            ).fetchone()
            previous = connection.execute(
                "SELECT state FROM library_catalogue WHERE native_id=?",
                (previous_id,),
            ).fetchone()
            if (
                not row
                or row["state"] != "staging"
                or row["replaces_id"] != previous_id
            ):
                raise ValueError("待发布版本状态已变化，请刷新后重试。")
            if previous and previous["state"] != "current":
                raise ValueError("被替换版本已变化，请刷新后重试。")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO "
                "library_catalogue(native_id,kb_id,state,created_at) "
                "VALUES (?,?,'archived',?) ON CONFLICT(native_id) "
                "DO UPDATE SET state='archived'",
                (previous_id, kb_id, now),
            )
            connection.execute(
                "UPDATE library_catalogue SET state='current',published_at=? "
                "WHERE native_id=?",
                (now, native_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO library_asset_links(native_id,asset_id) "
                "SELECT ?,asset_id FROM library_asset_links WHERE native_id=?",
                (native_id, previous_id),
            )

    def annotate(
        self,
        native_id: str,
        kb_id: str,
        *,
        kind: str = "",
        group_name: str = "",
    ) -> None:
        """用途与分组属于资料管理信息，不改变知识内容。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO library_catalogue "
                "(native_id,kb_id,kind,group_name,created_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(native_id) DO UPDATE SET kind=excluded.kind,"
                "group_name=excluded.group_name",
                (
                    native_id,
                    kb_id,
                    kind,
                    group_name,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def set_health(
        self, native_id: str, kb_id: str, health: dict[str, Any]
    ) -> None:
        """保存实际检查时间与结果，未检查的内容不显示为正常。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO library_catalogue "
                "(native_id,kb_id,created_at,health_json) VALUES (?,?,?,?) "
                "ON CONFLICT(native_id) DO UPDATE SET "
                "health_json=excluded.health_json",
                (
                    native_id,
                    kb_id,
                    datetime.now(UTC).isoformat(),
                    json.dumps(health),
                ),
            )

    def assets(self, native_id: str) -> list[dict[str, Any]]:
        """只按已授权的具体资料 ID 读取下载关联。"""
        with self._connections.transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT a.* FROM library_assets a JOIN "
                    "library_asset_links l "
                    "ON a.id=l.asset_id WHERE l.native_id=? ORDER "
                    "BY a.created_at,a.id",
                    (native_id,),
                )
            ]

    def add_asset(
        self,
        native_id: str,
        filename: str,
        content: bytes,
        digest: str,
        relation: str,
    ) -> dict[str, Any]:
        """原始模板单独存放，不重新解析成截图或混入问答索引。"""
        self.asset_root.mkdir(mode=0o700, exist_ok=True)
        asset_id = str(uuid.uuid4())
        path = self.asset_root / asset_id
        try:
            with path.open("xb") as stream:
                path.chmod(0o600)
                stream.write(content)
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO library_assets VALUES (?,?,?,?,?,?)",
                    (
                        asset_id,
                        filename,
                        len(content),
                        digest,
                        relation,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO library_asset_links VALUES (?,?)",
                    (native_id, asset_id),
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return next(
            row for row in self.assets(native_id) if row["id"] == asset_id
        )

    def link_asset(self, native_id: str, asset_id: str) -> None:
        """关联前由服务验证两端资料权限和资料区完全相同。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO library_asset_links VALUES (?,?)",
                (native_id, asset_id),
            )

    def forget(self, native_id: str) -> None:
        """删除资料后移除关联，仅清理再无引用的本地附件。"""
        assets = self.assets(native_id)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM library_asset_links WHERE native_id=?",
                (native_id,),
            )
            connection.execute(
                "DELETE FROM library_catalogue WHERE native_id=?", (native_id,)
            )
            for asset in assets:
                used = connection.execute(
                    "SELECT 1 FROM library_asset_links WHERE asset_id=?",
                    (asset["id"],),
                ).fetchone()
                if not used:
                    connection.execute(
                        "DELETE FROM library_assets WHERE id=?", (asset["id"],)
                    )
                    (self.asset_root / str(uuid.UUID(asset["id"]))).unlink(
                        missing_ok=True
                    )
