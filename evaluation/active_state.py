"""从生产活动状态装配 evaluator 使用的可信证据。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from qdrant_client import QdrantClient

from rag_app.active_evidence import (
    ActiveEvidenceExporter,
    TrustedActiveEvidence,
    write_active_evidence_manifest,
)
from rag_app.manifest import ManifestRepository

__all__ = ["add_active_state_arguments", "load_trusted_active_evidence"]


def add_active_state_arguments(parser: argparse.ArgumentParser) -> None:
    """向命令行加入活动索引现场参数。

    Args:
        parser: evaluator 或 load test 的参数解析器。

    Returns:
        无返回值。

    """
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--qdrant-alias", required=True)
    parser.add_argument("--manifest-database", type=Path, required=True)
    parser.add_argument(
        "--qdrant-api-key-env",
        default="RAG_QDRANT_API_KEY",
        help="只传环境变量名，不在命令行传密钥。",
    )
    parser.add_argument(
        "--active-evidence-output",
        type=Path,
        help="可选：写出本次现场证据清单供审计。",
    )


def load_trusted_active_evidence(
    arguments: argparse.Namespace,
) -> TrustedActiveEvidence:
    """从 alias、SQLite manifest 和 Qdrant 现场生成可信证据。

    Args:
        arguments: 含 `add_active_state_arguments` 所加字段的命令行参数。

    Returns:
        可直接进入 evaluator 或 load test 的可信活动证据。

    Raises:
        ValueError: API key 环境变量为空。

    """
    api_key_name = str(arguments.qdrant_api_key_env)
    api_key = os.environ.get(api_key_name)
    if not api_key:
        raise ValueError(f"环境变量 {api_key_name} 未提供 Qdrant API key。")
    repository = ManifestRepository(arguments.manifest_database)
    repository.initialize()
    client = QdrantClient(
        url=str(arguments.qdrant_url),
        api_key=api_key,
        timeout=30,
        check_compatibility=False,
    )
    evidence = ActiveEvidenceExporter(
        client,
        repository,
        alias_name=str(arguments.qdrant_alias),
    ).export()
    output = arguments.active_evidence_output
    if output is not None:
        write_active_evidence_manifest(evidence, output)
    return evidence
