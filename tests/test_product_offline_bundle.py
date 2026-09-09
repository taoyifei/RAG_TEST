from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from rag_app.product.asset_manifest import write_product_asset_manifest
from scripts import product_offline_bundle as bundle

_REVISION = "a" * 40
_APP_IMAGE_ID = "sha256:" + "1" * 64
_QDRANT_IMAGE_ID = "sha256:" + "2" * 64
_APP_IMAGE = "docx-rag:v1-candidate"
_QDRANT_IMAGE = "qdrant/qdrant:v1.18.3"


def _repository(root: Path) -> Path:
    source = Path(__file__).parents[1]
    root.mkdir()
    (root / "compose.yaml").write_bytes((source / "compose.yaml").read_bytes())
    (root / ".env.example").write_text(
        f"RAG_APP_IMAGE={_APP_IMAGE}\nRAG_QDRANT_IMAGE={_QDRANT_IMAGE}\n",
        encoding="utf-8",
    )
    return root


def _product_manifest(root: Path) -> bytes:
    app = root / "image-root"
    files = {
        "compatibility-manifest.json": b"{}\n",
        "frontend/assets/app.js": b"offline\n",
        "frontend/index.html": b'<div id="root"></div>\n',
        "migrations/universal_rag/001.sql": b"SELECT 1;\n",
        "openapi/openapi-v1.json": b"{}\n",
    }
    for relative, content in files.items():
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    manifest = app / "product-assets.json"
    write_product_asset_manifest(
        root=app,
        manifest_path=manifest,
        source_revision=_REVISION,
    )
    return manifest.read_bytes()


def _image_inspect(reference: str) -> bytes:
    is_app = reference == _APP_IMAGE
    payload = [
        {
            "Architecture": "amd64",
            "Config": {
                "Labels": {"org.opencontainers.image.revision": _REVISION}
                if is_app
                else {},
                "User": "rag:rag" if is_app else "1000:1000",
            },
            "Id": _APP_IMAGE_ID if is_app else _QDRANT_IMAGE_ID,
            "Os": "linux",
            "RepoDigests": []
            if is_app
            else [f"qdrant/qdrant@sha256:{'3' * 64}"],
        }
    ]
    return json.dumps(payload).encode()


