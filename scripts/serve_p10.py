"""启动 P10 React 控制台与真实离线 P09 服务。"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from rag_app.api.p10 import create_p10_app
from rag_app.composition.p09_runtime import build_p09_runtime


def main() -> None:
    """使用临时 SQLite 数据目录启动 loopback-only 服务。

    Args:
        无参数；从命令行读取 host、port、profile 与数据目录。

    Returns:
        无返回值；服务终止后释放运行时与临时目录。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    frontend_dir = args.frontend_dir or root / "frontend" / "dist"
    profile = args.profile or root / "configs" / "profiles" / "dev-offline.json"
    with tempfile.TemporaryDirectory(prefix="rag-p10-") as temporary:
        data_dir = args.data_dir or Path(temporary)
        with build_p09_runtime(profile, data_dir=data_dir) as runtime:
            app = create_p10_app(
                runtime,
                query_token="query-secret",  # noqa: S106
                admin_token="admin-secret",  # noqa: S106
                frontend_dir=frontend_dir,
                debug_enabled=True,
            )
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level="warning",
            )


if __name__ == "__main__":
    main()
