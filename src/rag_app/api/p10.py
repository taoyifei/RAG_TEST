"""P10 单一正式 React 控制台宿主。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rag_app.api.p09 import create_p09_app
from rag_app.composition.p09_runtime import P09Runtime


def create_p10_app(  # noqa: PLR0913
    runtime: P09Runtime,
    *,
    query_token: str,
    admin_token: str,
    frontend_dir: str | Path,
    debug_enabled: bool = False,
    max_upload_bytes: int = 32 * 1024 * 1024,
) -> FastAPI:
    """创建 P09 API 与 P10 React 静态入口共用的应用。

    Args:
        runtime: P09 Application Services 运行时。
        query_token: Search/Answer Bearer Token。
        admin_token: 管理员 Bearer Token。
        frontend_dir: 已构建且包含 index.html/assets 的目录。
        debug_enabled: 是否允许管理员读取安全检索诊断。
        max_upload_bytes: 单次上传硬上限。

    Returns:
        只保留一个正式 UI 入口的 FastAPI 应用。

    Raises:
        FileNotFoundError: 构建目录或必要入口不存在。

    """
    root = Path(frontend_dir).resolve()
    index = root / "index.html"
    assets = root / "assets"
    if not index.is_file() or not assets.is_dir():
        raise FileNotFoundError("P10 前端构建目录缺少 index.html 或 assets。")
    app = create_p09_app(
        runtime,
        query_token=query_token,
        admin_token=admin_token,
        debug_enabled=debug_enabled,
        max_upload_bytes=max_upload_bytes,
    )
    app.mount("/assets", StaticFiles(directory=assets), name="p10-assets")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @app.get("/{ui_path:path}", include_in_schema=False)
    def _spa_fallback(ui_path: str) -> FileResponse:
        if ui_path in {"live", "ready"} or ui_path.startswith(
            "api/"
        ):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    return app


__all__ = ["create_p10_app"]