def _fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    manifest: bytes,
) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()

    def capture(
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        del cwd, environment
        command = tuple(arguments)
        commands.append(command)
        if command[1:3] == ("image", "inspect"):
            return _image_inspect(command[3])
        if command[1] == "history":
            return b"safe image history\n"
        if command[1] == "run" and "cat" in command:
            return manifest
        if command[1] == "run":
            return json.dumps(
                {
                    "manifest_sha256": manifest_sha256,
                    "schema_version": "product-assets-v1",
                    "source_revision": _REVISION,
                    "verified_bytes": 1,
                    "verified_files": 5,
                }
            ).encode()
        raise AssertionError(f"unexpected capture command: {command}")

    def run_checked(
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        del cwd, environment
        command = tuple(arguments)
        commands.append(command)
        if command[1:3] == ("image", "save"):
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"deterministic docker image archive\n")

    monkeypatch.setattr(
        bundle,
        "require_clean_committed_head",
        lambda _: _REVISION,
    )
    monkeypatch.setattr(bundle, "_required_executable", lambda _: "docker")
    monkeypatch.setattr(bundle, "_capture", capture)
    monkeypatch.setattr(bundle, "_run_checked", run_checked)
    return commands


def test_bundle_build_consumes_same_images_without_building_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")
    manifest = _product_manifest(tmp_path)
    commands = _fake_docker(monkeypatch, manifest)

    first, first_sidecar = bundle.build_product_offline_bundle(
        repository_root=repository,
        output=tmp_path / "first.tar.gz",
    )
    second, second_sidecar = bundle.build_product_offline_bundle(
        repository_root=repository,
        output=tmp_path / "second.tar.gz",
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_sidecar.read_text(encoding="ascii").endswith(
        "  first.tar.gz\n"
    )
    assert second_sidecar.read_text(encoding="ascii").endswith(
        "  second.tar.gz\n"
    )
    assert not any(
        "build" in command or "npm" in command for command in commands
    )
    assert sum(command[1:3] == ("image", "save") for command in commands) == 2
    selfchecks = [
        command
        for command in commands
        if command[1] == "run" and "product-asset-selfcheck" in command
    ]
    assert selfchecks
    assert all(
        "--network" in command and "none" in command for command in selfchecks
    )

    report = bundle.verify_product_offline_bundle(
        archive=first,
        sidecar=first_sidecar,
        destination=tmp_path / "verified",
    )
    assert report.source_revision == _REVISION
    assert report.app_image_id == _APP_IMAGE_ID
    assert report.qdrant_image_id == _QDRANT_IMAGE_ID


def test_bundle_build_rejects_image_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")
    manifest = _product_manifest(tmp_path)
    _fake_docker(monkeypatch, manifest)
    monkeypatch.setattr(
        bundle,
        "require_clean_committed_head",
        lambda _: "b" * 40,
    )

    with pytest.raises(bundle.ProductBundleError, match="revision"):
        bundle.build_product_offline_bundle(
            repository_root=repository,
            output=tmp_path / "bundle.tar.gz",
        )


def test_bundle_build_rejects_dirty_or_uncommitted_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")

    def reject_checkout(_: Path) -> str:
        raise RuntimeError("RELEASE_CONTEXT_DIRTY_WORKTREE")

    monkeypatch.setattr(bundle, "require_clean_committed_head", reject_checkout)

    with pytest.raises(bundle.ProductBundleError, match="干净且已提交"):
        bundle.build_product_offline_bundle(
            repository_root=repository,
            output=tmp_path / "bundle.tar.gz",
        )


@pytest.mark.parametrize("forbidden", ["build", "bind"])
def test_bundle_compose_rejects_source_build_and_bind_mount(
    tmp_path: Path,
    forbidden: str,
) -> None:
    repository = _repository(tmp_path / "repository")
    compose = (repository / "compose.yaml").read_text(encoding="utf-8")
    if forbidden == "build":
        compose = compose.replace("  app:\n", "  app:\n    build: .\n")
    else:
        compose = compose.replace(
            "      - rag_data:/data",
            "      - ./src:/app/src",
        )
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(compose, encoding="utf-8")
    override = tmp_path / "compose.clean-room.yaml"
    override.write_text(
        "services: {}\nnetworks:\n  rag-egress:\n    internal: true\n",
        encoding="utf-8",
    )

    with pytest.raises(bundle.ProductBundleError, match=r"源码构建|非命名卷"):
        bundle._validate_compose_contract(compose_path, override)


def test_bundle_verify_rejects_tampered_outer_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")
    commands = _fake_docker(monkeypatch, _product_manifest(tmp_path))
    archive, sidecar = bundle.build_product_offline_bundle(
        repository_root=repository,
        output=tmp_path / "bundle.tar.gz",
    )
    assert commands
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="外层 SHA256"):
        bundle.verify_product_offline_bundle(
            archive=archive,
            sidecar=sidecar,
            destination=tmp_path / "verified",
        )


def test_bundle_static_stage_verifier_rejects_secret_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")
    _fake_docker(monkeypatch, _product_manifest(tmp_path))
    archive, sidecar = bundle.build_product_offline_bundle(
        repository_root=repository,
        output=tmp_path / "bundle.tar.gz",
    )
    extracted = bundle.safe_extract_bundle(
        archive,
        sidecar,
        tmp_path / "extracted",
        expected_top_level=bundle._TOP_LEVEL,
    )
    with (extracted / "compose.yaml").open("a", encoding="utf-8") as stream:
        stream.write(f"\n# sk-{'x' * 24}\n")

    with pytest.raises(bundle.ProductBundleError, match="Secret 形状"):
        bundle._verify_stage(extracted)


def test_clean_room_acceptance_returns_runtime_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")
    _fake_docker(monkeypatch, _product_manifest(tmp_path))
    archive, sidecar = bundle.build_product_offline_bundle(
        repository_root=repository,
        output=tmp_path / "bundle.tar.gz",
    )
    report = bundle.verify_product_offline_bundle(
        archive=archive,
        sidecar=sidecar,
        destination=tmp_path / "verified",
    )
    monkeypatch.setattr(bundle, "_negative_tamper_selfcheck", lambda **_: None)
    monkeypatch.setattr(bundle, "_scan_secret_shapes", lambda **_: None)
    monkeypatch.setattr(
        bundle,
        "_run_compose_smoke",
        lambda **_: bundle._CleanRoomSmokeIdentity(
            active_revision_id="irev_test",
            trace_id="trace_" + "f" * 32,
        ),
    )

    receipt = bundle.run_clean_room_acceptance(report)

    assert receipt.status == "PASS"
    assert receipt.source_revision == _REVISION
    assert receipt.app_image_id == _APP_IMAGE_ID
    assert receipt.qdrant_image_id == _QDRANT_IMAGE_ID
    assert receipt.app_user == "rag:rag"
    assert receipt.network_egress == "compose-internal-only"
    assert receipt.source_bind_mounts == 0
    assert receipt.pull_policy == "never"
    assert receipt.active_revision_id == "irev_test"
    assert receipt.trace_id == "trace_" + "f" * 32
    assert receipt.steps[-1] == "compose_cleanup"


def test_verify_cli_writes_exclusive_canonical_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = bundle.BundleVerificationReport(
        archive_sha256="a" * 64,
        source_revision=_REVISION,
        app_image_id=_APP_IMAGE_ID,
        qdrant_image_id=_QDRANT_IMAGE_ID,
        product_asset_manifest_sha256="b" * 64,
        extracted_path=str(tmp_path / "verified"),
    )
    monkeypatch.setattr(
        bundle,
        "verify_product_offline_bundle",
        lambda **_: report,
    )
    output = tmp_path / "receipt.json"
    arguments = [
        "verify",
        "--archive",
        str(tmp_path / "bundle.tar.gz"),
        "--destination",
        str(tmp_path / "destination"),
        "--report-output",
        str(output),
    ]

    assert bundle.main(arguments) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source_revision"] == _REVISION
    assert output.read_bytes().endswith(b"\n")
    assert bundle.main(arguments) == 1
