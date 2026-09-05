"""完整扫描和风险处置证据不能被计划、未知可达性或失效批准替代。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rag_app.product import release_evidence
from rag_app.product.os_risk_review import (
    disposition_digest,
    load_review_inputs,
)
from rag_app.product.release_evidence import combine, vulnerability_report

_NOW = datetime(2026, 9, 6, 8, tzinfo=UTC)
_SCAN_TIME = "2026-09-06T06:00:00Z"
_REVIEW_TIME = "2026-09-06T07:00:00Z"
_EXPIRY = "2026-09-07T07:00:00Z"


def _proof(root: Path, name: str, value: object) -> dict[str, str]:
    content = json.dumps(value).encode()
    (root / name).write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


def _case(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    scan: dict[str, Any] = {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "CreatedAt": _SCAN_TIME,
        "Trivy": {"Version": "0.74.0"},
        "Metadata": {
            "ImageID": "sha256:" + "a" * 64,
            "OS": {"Family": "debian", "Name": "13.6"},
        },
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "debian",
                "Target": "debian",
                "Packages": [
                    {
                        "Name": "lib-example",
                        "ID": "lib-example@1-2",
                        "SrcName": "example",
                        "Version": "1",
                        "SrcVersion": "1",
                        "SrcRelease": "2",
                    }
                ],
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-10000",
                        "PkgName": "lib-example",
                        "InstalledVersion": "1-2",
                        "Severity": "HIGH",
                    }
                ],
            }
        ],
    }
    raw = _proof(root, "scan.json", scan)
    database = _proof(
        root,
        "db.json",
        {
            "UpdatedAt": "2026-09-06T04:00:00Z",
            "DownloadedAt": "2026-09-06T05:00:00Z",
        },
    )
    overlay: dict[str, Any] = {
        "schema_version": 1,
        "scan": {
            "image_id": scan["Metadata"]["ImageID"],
            "sha256": raw["sha256"],
            "scanned_at": _SCAN_TIME,
            "scanner_version": "0.74.0",
            "db_updated_at": "2026-09-06T04:00:00Z",
            "db_metadata": database,
        },
        "reviews": [
            {
                "id": "CVE-2026-10000",
                "package": "lib-example",
                "installed_version": "1-2",
                "conclusion": "AFFECTED_MITIGATED",
                "scope": "P11_RELEASE",
                "reviewed_at": _REVIEW_TIME,
                "expires_at": _EXPIRY,
                "owner": "test-only-owner",
                "reachability": "REACHABLE_WITH_PRECONDITIONS",
                "trigger_conditions": "特定解析输入可以到达受影响函数。",
                "mitigation": "已执行有界输入与进程隔离验证，保留剩余风险。",
                "evidence": [
                    _proof(root, "mitigation.json", {"test": "passed"})
                ],
            }
        ],
    }
    return scan, overlay


def _approve(root: Path, overlay: dict[str, Any], **changes: object) -> None:
    review = overlay["reviews"][0]
    receipt = {
        "schema_version": 1,
        "kind": "os-risk-acceptance",
        "status": "APPROVED",
        "approval_source": "local_administrator",
        "scan": {
            k: v for k, v in overlay["scan"].items() if k != "db_metadata"
        },
        "finding": {
            k: review[k] for k in ("id", "package", "installed_version")
        },
        "review_sha256": disposition_digest(review),
        "approver": "test-only-human",
        "owner": review["owner"],
        "approval_reference": "test-only-local-decision-001",
        "scope": "P11_RELEASE",
        "approved_at": _REVIEW_TIME,
        "expires_at": _EXPIRY,
        **changes,
    }
    review["approval_evidence"] = _proof(root, "approval.json", receipt)


def _report(
    root: Path, scan: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    return vulnerability_report(
        scan, overlay=overlay, root=root, scan_path=root / "scan.json", now=_NOW
    )


def _objective(root: Path, overlay: dict[str, Any], fixed: str = "0") -> None:
    review = overlay["reviews"][0]
    review["conclusion"] = "NOT_AFFECTED_WITH_EVIDENCE"
    proof = _proof(
        root,
        "debian.json",
        {
            "example": {
                "CVE-2026-10000": {
                    "releases": {
                        "trixie": {"status": "resolved", "fixed_version": fixed}
                    }
                }
            }
        },
    )
    review["objective_evidence"] = {
        **proof,
        "source_url": "https://security-tracker.debian.org/tracker/data/json",
    }


def test_full_scan_counts_and_unapproved_risk_are_preserved(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    original = copy.deepcopy(scan)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "BLOCKED"
    assert report["all_high_critical"] == report["without_fix"] == 1
    assert report["unique_cves"] == report["unique_cve_package_versions"] == 1
    assert report["approved_dispositions"] == 0
    assert report["under_investigation"] == 1
    assert scan == original


def test_valid_human_acceptance_keeps_risk_visible_and_status_compatible(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == combine([report]) == "PASS"
    assert report["all_high_critical"] == report["without_fix"] == 1
    assert report["approved_dispositions"] == report["mitigated"] == 1
    assert report["accepted_risk_remaining"] is True
    assert report["reason"] == "ALL_RISKS_DISPOSED_WITH_ACCEPTED_RISK"


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_id", "sha256:" + "b" * 64),
        ("sha256", "f" * 64),
        ("scanner_version", "old"),
        ("scanned_at", "2026-09-05T00:00:00Z"),
        ("db_updated_at", "2026-09-05T00:00:00Z"),
    ],
)
def test_wrong_scan_binding_cannot_apply(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    scan, overlay = _case(tmp_path)
    overlay["scan"][field] = value
    _approve(tmp_path, overlay)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "BLOCKED"
    assert report["review_errors"] == ["REVIEW_SCAN_IDENTITY_MISMATCH"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("approver", ""),
        ("owner", ""),
        ("status", "PROPOSED"),
        ("approval_source", "http_request"),
        ("approval_reference", ""),
        ("scope", "TEST_ONLY"),
        ("expires_at", _REVIEW_TIME),
        ("approved_at", "2026-09-07T06:00:00Z"),
    ],
)
def test_invalid_approval_is_rejected(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay, **{field: value})
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "BLOCKED"
    assert report["approved_dispositions"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("installed_version", "other"),
        ("package", "other"),
        ("id", "CVE-other"),
        ("expires_at", _REVIEW_TIME),
        ("scope", "TEST_ONLY"),
        ("evidence", []),
        ("reachability", "UNKNOWN"),
        ("conclusion", "REMOVED"),
    ],
)
def test_invalid_or_unknown_disposition_cannot_apply(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    scan, overlay = _case(tmp_path)
    overlay["reviews"][0][field] = value
    _approve(tmp_path, overlay)
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"


def test_changed_evidence_and_changed_approved_review_are_rejected(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    overlay["reviews"][0]["mitigation"] = "未经批准的新缓解"
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"
    _approve(tmp_path, overlay)
    (tmp_path / "mitigation.json").write_text("changed", encoding="utf-8")
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"


def test_valid_official_not_affected_evidence_needs_no_risk_acceptance(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _objective(tmp_path, overlay)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "PASS"
    assert report["objective_dispositions"] == 1
    assert report["approved_dispositions"] == 0
    assert report["accepted_risk_remaining"] is False


def test_official_fixed_exact_source_version_can_resolve_stale_finding(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _objective(tmp_path, overlay, "1-2")
    overlay["reviews"][0]["conclusion"] = "FIXED"
    assert _report(tmp_path, scan, overlay)["status"] == "PASS"


@pytest.mark.parametrize("fixed", ["1-1", "1-3", ""])
def test_upstream_version_or_wrong_debian_version_cannot_waive(
    tmp_path: Path, fixed: str
) -> None:
    scan, overlay = _case(tmp_path)
    _objective(tmp_path, overlay, fixed)
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"


def test_objective_evidence_is_verified_beyond_status_text(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _objective(tmp_path, overlay)
    overlay["reviews"][0]["objective_evidence"]["source_url"] = (
        "https://example.com"
    )
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"
    _objective(tmp_path, overlay)
    (tmp_path / "debian.json").unlink()
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"


def test_partial_disposition_and_duplicate_reviews_do_not_green_release(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    overlay["reviews"].append(copy.deepcopy(overlay["reviews"][0]))
    assert _report(tmp_path, scan, overlay)["status"] == "BLOCKED"
    overlay["reviews"] = []
    assert _report(tmp_path, scan, overlay)["under_investigation"] == 1


def test_scan_file_and_payload_must_be_identical(tmp_path: Path) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    scan["Results"][0]["Vulnerabilities"] = []
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "BLOCKED"
    assert report["review_errors"] == ["REVIEW_SCAN_PAYLOAD_MISMATCH"]


def test_investigation_never_fabricates_an_expiry_or_approver(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    review = overlay["reviews"][0]
    review.update(conclusion="UNDER_INVESTIGATION", expires_at=None, owner=None)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "BLOCKED"
    assert (
        report["findings"][0]["review_reason"] == "REVIEW_UNDER_INVESTIGATION"
    )
    assert report["findings"][0]["risk_accepted"] is False
    assert report["findings"][0]["expires_at"] is None


def test_final_gate_inputs_require_unchanged_files_and_recheck_expiry(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    record = {
        "details": {
            "raw_scan": _proof(tmp_path, "scan.json", scan),
            "review_overlay": _proof(tmp_path, "overlay.json", overlay),
        }
    }
    payload, review, path = load_review_inputs(record, tmp_path)
    report = vulnerability_report(
        payload, overlay=review, root=tmp_path, scan_path=path, now=_NOW
    )
    assert report["status"] == "PASS"
    expired = vulnerability_report(
        payload,
        overlay=review,
        root=tmp_path,
        scan_path=path,
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )
    assert expired["status"] == "BLOCKED"
    (tmp_path / "overlay.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="REVIEW_EVIDENCE_HASH_MISMATCH"):
        load_review_inputs(record, tmp_path)


def test_final_gate_cannot_read_outside_evidence_root(tmp_path: Path) -> None:
    proof = {"path": "../outside.json", "sha256": "f" * 64}
    with pytest.raises(ValueError, match="REVIEW_EVIDENCE_OUTSIDE_ROOT"):
        load_review_inputs({"details": {"raw_scan": proof}}, tmp_path)


def test_fixable_vulnerability_cannot_use_unfixed_risk_acceptance_channel(
    tmp_path: Path,
) -> None:
    scan, overlay = _case(tmp_path)
    scan["Results"][0]["Vulnerabilities"][0]["FixedVersion"] = "1-3"
    overlay["scan"]["sha256"] = _proof(tmp_path, "scan.json", scan)["sha256"]
    _approve(tmp_path, overlay)
    report = _report(tmp_path, scan, overlay)
    assert report["status"] == "FAIL"
    assert report["approved_dispositions"] == 0
    assert report["review_errors"] == [
        "REVIEW_FIXABLE_VULNERABILITY_REQUIRES_FIX"
    ]


def test_final_build_report_rejects_expired_approval_without_any_file_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan, overlay = _case(tmp_path)
    _approve(tmp_path, overlay)
    proof = _proof(tmp_path, "aggregation.json", {"status": "PASS"})
    identity = {
        "image": "same-image-recipe",
        "image_id": scan["Metadata"]["ImageID"],
    }
    record = {
        "status": "PASS",
        "identity": identity,
        "origin": "本次执行",
        "exit_code": 0,
        "evidence": proof["path"],
        "sha256": proof["sha256"],
        "details": {
            "raw_scan": _proof(tmp_path, "scan.json", scan),
            "review_overlay": _proof(tmp_path, "overlay.json", overlay),
        },
    }
    current = [_NOW]
    monkeypatch.setattr(
        release_evidence,
        "datetime",
        SimpleNamespace(now=lambda _tz: current[0]),
    )
    evidence = {"checks": {"os_risk": record}}
    before = copy.deepcopy(evidence)
    report = release_evidence.build_report(tmp_path, evidence, identity)
    assert report["checks"]["os_risk"]["status"] == "PASS"
    current[0] = datetime(2026, 9, 8, tzinfo=UTC)
    report = release_evidence.build_report(tmp_path, evidence, identity)
    assert report["checks"]["os_risk"]["status"] == "BLOCKED"
    assert report["gates"]["SECURITY_READY"]["status"] == "BLOCKED"
    assert evidence == before
