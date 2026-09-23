"""核对 WB-08R 准备阶段候选容器与旧 8289 的有效运行语义。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

Stage = Literal["rendered_spec", "created_container"]

_CANDIDATE_CONTAINER = "wanshitong-sso-candidate-app"
_ISOLATED_MOUNTS = {
    "/data": "data",
    "/logs": "logs",
    "/private-diagnostics": "private-diagnostics",
}
_SECRET_MOUNT = "/run/rag-secrets"  # noqa: S105 - 容器挂载路径，不是密码。
_REQUIRED_MOUNTS = frozenset((*_ISOLATED_MOUNTS, _SECRET_MOUNT))
_BASE_IMAGE_LABELS = (
    "org.opencontainers.image.base.digest",
    "org.opencontainers.image.base.name",
)
_DEPARTMENT_SHADOW_KEY = "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
_POPULAR_QUESTIONS_KEY = "RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED"
_ROOT_PATH_KEY = "RAG_ROOT_PATH"
_TRUSTED_ORIGINS_KEY = "RAG_TRUSTED_ORIGINS"
_SSO_AUTH_MODE_KEY = "RAG_WANSHITONG_AUTH_MODE"
_SSO_DEPLOYMENT_ID_KEY = "RAG_WANSHITONG_SSO_DEPLOYMENT_ID"
_SSO_ENTRIES_KEY = "RAG_WANSHITONG_SSO_ENTRIES"
_SSO_VALIDATE_URL_KEY = "RAG_WANSHITONG_SSO_VALIDATE_URL"
_SSO_CLIENT_ID_KEY = "RAG_WANSHITONG_SSO_CLIENT_ID"
_SSO_SECRET_FILE_KEY = "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE"  # noqa: S105
_SSO_SECRET_FILE = "/run/rag-secrets/sso-client-secret"  # noqa: S105
_SSO_MAX_CLIENT_ID_CHARS = 64
_SSO_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SSO_ENTRY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SSO_REQUIRED_KEYS = frozenset(
    {
        _ROOT_PATH_KEY,
        _SSO_AUTH_MODE_KEY,
        _SSO_DEPLOYMENT_ID_KEY,
        _SSO_ENTRIES_KEY,
        _SSO_VALIDATE_URL_KEY,
        _SSO_CLIENT_ID_KEY,
        _SSO_SECRET_FILE_KEY,
    }
)
_SSO_TRANSITION_KEYS = _SSO_REQUIRED_KEYS | {_TRUSTED_ORIGINS_KEY}


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _inspect_object(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError(f"INSPECT_JSON_INVALID:{path}")
    item = value[0]
    if not isinstance(item, dict):
        raise ValueError(f"INSPECT_OBJECT_INVALID:{path}")
    return cast(dict[str, Any], item)


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _environment(entries: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key or key in result:
            raise ValueError("CONTAINER_ENVIRONMENT_INVALID")
        result[key] = value
    return result


def _application_environment(
    environment: dict[str, str],
) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key.startswith("RAG_") or key == "FORWARDED_ALLOW_IPS"
    }


def _normalized_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _sso_endpoint(value: str, *, suffix: str) -> str:
    parsed = urlsplit(value)
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValueError("端点端口无效") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.endswith(suffix)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"端点必须是以 {suffix} 结尾的 HTTP(S) URL")
    return value


def _sso_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValueError("入口 origin 端口无效") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("入口 origin 必须是无路径的完整 HTTP(S) origin")
    return value.rstrip("/")


def _sso_entry_origins(value: str) -> frozenset[str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("入口注册表必须是 JSON") from error
    if not isinstance(payload, list) or not payload:
        raise ValueError("入口注册表必须是非空 JSON 数组")
    entry_ids: set[str] = set()
    origins: set[str] = set()
    for raw_entry in payload:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "id",
            "origin",
            "authorize_url",
        }:
            raise ValueError("每个入口必须只含 id/origin/authorize_url")
        entry_id = raw_entry["id"]
        origin = raw_entry["origin"]
        authorize_url = raw_entry["authorize_url"]
        if not all(
            isinstance(item, str)
            for item in (entry_id, origin, authorize_url)
        ):
            raise ValueError("入口字段必须是字符串")
        if not _SSO_ENTRY_ID_PATTERN.fullmatch(entry_id):
            raise ValueError("入口 ID 格式不安全")
        normalized_origin = _sso_origin(origin)
        _sso_endpoint(authorize_url, suffix="/sso/authorize")
        if entry_id in entry_ids or normalized_origin in origins:
            raise ValueError("入口 ID 与 origin 均不得重复")
        entry_ids.add(entry_id)
        origins.add(normalized_origin)
    return frozenset(origins)


def _trusted_origins(value: str) -> frozenset[str]:
    origins: set[str] = set()
    for item in value.split(","):
        candidate = item.strip()
        if candidate:
            origins.add(_sso_origin(candidate))
    if not origins:
        raise ValueError("可信 Origin 至少包含一项")
    return frozenset(origins)


@dataclass(slots=True)
class _Report:
    """累积不泄露原值的字段级比较结果。"""

    stage: Stage
    missing_required_keys: list[dict[str, object]] = field(default_factory=list)
    semantic_mismatches: list[dict[str, object]] = field(default_factory=list)
    mount_mismatches: list[dict[str, object]] = field(default_factory=list)
    allowed_changes: list[dict[str, object]] = field(default_factory=list)
    unclassified_changes: list[dict[str, object]] = field(default_factory=list)

    def add_problem(
        self,
        category: Literal[
            "missing_required_keys",
            "semantic_mismatches",
            "mount_mismatches",
            "unclassified_changes",
        ],
        field_path: str,
        reason: str,
        *,
        baseline: object = None,
        candidate: object = None,
    ) -> None:
        """记录字段路径、来源层和两端摘要。"""
        item = {
            "field_path": field_path,
            "source_layer": "baseline_created_container",
            "candidate_layer": self.stage,
            "matches": False,
            "reason": reason,
            "baseline_sha256": _digest(baseline),
            "candidate_sha256": _digest(candidate),
        }
        getattr(self, category).append(item)

    def allow(
        self,
        field_path: str,
        reason: str,
        *,
        baseline: object = None,
        candidate: object = None,
    ) -> None:
        """登记本批预先声明且不会扩大运行语义的差异。"""
        self.allowed_changes.append(
            {
                "field_path": field_path,
                "source_layer": "baseline_created_container",
                "candidate_layer": self.stage,
                "matches": baseline == candidate,
                "reason": reason,
                "baseline_sha256": _digest(baseline),
                "candidate_sha256": _digest(candidate),
            }
        )

    def compare(
        self,
        field_path: str,
        baseline: object,
        candidate: object,
        reason: str,
    ) -> None:
        """只在值不等时登记有效语义差异。"""
        if baseline != candidate:
            self.add_problem(
                "semantic_mismatches",
                field_path,
                reason,
                baseline=baseline,
                candidate=candidate,
            )

    def result(self, layer_sha256: dict[str, str]) -> dict[str, object]:
        """返回计划要求的稳定 JSON 结构。"""
        ready = not any(
            (
                self.missing_required_keys,
                self.semantic_mismatches,
                self.mount_mismatches,
                self.unclassified_changes,
            )
        )
        return {
            "stage": self.stage,
            "missing_required_keys": self.missing_required_keys,
            "semantic_mismatches": self.semantic_mismatches,
            "mount_mismatches": self.mount_mismatches,
            "allowed_changes": self.allowed_changes,
            "unclassified_changes": self.unclassified_changes,
            "layer_sha256": layer_sha256,
            "counts": {
                "missing_required_keys": len(self.missing_required_keys),
                "semantic_mismatches": len(self.semantic_mismatches),
                "mount_mismatches": len(self.mount_mismatches),
                "allowed_changes": len(self.allowed_changes),
                "unclassified_changes": len(self.unclassified_changes),
            },
            "ready": ready,
        }


def _compare_popular_questions_toggle(
    report: _Report,
    baseline: dict[str, str],
    candidate: dict[str, str],
    ignored_keys: set[str],
) -> None:
    """只声明 F06 公共榜单布尔开关的安全差异。"""
    baseline_value = baseline.get(_POPULAR_QUESTIONS_KEY)
    candidate_value = candidate.get(_POPULAR_QUESTIONS_KEY)
    if baseline_value not in (
        None,
        "true",
        "false",
    ) or candidate_value not in ("true", "false"):
        ignored_keys.add(_POPULAR_QUESTIONS_KEY)
        report.add_problem(
            "semantic_mismatches",
            f"services.app.environment.{_POPULAR_QUESTIONS_KEY}",
            "公共问题开关只允许显式 true 或 false。",
            baseline=baseline_value,
            candidate=candidate_value,
        )
    elif baseline_value != candidate_value:
        ignored_keys.add(_POPULAR_QUESTIONS_KEY)
        report.allow(
            f"services.app.environment.{_POPULAR_QUESTIONS_KEY}",
            "F06 公共问题开关允许在候选中启用或回退。",
            baseline=baseline_value,
            candidate=candidate_value,
        )


def _compare_environment(  # noqa: PLR0913
    report: _Report,
    baseline: dict[str, str],
    candidate: dict[str, str],
    *,
    allow_department_shadow_enable: bool = False,
    allow_sso_enable: bool = False,
    allow_popular_questions_toggle: bool = False,
) -> None:
    ignored_keys: set[str] = set()
    if allow_popular_questions_toggle:
        _compare_popular_questions_toggle(
            report, baseline, candidate, ignored_keys
        )
    if allow_department_shadow_enable:
        baseline_value = baseline.get(_DEPARTMENT_SHADOW_KEY)
        candidate_value = candidate.get(_DEPARTMENT_SHADOW_KEY)
        if baseline_value == candidate_value:
            # 后续阶段可保持已经验收的 Shadow，不应重复判定为本次新启用。
            pass
        elif baseline_value not in (None, "false"):
            ignored_keys.add(_DEPARTMENT_SHADOW_KEY)
            report.add_problem(
                "semantic_mismatches",
                f"services.app.environment.{_DEPARTMENT_SHADOW_KEY}",
                "部门 Shadow 基准必须缺省或显式关闭。",
                baseline=baseline_value,
                candidate=candidate_value,
            )
        elif candidate_value != "true":
            ignored_keys.add(_DEPARTMENT_SHADOW_KEY)
            report.add_problem(
                "semantic_mismatches",
                f"services.app.environment.{_DEPARTMENT_SHADOW_KEY}",
                "本批已声明启用部门 Shadow，但候选最终值不是 true。",
                baseline=baseline_value,
                candidate=candidate_value,
            )
        else:
            ignored_keys.add(_DEPARTMENT_SHADOW_KEY)
            report.allow(
                f"services.app.environment.{_DEPARTMENT_SHADOW_KEY}",
                "阶段 04 只启用观察型部门 Shadow，不改变实际检索范围。",
                baseline=baseline_value,
                candidate=candidate_value,
            )
    if allow_sso_enable:
        inherited_sso = all(
            baseline.get(key) == candidate.get(key)
            for key in _SSO_TRANSITION_KEYS
        )
        if not inherited_sso:
            ignored_keys.update(_SSO_TRANSITION_KEYS)
        _compare_sso_environment(
            report,
            baseline,
            candidate,
            inherited_sso=inherited_sso,
        )
    baseline_keys = set(baseline) - ignored_keys
    candidate_keys = set(candidate) - ignored_keys
    for key in sorted(baseline_keys - candidate_keys):
        report.add_problem(
            "missing_required_keys",
            f"services.app.environment.{key}",
            "基准有效应用键未进入候选最终环境。",
            baseline=baseline[key],
        )
    for key in sorted(candidate_keys - baseline_keys):
        report.add_problem(
            "unclassified_changes",
            f"services.app.environment.{key}",
            "候选新增应用键未在本批差异清单中声明。",
            candidate=candidate[key],
        )
    for key in sorted(baseline_keys & candidate_keys):
        report.compare(
            f"services.app.environment.{key}",
            baseline[key],
            candidate[key],
            "候选应用键值与旧 8289 有效值不一致。",
        )


def _compare_sso_transition_baseline(
    report: _Report,
    baseline: dict[str, str],
    candidate: dict[str, str],
) -> None:
    """校验首次启用 SSO 前的基准确实处于未启用状态。"""
    baseline_inactive_values = {
        _ROOT_PATH_KEY: {None, ""},
        _SSO_AUTH_MODE_KEY: {None, "", "anonymous"},
        _SSO_DEPLOYMENT_ID_KEY: {None, ""},
        _SSO_ENTRIES_KEY: {None, ""},
        _SSO_VALIDATE_URL_KEY: {None, ""},
        _SSO_CLIENT_ID_KEY: {None, ""},
        _SSO_SECRET_FILE_KEY: {None, ""},
    }
    for key, accepted_values in baseline_inactive_values.items():
        if baseline.get(key) not in accepted_values:
            report.add_problem(
                "semantic_mismatches",
                f"services.app.environment.{key}",
                "SSO 切换基准必须处于匿名或未配置状态。",
                baseline=baseline.get(key),
                candidate=candidate.get(key),
            )


def _compare_sso_environment(
    report: _Report,
    baseline: dict[str, str],
    candidate: dict[str, str],
    *,
    inherited_sso: bool,
) -> None:
    """校验 SSO 首次启用，或校验候选完整继承既有 SSO 配置。"""
    if not inherited_sso:
        _compare_sso_transition_baseline(report, baseline, candidate)
    for key in sorted(_SSO_REQUIRED_KEYS):
        if not candidate.get(key):
            report.add_problem(
                "missing_required_keys",
                f"services.app.environment.{key}",
                "SSO 候选缺少必填环境变量。",
                baseline=baseline.get(key),
                candidate=candidate.get(key),
            )

    checks: tuple[tuple[str, bool, str], ...] = (
        (
            _ROOT_PATH_KEY,
            candidate.get(_ROOT_PATH_KEY) == "/kb",
            "SSO 候选 RAG_ROOT_PATH 必须精确为 /kb。",
        ),
        (
            _SSO_AUTH_MODE_KEY,
            candidate.get(_SSO_AUTH_MODE_KEY) == "sso",
            "SSO 候选鉴权模式必须精确为 sso。",
        ),
        (
            _SSO_DEPLOYMENT_ID_KEY,
            bool(
                _SSO_DEPLOYMENT_ID_PATTERN.fullmatch(
                    candidate.get(_SSO_DEPLOYMENT_ID_KEY, "")
                )
            ),
            "SSO deployment ID 格式不安全。",
        ),
        (
            _SSO_CLIENT_ID_KEY,
            bool(candidate.get(_SSO_CLIENT_ID_KEY, "").strip())
            and len(candidate.get(_SSO_CLIENT_ID_KEY, ""))
            <= _SSO_MAX_CLIENT_ID_CHARS,
            "SSO client ID 必须非空且不超过 64 字符。",
        ),
        (
            _SSO_SECRET_FILE_KEY,
            candidate.get(_SSO_SECRET_FILE_KEY) == _SSO_SECRET_FILE,
            "SSO client secret 只能来自固定只读挂载文件。",
        ),
    )
    for key, valid, reason in checks:
        if not valid:
            report.add_problem(
                "semantic_mismatches",
                f"services.app.environment.{key}",
                reason,
                baseline=baseline.get(key),
                candidate=candidate.get(key),
            )

    entry_origins: frozenset[str] = frozenset()
    entries_valid = False
    try:
        entry_origins = _sso_entry_origins(
            candidate.get(_SSO_ENTRIES_KEY, "")
        )
        entries_valid = True
    except ValueError as error:
        report.add_problem(
            "semantic_mismatches",
            f"services.app.environment.{_SSO_ENTRIES_KEY}",
            f"SSO 入口注册表无效：{error}。",
            baseline=baseline.get(_SSO_ENTRIES_KEY),
            candidate=candidate.get(_SSO_ENTRIES_KEY),
        )
    try:
        _sso_endpoint(
            candidate.get(_SSO_VALIDATE_URL_KEY, ""),
            suffix="/sso/validate",
        )
    except ValueError as error:
        report.add_problem(
            "semantic_mismatches",
            f"services.app.environment.{_SSO_VALIDATE_URL_KEY}",
            f"SSO validate 地址无效：{error}。",
            baseline=baseline.get(_SSO_VALIDATE_URL_KEY),
            candidate=candidate.get(_SSO_VALIDATE_URL_KEY),
        )

    try:
        baseline_origins = _trusted_origins(
            baseline.get(_TRUSTED_ORIGINS_KEY, "")
        )
        candidate_origins = _trusted_origins(
            candidate.get(_TRUSTED_ORIGINS_KEY, "")
        )
        missing_origins = baseline_origins - candidate_origins
        if entries_valid:
            missing_origins |= entry_origins - candidate_origins
        if missing_origins:
            raise ValueError("不得删除基准 Origin，且必须包含全部 SSO 入口")
    except ValueError as error:
        report.add_problem(
            "semantic_mismatches",
            f"services.app.environment.{_TRUSTED_ORIGINS_KEY}",
            f"SSO 可信 Origin 配置无效：{error}。",
            baseline=baseline.get(_TRUSTED_ORIGINS_KEY),
            candidate=candidate.get(_TRUSTED_ORIGINS_KEY),
        )

    if not inherited_sso:
        for key in sorted(_SSO_TRANSITION_KEYS):
            report.allow(
                f"services.app.environment.{key}",
                "阶段 03 显式启用固定 `/kb` SSO 登录所需的声明差异。",
                baseline=baseline.get(key),
                candidate=candidate.get(key),
            )


def _baseline_mounts(container: dict[str, Any]) -> dict[str, dict[str, object]]:
    return {
        item["Destination"]: {
            "source": item["Source"],
            "type": item["Type"],
            "read_only": not item["RW"],
        }
        for item in container["Mounts"]
    }


def _rendered_mounts(service: dict[str, Any]) -> dict[str, dict[str, object]]:
    return {
        item["target"]: {
            "source": item["source"],
            "type": item["type"],
            "read_only": bool(item.get("read_only", False)),
        }
        for item in service.get("volumes", [])
    }


def _created_mounts(container: dict[str, Any]) -> dict[str, dict[str, object]]:
    return _baseline_mounts(container)


def _compare_mounts(
    report: _Report,
    baseline: dict[str, dict[str, object]],
    candidate: dict[str, dict[str, object]],
    candidate_root: PurePosixPath,
    forbidden_root: PurePosixPath,
) -> None:
    if not candidate_root.is_absolute():
        raise ValueError("CANDIDATE_ROOT_MUST_BE_ABSOLUTE")
    if _is_within(candidate_root, forbidden_root):
        report.add_problem(
            "unclassified_changes",
            "candidate_root",
            "候选根目录落入禁止写入的生产根目录。",
            candidate=str(candidate_root),
        )
    for target in sorted(_REQUIRED_MOUNTS - set(candidate)):
        report.add_problem(
            "mount_mismatches",
            f"services.app.mounts.{target}",
            "候选缺少必需挂载。",
            baseline=baseline.get(target),
        )
    for target in sorted(set(candidate) - _REQUIRED_MOUNTS):
        report.add_problem(
            "unclassified_changes",
            f"services.app.mounts.{target}",
            "候选新增挂载未分类。",
            candidate=candidate[target],
        )
    for target in sorted(_REQUIRED_MOUNTS & set(candidate)):
        baseline_mount = baseline.get(target)
        candidate_mount = candidate[target]
        if baseline_mount is None:
            report.add_problem(
                "mount_mismatches",
                f"services.app.mounts.{target}",
                "旧 8289 缺少计划要求的基准挂载。",
                candidate=candidate_mount,
            )
            continue
        for key, reason in (
            ("type", "挂载类型与旧 8289 不一致。"),
            ("read_only", "挂载读写语义与旧 8289 不一致。"),
        ):
            if baseline_mount[key] != candidate_mount[key]:
                report.add_problem(
                    "mount_mismatches",
                    f"services.app.mounts.{target}.{key}",
                    reason,
                    baseline=baseline_mount[key],
                    candidate=candidate_mount[key],
                )
        candidate_source = PurePosixPath(str(candidate_mount["source"]))
        if target == _SECRET_MOUNT:
            if candidate_mount["source"] != baseline_mount["source"]:
                report.add_problem(
                    "mount_mismatches",
                    f"services.app.mounts.{target}.source",
                    "Secret 必须复用已批准的旧 8289 只读来源。",
                    baseline=baseline_mount["source"],
                    candidate=candidate_mount["source"],
                )
            continue
        expected = candidate_root / _ISOLATED_MOUNTS[target]
        if candidate_source != expected:
            report.add_problem(
                "mount_mismatches",
                f"services.app.mounts.{target}.source",
                "候选可写目录必须使用本批独立精确路径。",
                baseline=str(expected),
                candidate=str(candidate_source),
            )
            continue
        if candidate_mount["source"] == baseline_mount["source"]:
            report.add_problem(
                "mount_mismatches",
                f"services.app.mounts.{target}.source",
                "候选可写目录不得复用旧 8289 回退点。",
                baseline=baseline_mount["source"],
                candidate=candidate_mount["source"],
            )
            continue
        report.allow(
            f"services.app.mounts.{target}.source",
            "候选使用独立可写目录，容器内用途保持不变。",
            baseline=baseline_mount["source"],
            candidate=candidate_mount["source"],
        )


def _rendered_ports(service: dict[str, Any]) -> list[dict[str, object]]:
    result = [
        {
            "host_ip": str(item.get("host_ip", "")),
            "published": str(item["published"]),
            "target": int(item["target"]),
            "protocol": item.get("protocol", "tcp"),
        }
        for item in service.get("ports", [])
    ]
    return sorted(result, key=lambda value: json.dumps(value, sort_keys=True))


def _created_ports(container: dict[str, Any]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for target, bindings in container["HostConfig"]["PortBindings"].items():
        target_port, protocol = target.split("/", 1)
        result.extend(
            {
                "host_ip": binding["HostIp"],
                "published": str(binding["HostPort"]),
                "target": int(target_port),
                "protocol": protocol,
            }
            for binding in bindings or []
        )
    return sorted(result, key=lambda value: json.dumps(value, sort_keys=True))


def _baseline_ports(container: dict[str, Any]) -> list[dict[str, object]]:
    return _created_ports(container)


def _rendered_networks(
    rendered: dict[str, Any], service: dict[str, Any]
) -> set[str]:
    definitions = rendered.get("networks", {})
    return {
        str(definitions.get(key, {}).get("name", key))
        for key in service.get("networks", {})
    }


def _created_networks(
    container: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
) -> set[str]:
    names = set(container["NetworkSettings"]["Networks"])
    if baseline is None:
        return names
    baseline_networks = baseline["NetworkSettings"]["Networks"]
    names_by_id: dict[str, str] = {}
    for name, settings in baseline_networks.items():
        network_id = str(settings.get("NetworkID", ""))
        if not network_id:
            continue
        if network_id in names_by_id:
            raise ValueError("BASELINE_NETWORK_ID_DUPLICATED")
        names_by_id[network_id] = str(name)
    return {names_by_id.get(name, name) for name in names}


def _baseline_networks(container: dict[str, Any]) -> set[str]:
    return _created_networks(container)


def _tmpfs_from_rendered(service: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in service.get("tmpfs", []):
        target, separator, options = str(item).partition(":")
        result[target] = options if separator else ""
    return result


def _extra_hosts_from_rendered(service: dict[str, Any]) -> set[str]:
    value = service.get("extra_hosts", {})
    if isinstance(value, dict):
        return {f"{key}:{item}" for key, item in value.items()}
    result: set[str] = set()
    for item in value:
        text = str(item)
        host, separator, address = text.partition("=")
        result.add(f"{host}:{address}" if separator else text)
    return result


def _compare_image_contract(
    report: _Report,
    baseline: dict[str, Any],
    target_image: dict[str, Any],
) -> None:
    image_config = target_image["Config"]
    baseline_config = baseline["Config"]
    for key in ("User", "WorkingDir", "Entrypoint", "Cmd", "Healthcheck"):
        report.compare(
            f"image.config.{key}",
            baseline_config.get(key),
            image_config.get(key),
            "候选镜像运行合同与旧 8289 镜像不一致。",
        )
    baseline_labels = baseline_config.get("Labels") or {}
    image_labels = image_config.get("Labels") or {}
    for key in _BASE_IMAGE_LABELS:
        report.compare(
            f"image.labels.{key}",
            baseline_labels.get(key),
            image_labels.get(key),
            "候选基础镜像身份与旧 8289 不一致。",
        )
    report.allow(
        "image.id",
        "候选加载本批源码，镜像身份按目标 image inspect 单独锁定。",
        baseline=baseline["Image"],
        candidate=target_image["Id"],
    )
    report.allow(
        "image.labels.org.opencontainers.image.revision",
        "候选源码 revision 是本批声明差异。",
        baseline=baseline_labels.get("org.opencontainers.image.revision"),
        candidate=image_labels.get("org.opencontainers.image.revision"),
    )


def _compare_rendered_runtime(
    report: _Report,
    baseline: dict[str, Any],
    rendered: dict[str, Any],
    service: dict[str, Any],
    target_image: dict[str, Any],
) -> None:
    host = baseline["HostConfig"]
    services = set(rendered.get("services", {}))
    if services != {"app"}:
        report.add_problem(
            "unclassified_changes",
            "services",
            "候选 Compose 必须只管理唯一 app 服务。",
            baseline=["app"],
            candidate=sorted(services),
        )
    definitions = rendered.get("networks", {})
    for logical_name in service.get("networks", {}):
        if definitions.get(logical_name, {}).get("external") is not True:
            report.add_problem(
                "semantic_mismatches",
                f"networks.{logical_name}.external",
                "候选只能引用既有 external 网络。",
                baseline=True,
                candidate=definitions.get(logical_name, {}).get("external"),
            )
    report.compare(
        "services.app.ports",
        _baseline_ports(baseline),
        _rendered_ports(service),
        "候选端口映射与旧 8289 不一致。",
    )
    report.compare(
        "services.app.networks",
        sorted(_baseline_networks(baseline)),
        sorted(_rendered_networks(rendered, service)),
        "候选没有精确复用旧 8289 的两个既有网络。",
    )
    report.compare(
        "services.app.read_only",
        bool(host["ReadonlyRootfs"]),
        bool(service.get("read_only", False)),
        "只读根文件系统设置不一致。",
    )
    report.compare(
        "services.app.tmpfs",
        host.get("Tmpfs") or {},
        _tmpfs_from_rendered(service),
        "tmpfs 设置与旧 8289 不一致。",
    )
    report.compare(
        "services.app.pids_limit",
        host.get("PidsLimit"),
        service.get("pids_limit"),
        "PID 限制与旧 8289 不一致。",
    )
    report.compare(
        "services.app.cap_drop",
        sorted(host.get("CapDrop") or []),
        sorted(service.get("cap_drop") or []),
        "Capability 限制与旧 8289 不一致。",
    )
    report.compare(
        "services.app.security_opt",
        sorted(host.get("SecurityOpt") or []),
        sorted(service.get("security_opt") or []),
        "security_opt 与旧 8289 不一致。",
    )
    report.compare(
        "services.app.restart",
        host.get("RestartPolicy", {}).get("Name", ""),
        service.get("restart", ""),
        "重启策略与旧 8289 不一致。",
    )
    report.compare(
        "services.app.extra_hosts",
        sorted(host.get("ExtraHosts") or []),
        sorted(_extra_hosts_from_rendered(service)),
        "host-gateway 注入与旧 8289 不一致。",
    )
    image_name = str(service.get("image", ""))
    if image_name not in (target_image.get("RepoTags") or []):
        report.add_problem(
            "semantic_mismatches",
            "services.app.image",
            "渲染镜像标签没有解析到本批锁定镜像。",
            baseline=target_image.get("RepoTags"),
            candidate=image_name,
        )
    _compare_image_contract(report, baseline, target_image)


def _compare_created_runtime(
    report: _Report,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    target_image: dict[str, Any],
) -> None:
    baseline_host = baseline["HostConfig"]
    candidate_host = candidate["HostConfig"]
    for key in (
        "ReadonlyRootfs",
        "Tmpfs",
        "PidsLimit",
        "CapDrop",
        "SecurityOpt",
        "ExtraHosts",
    ):
        report.compare(
            f"container.host_config.{key}",
            baseline_host.get(key),
            candidate_host.get(key),
            "创建后容器的 HostConfig 与旧 8289 不一致。",
        )
    report.compare(
        "container.host_config.RestartPolicy.Name",
        baseline_host.get("RestartPolicy", {}).get("Name"),
        candidate_host.get("RestartPolicy", {}).get("Name"),
        "创建后容器的重启策略与旧 8289 不一致。",
    )
    report.compare(
        "container.ports",
        _baseline_ports(baseline),
        _created_ports(candidate),
        "创建后容器的端口映射与旧 8289 不一致。",
    )
    report.compare(
        "container.networks",
        sorted(_baseline_networks(baseline)),
        sorted(_created_networks(candidate, baseline=baseline)),
        "创建后容器没有精确连接既有网络。",
    )
    for key in ("User", "WorkingDir", "Entrypoint", "Cmd", "Healthcheck"):
        report.compare(
            f"container.config.{key}",
            baseline["Config"].get(key),
            candidate["Config"].get(key),
            "创建后容器的镜像运行合同与旧 8289 不一致。",
        )
    report.compare(
        "container.image",
        target_image["Id"],
        candidate["Image"],
        "创建后容器没有绑定本批锁定镜像 ID。",
    )
    name = str(candidate.get("Name", "")).removeprefix("/")
    if name != _CANDIDATE_CONTAINER:
        report.add_problem(
            "semantic_mismatches",
            "container.name",
            "候选容器名不是固定的独立名称。",
            baseline=_CANDIDATE_CONTAINER,
            candidate=name,
        )
    for network, settings in candidate["NetworkSettings"]["Networks"].items():
        aliases = set(settings.get("Aliases") or [])
        if "wanshitong-app" in aliases:
            report.add_problem(
                "unclassified_changes",
                f"container.networks.{network}.aliases",
                "候选不得复用旧 app 的网络别名。",
                candidate=sorted(aliases),
            )
    report.allow(
        "container.id",
        "新候选容器身份是本批声明差异。",
        baseline=baseline.get("Id"),
        candidate=candidate.get("Id"),
    )
    report.allow(
        "container.name",
        "候选使用独立名称，不接管旧 8289 容器。",
        baseline=baseline.get("Name"),
        candidate=candidate.get("Name"),
    )
    report.allow(
        "container.compose_labels",
        "新候选由固定 Compose project 管理。",
        baseline=baseline["Config"].get("Labels"),
        candidate=candidate["Config"].get("Labels"),
    )
    _compare_image_contract(report, baseline, target_image)


def compare_runtime(  # noqa: PLR0913
    *,
    stage: Stage,
    baseline_inspect: Path,
    candidate_input: Path,
    target_image_inspect: Path,
    candidate_root: PurePosixPath,
    forbidden_root: PurePosixPath,
    allow_department_shadow_enable: bool = False,
    allow_sso_enable: bool = False,
    allow_popular_questions_toggle: bool = False,
) -> dict[str, object]:
    """比较候选描述或创建后容器，返回无秘密的字段级报告。

    Args:
        stage: 当前检查层次。
        baseline_inspect: 旧 8289 的私有 `docker inspect` JSON。
        candidate_input: 渲染 Compose 或新容器 inspect JSON。
        target_image_inspect: 本批目标镜像 inspect JSON。
        candidate_root: 本批独立宿主根目录。
        forbidden_root: 不得写入的生产宿主根目录。
        allow_department_shadow_enable: 是否允许唯一的 Shadow 开关启用差异。
        allow_sso_enable: 是否允许并严格校验阶段 03 的 SSO 切换差异。
        allow_popular_questions_toggle: 是否允许 F06 公共问题开关的声明差异。

    Returns:
        含逐类差异、各层摘要和 `ready` 判定的安全报告。

    """
    baseline = _inspect_object(baseline_inspect)
    target_image = _inspect_object(target_image_inspect)
    report = _Report(stage=stage)
    baseline_environment = _application_environment(
        _environment(baseline["Config"]["Env"])
    )
    baseline_mounts = _baseline_mounts(baseline)
    candidate_payload = _load_json(candidate_input)
    if stage == "rendered_spec":
        if not isinstance(candidate_payload, dict):
            raise ValueError("RENDERED_SPEC_INVALID")
        rendered = cast(dict[str, Any], candidate_payload)
        service = rendered.get("services", {}).get("app")
        if not isinstance(service, dict):
            raise ValueError("RENDERED_APP_SERVICE_MISSING")
        service = cast(dict[str, Any], service)
        candidate_environment = {
            key: _normalized_text(value)
            for key, value in service.get("environment", {}).items()
        }
        candidate_mounts = _rendered_mounts(service)
        _compare_rendered_runtime(
            report, baseline, rendered, service, target_image
        )
    else:
        candidate = _inspect_object(candidate_input)
        candidate_environment = _application_environment(
            _environment(candidate["Config"]["Env"])
        )
        candidate_mounts = _created_mounts(candidate)
        _compare_created_runtime(report, baseline, candidate, target_image)
    _compare_environment(
        report,
        baseline_environment,
        candidate_environment,
        allow_department_shadow_enable=allow_department_shadow_enable,
        allow_sso_enable=allow_sso_enable,
        allow_popular_questions_toggle=allow_popular_questions_toggle,
    )
    _compare_mounts(
        report,
        baseline_mounts,
        candidate_mounts,
        candidate_root,
        forbidden_root,
    )
    layer_sha256 = {
        "baseline_created_container": _digest(baseline),
        stage: _digest(candidate_payload),
        "target_image": _digest(target_image),
        "baseline_application_environment": _digest(baseline_environment),
        "candidate_application_environment": _digest(candidate_environment),
    }
    return report.result(layer_sha256)


def _dotenv_quote(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("ENV_VALUE_MULTILINE_OR_NUL")
    return "'" + value.replace("'", "\\'") + "'"


def write_candidate_environment(  # noqa: PLR0913
    *,
    baseline_inspect: Path,
    output: Path,
    image: str,
    candidate_root: PurePosixPath,
    secret_directory: PurePosixPath,
    internal_network: str,
    egress_network: str,
    department_shadow_enabled: bool = False,
) -> None:
    """从旧 8289 最终环境生成权限受限的唯一候选环境文件。

    Args:
        baseline_inspect: 旧 8289 的私有 inspect JSON。
        output: 必须尚不存在的输出路径。
        image: 已构建并核对的候选镜像标签。
        candidate_root: 本批独立宿主根目录。
        secret_directory: 已批准的只读 Secret 宿主目录。
        internal_network: 既有内部网络精确名称。
        egress_network: 既有出口网络精确名称。
        department_shadow_enabled: 是否显式启用阶段 04 观察型 Shadow。

    Raises:
        FileExistsError: 输出文件已存在时抛出。
        ValueError: 基准环境或输出值不能安全表达时抛出。

    """
    baseline = _inspect_object(baseline_inspect)
    environment = _application_environment(
        _environment(baseline["Config"]["Env"])
    )
    if not environment or "RAG_PRODUCT_MODE" not in environment:
        raise ValueError("BASELINE_APPLICATION_ENVIRONMENT_INCOMPLETE")
    if department_shadow_enabled:
        baseline_shadow = environment.get(_DEPARTMENT_SHADOW_KEY)
        if baseline_shadow not in (None, "false"):
            raise ValueError("BASELINE_DEPARTMENT_SHADOW_NOT_DISABLED")
        environment[_DEPARTMENT_SHADOW_KEY] = "true"
    interpolation = {
        "RAG_APP_IMAGE": image,
        "CANDIDATE_BIND_ADDRESS": "127.0.0.1",
        "CANDIDATE_PORT": "8289",
        "CANDIDATE_DATA_DIR": str(candidate_root / "data"),
        "CANDIDATE_LOG_DIR": str(candidate_root / "logs"),
        "CANDIDATE_DIAGNOSTIC_DIR": str(candidate_root / "private-diagnostics"),
        "WANSHITONG_SECRET_DIR": str(secret_directory),
        "WANSHITONG_INTERNAL_NETWORK": internal_network,
        "WANSHITONG_EGRESS_NETWORK": egress_network,
    }
    collisions = set(environment) & set(interpolation)
    if collisions:
        raise ValueError(
            "ENVIRONMENT_INTERPOLATION_COLLISION:" + ",".join(collisions)
        )
    values = {**environment, **interpolation}
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for key in sorted(values):
            stream.write(f"{key}={_dotenv_quote(values[key])}\n")


def _write_report(path: Path, report: dict[str, object]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_env = subparsers.add_parser("write-env")
    write_env.add_argument("--baseline-inspect", type=Path, required=True)
    write_env.add_argument("--output", type=Path, required=True)
    write_env.add_argument("--image", required=True)
    write_env.add_argument(
        "--candidate-root", type=PurePosixPath, required=True
    )
    write_env.add_argument(
        "--secret-directory", type=PurePosixPath, required=True
    )
    write_env.add_argument("--internal-network", required=True)
    write_env.add_argument("--egress-network", required=True)
    write_env.add_argument(
        "--department-shadow-enabled",
        action="store_true",
    )

    compare = subparsers.add_parser("compare")
    compare.add_argument(
        "--stage",
        choices=("rendered_spec", "created_container"),
        required=True,
    )
    compare.add_argument("--baseline-inspect", type=Path, required=True)
    compare.add_argument("--candidate-input", type=Path, required=True)
    compare.add_argument("--target-image-inspect", type=Path, required=True)
    compare.add_argument("--candidate-root", type=PurePosixPath, required=True)
    compare.add_argument("--forbidden-root", type=PurePosixPath, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument(
        "--allow-department-shadow-enable",
        action="store_true",
    )
    compare.add_argument("--allow-sso-enable", action="store_true")
    compare.add_argument(
        "--allow-popular-questions-toggle", action="store_true"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行候选环境生成或字段级运行门禁。"""
    arguments = _parser().parse_args(argv)
    if arguments.command == "write-env":
        write_candidate_environment(
            baseline_inspect=arguments.baseline_inspect,
            output=arguments.output,
            image=arguments.image,
            candidate_root=arguments.candidate_root,
            secret_directory=arguments.secret_directory,
            internal_network=arguments.internal_network,
            egress_network=arguments.egress_network,
            department_shadow_enabled=arguments.department_shadow_enabled,
        )
        return 0
    report = compare_runtime(
        stage=arguments.stage,
        baseline_inspect=arguments.baseline_inspect,
        candidate_input=arguments.candidate_input,
        target_image_inspect=arguments.target_image_inspect,
        candidate_root=arguments.candidate_root,
        forbidden_root=arguments.forbidden_root,
        allow_department_shadow_enable=(
            arguments.allow_department_shadow_enable
        ),
        allow_sso_enable=arguments.allow_sso_enable,
        allow_popular_questions_toggle=(
            arguments.allow_popular_questions_toggle
        ),
    )
    _write_report(arguments.output, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
