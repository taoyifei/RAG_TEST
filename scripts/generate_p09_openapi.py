"""显式生成 P09 OpenAPI 快照，供维护者更新合同。"""

from __future__ import annotations

import json
from pathlib import Path

from rag_app.composition.p09_cli import openapi_snapshot

_OUTPUT = Path("docs/public/openapi-v1.json")


def main() -> int:
    """以默认离线 Profile 写出确定性 OpenAPI 快照。

    Args:
        无参数；使用默认离线 Profile。

    Returns:
        写出成功返回 0。

    """
    schema = openapi_snapshot()
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
