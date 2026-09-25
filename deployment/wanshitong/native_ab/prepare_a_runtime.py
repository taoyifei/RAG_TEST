"""从受保护 8289 的配置生成独立 A 实例的私有环境文件。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

BASELINE_CONTAINER_ID = (
    "14c0f9a42c1479ef9c272ef4ff9f0c352c3855d292e9a5c9821f3021c0fdb591"
)
BASELINE_IMAGE_ID = (
    "sha256:ca38110829dd5a1e65589e9571744a1c7342b3b2fc95e94004ba9f7aa9168d06"
)


def _baseline() -> dict[str, object]:
    """先核对受保护容器身份，再读取配置。"""
    result = subprocess.run(  # noqa: S603 - 固定容器 ID，非用户输入。
        ["/usr/bin/docker", "inspect", BASELINE_CONTAINER_ID],
        check=True,
        capture_output=True,
        text=True,
    )
    inspected = json.loads(result.stdout)[0]
    if (
        inspected["Id"] != BASELINE_CONTAINER_ID
        or inspected["Image"] != BASELINE_IMAGE_ID
        or inspected["Name"] != "/wanshitong-sso-candidate-app"
        or inspected["State"]["Health"]["Status"] != "healthy"
    ):
        raise RuntimeError("8289 身份或健康状态与冻结基线不符")
    return inspected


def main() -> None:
    """生成只属于本轮隔离实例的端口、索引和认证配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ab_root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    inspected = _baseline()
    environment: dict[str, str] = {}
    for line in inspected["Config"]["Env"]:
        key, _, value = line.partition("=")
        if (
            key.startswith("RAG_") and not key.startswith("RAG_WANSHITONG_SSO_")
        ) or key == "FORWARDED_ALLOW_IPS":
            environment[key] = value
    secret_directory = args.ab_root / "secrets"
    secret_directory.mkdir(mode=0o700, exist_ok=False)
    qdrant_key = secrets.token_hex(32)
    secret_values = {
        "qdrant-api-key": qdrant_key.encode("ascii"),
        "admin-bootstrap-token": secrets.token_hex(32).encode("ascii"),
        "master-key": secrets.token_bytes(32),
    }
    for name, value in secret_values.items():
        descriptor = os.open(
            secret_directory / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
    environment.update(
        {
            "AB_A_ROOT": str(args.ab_root),
            "AB_A_SECRET_DIR": str(secret_directory),
            "AB_A_QDRANT_API_KEY": qdrant_key,
            "RAG_WANSHITONG_AUTH_MODE": "anonymous",
            "RAG_WANSHITONG_SSO_DEPLOYMENT_ID": "wk-ab-a-20260925",
            "RAG_TRUSTED_ORIGINS": (
                "http://127.0.0.1:18389,http://localhost:18389"
            ),
            "RAG_ROOT_PATH": "",
            "RAG_QDRANT_URL": "http://qdrant:6333",
            "RAG_WK_CHUNKER_MODE": "parent-child",
            "RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED": "true",
            "RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE": "wk-standard-pc-v1",
            "RAG_WANSHITONG_LLM_DISABLE_THINKING": "true",
            "RAG_PRIVATE_PROVIDER_DIAGNOSTIC_CAPTURE_SUCCESS": "true",
        }
    )
    if any(
        any(char in value for char in "\r\n$") for value in environment.values()
    ):
        raise ValueError("环境值包含 Compose 无法安全解析的字符")
    payload = "".join(
        f"{key}={value}\n" for key, value in sorted(environment.items())
    )
    descriptor = os.open(
        args.destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    print("已核对 8289 身份并创建隔离 A 环境文件；密钥未输出。")


if __name__ == "__main__":
    main()
