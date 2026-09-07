"""对完整扫描应用绑定镜像、文件证据和有效期的本地风险处置。

此入口只读取受发布管理员控制的本地文件。普通 HTTP 请求不能上传或激活
overlay；人工接受必须另附实际批准记录，计划及调查记录均不构成批准。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_MAX_REVIEWS = 10000
_MAX_TEXT_LENGTH = 4096
_FIRST_PRINTABLE = 32
_RELEASE_SCOPE = "P11_RELEASE"
_KEY_FIELDS = ("id", "package", "installed_version")
_IDENTITY_FIELDS = (
    "image_id",
    "sha256",
    "scanned_at",
    "scanner_version",
    "db_updated_at",
)
_DEBIAN_RELEASES = {"12": "bookworm", "13": "trixie"}
_TRACKER_URL = "https://security-tracker.debian.org/tracker/data/json"


@dataclass(frozen=True)
class ReviewContext:
    """发布入口提供的本地证据边界，不执行外部调用。"""

    overlay: Mapping[str, object] | None
    root: Path | None
    scan_path: Path | None
    now: datetime


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("REVIEW_OBJECT_REQUIRED")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_TEXT_LENGTH
        or any(ord(char) < _FIRST_PRINTABLE for char in value)
    ):
        raise ValueError("REVIEW_TEXT_REQUIRED")
    return value


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("REVIEW_TIMEZONE_REQUIRED")
    return parsed.astimezone(UTC)


def _key(value: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(_text(value.get(name)) for name in _KEY_FIELDS)


def _read_proof(root: Path, value: object) -> bytes:
    proof = _object(value)
    name = _text(proof.get("path"))
    digest = _text(proof.get("sha256"))
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("REVIEW_EVIDENCE_OUTSIDE_ROOT")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("REVIEW_EVIDENCE_HASH_MISMATCH")
    return content


def _scan_binding(
    payload: Mapping[str, object], context: ReviewContext
) -> dict[str, object]:
    if context.root is None or context.scan_path is None:
        raise ValueError("REVIEW_SCAN_EVIDENCE_REQUIRED")
    overlay = context.overlay or {}
    binding = _object(overlay.get("scan"))
    raw = context.scan_path.read_bytes()
    if json.loads(raw) != payload:
        raise ValueError("REVIEW_SCAN_PAYLOAD_MISMATCH")
    metadata = _object(payload.get("Metadata"))
    scanner = _object(payload.get("Trivy"))
    database = _object(
        json.loads(_read_proof(context.root, binding.get("db_metadata")))
    )
    identity = {
        "image_id": metadata.get("ImageID"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "scanned_at": payload.get("CreatedAt"),
        "scanner_version": scanner.get("Version"),
        "db_updated_at": database.get("UpdatedAt"),
    }
    if any(binding.get(name) != identity[name] for name in _IDENTITY_FIELDS):
        raise ValueError("REVIEW_SCAN_IDENTITY_MISMATCH")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", _text(identity["image_id"])):
        raise ValueError("REVIEW_IMAGE_DIGEST_INVALID")
    _text(identity["scanner_version"])
    if not (
        _time(identity["db_updated_at"])
        <= _time(database.get("DownloadedAt"))
        <= _time(identity["scanned_at"])
        <= context.now
    ):
        raise ValueError("REVIEW_SCAN_TIME_INVALID")
    return identity


def _index_reviews(
    overlay: Mapping[str, object], findings: list[dict[str, object]]
) -> dict[tuple[str, ...], dict[str, object]]:
    reviews = overlay.get("reviews")
    if not isinstance(reviews, list) or len(reviews) > _MAX_REVIEWS:
        raise ValueError("REVIEW_LIST_INVALID")
    known = {_key(item) for item in findings}
    indexed: dict[tuple[str, ...], dict[str, object]] = {}
    for raw in reviews:
        item = _object(raw)
        key = _key(item)
        if key not in known or key in indexed:
            raise ValueError("REVIEW_FINDING_MISMATCH_OR_DUPLICATE")
        indexed[key] = item
    return indexed


def _validate_evidence(
    review: Mapping[str, object], context: ReviewContext
) -> None:
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence or context.root is None:
        raise ValueError("REVIEW_EVIDENCE_REQUIRED")
    for proof in evidence:
        _read_proof(context.root, proof)


def _objective_disposition(
    review: Mapping[str, object],
    finding: Mapping[str, object],
    report: Mapping[str, object],
    root: Path,
) -> None:
    """只接受发行版结构化结论；自由文本不可成为自动豁免。"""
    proof = _object(review.get("objective_evidence"))
    if proof.get("source_url") != _TRACKER_URL:
        raise ValueError("REVIEW_OFFICIAL_EVIDENCE_REQUIRED")
    tracker = _object(json.loads(_read_proof(root, proof)))
    source = _object(tracker.get(_text(finding.get("source_package"))))
    cve = _object(source.get(_text(finding.get("id"))))
    releases = _object(cve.get("releases"))
    os_info = _object(report.get("os"))
    version = _text(os_info.get("Name")).split(".", 1)[0]
    release = _DEBIAN_RELEASES.get(version)
    if os_info.get("Family") != "debian" or release is None:
        raise ValueError("REVIEW_DISTRIBUTION_UNSUPPORTED")
    record = _object(releases.get(release))
    fixed = record.get("fixed_version")
    expected = (
        "0"
        if review.get("conclusion") == "NOT_AFFECTED_WITH_EVIDENCE"
        else finding.get("source_version")
    )
    if record.get("status") != "resolved" or fixed != expected or not fixed:
        raise ValueError("REVIEW_OFFICIAL_CONCLUSION_UNCONFIRMED")


def _human_approval(
    review: Mapping[str, object],
    identity: Mapping[str, object],
    context: ReviewContext,
) -> dict[str, object]:
    if context.root is None:
        raise ValueError("REVIEW_EVIDENCE_REQUIRED")
    if not isinstance(review.get("approval_evidence"), dict):
        raise ValueError("REVIEW_HUMAN_APPROVAL_REQUIRED")
    approval = _object(
        json.loads(_read_proof(context.root, review.get("approval_evidence")))
    )
    if (
        approval.get("schema_version") != 1
        or approval.get("kind") != "os-risk-acceptance"
        or approval.get("status") != "APPROVED"
        or approval.get("approval_source") != "local_administrator"
        or approval.get("scan") != identity
        or approval.get("review_sha256") != disposition_digest(review)
        or _key(_object(approval.get("finding"))) != _key(review)
    ):
        raise ValueError("REVIEW_HUMAN_APPROVAL_REQUIRED")
    for name in ("approval_reference", "approver", "owner", "scope"):
        _text(approval.get(name))
    if (
        approval.get("scope") != review.get("scope")
        or approval.get("owner") != review.get("owner")
        or not (
            _time(review.get("reviewed_at"))
            <= _time(approval.get("approved_at"))
            <= context.now
        )
        or _time(approval.get("expires_at")) <= context.now
        or _time(approval.get("expires_at")) > _time(review.get("expires_at"))
    ):
        raise ValueError("REVIEW_APPROVAL_SCOPE_OR_EXPIRY_INVALID")
    return approval


def _apply_disposition(
    finding: dict[str, object],
    review: Mapping[str, object],
    report: Mapping[str, object],
    identity: Mapping[str, object],
    context: ReviewContext,
) -> None:
    conclusion = review.get("conclusion")
    if conclusion == "UNDER_INVESTIGATION":
        finding["review_reason"] = "REVIEW_UNDER_INVESTIGATION"
        return
    _validate_evidence(review, context)
    if not (
        _time(identity["scanned_at"])
        <= _time(review.get("reviewed_at"))
        <= context.now
        < _time(review.get("expires_at"))
    ):
        raise ValueError("REVIEW_EXPIRED_OR_FUTURE")
    for name in ("reachability", "trigger_conditions", "scope"):
        _text(review.get(name))
    if review.get("scope") != _RELEASE_SCOPE:
        raise ValueError("REVIEW_SCOPE_INVALID")
    if review.get("reachability") in {
        "UNKNOWN",
        "NOT_ASSESSED",
        "UNDER_INVESTIGATION",
    }:
        raise ValueError("REVIEW_REACHABILITY_UNKNOWN")
    if conclusion in {"NOT_AFFECTED_WITH_EVIDENCE", "FIXED"}:
        if context.root is None:
            raise ValueError("REVIEW_EVIDENCE_REQUIRED")
        _objective_disposition(review, finding, report, context.root)
    elif conclusion == "AFFECTED_MITIGATED":
        if finding.get("fixed_version"):
            raise ValueError("REVIEW_FIXABLE_VULNERABILITY_REQUIRES_FIX")
        _text(review.get("mitigation"))
        approval = _human_approval(review, identity, context)
        finding.update(
            risk_accepted=True,
            approver=approval["approver"],
            approval_reference=approval["approval_reference"],
        )
    else:
        # 当前扫描仍含此包版本，不能将自由文本 REMOVED 视为删除证据。
        raise ValueError("REVIEW_CONCLUSION_CONFLICTS_WITH_SCAN")
    finding.update(
        disposition=conclusion,
        review_valid=True,
        review_reason="VALID_DISPOSITION",
        reachability=review["reachability"],
        mitigation=review.get("mitigation"),
        owner=review.get("owner"),
        expires_at=review["expires_at"],
        scope=review["scope"],
    )


def apply_review(
    report: dict[str, object],
    payload: Mapping[str, object],
    context: ReviewContext,
) -> dict[str, object]:
    """应用有效处置并保留原始总数，错误证据只能阻断安全门。

    Args:
        report: 已从完整扫描提取的风险清单。
        payload: 原始完整扫描。
        context: 本地 overlay、证据根目录和统一时钟。

    Returns:
        仍使用 PASS、FAIL、BLOCKED 状态的完整风险报告。

    """
    findings = cast(list[dict[str, object]], report["findings"])
    for finding in findings:
        finding.update(
            disposition="UNDER_INVESTIGATION",
            review_valid=False,
            review_reason="REVIEW_NOT_PROVIDED",
        )
    errors: list[str] = []
    overlay = context.overlay
    if overlay is not None:
        try:
            if overlay.get("schema_version") != 1:
                raise ValueError("REVIEW_SCHEMA_INVALID")
            identity = _scan_binding(payload, context)
            indexed = _index_reviews(overlay, findings)
            report["scan_identity"] = identity
        except (ValueError, OSError, TypeError) as error:
            errors.append(_safe_error(error))
        else:
            for finding in findings:
                review = indexed.get(_key(finding))
                if review is None:
                    continue
                try:
                    _apply_disposition(
                        finding, review, report, identity, context
                    )
                except (ValueError, OSError, TypeError) as error:
                    finding["review_reason"] = _safe_error(error)
                    errors.append(_safe_error(error))
    unresolved = [item for item in findings if not item["review_valid"]]
    fixable = sum(bool(item["fixed_version"]) for item in unresolved)
    accepted = sum(item["risk_accepted"] is True for item in findings)
    status = (
        "FAIL" if fixable else "BLOCKED" if unresolved or errors else "PASS"
    )
    report.update(
        status=status,
        reason=(
            "FIXABLE_HIGH_CRITICAL"
            if fixable
            else "RISK_REVIEW_REQUIRED"
            if unresolved or errors
            else "ALL_RISKS_DISPOSED_WITH_ACCEPTED_RISK"
            if accepted
            else "ALL_RISKS_DISPOSED"
            if findings
            else "NO_HIGH_CRITICAL"
        ),
        under_investigation=len(unresolved),
        mitigated=sum(
            item["disposition"] == "AFFECTED_MITIGATED" for item in findings
        ),
        approved_dispositions=accepted,
        objective_dispositions=sum(
            item["review_valid"] is True and not item["risk_accepted"]
            for item in findings
        ),
        accepted_risk_remaining=accepted > 0,
        review_errors=sorted(set(errors)),
    )
    return report


def _safe_error(error: ValueError | OSError | TypeError) -> str:
    message = str(error)
    if isinstance(error, ValueError) and re.fullmatch(
        r"REVIEW_[A-Z_]+", message
    ):
        return message
    return f"REVIEW_EVIDENCE_INVALID_{type(error).__name__}"


def disposition_digest(review: Mapping[str, object]) -> str:
    """对人工实际批准的处置内容取摘要，防止批准后更换缓解和证据。

    Args:
        review: 除批准附件引用外的完整处置记录。

    Returns:
        写入独立人工批准附件的 SHA-256。

    """
    content = {
        key: value
        for key, value in review.items()
        if key != "approval_evidence"
    }
    return hashlib.sha256(
        json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def load_review_inputs(
    record: Mapping[str, object], root: Path
) -> tuple[dict[str, object], dict[str, object], Path]:
    """读取最终发布门必须重新计算的完整扫描和处置输入。

    Args:
        record: os_risk 检查记录，details 包含原始文件路径和摘要。
        root: 允许读取的发布证据根目录。

    Returns:
        完整扫描、overlay 和扫描路径，供当前时钟下的安全聚合重新校验。

    Raises:
        ValueError: 输入缺失、越界、摘要变化或不是 JSON 对象。
        OSError: 所需证据文件无法读取。

    """
    details = _object(record.get("details"))
    scan_ref = _object(details.get("raw_scan"))
    raw = _object(json.loads(_read_proof(root, scan_ref)))
    overlay = _object(
        json.loads(_read_proof(root, details.get("review_overlay")))
    )
    return raw, overlay, (root / _text(scan_ref.get("path"))).resolve()
