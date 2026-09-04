"""拒绝产品主路径中的阶段文案与旧 Runtime 回归。"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_UI_ROOTS = (
    _ROOT / "frontend" / "src" / "features",
    _ROOT / "frontend" / "src" / "pages",
    _ROOT / "frontend" / "src" / "components",
)
_FORBIDDEN_COPY = (
    "P10 管理控制台",
    "Knowledge Console",
    "Active Revision",
    "Provider Probe",
    "Evidence V2",
    "Primary LIVE",
    "Standby LIVE",
    "Offline Evaluation V3",
)
_CATALOG_MODEL_IDS = (
    "jina-embeddings-v5-text-small",
    "jina-reranker-v3.5",
    "qwen3.7-text-embedding",
)


def main() -> int:
    """检查 UI 文案、CLI 默认入口和产品环境变量边界。

    Args:
        无参数；读取当前源码树。

    Returns:
        无违例返回 0，否则返回 1。

    """
    violations: list[str] = []
    for root in _UI_ROOTS:
        for path in root.rglob("*.tsx"):
            if path.name.endswith(".test.tsx"):
                continue
            content = path.read_text(encoding="utf-8")
            violations.extend(
                f"{path.relative_to(_ROOT)}: {phrase}"
                for phrase in _FORBIDDEN_COPY
                if phrase in content
            )
            violations.extend(
                f"{path.relative_to(_ROOT)}: 模型 ID 必须来自 Provider Catalog"
                for model_id in _CATALOG_MODEL_IDS
                if model_id in content
            )
    cli = (_ROOT / "src" / "rag_app" / "cli.py").read_text(encoding="utf-8")
    serve_block = cli.split('if arguments.command == "serve":', maxsplit=1)[1]
    serve_block = serve_block.split(
        'if arguments.command == "legacy-serve":', maxsplit=1
    )[0]
    if "build_runtime(" in serve_block:
        violations.append("src/rag_app/cli.py: 默认 serve 调用旧 build_runtime")
    product_api = (_ROOT / "src" / "rag_app" / "api" / "product.py").read_text(
        encoding="utf-8"
    )
    violations.extend(
        "src/rag_app/api/product.py: 模型 ID 必须由 Provider Catalog 校验"
        for model_id in _CATALOG_MODEL_IDS
        if model_id in product_api
    )
    product_runtime = (
        _ROOT / "src" / "rag_app" / "composition" / "product_runtime.py"
    ).read_text(encoding="utf-8")
    old_names = (
        "TEI_ENDPOINT",
        "RAG_RELEASE_REVISION",
        "RAG_PIPELINE_PATH",
        "RAG_RETRIEVAL_PATH",
    )
    violations.extend(
        f"product_runtime.py: 仍要求旧变量 {name}"
        for name in old_names
        if name in product_runtime
    )
    hardcoded_statuses = (
        '"remote_production_profile_ready": True',
        'primary_live_evaluation_status="live_validated"',
        'standby_live_evaluation_status="live_validated"',
        'reranker_live_evaluation_status="live_validated"',
    )
    violations.extend(
        f"product_runtime.py: 生产状态禁止固定为 {value}"
        for value in hardcoded_statuses
        if value in product_runtime
    )
    if violations:
        for violation in violations:
            print(f"FAIL {violation}")
        return 1
    print("OK product hardcode audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
