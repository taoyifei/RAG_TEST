#!/usr/bin/env python3
"""比较 Industry serving update 前后的 Compose canonical JSON。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PORT_KEYS = {
    "app_protocol",
    "host_ip",
    "mode",
    "name",
    "protocol",
    "published",
    "target",
}
_VOLUME_KEYS = {
    "bind",
    "consistency",
    "read_only",
    "source",
    "target",
    "type",
    "volume",
}


class ComposeCheckError(RuntimeError):
    """表示旧、新 Compose 存在不允许的语义变化。"""


def compare_compose_models(  # noqa: PLR0912, PLR0913
    old: dict[str, object],
    new: dict[str, object],
    *,
    source_config: str,
    source_image: str,
    source_revision: str,
    target_config: str,
    target_image: str,
    target_revision: str,
) -> dict[str, object]:
    """比较旧、新 Compose canonical JSON 的安全语义。

    Args:
        old: 旧 Compose 的 canonical JSON object。
        new: 目标 Compose 的 canonical JSON object。
        source_config: 旧只读 config 的绝对宿主路径。
        source_image: 旧 App image ref。
        source_revision: 旧 App 完整 Git revision。
        target_config: 目标只读 config 的绝对宿主路径。
        target_image: 目标 App image ref。
        target_revision: 目标 App 完整 Git revision。

    Returns:
        已规范化的 App 端口与受保护依赖摘要。

    Raises:
        ComposeCheckError: 任一非允许语义变化或结构异常。

    """
    if any(
        re.fullmatch(r"[0-9a-f]{40}", revision) is None
        for revision in (source_revision, target_revision)
    ):
        raise ComposeCheckError("RELEASE_REVISION_INVALID")
    if source_revision == target_revision:
        raise ComposeCheckError("RELEASE_REVISION_TRANSITION_INVALID")
    if old.get("name") != "rag-industry" or new.get("name") != "rag-industry":
        raise ComposeCheckError("COMPOSE_PROJECT_CHANGED")
    old_services = _object_field(old, "services")
    new_services = _object_field(new, "services")
    for service in ("rag-industry-qdrant", "rag-industry-ocr"):
        if old_services.get(service) != new_services.get(service):
            raise ComposeCheckError("DEPENDENCY_SERVICE_CHANGED")
    if old.get("networks") != new.get("networks"):
        raise ComposeCheckError("COMPOSE_NETWORKS_CHANGED")
    old_app = _object_field(old_services, "rag-industry-app")
    new_app = _object_field(new_services, "rag-industry-app")
    if old_app.get("image") != source_image:
        raise ComposeCheckError("SOURCE_APP_IMAGE_INVALID")
    if new_app.get("image") != target_image:
        raise ComposeCheckError("TARGET_APP_IMAGE_INVALID")
    old_port = _single_app_port(old_app.get("ports"))
    new_port = _single_app_port(new_app.get("ports"))
    expected_port = {
        "host_ip": "",
        "mode": "ingress",
        "protocol": "tcp",
        "published": "8188",
        "target": 8088,
    }
    if old_port != new_port or new_port != expected_port:
        raise ComposeCheckError("APP_PORT_CHANGED")
    _compare_app_service_structure(old_app, new_app)
    _compare_service_volumes(
        old_app,
        new_app,
        source_config=source_config,
        target_config=target_config,
    )
    _verify_target_environment(
        old_app,
        new_app,
        source_revision=source_revision,
        target_revision=target_revision,
    )
    old_worker = old_services.get("rag-industry-worker")
    new_worker = new_services.get("rag-industry-worker")
    if old_worker is not None or new_worker is not None:
        if not isinstance(old_worker, dict) or not isinstance(new_worker, dict):
            raise ComposeCheckError("APP_WORKER_SERVICE_CHANGED")
        if old_worker.get("image") != source_image:
            raise ComposeCheckError("SOURCE_APP_WORKER_IMAGE_INVALID")
        if new_worker.get("image") != target_image:
            raise ComposeCheckError("TARGET_APP_WORKER_IMAGE_INVALID")
        _compare_worker_service(
            old_worker,
            new_worker,
            source_config=source_config,
            source_revision=source_revision,
            target_config=target_config,
            target_revision=target_revision,
        )
    return {
        "app_port": new_port,
        "dependency_services": [
            "rag-industry-ocr",
            "rag-industry-qdrant",
        ],
        "schema_version": "1",
    }


def _object_field(
    value: dict[str, object], key: str
) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ComposeCheckError(f"{key.upper()}_INVALID")
    return item


def _single_app_port(value: object) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1:
        raise ComposeCheckError("APP_PORT_INVALID")
    item = value[0]
    if not isinstance(item, dict) or not set(item).issubset(_PORT_KEYS):
        raise ComposeCheckError("APP_PORT_INVALID")
    target = item.get("target")
    published = item.get("published")
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or not isinstance(published, (str, int))
        or isinstance(published, bool)
    ):
        raise ComposeCheckError("APP_PORT_INVALID")
    host_ip = item.get("host_ip", "")
    if host_ip == "0.0.0.0":  # noqa: S104 - canonical wildcard value
        host_ip = ""
    protocol = item.get("protocol", "tcp")
    mode = item.get("mode", "ingress")
    if not all(isinstance(field, str) for field in (host_ip, protocol, mode)):
        raise ComposeCheckError("APP_PORT_INVALID")
    return {
        "host_ip": host_ip,
        "mode": mode.lower(),
        "protocol": protocol.lower(),
        "published": str(published),
        "target": target,
    }


def _compare_app_service_structure(
    old: dict[str, object], new: dict[str, object]
) -> None:
    mutable = {"environment", "image", "ports", "volumes"}
    for key in set(old) | set(new):
        if key not in mutable and old.get(key) != new.get(key):
            raise ComposeCheckError("APP_WORKER_STRUCTURE_CHANGED")


def _normalized_volumes(service: dict[str, object]) -> dict[str, object]:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        raise ComposeCheckError("APP_VOLUME_INVALID")
    result: dict[str, object] = {}
    for item in volumes:
        if not isinstance(item, dict) or not set(item).issubset(_VOLUME_KEYS):
            raise ComposeCheckError("APP_VOLUME_INVALID")
        target = item.get("target")
        source = item.get("source")
        if not isinstance(target, str) or not isinstance(source, str):
            raise ComposeCheckError("APP_VOLUME_INVALID")
        if target in result:
            raise ComposeCheckError("APP_VOLUME_DUPLICATE_TARGET")
        result[target] = {
            "bind": item.get("bind"),
            "consistency": item.get("consistency"),
            "read_only": item.get("read_only", False),
            "source": source,
            "type": item.get("type", "volume"),
            "volume": item.get("volume"),
        }
    return result


def _compare_service_volumes(
    old: dict[str, object],
    new: dict[str, object],
    *,
    source_config: str,
    target_config: str,
) -> None:
    old_volumes = _normalized_volumes(old)
    new_volumes = _normalized_volumes(new)
    required = {"/config", "/data/docs", "/logs", "/state"}
    if (
        not required.issubset(old_volumes)
        or set(old_volumes) != set(new_volumes)
    ):
        raise ComposeCheckError("APP_VOLUME_CHANGED")
    expected_read_only = {
        "/config": True,
        "/data/docs": True,
        "/logs": False,
        "/state": False,
    }
    for target in old_volumes:
        old_item = old_volumes[target]
        new_item = new_volumes[target]
        if not isinstance(old_item, dict) or not isinstance(new_item, dict):
            raise ComposeCheckError("APP_VOLUME_INVALID")
        if target in expected_read_only and (
            old_item.get("type") != "bind"
            or new_item.get("type") != "bind"
            or old_item.get("read_only") != expected_read_only[target]
            or new_item.get("read_only") != expected_read_only[target]
        ):
            raise ComposeCheckError("APP_VOLUME_PERMISSION_INVALID")
        if target == "/config":
            if (
                old_item.get("source") != source_config
                or new_item.get("source") != target_config
            ):
                raise ComposeCheckError("CONFIG_MOUNT_INVALID")
            old_item = {**old_item, "source": "<CONFIG>"}
            new_item = {**new_item, "source": "<CONFIG>"}
        if old_item != new_item:
            raise ComposeCheckError("APP_VOLUME_CHANGED")


def _verify_target_environment(
    old: dict[str, object],
    new: dict[str, object],
    *,
    source_revision: str,
    target_revision: str,
) -> None:
    old_environment = _object_field(old, "environment")
    environment = _object_field(new, "environment")
    expected = {
        "RAG_RUN_MODE": "demo",
        "RAG_TRACE_QUESTION_CAPTURE": "plaintext",
        "RAG_TRACE_QUESTION_RETENTION_SECONDS": "604800",
        "RAG_UI_ALLOW_INSECURE_HTTP": "true",
        "RAG_UI_COOKIE_SECURE": "false",
        "RAG_UI_QUERY_AUTH_MODE": "same_origin_session",
        "RAG_UI_SESSION_TTL_SECONDS": "1800",
    }
    if any(
        str(environment.get(key)).lower() != value
        for key, value in expected.items()
    ):
        raise ComposeCheckError("TARGET_SERVING_ENV_INVALID")
    if old_environment.get("RAG_RELEASE_REVISION") != source_revision:
        raise ComposeCheckError("SOURCE_RELEASE_REVISION_INVALID")
    if environment.get("RAG_RELEASE_REVISION") != target_revision:
        raise ComposeCheckError("TARGET_RELEASE_REVISION_INVALID")
    for key in set(old_environment) | set(environment):
        if (
            key not in set(expected) | {"RAG_RELEASE_REVISION"}
            and old_environment.get(key) != environment.get(key)
        ):
            raise ComposeCheckError("APP_ENVIRONMENT_CHANGED")


def _compare_worker_service(  # noqa: PLR0913
    old: dict[str, object],
    new: dict[str, object],
    *,
    source_config: str,
    source_revision: str,
    target_config: str,
    target_revision: str,
) -> None:
    mutable = {"environment", "image", "volumes"}
    for key in set(old) | set(new):
        if key not in mutable and old.get(key) != new.get(key):
            raise ComposeCheckError("APP_WORKER_STRUCTURE_CHANGED")
    _compare_service_volumes(
        old,
        new,
        source_config=source_config,
        target_config=target_config,
    )
    _verify_target_environment(
        old,
        new,
        source_revision=source_revision,
        target_revision=target_revision,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("source_config")
    parser.add_argument("source_image")
    parser.add_argument("source_revision")
    parser.add_argument("target_config")
    parser.add_argument("target_image")
    parser.add_argument("target_revision")
    return parser.parse_args()


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ComposeCheckError(f"{label.upper()}_JSON_INVALID") from error
    if not isinstance(value, dict):
        raise ComposeCheckError(f"{label.upper()}_JSON_INVALID")
    return value


def main() -> int:
    """执行 Compose canonical JSON 比较。

    Args:
        无参数；命令行参数由 argparse 读取。

    Returns:
        合同成立返回 0，否则返回 1 且只输出稳定错误码。

    """
    arguments = _arguments()
    try:
        report = compare_compose_models(
            _json_object(arguments.old, "old compose"),
            _json_object(arguments.new, "new compose"),
            source_config=arguments.source_config,
            source_image=arguments.source_image,
            source_revision=arguments.source_revision,
            target_config=arguments.target_config,
            target_image=arguments.target_image,
            target_revision=arguments.target_revision,
        )
    except ComposeCheckError as error:
        print(f"RAG_INDUSTRY_COMPOSE_CHECK_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
