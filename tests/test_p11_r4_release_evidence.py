"""R4 发布证据不可由局部成功、过期身份或过滤扫描推导全绿。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from rag_app.product.release_evidence import (
    _CHECK_IDENTITIES,
    build_report,
    component_identity,
    vulnerability_report,
)

_IDENTITY = {
    "runtime": "current",
    "tests": "tests",
    "migrations": "migrations",
    "frontend": "frontend",
    "image": "image",
    "image_id": "sha256:actual",
    "evaluation": "evaluation",
    "release": "release",
    "provider_jina": "jina",
    "provider_aliyun": "aliyun",
}
_SCAN_IDENTITY = {
    "SchemaVersion": 2,
    "ArtifactType": "container_image",
    "Metadata": {"ImageID": "sha256:test", "OS": {"Family": "debian"}},
}


def _pass_record(path: Path) -> dict[str, object]:
    path.write_text("executed command, exit=0", encoding="utf-8")
    return {
        "status": "PASS",
        "identity": dict(_IDENTITY),
        "origin": "本次执行",
        "evidence": str(path),
        "exit_code": 0,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_selected_steps_do_not_mark_whole_release_ready(tmp_path: Path) -> None:
    record = _pass_record(tmp_path / "proof.log")
    report = build_report(
        tmp_path,
        {"checks": {"aliyun_document_canary": record}},
        _IDENTITY,
    )
    assert report["P11_READY"] is False
    assert report["MERGE_TO_MAIN_AUTHORIZED"] is False
    assert report["gates"]["PROVIDER_CONNECTIVITY_READY"]["status"] == "NOT_RUN"
    assert report["gates"]["RETRIEVAL_QUALITY_READY"]["status"] == "NOT_RUN"


def test_component_change_invalidates_reuse(tmp_path: Path) -> None:
    record = _pass_record(tmp_path / "proof.log")
    report = build_report(
        tmp_path,
        {"checks": {"ci": record}},
        {**_IDENTITY, "runtime": "changed"},
    )
    assert report["checks"]["ci"]["status"] == "BLOCKED"
    assert report["checks"]["ci"]["reason"] == "EVIDENCE_IDENTITY_MISMATCH"


def test_proof_hash_and_failed_exit_cannot_be_hidden(tmp_path: Path) -> None:
    proof = tmp_path / "proof.log"
    record = _pass_record(proof)
    proof.write_text("not the original evidence", encoding="utf-8")
    report = build_report(tmp_path, {"checks": {"ci": record}}, _IDENTITY)
    assert report["checks"]["ci"]["reason"] == "EVIDENCE_ARTIFACT_CHANGED"
    record["exit_code"] = 1
    report = build_report(tmp_path, {"checks": {"ci": record}}, _IDENTITY)
    assert report["checks"]["ci"]["status"] == "FAIL"


def test_document_only_commit_preserves_asset_identity(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs/progress.md").write_text("old report", encoding="utf-8")
    paths = ["src/app.py", "docs/progress.md"]
    before = component_identity(tmp_path, paths)
    (tmp_path / "docs/progress.md").write_text("new report", encoding="utf-8")
    assert component_identity(tmp_path, paths) == before
    (tmp_path / "src/app.py").write_text("x = 2\n", encoding="utf-8")
    assert component_identity(tmp_path, paths)["runtime"] != before["runtime"]


def test_unfixed_os_high_is_not_an_automatic_waiver() -> None:
    report = vulnerability_report(
        {
            **_SCAN_IDENTITY,
            "Results": [
                {
                    "Class": "os-pkgs",
                    "Target": "debian",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-test",
                            "PkgName": "lib-example",
                            "InstalledVersion": "1",
                            "Severity": "HIGH",
                        },
                    ],
                }
            ],
        }
    )
    assert report["status"] == "BLOCKED"
    assert report["all_high_critical"] == 1
    assert report["fixable_high_critical"] == 0
    assert report["without_fix"] == 1
    assert report["findings"][0]["risk_accepted"] is False


def test_fixable_critical_is_a_failure() -> None:
    report = vulnerability_report(
        {
            **_SCAN_IDENTITY,
            "Results": [
                {
                    "Class": "os-pkgs",
                    "Vulnerabilities": [
                        {
                            "Severity": "CRITICAL",
                            "FixedVersion": "2",
                            "PkgName": "example",
                        }
                    ],
                }
            ],
        }
    )
    assert report["status"] == "FAIL"


def test_required_checks_cannot_be_waived(tmp_path: Path) -> None:
    records = {name: {"status": "NOT_APPLICABLE"} for name in _CHECK_IDENTITIES}
    records["candidate_startup"] = _pass_record(tmp_path / "proof.log")
    report = build_report(tmp_path, {"checks": records}, _IDENTITY)
    assert report["P11_READY"] is False
    assert report["gates"]["SECURITY_READY"]["status"] == "BLOCKED"


def test_changed_image_and_missing_image_dimension_block_reuse(
    tmp_path: Path,
) -> None:
    record = _pass_record(tmp_path / "proof.log")
    report = build_report(
        tmp_path,
        {"checks": {"candidate_startup": record}},
        {**_IDENTITY, "image_id": "new-image"},
    )
    assert report["checks"]["candidate_startup"]["status"] == "BLOCKED"
    del record["identity"]["image_id"]
    report = build_report(
        tmp_path, {"checks": {"candidate_startup": record}}, _IDENTITY
    )
    assert report["checks"]["candidate_startup"]["status"] == "BLOCKED"


def test_smoke_failure_cannot_be_hidden_by_other_passes(tmp_path: Path) -> None:
    records = dict.fromkeys(
        _CHECK_IDENTITIES, _pass_record(tmp_path / "proof.log")
    )
    records["smoke"] = {"status": "FAIL", "reason": "ACTUAL_FAILURE"}
    report = build_report(tmp_path, {"checks": records}, _IDENTITY)
    assert report["P11_READY"] is False
    assert report["gates"]["P11_READY"]["status"] == "FAIL"


def test_missing_os_scan_does_not_mean_zero_vulnerabilities() -> None:
    assert vulnerability_report({})["status"] == "BLOCKED"
    assert (
        vulnerability_report(
            {**_SCAN_IDENTITY, "Results": [{"Class": "lang-pkgs"}]}
        )["status"]
        == "BLOCKED"
    )
