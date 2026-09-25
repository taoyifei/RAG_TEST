#!/usr/bin/env python3
"""记录隔离实验及受保护容器的不可变身份与健康状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTECTED = (
    "wanshitong-wb08r06-production-app",
    "wanshitong-wb08r06-prefix-proxy",
    "wanshitong-wb08r-root-production-app",
    "wanshitong-wb08r-root-production-prefix-proxy",
    "wanshitong-sso-candidate-app",
    "wanshitong-wb08r01-qdrant",
    "wanshitong-qdrant",
)
EXPERIMENT = (
    "wkab-native-a-app",
    "wkab-native-a-qdrant",
    "wkab-native-b-app",
    "wkab-native-b-frontend",
    "wkab-native-b-postgres",
    "wkab-native-b-redis",
    "wkab-native-b-docreader",
    "wkab-native-b-rerank-adapter",
)
LOCKED_FILES = (
    "weknora-v0.8.2.tar.gz",
    "corpus.lock.json",
    "codex-wkab-cases-frozen.ndjson",
    "upstream/config/config.eval.yaml",
)


def _docker_inspect(name: str) -> dict[str, Any] | None:
    """仅提取身份和状态，绝不读取容器环境变量。"""
    result = subprocess.run(  # noqa: S603 - 固定 docker 命令，不经 shell。
        ["/usr/bin/docker", "container", "inspect", name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return None
    data = json.loads(result.stdout)[0]
    state = data.get("State", {})
    health = state.get("Health") or {}
    return {
        "id": data["Id"],
        "image_id": data["Image"],
        "state": state.get("Status"),
        "health": health.get("Status"),
        "ports": data.get("NetworkSettings", {}).get("Ports", {}),
        "compose_project": data.get("Config", {})
        .get("Labels", {})
        .get("com.docker.compose.project"),
    }


def _sha256(path: Path) -> str | None:
    """逐块计算固定输入的 SHA-256。"""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    """保存单次现场快照，供前后身份比对。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = {
        "phase": args.phase,
        "captured_at_utc": datetime.now(
            tz=timezone.utc  # noqa: UP017 - 内网宿主 Python 3.10。
        ).isoformat(),
        "a_source_sha": "dca24a80823b4e5d24dbbdd725e10c97167ad7a5",
        "b_source_sha": "3e8b0bfc80b845b2d4b2ed683994748741450a97",
        "protected": {name: _docker_inspect(name) for name in PROTECTED},
        "experiment": {name: _docker_inspect(name) for name in EXPERIMENT},
        "locked_files": {
            name: _sha256(args.root / name) for name in LOCKED_FILES
        },
    }
    descriptor = os.open(
        args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "phase": args.phase,
                "protected_present": sum(
                    x is not None for x in output["protected"].values()
                ),
                "experiment_present": sum(
                    x is not None for x in output["experiment"].values()
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
