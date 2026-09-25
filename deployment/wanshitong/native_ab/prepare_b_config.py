"""从固定上游配置生成 B_eval，拒绝意外的上游配置漂移。"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def prepare_config(source: str) -> str:
    """只调整原生配置允许的兜底、采样和输出预算。"""
    replacements = (
        ("  rerank_top_k: 30", "  rerank_top_k: 6"),
        ('  fallback_strategy: "model"', '  fallback_strategy: "fixed"'),
        (
            '  fallback_response: "Sorry, I am unable to answer this '
            'question."',
            '  fallback_response: "根据现有资料无法确定。"',
        ),
        ("    temperature: 0.3", "    temperature: 0"),
        (
            "    max_completion_tokens: 1024",
            "    max_completion_tokens: 2680\n    thinking: false",
        ),
    )
    result = source
    for old, new in replacements:
        if result.count(old) != 1:
            raise ValueError(f"上游配置项出现次数不是 1：{old}")
        result = result.replace(old, new, 1)
    return result


def main() -> None:
    """复制上游配置并输出内容摘要以绑定运行身份。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.destination.exists():
        raise SystemExit("目标配置已存在，拒绝覆盖")
    result = prepare_config(args.source.read_text(encoding="utf-8"))
    args.destination.write_text(result, encoding="utf-8")
    print(hashlib.sha256(result.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
