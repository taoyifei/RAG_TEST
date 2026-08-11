from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
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
        config_directory: Path | None = None,
        assets_manifest_path: Path | None = None,
    ) -> ImageArtifact:
        assert repository_root == root.resolve()
        assert revision == _REVISION
        assert config_directory is not None
        assert assets_manifest_path is not None
        assert hashlib.sha256(
            (config_directory / "pipeline.json").read_bytes()
        ).hexdigest() == (
            "9f139e0995c60998e1e15098700a78580d7ff279dc148b0b593671687a8c1cdc"
        )
        assert "deployment/config/pipeline.json" in (
            assets_manifest_path.read_text(encoding="ascii")
        )
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
    commands = output / "SERVER_UPDATE_COMMANDS.txt"
    syntax = subprocess.run(  # noqa: S603
        ["/usr/bin/bash", "-n", str(commands)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    command_text = commands.read_text(encoding="utf-8")
    assert command_text.splitlines()[0] == "set -euo pipefail"
    assert "command -v flock" in command_text
    assert "transaction-state.json" in command_text
    assert "run-index.sh" in command_text
    manifest = json.loads((output / "UPDATE_MANIFEST.json").read_bytes())
    assert len(manifest["runtime"]["files"]) == 19
    assert "finalize-app-update.sh" in manifest["runtime"]["files"]
    assert "compose_check.py" in manifest["runtime"]["files"]
    assert "rollback-app-update-core.sh" in manifest["runtime"]["files"]
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
    assert manifest["index_fingerprint"]["source"] == (
        "sha256:d2497bc2813f9281d3cb5bf5f6ac9c9ed36e7aec5b96f1333039a220018b6b58"
    )
    assert manifest["serving_fingerprint"]["source"] == (
        "sha256:cd69c286315b9adc41a9d6e092efbf54f1905150d556a6e31437780508b47b8e"
    )
    assert (
        manifest["serving_fingerprint"]["target"]
        != (manifest["serving_fingerprint"]["source"])
    )
    assert manifest["serving_fingerprint"]["target"] == (
        "sha256:06b6a71f16098632a8f586e4f366e9dc925ad89835025e7bfe33730296106dc6"
    )
    source_config = manifest["source_compatibility"]["config_files"]
    assert source_config == build_industry_app_update._SOURCE_CONFIG_SHA256
    assert {
        name
        for name, digest in manifest["config_files"].items()
        if digest != source_config[name]
    } == {"pipeline.json"}
    assert manifest["source_compatibility"]["trace_compatibility"] == {
        "accepted_user_versions": [0, 1, 2],
        "legacy_v0_profile": "industry-trace-2c4-v0",
        "target_schema_version": 2,
    }
    assert manifest["source_compatibility"]["source_release"] == {
        "manifest_sha256": (
            "2db506689d7ed39ac960c63ba7f833b9076901072f3202bd466b8eb60f2d9af5"
        ),
        "package_contract_revision": "industry-package-reuse-images-v1",
        "release_id": "2c4cf220c7cf-87860c8b7496",
        "revision": build_industry_app_update._OLD_REVISION,
    }
    assert manifest["source_compatibility"][
        "trusted_last_good_revisions"
    ][0] == build_industry_app_update._OLD_REVISION
    assert manifest["revision"] == _REVISION
    assert manifest["schema_version"] == "3"
    assert manifest["package_contract_revision"] == (
        "industry-serving-update-v3"
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
    assert normalized_script.count(update_command) == 2
    assert "up -d --force-recreate rag-industry-worker" not in script
    assert "up -d --force-recreate rag-industry-ocr" not in script
    assert "up -d --force-recreate rag-industry-qdrant" not in script
    assert "worker --once" not in script
    assert "manual withdrawal is valid only for a verified attempt" in (
        command_text
    )
    assert "target may be healthy, unhealthy, stopped, or missing" in (
        command_text
    )
    assert "precheck failure leaves the attempt verified" in command_text
    assert "Only a failure after rolling_back" in command_text
    assert "${RUNTIME_DIR}/rollback-app-update.sh" in command_text
    assert '"${ENV_FILE}" "${VERIFIED_ATTEMPT}"' in command_text

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
                "trace_compatibility"
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

    source = build_industry_app_update._load_source_release(
        root
        / "artifacts/industry-deploy/2c4cf220c7cf-87860c8b7496"
    )
    first_target = build_industry_app_update._build_target_config(
        root, source, tmp_path / "first-config"
    )
    second_target = build_industry_app_update._build_target_config(
        root, source, tmp_path / "second-config"
    )
    first_identity = build_industry_app_update._build_runtime_archive(
        root, first, identity, first_target
    )
    second_identity = build_industry_app_update._build_runtime_archive(
        root, second, identity, second_target
    )

    assert first_identity == second_identity
    assert (first / "serving-runtime.tar.gz").read_bytes() == (
        second / "serving-runtime.tar.gz"
    ).read_bytes()
