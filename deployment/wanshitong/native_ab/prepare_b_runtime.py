"""为隔离 WeKnora 实例生成仅在服务器保存的 Compose 环境文件。"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def main() -> None:
    """一次性创建私有配置，拒绝覆盖既有文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    values = {
        "WEKNORA_VERSION": "v0.8.2",
        "APP_PORT": "18390",
        "FRONTEND_PORT": "18391",
        "DB_DRIVER": "postgres",
        "DB_HOST": "postgres",
        "DB_PORT": "5432",
        "DB_USER": "wkab",
        "DB_PASSWORD": secrets.token_hex(24),
        "DB_NAME": "wkab",
        "REDIS_ADDR": "redis:6379",
        "REDIS_PASSWORD": secrets.token_hex(24),
        "STREAM_MANAGER_TYPE": "redis",
        "REDIS_DB": "0",
        "REDIS_PREFIX": "wk-ab:",
        "WEKNORA_REDIS_NAMESPACE": "wk-ab-20260925",
        "RETRIEVE_DRIVER": "postgres",
        "STORAGE_TYPE": "local",
        "LOCAL_STORAGE_BASE_DIR": "/data/files",
        "AUTO_MIGRATE": "true",
        "JWT_SECRET": secrets.token_hex(32),
        "SYSTEM_AES_KEY": secrets.token_hex(16),
        "SYSTEM_SIGNING_KEY": secrets.token_hex(32),
        "LOG_LEVEL": "info",
        "LLM_DEBUG_LOG": "false",
        "LANGFUSE_ENABLED": "false",
        "NEO4J_ENABLE": "false",
        "WEKNORA_SANDBOX_DOCKER_ENABLED": "false",
        "DOCREADER_ODL_HYBRID": "off",
        "SSRF_WHITELIST": "10.242.180.54,host.docker.internal,rerank-adapter",
        "SSRF_DNS_WHITELIST_ONLY": "true",
        "CONCURRENCY_POOL_SIZE": "1",
        "WEKNORA_ASYNQ_CORE_CONCURRENCY": "2",
        "WEKNORA_ASYNQ_ENRICHMENT_CONCURRENCY": "1",
        "WEKNORA_ASYNQ_SHARED_CONCURRENCY": "1",
        "WEKNORA_MODEL_MAX_CONCURRENCY": "1",
        "MAX_FILE_SIZE_MB": "50",
    }
    payload = "".join(f"{key}={value}\n" for key, value in values.items())
    descriptor = os.open(
        args.destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    print("已创建受限环境文件；密钥未输出。")


if __name__ == "__main__":
    main()
