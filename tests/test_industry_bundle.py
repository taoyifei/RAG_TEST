"""验证 Industry 首次部署包的身份、确定性与安全边界。"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from deployment.industry.package_selfcheck import (
    PackageSelfcheckError,
    verify_outer_archive,
    verify_release,
)
from rag_app.corpus_policy import CorpusPolicy
from scripts.build_industry_bundle import (
    IndustryBuildError,
    require_industry_source,
)
from scripts.industry_bundle import (
    ExistingImageIdentity,
    ImageArtifact,
    IndustryImageError,
    assemble_release,
    build_outer_upload,
    existing_image_identity,
)
from scripts.industry_bundle import images as image_module
from scripts.industry_bundle.assembly import IndustryReleaseError

_GIT_SHA = "a" * 40
_MAIN_SHA = "b" * 40
_SOURCE_DATE_EPOCH = 1_700_000_000


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_release_integrity(release: Path) -> None:
    manifest_path = release / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_bytes())
    for item in manifest["payload_files"]:
        path = release / item["path"]
        item["mode"] = stat.S_IMODE(path.stat().st_mode)
        item["sha256"] = _sha256(path)
        item["size"] = path.stat().st_size
    manifest_path.write_bytes(_canonical_json(manifest))
    manifest_path.chmod(0o600)
    files = sorted(
        path
        for path in release.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    (release / "SHA256SUMS").write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(release).as_posix()}\n"
            for path in files
        ),
        encoding="ascii",
    )
    (release / "SHA256SUMS").chmod(0o600)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expected_names() -> tuple[str, ...]:
    payload = json.loads(
        (_repository_root() / "evaluation/industry/expected-corpus.json")
        .read_text(encoding="utf-8")
    )
    return tuple(payload["active_documents"])


def _create_corpus(root: Path, *, git_sha: str = _GIT_SHA) -> Path:
    docs = root / "docs"
    reference = root / "reference"
    docs.mkdir(parents=True)
    reference.mkdir()
    manifest_documents: list[dict[str, object]] = []
    identities: list[dict[str, object]] = []
    active: list[str] = []
    audit_documents: list[dict[str, object]] = []
    for index, name in enumerate(_expected_names(), start=1):
        target = docs / name
        target.write_bytes(f"synthetic-docx-{index}".encode())
        relative = f"docs/{name}"
        digest = _sha256(target)
        active.append(relative)
        identities.append(
            {"path": relative, "sha256": digest, "size": target.stat().st_size}
        )
        manifest_documents.append(
            {
                "actual_name": name.removesuffix("x"),
                "canonical_name": name.removesuffix("x"),
                "external_relationship_type_counts": {},
                "heading_accepted_count": 0,
                "heading_candidate_count": 0,
                "heading_count": 1,
                "heading_reason_counts": {},
                "heading_rejected_count": 0,
                "image_count": 0,
                "list_level_count": 0,
                "mtime_ns": index,
                "ocr_candidate_count": 0,
                "paragraph_count": 1,
                "parser_unsupported_node_count": 0,
                "remaining_private_character_count": 0,
                "removed_external_relationship_count": 0,
                "removed_private_character_count": 0,
                "role": "active",
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
                "size": index,
                "table_count": 0,
                "target_relative_path": relative,
                "target_sha256": digest,
                "target_size": target.stat().st_size,
                "visible_text_sha256": hashlib.sha256(
                    f"visible-{index}".encode()
                ).hexdigest(),
                "warnings": [],
            }
        )
        audit_documents.append({"target_relative_path": relative})
    corpus_sha = hashlib.sha256(_canonical_json(identities)).hexdigest()
    revision = corpus_sha[:16]
    manifest = {
        "active_document_count": 10,
        "active_documents": active,
        "authority_basis": "verified test input",
        "converter_name": "LibreOffice",
        "converter_version": "24.2.0",
        "corpus_name": "industry-management",
        "corpus_revision": revision,
        "corpus_sha256": corpus_sha,
        "documents": manifest_documents,
        "generated_at": "2023-11-14T22:13:20Z",
        "generated_from_git_sha": git_sha,
        "preprocessing_revision": "industry-corpus-v1",
        "reference_document_count": 0,
        "reference_documents": [],
        "schema_version": "1",
        "source_date_epoch": _SOURCE_DATE_EPOCH,
        "source_directory_sha256": "c" * 64,
        "source_inventory_sha256": "d" * 64,
        "status_basis": "active is an explicit deployment decision",
    }
    audit = {
        "converter_name": "LibreOffice",
        "converter_version": "24.2.0",
        "corpus_revision": revision,
        "documents": audit_documents,
        "generated_at": "2023-11-14T22:13:20Z",
        "network_namespace_enabled": False,
        "preprocessing_revision": "industry-corpus-v1",
        "schema_version": "1",
        "warnings": ["NETWORK_NAMESPACE_UNAVAILABLE"],
    }
    (root / "industry-corpus-manifest.json").write_bytes(
        _canonical_json(manifest)
    )
    (root / "industry-corpus-audit.json").write_bytes(_canonical_json(audit))
    return root


def _create_images(stage: Path) -> tuple[ImageArtifact, ...]:
    definitions = (
        ("app", "rag-industry-app:test", "app-image.tar.gz", _GIT_SHA),
        ("ocr", "rag-industry-ocr:test", "ocr-image.tar.gz", _GIT_SHA),
        ("qdrant", "qdrant/qdrant:v1.16.3", "qdrant-image.tar.gz", None),
    )
    images: list[ImageArtifact] = []
    for name, ref, archive_name, revision in definitions:
        archive = stage / archive_name
        archive.write_bytes(f"deterministic-{name}-archive".encode())
        images.append(
            ImageArtifact(
                name=name,
                ref=ref,
                image_id=f"sha256:{hashlib.sha256(name.encode()).hexdigest()}",
                platform="linux/amd64",
                revision=revision,
                archive_name=archive_name,
                archive_sha256=_sha256(archive),
                manifest_digest=f"sha256:{'e' * 64}",
                config_digest=f"sha256:{'f' * 64}",
            )
        )
    return tuple(images)


def _create_reuse_images(
    stage: Path,
) -> tuple[ImageArtifact, ExistingImageIdentity, ExistingImageIdentity]:
    app = _create_images(stage)[0]
    (stage / "ocr-image.tar.gz").unlink()
    (stage / "qdrant-image.tar.gz").unlink()
    ocr = existing_image_identity(
        name="ocr",
        ref="rag-industry-ocr:server-fixed",
        image_id=f"sha256:{'1' * 64}",
        revision="c" * 40,
    )
    qdrant = existing_image_identity(
        name="qdrant",
        ref="rag-qdrant:server-fixed",
        image_id=f"sha256:{'2' * 64}",
        revision=None,
    )
    return app, ocr, qdrant


def _assemble(tmp_path: Path, name: str) -> Path:
    corpus = _create_corpus(tmp_path / f"{name}-corpus")
    stage = tmp_path / f"{name}-stage"
    stage.mkdir()
    images = _create_images(stage)
    identity = assemble_release(
        repository_root=_repository_root(),
        stage=stage,
        corpus_root=corpus,
        git_sha=_GIT_SHA,
        main_sha=_MAIN_SHA,
        source_date_epoch=_SOURCE_DATE_EPOCH,
        images=images,
    )
    release = tmp_path / identity.release_id
    stage.rename(release)
    return release


def _assemble_reuse(tmp_path: Path, name: str) -> Path:
    corpus = _create_corpus(tmp_path / f"{name}-corpus")
    stage = tmp_path / f"{name}-stage"
    stage.mkdir()
    identity = assemble_release(
        repository_root=_repository_root(),
        stage=stage,
        corpus_root=corpus,
        git_sha=_GIT_SHA,
        main_sha=_MAIN_SHA,
        source_date_epoch=_SOURCE_DATE_EPOCH,
        images=_create_reuse_images(stage),
    )
    release = tmp_path / identity.release_id
    stage.rename(release)
    return release


def test_release_and_outer_archive_are_self_contained(tmp_path: Path) -> None:
    release = _assemble(tmp_path, "first")
    result = verify_release(release)
    assert result["release_kind"] == "industry-first-deploy"
    manifest = json.loads((release / "RELEASE_MANIFEST.json").read_bytes())
    assert manifest["git_sha"] == _GIT_SHA
    assert manifest["corpus"]["active_count"] == 10
    assert manifest["corpus"]["reference_count"] == 0
    assert manifest["intent_router_mode"] == "shadow"
    assert manifest["calibration_status"] == "unverified"
    policy = json.loads((release / "config/corpus-policy.json").read_bytes())
    assert [item["path"] for item in policy["overrides"]] == list(
        _expected_names()
    )
    assert {item["authority_level"] for item in policy["overrides"]} == {
        "verified"
    }
    retrieval = json.loads((release / "config/retrieval.json").read_bytes())
    assert retrieval["allowed_authority_levels"] == [
        "official",
        "verified",
    ]
    policy_path = release / "config/corpus-policy.json"
    pipeline = json.loads((release / "config/pipeline.json").read_bytes())
    assert pipeline["corpus_policy_sha256"] == CorpusPolicy.load(
        policy_path
    ).semantic_sha256()
    assert pipeline["corpus_policy_sha256"] != _sha256(policy_path)
    assert not list(release.rglob("*.doc"))
    assert not list(release.rglob("*.docx"))
    assert not any(
        part in {".git", ".venv", "__pycache__"}
        for path in release.rglob("*")
        for part in path.relative_to(release).parts
    )
    bash = shutil.which("bash")
    assert bash is not None
    for script in ("preflight.sh", "verify.sh"):
        completed = subprocess.run(  # noqa: S603
            [bash, str(release / script), "--package-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "PACKAGE_OK" in completed.stdout
    outer = build_outer_upload(
        release_dir=release,
        upload_parent=tmp_path / "uploads",
        source_date_epoch=_SOURCE_DATE_EPOCH,
    )
    assert outer.outer_archive is not None
    assert outer.outer_sidecar is not None
    extracted = verify_outer_archive(
        outer.outer_archive,
        outer.outer_sidecar,
        tmp_path / "fresh",
    )
    assert extracted.name == release.name


def test_reuse_release_excludes_server_image_archives(tmp_path: Path) -> None:
    release = _assemble_reuse(tmp_path, "reuse")
    result = verify_release(release)
    assert result["release_kind"] == (
        "industry-first-deploy-reuse-images"
    )
    assert not (release / "ocr-image.tar.gz").exists()
    assert not (release / "qdrant-image.tar.gz").exists()
    manifest = json.loads((release / "RELEASE_MANIFEST.json").read_bytes())
    assert manifest["images"]["app"]["delivery"] == "archive"
    for name in ("ocr", "qdrant"):
        image = manifest["images"][name]
        assert image["delivery"] == "server-existing"
        assert "archive_name" not in image
        assert "archive_sha256" not in image
    outer = build_outer_upload(
        release_dir=release,
        upload_parent=tmp_path / "reuse-upload",
        source_date_epoch=_SOURCE_DATE_EPOCH,
    )
    assert outer.outer_archive is not None
    assert outer.outer_archive.name.startswith(
        "rag-industry-first-deploy-reuse-images-"
    )


def test_release_rejects_pipeline_corpus_policy_mismatch(
    tmp_path: Path,
) -> None:
    release = _assemble_reuse(tmp_path, "policy-mismatch")
    pipeline_path = release / "config/pipeline.json"
    pipeline = json.loads(pipeline_path.read_bytes())
    pipeline["corpus_policy_sha256"] = "0" * 64
    pipeline_path.write_bytes(_canonical_json(pipeline))

    manifest_path = release / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["config_sha256"]["pipeline.json"] = _sha256(pipeline_path)
    manifest_path.write_bytes(_canonical_json(manifest))
    _refresh_release_integrity(release)

    with pytest.raises(
        PackageSelfcheckError,
        match="pipeline corpus policy SHA256",
    ):
        verify_release(release)


def test_release_rejects_unqueryable_corpus_authority(
    tmp_path: Path,
) -> None:
    release = _assemble_reuse(tmp_path, "authority-mismatch")
    retrieval_path = release / "config/retrieval.json"
    retrieval = json.loads(retrieval_path.read_bytes())
    retrieval["allowed_authority_levels"] = ["official"]
    retrieval_path.write_bytes(_canonical_json(retrieval))

    manifest_path = release / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["config_sha256"]["retrieval.json"] = _sha256(retrieval_path)
    manifest_path.write_bytes(_canonical_json(manifest))
    _refresh_release_integrity(release)

    with pytest.raises(
        PackageSelfcheckError,
        match="corpus policy 元数据被 retrieval 全量过滤",
    ):
        verify_release(release)


def test_release_build_is_reproducible(tmp_path: Path) -> None:
    first = _assemble(tmp_path / "one", "release")
    second = _assemble(tmp_path / "two", "release")
    assert _sha256(first / "corpus.tar.gz") == _sha256(
        second / "corpus.tar.gz"
    )
    assert (first / "RELEASE_MANIFEST.json").read_bytes() == (
        second / "RELEASE_MANIFEST.json"
    ).read_bytes()
    first_outer = build_outer_upload(
        release_dir=first,
        upload_parent=tmp_path / "one-upload",
        source_date_epoch=_SOURCE_DATE_EPOCH,
    )
    second_outer = build_outer_upload(
        release_dir=second,
        upload_parent=tmp_path / "two-upload",
        source_date_epoch=_SOURCE_DATE_EPOCH,
    )
    assert first_outer.outer_archive is not None
    assert second_outer.outer_archive is not None
    assert _sha256(first_outer.outer_archive) == _sha256(
        second_outer.outer_archive
    )


def test_release_rejects_wrong_corpus_name(tmp_path: Path) -> None:
    corpus = _create_corpus(tmp_path / "corpus")
    manifest_path = corpus / "industry-corpus-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["active_documents"][0] = "docs/GM-01 wrong.docx"
    manifest_path.write_bytes(_canonical_json(manifest))
    stage = tmp_path / "stage"
    stage.mkdir()
    images = _create_images(stage)
    with pytest.raises(IndustryReleaseError, match="文件名集合"):
        assemble_release(
            repository_root=_repository_root(),
            stage=stage,
            corpus_root=corpus,
            git_sha=_GIT_SHA,
            main_sha=_MAIN_SHA,
            source_date_epoch=_SOURCE_DATE_EPOCH,
            images=images,
        )


def test_release_requires_exact_image_stage(tmp_path: Path) -> None:
    corpus = _create_corpus(tmp_path / "corpus")
    stage = tmp_path / "stage"
    stage.mkdir()
    images = _create_images(stage)
    (stage / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(IndustryReleaseError, match="exact set"):
        assemble_release(
            repository_root=_repository_root(),
            stage=stage,
            corpus_root=corpus,
            git_sha=_GIT_SHA,
            main_sha=_MAIN_SHA,
            source_date_epoch=_SOURCE_DATE_EPOCH,
            images=images,
        )


def test_release_secret_scanner_runs_after_identity_checks(
    tmp_path: Path,
) -> None:
    release = _assemble(tmp_path, "secret")
    guide = release / "DEPLOYMENT_GUIDE.md"
    guide.write_text(
        guide.read_text(encoding="utf-8")
        + "\nAuthorization: Bearer "
        + "x" * 40
        + "\n",
        encoding="utf-8",
    )
    _refresh_release_integrity(release)
    with pytest.raises(PackageSelfcheckError, match="secret/path"):
        verify_release(release)


def test_release_rejects_wrong_sidecar_basename(tmp_path: Path) -> None:
    release = _assemble(tmp_path, "sidecar")
    sidecar = release / "app-image.tar.gz.sha256"
    sidecar.write_text(
        f"{_sha256(release / 'app-image.tar.gz')}  wrong.tar.gz\n",
        encoding="ascii",
    )
    _refresh_release_integrity(release)
    with pytest.raises(PackageSelfcheckError, match="sidecar basename"):
        verify_release(release)


def test_release_rejects_symlink(tmp_path: Path) -> None:
    release = _assemble(tmp_path, "symlink")
    (release / "unexpected-link").symlink_to(release / "compose.yaml")
    with pytest.raises(PackageSelfcheckError, match="symlink"):
        verify_release(release)


def test_release_rejects_extra_file_even_with_refreshed_hashes(
    tmp_path: Path,
) -> None:
    release = _assemble(tmp_path, "extra")
    (release / "extra.txt").write_text("benign", encoding="utf-8")
    _refresh_release_integrity(release)
    with pytest.raises(PackageSelfcheckError, match="exact set"):
        verify_release(release)


def test_outer_archive_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("bad", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="../escape")
    sidecar = tmp_path / "bad.tar.gz.sha256"
    sidecar.write_text(
        f"{_sha256(archive)}  {archive.name}\n",
        encoding="ascii",
    )
    with pytest.raises(PackageSelfcheckError, match="路径"):
        verify_outer_archive(archive, sidecar, tmp_path / "extract")


def test_image_builder_rejects_ocr_without_full_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_app_archive(
        *,
        repository_root: Path,
        revision: str,
        destination: Path,
        config_directory: Path | None = None,
        assets_manifest_path: Path | None = None,
    ) -> str:
        del (
            repository_root,
            revision,
            config_directory,
            assets_manifest_path,
        )
        destination.write_bytes(b"app")
        return "rag-industry-app:test"

    def fake_save(
        image: str,
        destination: Path,
        repository_root: Path,
    ) -> None:
        del image, repository_root
        destination.write_bytes(b"image")

    def fake_binary_check(_image: str, _root: Path) -> None:
        return None

    def fake_root_check(_root: Path) -> None:
        return None

    def fake_inspect(**_kwargs: object) -> ImageArtifact:
        return _create_images(tmp_path)[0]

    def fake_revision(_image: str, _root: Path) -> None:
        return None

    monkeypatch.setattr(
        image_module,
        "build_app_archive",
        fake_build_app_archive,
    )
    monkeypatch.setattr(
        image_module,
        "_require_app_has_no_corpus",
        fake_binary_check,
    )
    monkeypatch.setattr(
        image_module,
        "_require_linux_amd64_image",
        fake_binary_check,
    )
    monkeypatch.setattr(
        image_module,
        "_require_qdrant_image",
        fake_root_check,
    )
    monkeypatch.setattr(image_module, "_save_compressed_image", fake_save)
    monkeypatch.setattr(
        image_module,
        "_inspect_artifact",
        fake_inspect,
    )
    monkeypatch.setattr(image_module, "_image_revision", fake_revision)

    with pytest.raises(IndustryImageError, match="revision label"):
        image_module.build_image_archives(
            repository_root=_repository_root(),
            revision=_GIT_SHA,
            output_dir=tmp_path,
            ocr_image="rag-industry-ocr:test",
        )


def test_app_image_scan_uses_no_network_and_rejects_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(arguments: tuple[str, ...], *, cwd: Path) -> str:
        del cwd
        calls.append(arguments)
        return ""

    monkeypatch.setattr(image_module, "_run_output", fake_run)
    image_module._require_app_has_no_corpus("rag-industry-app:test", Path())
    assert len(calls) == 1
    assert "--network" in calls[0]
    assert "none" in calls[0]
    assert ".docx" in calls[0][-1]


@pytest.mark.parametrize(
    ("branch", "status", "message"),
    (("main", "", "分支"), ("Industry", "?? local.txt\n", "clean")),
)
def test_source_gate_rejects_wrong_branch_or_dirty_tree(
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    status: str,
    message: str,
) -> None:
    def fake_git_output(root: Path, *arguments: str) -> str:
        del root
        if arguments == ("branch", "--show-current"):
            return f"{branch}\n"
        if arguments[:2] == ("status", "--porcelain"):
            return status
        raise AssertionError(arguments)

    monkeypatch.setattr(
        "scripts.build_industry_bundle._git_output",
        fake_git_output,
    )
    with pytest.raises(IndustryBuildError, match=message):
        require_industry_source(_repository_root())


def test_industry_smoke_uses_source_specific_positive_questions() -> None:
    smoke_path = _repository_root() / "evaluation" / "industry" / "smoke.jsonl"
    rows = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in smoke_path.read_text(encoding="utf-8").splitlines()
        )
    }

    assert rows["industry-smoke-005"]["question"] == (
        "GM-06《产品质量检验管理制度》有哪些要求？"
    )
    assert rows["industry-smoke-008"]["question"] == (
        "GM-03《质量管理制度》有哪些要求？"
    )
    assert rows["industry-smoke-012"]["question"] == (
        "《岗位职责规定》中质量检验由谁负责？"
    )
    assert rows["industry-smoke-012"]["expected_source_patterns"] == [
        "GM-02"
    ]
    assert rows["industry-smoke-016"]["question"] == (
        "GM-07《技术文件管理规定》和 GM-09《仓库管理制度》"
        "在归档保存与物资贮存搬运方面有什么不同？"
    )
    assert rows["industry-smoke-019"]["question"] == (
        "《产品开发全流程》有哪些阶段？"
    )
    assert rows["industry-smoke-020"]["question"] == (
        "《需求快验流程》中哪些环节可以灵活处理？"
    )
