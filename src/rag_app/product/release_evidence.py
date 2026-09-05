"""P11 发布入口使用的证据身份、风险清单与就绪状态计算。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "NOT_RUN", "NOT_APPLICABLE"})
_TRIVY_SCHEMA_VERSION = 2
_GATE_CHECKS = {
    "AUTHORIZATION_BOUNDARY_READY": ("authorization_tests", "campaign_config"),
    "ALIYUN_ENDPOINT_CONTRACT_READY": ("endpoint_tests", "campaign_config"),
    "CONNECTION_EDIT_READY": ("connection_tests", "web_e2e"),
    "RESOLVED_POLICY_CONFORMANCE_READY": ("conformance_tests",),
    "PROFILE_INDEX_SWITCH_READY": ("publication_tests", "upgrade_tests"),
    "PROVIDER_CONNECTIVITY_READY": (
        "aliyun_document_canary",
        "aliyun_query_canary",
        "jina_connection",
    ),
    "DUAL_SLOT_FUNCTION_READY": ("dual_index", "primary_query", "qdrant"),
    "FAILOVER_RECOVERY_READY": ("standby_failover", "recovery"),
    "RETRIEVAL_QUALITY_READY": ("citation_quality",),
    "PRODUCT_BROWSER_READY": ("web_e2e", "candidate_browser"),
    "BACKUP_RESTORE_READY": ("backup_restore", "upgrade_tests"),
    "SECURITY_READY": (
        "security_matrix",
        "secret_scan",
        "image_secret_scan",
        "python_dependency_audit",
        "npm_dependency_audit",
        "os_risk",
    ),
    "CI_READY": ("ci",),
}
_REMOTE_GATES = (
    "ALIYUN_ENDPOINT_CONTRACT_READY",
    "RESOLVED_POLICY_CONFORMANCE_READY",
    "PROFILE_INDEX_SWITCH_READY",
    "PROVIDER_CONNECTIVITY_READY",
    "DUAL_SLOT_FUNCTION_READY",
    "FAILOVER_RECOVERY_READY",
    "RETRIEVAL_QUALITY_READY",
)
_SOURCE_IDENTITY = ("runtime", "tests", "migrations")
_RUNTIME_IDENTITY = ("runtime", "migrations", "image", "evaluation")
_IMAGE_IDENTITY = (*_RUNTIME_IDENTITY, "image_id", "frontend")
_CHECK_IDENTITIES = {
    **dict.fromkeys(
        (
            "authorization_tests",
            "endpoint_tests",
            "conformance_tests",
            "publication_tests",
            "upgrade_tests",
            "security_matrix",
        ),
        _SOURCE_IDENTITY,
    ),
    **dict.fromkeys(
        ("connection_tests", "web_e2e"), (*_SOURCE_IDENTITY, "frontend")
    ),
    "campaign_config": ("runtime", "migrations"),
    "aliyun_document_canary": ("provider_aliyun",),
    "aliyun_query_canary": ("provider_aliyun",),
    "jina_connection": ("provider_jina",),
    **dict.fromkeys(
        (
            "dual_index",
            "primary_query",
            "standby_failover",
            "recovery",
            "citation_quality",
        ),
        _RUNTIME_IDENTITY,
    ),
    **dict.fromkeys(
        ("candidate_startup", "candidate_browser"), _IMAGE_IDENTITY
    ),
    **dict.fromkeys(("qdrant", "backup_restore"), (*_SOURCE_IDENTITY, "image")),
    "image_secret_scan": _IMAGE_IDENTITY,
    "secret_scan": ("runtime", "frontend", "evaluation", "release"),
    "python_dependency_audit": ("image",),
    "npm_dependency_audit": ("frontend",),
    "os_risk": ("image", "image_id"),
    "ci": (*_SOURCE_IDENTITY, "frontend", "image", "evaluation", "release"),
}
_OFFLINE_CHECKS = ("doctor", "check", "smoke", "product_check", "product_smoke")
_CHECK_IDENTITIES.update(dict.fromkeys(_OFFLINE_CHECKS, _SOURCE_IDENTITY))
_FRONTEND_CHECKS = ("web_lint", "web_typecheck", "web_test", "web_e2e")
_CHECK_IDENTITIES.update(
    dict.fromkeys(_FRONTEND_CHECKS, (*_SOURCE_IDENTITY, "frontend"))
)


def component_identity(root: Path, paths: Sequence[str]) -> dict[str, str]:
    """对受 Git 管理的业务资产分组计算身份，文档提交不改变业务身份。

    Args:
        root: 仓库根目录。
        paths: 现有发布入口使用 git ls-files 得到的受管理文件。

    Returns:
        代码、测试、前端、镜像依赖及迁移的内容摘要。

    """
    groups: dict[str, list[str]] = {
        "runtime": [],
        "tests": [],
        "frontend": [],
        "image": [],
        "migrations": [],
        "release": [],
        "evaluation": [],
    }
    for name in sorted(filter(None, paths)):
        if name.startswith("src/"):
            groups["runtime"].append(name)
        if name.startswith("tests/"):
            groups["tests"].append(name)
        if (
            name.startswith("frontend/")
            or name == "docs/public/openapi-v1.json"
        ):
            groups["frontend"].append(name)
        if (
            name.startswith("migrations/")
            or name == "compatibility-manifest.json"
        ):
            groups["migrations"].append(name)
        if name.startswith(("scripts/", ".github/workflows/p11")):
            groups["release"].append(name)
        if name.startswith("evaluation/"):
            groups["evaluation"].append(name)
        if name in {
            "Dockerfile",
            ".dockerignore",
            "Dockerfile.dockerignore",
            "requirements.runtime.lock",
            "requirements.lock",
            "pyproject.toml",
            "frontend/package-lock.json",
            "compose.yaml",
        }:
            groups["image"].append(name)
    return {
        group: _content_digest(root, names) for group, names in groups.items()
    }


def _content_digest(root: Path, names: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def provider_operation_identities(root: Path) -> dict[str, str]:
    """按真实请求依赖绑定连接证据，前端变化不触发付费重验。

    Args:
        root: 仓库根目录。

    Returns:
        Jina 和百炼 canary 操作依赖的内容身份。

    """
    providers = root / "src/rag_app/adapters/providers"
    shared = [
        "src/rag_app/product/provider_runtime.py",
        "src/rag_app/product/verification.py",
        "src/rag_app/product/resolved_profile.py",
        "src/rag_app/product/credential_store.py",
        "src/rag_app/product/crypto.py",
        "src/rag_app/product/catalog.py",
        "src/rag_app/core/tokenization.py",
    ]
    hashes = {}
    for provider in ("jina", "aliyun"):
        names = shared + [
            path.relative_to(root).as_posix()
            for path in providers.glob("*.py")
            if not (
                (provider == "jina" and path.name.startswith("aliyun"))
                or (provider == "aliyun" and path.name == "jina.py")
            )
        ]
        hashes[provider] = _content_digest(root, sorted(names))
    return {
        "jina_connection": hashes["jina"],
        "aliyun_document_canary": hashes["aliyun"],
        "aliyun_query_canary": hashes["aliyun"],
    }


def combine(records: Sequence[Mapping[str, object]]) -> str:
    """按必需证据计算统一状态，缺记录或局部通过均不能变绿。

    Args:
        records: 必需子项记录。

    Returns:
        统一状态枚举文本。

    """
    if not records:
        return "NOT_RUN"
    statuses = {record.get("status", "NOT_RUN") for record in records}
    if not statuses <= _STATUSES or "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "NOT_RUN" in statuses:
        return "NOT_RUN"
    return "PASS" if "PASS" in statuses else "NOT_APPLICABLE"


def evidence_identity(name: str, identity: Mapping[str, str]) -> dict[str, str]:
    """选择检查的必需身份维度，防止用源码身份替代镜像身份。

    Args:
        name: 检查名称。
        identity: 当前全部组件和资产身份。

    Returns:
        检查实际依赖的身份字段；缺维度会由报告校验阻断。

    """
    required = _CHECK_IDENTITIES.get(name, _SOURCE_IDENTITY)
    return {key: identity[key] for key in required if key in identity}


def _checked_record(
    name: str,
    record: Mapping[str, object],
    identity: Mapping[str, str],
    root: Path,
) -> dict[str, object]:
    result = dict(record)
    if record.get("status") == "NOT_APPLICABLE" and name in _CHECK_IDENTITIES:
        result.update(
            status="BLOCKED", reason="REQUIRED_CHECK_CANNOT_BE_WAIVED"
        )
        return result
    if record.get("status") != "PASS":
        return result
    bound = record.get("identity")
    if (
        not isinstance(bound, dict)
        or not bound
        or any(key not in bound for key in _CHECK_IDENTITIES.get(name, ()))
        or any(value in {"", "unavailable"} for value in bound.values())
        or any(identity.get(key) != value for key, value in bound.items())
    ):
        result.update(status="BLOCKED", reason="EVIDENCE_IDENTITY_MISMATCH")
        return result
    evidence = record.get("evidence")
    expected_hash = record.get("sha256")
    if not isinstance(evidence, str) or not isinstance(expected_hash, str):
        result.update(status="BLOCKED", reason="EVIDENCE_ARTIFACT_MISSING")
        return result
    path = root / evidence
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash
    ):
        result.update(status="BLOCKED", reason="EVIDENCE_ARTIFACT_CHANGED")
    if record.get("origin") not in {"本次执行", "有效复用"}:
        result.update(status="BLOCKED", reason="EVIDENCE_ORIGIN_UNVERIFIED")
    if record.get("exit_code") != 0:
        result.update(status="FAIL", reason="EVIDENCE_COMMAND_FAILED")
    return result


def build_report(
    root: Path,
    evidence: Mapping[str, object],
    identity: Mapping[str, str],
) -> dict[str, object]:
    """由已验证证据聚合发布门，保留所有未运行和阻断项。

    Args:
        root: 相对证据路径的根目录。
        evidence: 检查记录、资产及脱敏预算。
        identity: 当前组件和可选镜像身份。

    Returns:
        不包含密钥、正文或向量的验收报告。

    """
    raw_checks = cast(dict[str, dict[str, object]], evidence.get("checks", {}))
    checks = {
        name: _checked_record(name, value, identity, root)
        for name, value in raw_checks.items()
    }
    gates: dict[str, dict[str, object]] = {}
    for name, required in _GATE_CHECKS.items():
        children = [
            checks.get(item, {"status": "NOT_RUN"}) for item in required
        ]
        gates[name] = _gate(children, required, identity)
    gates["REMOTE_PRODUCTION_PROFILE_READY"] = _gate(
        [gates[name] for name in _REMOTE_GATES],
        _REMOTE_GATES,
        identity,
    )
    candidate_requirements = (*_GATE_CHECKS, "REMOTE_PRODUCTION_PROFILE_READY")
    gates["RELEASE_CANDIDATE_READY"] = _gate(
        [gates[name] for name in candidate_requirements]
        + [
            checks.get(name, {"status": "NOT_RUN"})
            for name in (
                *_OFFLINE_CHECKS,
                *_FRONTEND_CHECKS,
                "candidate_startup",
            )
        ],
        (
            *candidate_requirements,
            *_OFFLINE_CHECKS,
            *_FRONTEND_CHECKS,
            "candidate_startup",
        ),
        identity,
    )
    gates["P11_READY"] = _gate(
        [gates["RELEASE_CANDIDATE_READY"]],
        ("RELEASE_CANDIDATE_READY",),
        identity,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": dict(identity),
        "assets": evidence.get("assets", {}),
        "budget": evidence.get("budget", {"status": "NOT_RUN"}),
        "checks": checks,
        "gates": gates,
        "P11_READY": gates["P11_READY"]["status"] == "PASS",
        "MERGE_TO_MAIN_AUTHORIZED": False,
        "limitations": evidence.get("limitations", []),
        "private_documents_sent": evidence.get(
            "private_documents_sent", "unknown"
        ),
    }


def _gate(
    children: Sequence[Mapping[str, object]],
    names: Sequence[str],
    identity: Mapping[str, str],
) -> dict[str, object]:
    missing = [
        name
        for name, child in zip(names, children, strict=True)
        if child.get("status") not in {"PASS", "NOT_APPLICABLE"}
    ]
    return {
        "status": combine(children),
        "reason": ", ".join(missing) or "ALL_REQUIRED_PASSED",
        "evidence": list(names),
        "identity": dict(identity),
    }


def write_report(report: Mapping[str, object], output: Path) -> None:
    """同时写出机器报告和简明 Markdown，不重复昂贵验收。

    Args:
        report: 已计算的完整报告。
        output: JSON 目标，Markdown 使用相同文件名主体。

    Returns:
        文件写入完成时无返回值。

    """
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    gates = cast(dict[str, dict[str, object]], report["gates"])
    lines = [
        "# P11-R4 验收",
        "",
        f"P11_READY={report['P11_READY']}",
        "",
        "| Gate | 状态 | 原因 / 待补证据 |",
        "| --- | --- | --- |",
        *(
            f"| {name} | {record['status']} | {record['reason']} |"
            for name, record in gates.items()
        ),
        "",
        "预算与用量：",
        "",
        "```json",
        json.dumps(report["budget"], ensure_ascii=False, indent=2),
        "```",
        "",
        "限制：",
        "",
        *(
            f"- {item}"
            for item in cast(list[str], report.get("limitations", []))
        ),
        "",
        "详细证据来源、命令退出码、资产身份见同名 JSON。",
        "MERGE_TO_MAIN_AUTHORIZED=false。",
        "",
    ]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def vulnerability_report(payload: Mapping[str, object]) -> dict[str, object]:
    """分列全部与可修复高危，未知可达性不会自动接受风险。

    Args:
        payload: 完整 Trivy JSON，不是 ignore-unfixed 过滤结果。

    Returns:
        包级风险清单和保守的安全子门状态。

    """
    metadata = payload.get("Metadata")
    results = payload.get("Results")
    if (
        payload.get("SchemaVersion") != _TRIVY_SCHEMA_VERSION
        or payload.get("ArtifactType") != "container_image"
        or not isinstance(metadata, dict)
        or not isinstance(metadata.get("ImageID"), str)
        or not metadata["ImageID"].startswith("sha256:")
        or not isinstance(metadata.get("OS"), dict)
        or not metadata["OS"].get("Family")
        or not isinstance(results, list)
        or not any(
            isinstance(item, dict) and item.get("Class") == "os-pkgs"
            for item in results
        )
    ):
        return {
            "status": "BLOCKED",
            "reason": "OS_SCAN_INCOMPLETE",
            "all_high_critical": None,
            "fixable_high_critical": None,
            "without_fix": None,
            "findings": [],
        }
    findings: list[dict[str, object]] = []
    for result in cast(list[dict[str, object]], payload.get("Results", [])):
        for item in cast(
            list[dict[str, object]], result.get("Vulnerabilities") or []
        ):
            if item.get("Severity") not in {"HIGH", "CRITICAL"}:
                continue
            findings.append(
                {
                    "id": item.get("VulnerabilityID"),
                    "package": item.get("PkgName"),
                    "installed_version": item.get("InstalledVersion"),
                    "fixed_version": item.get("FixedVersion") or None,
                    "severity": item.get("Severity"),
                    "target": result.get("Target"),
                    "reachability": "NOT_ASSESSED",
                    "mitigation": "NOT_ASSESSED",
                    "risk_accepted": False,
                    "owner": None,
                    "expires_at": None,
                }
            )
    fixable = sum(bool(item["fixed_version"]) for item in findings)
    return {
        "status": "FAIL" if fixable else "BLOCKED" if findings else "PASS",
        "image_id": metadata["ImageID"],
        "reason": "FIXABLE_HIGH_CRITICAL"
        if fixable
        else "RISK_REVIEW_REQUIRED"
        if findings
        else "NO_HIGH_CRITICAL",
        "all_high_critical": len(findings),
        "fixable_high_critical": fixable,
        "without_fix": len(findings) - fixable,
        "findings": findings,
    }
