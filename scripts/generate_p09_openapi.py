"""显式生成 Product API OpenAPI 快照，供 SDK 与前端复用。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.product.crypto import initialize_master_key
from rag_app.product.provider_runtime import build_offline_mock_transport

_OUTPUT = Path("docs/public/openapi-v1.json")


def _product_openapi_snapshot() -> dict[str, Any]:
    """在隔离目录构造 Product Runtime 并返回 schema。"""
    with tempfile.TemporaryDirectory(prefix="rag-openapi-") as temporary:
        root = Path(temporary)
        frontend = root / "frontend"
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text(
            "<!doctype html><title>知识库工作台</title>", encoding="utf-8"
        )
        bootstrap = root / "bootstrap-token"
        bootstrap.write_text(
            "-".join(("openapi", "bootstrap", "credential")),
            encoding="utf-8",
        )
        bootstrap.chmod(0o600)
        master = root / "master-key"
        initialize_master_key(master)
        settings = ProductRuntimeSettings(
            data_dir=root / "data",
            frontend_dir=frontend,
            bootstrap_token_file=bootstrap,
            master_key_file=master,
        )
        with build_product_runtime(
            settings,
            transport_factory=build_offline_mock_transport,
        ) as runtime:
            return create_product_app(runtime).openapi()


def main() -> int:
    """写出确定性的 Product API OpenAPI 快照。

    Args:
        无参数；使用隔离的 Product Runtime。

    Returns:
        写出成功返回 0。

    """
    schema = _product_openapi_snapshot()
    _OUTPUT.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        raise TypeError("OpenAPI schema 缺少 paths mapping。")
    print(f"wrote {_OUTPUT} paths={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
