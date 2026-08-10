from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from deployment.industry import serving_update_selfcheck
from scripts import build_industry_app_update
from scripts.build_industry_bundle import IndustrySourceIdentity
from scripts.industry_bundle.images import ImageArtifact

_REVISION = "a" * 40


def test_industry_app_update_builds_exact_serving_bundle_contract(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[1]
    archive_bytes = b"verified-industry-app-image"

    monkeypatch.setattr(
        build_industry_app_update,
        "require_industry_source",
        lambda _root: IndustrySourceIdentity(
            git_sha=_REVISION,
            main_sha="b" * 40,
            source_date_epoch=1_786_000_000,
        ),
    )
    monkeypatch.setattr(
        build_industry_app_update,
        "prepare_project_wheel",
        lambda _root, _revision: None,
    )

    def build_image(
        *,
        repository_root: Path,
        revision: str,
        output_dir: Path,
    ) -> ImageArtifact:
        assert repository_root == root.resolve()
        assert revision == _REVISION
        archive = output_dir / "app-image.tar.gz"
        archive.write_bytes(archive_bytes)
        return ImageArtifact(
            name="app",
            ref=f"docx-rag:{_REVISION[:12]}",
            image_id="sha256:" + "c" * 64,
            platform="linux/amd64",
            revision=_REVISION,
            archive_name=archive.name,
            archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            manifest_digest="sha256:" + "d" * 64,
            config_digest="sha256:" + "e" * 64,
        )

    monkeypatch.setattr(
        build_industry_app_update,
        "build_app_image_archive",
        build_image,
    )
    monkeypatch.setattr(
        build_industry_app_update,
        "_git_output",
        lambda *_args: "",
    )

    output = build_industry_app_update.build_industry_app_update(
        repository_root=root,
        output_parent=tmp_path,
    )

    assert {path.name for path in output.iterdir()} == {
        "SERVER_UPDATE_COMMANDS.txt",
        "UPDATE_MANIFEST.json",
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "package_selfcheck.py",
        "serving-runtime.tar.gz",
        "serving-runtime.tar.gz.sha256",
        "update-app.sh",
    }
    manifest = json.loads((output / "UPDATE_MANIFEST.json").read_bytes())
    assert manifest["branch"] == "Industry"
    assert manifest["target"] == {
        "alias": "rag-industry-active",
        "project": "rag-industry",
        "service": "rag-industry-app",
    }
    assert manifest["image"]["ref"] == f"docx-rag:{_REVISION[:12]}"
    assert manifest["image"]["id"] == "sha256:" + "c" * 64
    assert manifest["image"]["revision"] == _REVISION
    assert manifest["image"]["platform"] == "linux/amd64"
    assert manifest["index_fingerprint"]["reindex_required"] is False
    assert (
        manifest["index_fingerprint"]["source"]
        == (manifest["index_fingerprint"]["target"])
    )
    assert manifest["serving_fingerprint"]["source"] == (
        "sha256:41dc694db23d1895b08a703e058fc5ea6d7511da9484e42268c6bb3258c81c9b"
    )
    assert (
        manifest["serving_fingerprint"]["target"]
        != (manifest["serving_fingerprint"]["source"])
    )
    assert manifest["source_compatibility"]["trace_v2_read_compatible"] is True
    assert manifest["revision"] == _REVISION
    assert manifest["schema_version"] == "2"
    assert manifest["package_contract_revision"] == (
        "industry-serving-update-v2"
    )
    assert manifest["runtime"]["root"] == (f"serving-runtime/{_REVISION[:12]}")
    assert manifest["ui"] == {
        "allow_insecure_http": True,
        "cookie_secure": False,
        "query_auth_mode": "same_origin_session",
        "session_ttl_seconds": 1800,
    }
    assert manifest["trace"] == {
        "question_capture": "plaintext",
        "question_retention_seconds": 604800,
        "schema_version": 2,
    }
    assert (output / "app-image.tar.gz.sha256").read_text(encoding="ascii") == (
        f"{hashlib.sha256(archive_bytes).hexdigest()}  app-image.tar.gz\n"
    )
    assert (
        (output / "serving-runtime.tar.gz.sha256")
        .read_text(encoding="ascii")
        .endswith("  serving-runtime.tar.gz\n")
    )
    script = (output / "update-app.sh").read_text(encoding="utf-8")
    assert "rag-industry-app" in script
    assert "--force-recreate" in script
    update_command = (
        "up -d --no-deps --no-build --pull never --force-recreate "
        "rag-industry-app"
    )
    normalized_script = " ".join(script.replace("\\\n", " ").split())
    assert normalized_script.count(update_command) == 1
    assert "up -d --force-recreate rag-industry-worker" not in script
    assert "up -d --force-recreate rag-industry-ocr" not in script
    assert "up -d --force-recreate rag-industry-qdrant" not in script
    assert "worker --once" not in script

    extracted = tmp_path / "fresh-extraction"
    runtime_root = serving_update_selfcheck.safe_extract_runtime(
        output, extracted
    )
    assert runtime_root.name == _REVISION[:12]
    assert {
        str(path.relative_to(runtime_root))
        for path in runtime_root.rglob("*")
        if path.is_file()
    } == set(manifest["runtime"]["files"])
    with tarfile.open(output / "serving-runtime.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
    directory_names = [member.name for member in members if member.isdir()]
    file_names = [member.name for member in members if member.isfile()]
    assert directory_names == sorted(directory_names)
    assert file_names == sorted(file_names)
    assert members == [
        *[member for member in members if member.isdir()],
        *[member for member in members if member.isfile()],
    ]
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(not member.uname and not member.gname for member in members)
    assert all(
        member.mtime == manifest["runtime"]["source_date_epoch"]
        for member in members
    )

    for name, mutate in (
        (
            "bad-source",
            lambda value: value["source_compatibility"].pop(
                "trace_v2_read_compatible"
            ),
        ),
        (
            "bad-image-id",
            lambda value: value["image"].update({"id": "invalid"}),
        ),
        (
            "bad-config-binding",
            lambda value: value["config_files"].update(
                {"pipeline.json": "0" * 64}
            ),
        ),
    ):
        tampered = tmp_path / name
        shutil.copytree(output, tampered)
        tampered_manifest = json.loads(
            (tampered / "UPDATE_MANIFEST.json").read_bytes()
        )
        mutate(tampered_manifest)
        (tampered / "UPDATE_MANIFEST.json").write_text(
            json.dumps(tampered_manifest), encoding="utf-8"
        )
        with pytest.raises(serving_update_selfcheck.PackageSelfcheckError):
            serving_update_selfcheck.verify_package(tampered)


@pytest.mark.parametrize(
    "case",
    (
        "traversal",
        "absolute",
        "symlink",
        "hardlink",
        "fifo",
        "device",
        "setuid",
        "group-writable",
        "extra",
        "duplicate",
    ),
)
def test_runtime_archive_rejects_unsafe_tar_members(  # noqa: PLR0915
    tmp_path: Path,
    case: str,
) -> None:
    root = "serving-runtime/" + "a" * 12
    archive_path = tmp_path / f"{case}.tar.gz"
    safe_name = f"{root}/payload.txt"
    payload = b"safe"
    expected = {"payload.txt": hashlib.sha256(payload).hexdigest()}
    manifest = {
        "runtime": {
            "files": expected,
            "root": root,
            "source_date_epoch": 1_786_000_000,
        }
    }
    with tarfile.open(archive_path, mode="w:gz") as archive:
        directory = tarfile.TarInfo("serving-runtime/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = 1_786_000_000
        archive.addfile(directory)
        runtime_directory = tarfile.TarInfo(root + "/")
        runtime_directory.type = tarfile.DIRTYPE
        runtime_directory.mode = 0o755
        runtime_directory.mtime = 1_786_000_000
        archive.addfile(runtime_directory)
        member = tarfile.TarInfo(safe_name)
        member.mode = 0o644
        member.mtime = 1_786_000_000
        member.size = len(payload)
        if case == "traversal":
            member.name = f"{root}/../escape"
        elif case == "absolute":
            member.name = "/absolute"
        elif case == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "target"
            member.size = 0
        elif case == "hardlink":
            member.type = tarfile.LNKTYPE
            member.linkname = safe_name
            member.size = 0
        elif case == "fifo":
            member.type = tarfile.FIFOTYPE
            member.size = 0
        elif case == "device":
            member.type = tarfile.CHRTYPE
            member.size = 0
        elif case == "setuid":
            member.mode = 0o4755
        elif case == "group-writable":
            member.mode = 0o664
        elif case == "extra":
            member.name = f"{root}/extra.txt"
        archive.addfile(
            member, None if member.size == 0 else io.BytesIO(payload)
        )
        if case == "duplicate":
            duplicate = tarfile.TarInfo(safe_name)
            duplicate.mode = 0o644
            duplicate.mtime = 1_786_000_000
            duplicate.size = len(payload)
            archive.addfile(duplicate, io.BytesIO(payload))

    with pytest.raises(serving_update_selfcheck.PackageSelfcheckError):
        serving_update_selfcheck.verify_runtime_archive(archive_path, manifest)


def test_runtime_archive_is_deterministic(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    identity = IndustrySourceIdentity(
        git_sha=_REVISION,
        main_sha="b" * 40,
        source_date_epoch=1_786_000_000,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_identity = build_industry_app_update._build_runtime_archive(
        root, first, identity
    )
    second_identity = build_industry_app_update._build_runtime_archive(
        root, second, identity
    )

    assert first_identity == second_identity
    assert (first / "serving-runtime.tar.gz").read_bytes() == (
        second / "serving-runtime.tar.gz"
    ).read_bytes()
