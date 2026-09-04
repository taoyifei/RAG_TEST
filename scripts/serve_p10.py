"""启动只使用 MockTransport 的 P10.5 Product Runtime。"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import uvicorn

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.product.crypto import initialize_master_key
from rag_app.product.provider_runtime import build_offline_mock_transport


def main() -> None:
    """使用临时 SQLite 和合成 Provider 响应启动 loopback 服务。

    Args:
        无参数；从命令行读取 host、port、前端与数据目录。

    Returns:
        无返回值；服务终止后释放全部运行时资源。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--profile", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    frontend_dir = args.frontend_dir or root / "frontend" / "dist"
    with tempfile.TemporaryDirectory(prefix="rag-product-") as temporary:
        temporary_root = Path(temporary)
        data_dir = args.data_dir or temporary_root / "data"
        bootstrap = temporary_root / "bootstrap-token"
        bootstrap.write_text(
            os.environ.get(
                "RAG_E2E_BOOTSTRAP_TOKEN",
                "-".join(("offline", "bootstrap", "credential")),
            ),
            encoding="utf-8",
        )
        bootstrap.chmod(0o600)
        master_key = temporary_root / "master-key"
        initialize_master_key(master_key)
        settings = ProductRuntimeSettings(
            data_dir=data_dir,
            frontend_dir=frontend_dir,
            bootstrap_token_file=bootstrap,
            master_key_file=master_key,
            compatibility_manifest=root / "compatibility-manifest.json",
            host=args.host,
            port=args.port,
            qdrant_mode="memory",
            debug_enabled=True,
            trusted_origins=(f"http://{args.host}:{args.port}",),
        )
        with build_product_runtime(
            settings,
            transport_factory=build_offline_mock_transport,
        ) as runtime:
            uvicorn.run(
                create_product_app(runtime),
                host=args.host,
                port=args.port,
                log_level="warning",
            )


if __name__ == "__main__":
    main()
