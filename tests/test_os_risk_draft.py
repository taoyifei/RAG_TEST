"""新扫描草案可复用调查文本，但绝不迁移人工批准。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.os_risk_draft import build_investigation_draft


def _write(path: Path, value: object) -> dict[str, str]:
    path.write_text(json.dumps(value), encoding="utf-8")
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _scan(image: str, created_at: str) -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "ArtifactID": "sha256:" + "c" * 64,
        "ArtifactName": "candidate",
        "CreatedAt": created_at,
        "Trivy": {"Version": "0.74.0"},
        "Metadata": {
            "ImageID": image,
            "OS": {"Family": "debian", "Name": "13.6"},
            "RepoDigests": [f"candidate@{image}"],
            "ImageConfig": {"os": "linux", "architecture": "amd64"},
        },
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "debian",
                "Target": "candidate (debian 13.6)",
                "Packages": [
                    {
                        "ID": "lib-example@1",
                        "Name": "lib-example",
                        "Version": "1",
                        "SrcName": "example",
                        "SrcVersion": "1",
                        "Arch": "amd64",
                        "Identifier": {"PURL": "pkg:deb/debian/lib-example@1"},
                    }
                ],
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-10000",
                        "PkgID": "lib-example@1",
                        "PkgName": "lib-example",
                        "InstalledVersion": "1",
                        "Severity": "HIGH",
                        "Status": "affected",
                    }
                ],
            }
        ],
    }


def test_new_scan_draft_keeps_analysis_and_drops_old_approval(
    tmp_path: Path,
) -> None:
    old_scan = tmp_path / "old-scan.json"
    old_proof = _write(
        old_scan,
        _scan("sha256:" + "a" * 64, "2026-09-07T00:00:00Z"),
    )
    prior = {
        "reviews": [
            {
                "id": "CVE-2026-10000",
                "package": "lib-example",
                "installed_version": "1",
                "trigger_conditions": "恶意输入触发。",
                "reachability": "REACHABLE_WITH_PRECONDITIONS",
                "mitigation": "隔离运行。",
                "approval_evidence": {
                    "path": "approval.json",
                    "sha256": "f" * 64,
                },
                "approver": "old-human",
                "owner": "old-owner",
                "risk_accepted": True,
                "conclusion": "AFFECTED_MITIGATED",
                "evidence": [old_proof],
            }
        ]
    }
    previous_path = tmp_path / "previous.json"
    _write(previous_path, prior)
    scan_path = tmp_path / "scan.json"
    _write(
        scan_path,
        _scan("sha256:" + "b" * 64, "2026-09-08T00:00:00Z"),
    )
    database_path = tmp_path / "db.json"
    _write(
        database_path,
        {
            "UpdatedAt": "2026-09-07T23:00:00Z",
            "DownloadedAt": "2026-09-07T23:30:00Z",
        },
    )

    draft = build_investigation_draft(
        root=tmp_path,
        scan_path=scan_path,
        db_metadata_path=database_path,
        previous_review_path=previous_path,
        generated_at=datetime(2026, 9, 8, 1, tzinfo=UTC),
    )

    review = draft["reviews"][0]
    assert review["trigger_conditions"] == "恶意输入触发。"
    assert review["conclusion"] == "UNDER_INVESTIGATION"
    assert review["owner"] is review["approver"] is None
    assert review["expires_at"] is None
    assert review["risk_accepted"] is False
    assert "approval_evidence" not in review
    assert draft["summary"] == {
        "raw_findings": 1,
        "raw_high_critical": 1,
        "unique_cves": 1,
        "unique_cve_package_versions": 1,
        "fixable_high_critical": 0,
        "without_fix": 1,
        "reviewed_findings": 1,
        "under_investigation": 1,
        "mitigated": 0,
        "approved_dispositions": 0,
        "objective_dispositions": 0,
        "status": "BLOCKED",
    }
    assert draft["scan_artifact"]["registry_manifest_digest"] is None
    assert draft["scan_artifact"]["manifest_scope"] == (
        "local_image_store_only"
    )
    assert draft["scan_artifact"]["trivy_artifact_id"] == (
        "sha256:" + "c" * 64
    )
    assert draft["technical_assessment_reuse"] == {
        "status": "PORTED_UNDER_INVESTIGATION",
        "matched_tuple_count": 1,
        "source_scan": None,
        "approval_state_transferred": False,
        "requires_human_revalidation": True,
    }
