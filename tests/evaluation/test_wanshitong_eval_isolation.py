"""湾事通评测资料不得进入运行时导入、规则和镜像。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluation.wanshitong.v2.validation import validate_dataset

_ROOT = Path(__file__).resolve().parents[2]


def test_v2_dataset_is_excluded_from_runtime_and_docker_context() -> None:
    loaded = validate_dataset()
    for ignore_name in (".dockerignore", "Dockerfile.dockerignore"):
        ignore_lines = (_ROOT / ignore_name).read_text(encoding="utf-8").splitlines()
        assert "evaluation/wanshitong/**" in ignore_lines

    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "evaluation/wanshitong" not in dockerfile
    assert "COPY ." not in dockerfile

    runtime_text = []
    for path in (_ROOT / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        runtime_text.append(source)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("evaluation.wanshitong")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("evaluation.wanshitong")
    for path in (_ROOT / "frontend" / "src").rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        # 现有公开推荐问题是 UI 文案，不能把它误判为本轮评测注入。
        if path.name != "suggestedQuestions.ts":
            runtime_text.append(source)
        assert not re.search(
            r"\b(?:from|import|require)\b[^;\n]*evaluation/wanshitong",
            source,
        )

    runtime_source = "\n".join(runtime_text)
    for filename, cases in loaded["datasets"].items():
        if filename == "latency-24.ndjson":
            continue
        assert all(case["question"] not in runtime_source for case in cases)
