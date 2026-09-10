"""为新的完整 Trivy 扫描生成不可批准的逐项风险调查草案。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from rag_app.product.release_evidence import vulnerability_report

_ROOT = Path(__file__).resolve().parents[1]
_KEY_FIELDS = ("id", "package", "installed_version")
_TRANSFER_FIELDS = (
    "package_present",
    "loaded_or_called",
    "trigger_conditions",
    "upload_reachability",
    "network_path",
    "reachability",
    "distribution_advisory",
    "mitigation",
    "remaining_impact",
    "recommended_action",
)


def _load(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"风险证据不是 JSON object：{path}")
    return cast(dict[str, object], payload)


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(root.resolve(strict=True)).as_posix()
    except ValueError as error:
        raise ValueError("风险草案证据必须位于仓库受控根目录内。") from error


def _proof(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": _relative(root, path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _key(value: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(value.get(name, "")) for name in _KEY_FIELDS)


def _valid_prior_proofs(
    root: Path, review: Mapping[str, object]
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    evidence = review.get("evidence")
    if not isinstance(evidence, list):
        return result
    for raw in evidence:
        if not isinstance(raw, dict):
            continue
        name = raw.get("path")
        digest = raw.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            continue
        if path.name in {"trivy-all.json", "trivy-db-metadata.json"}:
            continue
        result.append({"path": name, "sha256": digest})
    return result


def build_investigation_draft(
    *,
    root: Path,
    scan_path: Path,
    db_metadata_path: Path,
    previous_review_path: Path | None,
    generated_at: datetime,
) -> dict[str, object]:
    """构造与新扫描绑定、但永远不迁移批准状态的调查草案。

    Args:
        root: 受控证据根目录。
        scan_path: 完整且未过滤的 Trivy JSON。
        db_metadata_path: 同次扫描使用的 Trivy DB metadata。
        previous_review_path: 可选旧调查文本，仅复用技术分析字段。
        generated_at: 草案生成时间。

    Returns:
        包含全部 High/Critical 包版本元组的 `UNDER_INVESTIGATION` 草案。

    Raises:
        ValueError: 输入不是完整扫描或元数据合同无效。

    """
    scan = _load(scan_path)
    database = _load(db_metadata_path)
    extracted = vulnerability_report(scan)
    if extracted.get("reason") == "OS_SCAN_INCOMPLETE":
        raise ValueError("OS_SCAN_INCOMPLETE")
    previous: dict[tuple[str, ...], dict[str, object]] = {}
    previous_scan: dict[str, object] | None = None
    if previous_review_path is not None and previous_review_path.is_file():
        old = _load(previous_review_path)
        raw_previous_scan = old.get("scan")
        if isinstance(raw_previous_scan, dict):
            previous_scan = {
                name: raw_previous_scan.get(name)
                for name in (
                    "image_id",
                    "sha256",
                    "scanned_at",
                    "scanner_version",
                    "db_updated_at",
                )
            }
        reviews = old.get("reviews")
        if isinstance(reviews, list):
            previous = {
                _key(item): cast(dict[str, object], item)
                for item in reviews
                if isinstance(item, dict)
            }
    scan_proof = _proof(root, scan_path)
    database_proof = _proof(root, db_metadata_path)
    scanner = cast(dict[str, object], scan.get("Trivy") or {})
    metadata = cast(dict[str, object], scan.get("Metadata") or {})
    identity = {
        "image_id": metadata.get("ImageID"),
        "sha256": scan_proof["sha256"],
        "scanned_at": scan.get("CreatedAt"),
        "scanner_version": scanner.get("Version"),
        "db_updated_at": database.get("UpdatedAt"),
        "db_metadata": database_proof,
    }
    reviews_output: list[dict[str, object]] = []
    reused_assessments = 0
    findings = cast(list[dict[str, object]], extracted.get("findings", []))
    for finding in findings:
        prior = previous.get(_key(finding), {})
        if prior:
            reused_assessments += 1
        review = {
            name: prior.get(name, "NOT_ASSESSED") for name in _TRANSFER_FIELDS
        }
        review.update(
            {
                name: finding.get(name)
                for name in (
                    "id",
                    "package",
                    "installed_version",
                    "source_package",
                    "source_version",
                    "severity",
                    "vulnerability_status",
                    "fixed_version",
                    "purl",
                    "arch",
                    "target",
                    "target_class",
                    "target_type",
                    "layer_digest",
                    "layer_diff_id",
                    "raw_mapping",
                )
            }
        )
        evidence = _valid_prior_proofs(root, prior)
        evidence.extend((scan_proof, database_proof))
        technical_action = "INVESTIGATE"
        old_disposition = prior.get("closure_disposition")
        if isinstance(old_disposition, dict) and isinstance(
            old_disposition.get("technical_action"), str
        ):
            technical_action = old_disposition["technical_action"]
        review.update(
            conclusion="UNDER_INVESTIGATION",
            reviewed_at=generated_at.isoformat(),
            scope="P11_RELEASE",
            owner=None,
            approver=None,
            expires_at=None,
            risk_accepted=False,
            evidence=evidence,
            closure_disposition={
                "assessment_status": "ASSESSED_PENDING_HUMAN_DECISION",
                "approval_status": "NOT_APPROVED",
                "technical_action": technical_action,
                "gate_disposition": "NONE",
            },
        )
        reviews_output.append(review)
    summary = {
        name: extracted.get(name)
        for name in (
            "raw_findings",
            "raw_high_critical",
            "unique_cves",
            "unique_cve_package_versions",
            "fixable_high_critical",
            "without_fix",
        )
    }
    summary.update(
        reviewed_findings=len(reviews_output),
        under_investigation=len(reviews_output),
        mitigated=0,
        approved_dispositions=0,
        objective_dispositions=0,
        status="BLOCKED",
    )
    return {
        "schema_version": 1,
        "review_status": "ASSESSED_PENDING_HUMAN_DECISION",
        "generated_at": generated_at.isoformat(),
        "scan": identity,
        "scan_path": scan_proof["path"],
        "scan_artifact": {
            "trivy_artifact_id": extracted.get("artifact_id"),
            "artifact_name": extracted.get("artifact_name"),
            "image_id": extracted.get("image_id"),
            "local_repo_digests": extracted.get("local_repo_digests"),
            "registry_manifest_digest": None,
            "manifest_scope": "local_image_store_only",
            "platform": extracted.get("platform"),
        },
        "technical_assessment_reuse": {
            "status": (
                "PORTED_UNDER_INVESTIGATION"
                if reused_assessments
                else "NOT_USED"
            ),
            "matched_tuple_count": reused_assessments,
            "source_scan": previous_scan,
            "approval_state_transferred": False,
            "requires_human_revalidation": bool(reused_assessments),
        },
        "summary": summary,
        "reviews": reviews_output,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--db-metadata", type=Path, required=True)
    parser.add_argument("--previous-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """生成未批准调查草案。

    Args:
        arguments: 可选命令行参数。

    Returns:
        成功为 0，输入不满足证据合同时为 2。

    """
    args = _parser().parse_args(arguments)
    try:
        draft = build_investigation_draft(
            root=_ROOT,
            scan_path=args.scan,
            db_metadata_path=args.db_metadata,
            previous_review_path=args.previous_review,
            generated_at=datetime.now(UTC),
        )
    except (OSError, ValueError, TypeError) as error:
        print(f"BLOCKED risk-draft: {error}")
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"UNDER_INVESTIGATION risk-draft={args.output} "
        f"findings={len(cast(list[object], draft['reviews']))} approved=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
