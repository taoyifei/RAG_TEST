"""离线构造只读、不可覆盖的模型部署清单。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.verify_model_contracts import (  # noqa: E402
    DeploymentManifestV2Spec,
    ServiceName,
    build_deployment_manifest_v2,
)

_SERVICES = ("embedding", "reranker", "llm")


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=_SERVICES)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--runtime-name", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--score-min", type=float)
    parser.add_argument("--score-max", type=float)
    parser.add_argument("--quantization")
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument("--chat-template-sha256")
    return parser.parse_args(argv)


def _service_contract(arguments: argparse.Namespace) -> dict[str, object]:
    service = arguments.service
    if service == "embedding":
        if arguments.dimension is None or _has_non_embedding_fields(arguments):
            raise ValueError("embedding 只能且必须提供 --dimension。")
        return {"dimension": arguments.dimension}
    if service == "reranker":
        if (
            arguments.score_min is None
            or arguments.score_max is None
            or arguments.dimension is not None
            or _has_llm_fields(arguments)
        ):
            raise ValueError(
                "reranker 只能且必须提供 --score-min/--score-max。"
            )
        return {
            "score_min": arguments.score_min,
            "score_max": arguments.score_max,
        }
    if (
        arguments.quantization is None
        or arguments.max_context_tokens is None
        or arguments.chat_template_sha256 is None
        or arguments.dimension is not None
        or arguments.score_min is not None
        or arguments.score_max is not None
    ):
        raise ValueError("llm 必须且只能提供完整的 LLM service contract。")
    return {
        "quantization": arguments.quantization,
        "max_context_tokens": arguments.max_context_tokens,
        "chat_template_sha256": arguments.chat_template_sha256,
    }


def _has_non_embedding_fields(arguments: argparse.Namespace) -> bool:
    return (
        arguments.score_min is not None
        or arguments.score_max is not None
        or _has_llm_fields(arguments)
    )


def _has_llm_fields(arguments: argparse.Namespace) -> bool:
    return (
        arguments.quantization is not None
        or arguments.max_context_tokens is not None
        or arguments.chat_template_sha256 is not None
    )


def _write_atomic_read_only(
    output: Path,
    payload: dict[str, object],
) -> None:
    parent = output.parent
    absolute_parent = parent.absolute()
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("输出父目录不存在或不可解析。") from error
    if (
        not parent.is_dir()
        or parent.is_symlink()
        or resolved_parent != absolute_parent
    ):
        raise ValueError("输出父目录必须是不含符号链接的真实目录。")
    if output.exists() or output.is_symlink():
        raise FileExistsError("输出已存在，拒绝覆盖。")
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(
                stream.fileno(),
                stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH,
            )
        os.link(temporary_path, output, follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """校验参数并原子发布 schema v2 模型部署清单。

    Args:
        argv: 不含程序名的命令行参数；为空时读取进程参数。

    Returns:
        成功返回 0；输入、写入或防覆盖检查失败返回 1。

    """
    arguments = _arguments(argv)
    try:
        service = cast(ServiceName, arguments.service)
        payload = build_deployment_manifest_v2(
            DeploymentManifestV2Spec(
                service=service,
                endpoint=arguments.endpoint,
                model=arguments.model,
                model_revision=arguments.model_revision,
                tokenizer_revision=arguments.tokenizer_revision,
                runtime_name=arguments.runtime_name,
                runtime_version=arguments.runtime_version,
                runtime_revision=arguments.runtime_revision,
                service_contract=_service_contract(arguments),
            )
        )
        _write_atomic_read_only(arguments.output, payload)
    except (OSError, ValueError) as error:
        print(
            f"MODEL_DEPLOYMENT_MANIFEST_FAILED: {error}",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "manifest_sha256": payload["manifest_sha256"],
                "output": str(arguments.output),
                "schema_version": "2",
                "service": service,
                "status": "passed",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
