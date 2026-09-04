import hashlib
from pathlib import Path

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from rag_app.assets import AssetPaths, verify_offline_assets


def _write_manifest(root: Path, paths: tuple[str, ...]) -> Path:
    rows = []
    for relative in paths:
        digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        rows.append(f"{digest}  {relative}")
    manifest = root / "ASSETS.sha256"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return manifest


def test_asset_selfcheck_passes_without_remote_resources(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "frontend/assets").mkdir(parents=True)
    source_root = Path(__file__).parents[1]
    pipeline = source_root / "deployment/config/pipeline.json"
    retrieval = source_root / "deployment/config/retrieval.json"
    tokenizer = Tokenizer(
        WordLevel(
            vocab={"[UNK]": 0, "公开": 1, "合成": 2},
            unk_token="[UNK]",  # noqa: S106
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    (tmp_path / "config/pipeline.json").write_bytes(pipeline.read_bytes())
    (tmp_path / "config/retrieval.json").write_bytes(retrieval.read_bytes())
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "frontend/index.html").write_text(
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "frontend/assets/app.js").write_text(
        "document.querySelector('#root')", encoding="utf-8"
    )
    (tmp_path / "frontend/assets/styles.css").write_text(
        ":root { color-scheme: light; }", encoding="utf-8"
    )
    paths = (
        "config/pipeline.json",
        "config/retrieval.json",
        "tokenizer.json",
        "frontend/index.html",
        "frontend/assets/styles.css",
        "frontend/assets/app.js",
    )
    manifest = _write_manifest(tmp_path, paths)

    report = verify_offline_assets(
        AssetPaths(
            root=tmp_path,
            manifest_path=manifest,
            pipeline_path=tmp_path / "config/pipeline.json",
            retrieval_path=tmp_path / "config/retrieval.json",
            tokenizer_path=tmp_path / "tokenizer.json",
            frontend_dir=tmp_path / "frontend",
        )
    )

    assert report.verified_files == 6
    assert report.retrieval_state == "provisional"
    assert report.tokenizer_probe_tokens > 0


def test_asset_selfcheck_rejects_checksum_drift(tmp_path: Path) -> None:
    asset = tmp_path / "asset.txt"
    asset.write_text("before", encoding="utf-8")
    manifest = _write_manifest(tmp_path, ("asset.txt",))
    asset.write_text("after", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA256"):
        verify_offline_assets(
            AssetPaths(
                root=tmp_path,
                manifest_path=manifest,
                pipeline_path=tmp_path / "missing-pipeline.json",
                retrieval_path=tmp_path / "missing-retrieval.json",
                tokenizer_path=tmp_path / "missing-tokenizer.json",
                frontend_dir=tmp_path / "missing-frontend",
            )
        )
