from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_app.cli import main as cli_main
from rag_app.product.asset_manifest import (
    PRODUCT_ASSET_SCHEMA_VERSION,
    load_product_asset_manifest,
    verify_product_asset_manifest,
    write_product_asset_manifest,
)

_REVISION = "a" * 40


def _asset_root(root: Path) -> Path:
    files = {
        "compatibility-manifest.json": b'{"compatible":true}\n',
        "frontend/assets/app.js": b"console.log('offline');\n",
        "frontend/assets/style.css": b"body { color: #123; }\n",
        "frontend/index.html": b'<!doctype html><div id="root"></div>\n',
        "migrations/universal_rag/001.sql": b"SELECT 1;\n",
        "openapi/openapi-v1.json": b'{"openapi":"3.1.0"}\n',
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    return root


def _write(root: Path) -> Path:
    manifest = root / "product-assets.json"
    write_product_asset_manifest(
        root=root,
        manifest_path=manifest,
        source_revision=_REVISION,
    )
    return manifest


def test_product_manifest_is_canonical_and_verifies_exact_asset_set(
    tmp_path: Path,
) -> None:
    root = _asset_root(tmp_path / "app")
    manifest_path = _write(root)

    manifest = load_product_asset_manifest(manifest_path)
    report = verify_product_asset_manifest(
        root=root,
        manifest_path=manifest_path,
        expected_source_revision=_REVISION,
    )

    assert manifest.schema_version == PRODUCT_ASSET_SCHEMA_VERSION
    assert [item.path for item in manifest.files] == sorted(
        item.path for item in manifest.files
    )
    assert manifest_path.read_bytes() == manifest.canonical_bytes()
    assert report.verified_files == 6
    assert report.verified_bytes == manifest.total_bytes
    assert len(report.manifest_sha256) == 64


def test_product_manifest_is_independent_of_creation_order(
    tmp_path: Path,
) -> None:
    first = _asset_root(tmp_path / "first")
    second = tmp_path / "second"
    for source in sorted(
        (path for path in first.rglob("*") if path.is_file()),
        reverse=True,
    ):
        target = second / source.relative_to(first)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o644)

    assert _write(first).read_bytes() == _write(second).read_bytes()


@pytest.mark.parametrize("mutation", ["content", "mode"])
def test_product_manifest_rejects_tampered_asset(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _asset_root(tmp_path / "app")
    manifest = _write(root)
    target = root / "frontend/assets/app.js"
    if mutation == "content":
        target.write_bytes(b"console.log('tampered');\n")
        error = "SHA256"
    else:
        target.chmod(0o600)
        error = "权限"

    with pytest.raises(ValueError, match=error):
        verify_product_asset_manifest(
            root=root,
            manifest_path=manifest,
            expected_source_revision=_REVISION,
        )


def test_product_manifest_rejects_missing_and_extra_assets(
    tmp_path: Path,
) -> None:
    missing_root = _asset_root(tmp_path / "missing")
    missing_manifest = _write(missing_root)
    (missing_root / "frontend/assets/app.js").unlink()
    with pytest.raises(ValueError, match="缺失"):
        verify_product_asset_manifest(
            root=missing_root,
            manifest_path=missing_manifest,
        )

    extra_root = _asset_root(tmp_path / "extra")
    extra_manifest = _write(extra_root)
    (extra_root / "frontend/assets/unlisted.js").write_text(
        "unlisted",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="额外成员"):
        verify_product_asset_manifest(
            root=extra_root,
            manifest_path=extra_manifest,
        )


def test_product_manifest_rejects_symlink_and_traversal(tmp_path: Path) -> None:
    root = _asset_root(tmp_path / "app")
    outside = tmp_path / "outside.js"
    outside.write_text("outside", encoding="utf-8")
    (root / "frontend/assets/link.js").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        write_product_asset_manifest(
            root=root,
            manifest_path=root / "product-assets.json",
            source_revision=_REVISION,
        )
    with pytest.raises(ValueError, match="越界或不规范"):
        write_product_asset_manifest(
            root=root,
            manifest_path=root / "product-assets.json",
            source_revision=_REVISION,
            include_paths=("../outside.js",),
        )


def test_product_manifest_resolves_relative_output_inside_asset_root(
    tmp_path: Path,
) -> None:
    root = _asset_root(tmp_path / "app")

    write_product_asset_manifest(
        root=root,
        manifest_path=Path("product-assets.json"),
        source_revision=_REVISION,
    )

    assert (root / "product-assets.json").is_file()
    with pytest.raises(ValueError, match="越出允许根目录"):
        write_product_asset_manifest(
            root=root,
            manifest_path=Path("../escaped.json"),
            source_revision=_REVISION,
        )
    assert not (tmp_path / "escaped.json").exists()


def test_product_manifest_rejects_noncanonical_json_and_revision_drift(
    tmp_path: Path,
) -> None:
    root = _asset_root(tmp_path / "app")
    manifest_path = _write(root)
    payload = json.loads(manifest_path.read_bytes())
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="规范 JSON"):
        load_product_asset_manifest(manifest_path)

    manifest_path.unlink()
    _write(root)
    with pytest.raises(ValueError, match="source revision 不一致"):
        verify_product_asset_manifest(
            root=root,
            manifest_path=manifest_path,
            expected_source_revision="b" * 40,
        )


def test_product_asset_selfcheck_cli_keeps_legacy_command_separate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _asset_root(tmp_path / "app")
    manifest = _write(root)

    exit_code = cli_main(
        [
            "product-asset-selfcheck",
            "--root",
            str(root),
            "--manifest",
            str(manifest),
            "--expected-revision",
            _REVISION,
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["schema_version"] == PRODUCT_ASSET_SCHEMA_VERSION
    assert report["source_revision"] == _REVISION
    assert report["verified_files"] == 6
