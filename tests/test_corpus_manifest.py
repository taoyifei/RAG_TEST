from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.freeze_corpus_manifest import (
    freeze_corpus_manifest,
    load_corpus_manifest,
    stage_verified_corpus,
    verify_corpus,
)


def _write_docs(root: Path, count: int) -> None:
    for index in range(count):
        path = root / f"group-{index % 7}" / f"doc-{index:04d}.docx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"docx-{index}".encode())


@pytest.mark.parametrize("count", (1, 6, 1000))
def test_freeze_verify_and_stage_variable_corpus_sizes(
    tmp_path: Path,
    count: int,
) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs, count)
    manifest_path = tmp_path / "corpus-manifest.json"

    frozen = freeze_corpus_manifest(
        docs_root=docs,
        corpus_id=f"corpus-{count}",
        output_path=manifest_path,
    )
    verified = verify_corpus(
        docs_root=docs,
        manifest_path=manifest_path,
    )
    staged_docs = tmp_path / "staged/docs"
    staged = stage_verified_corpus(
        docs_root=docs,
        manifest_path=manifest_path,
        destination=staged_docs,
    )

    assert frozen == verified == staged
    assert frozen.document_count == count
    assert frozen.total_bytes == sum(
        path.stat().st_size for path in docs.rglob("*.docx")
    )
    assert [item.path for item in frozen.documents] == sorted(
        item.path for item in frozen.documents
    )
    assert verify_corpus(
        docs_root=staged_docs,
        manifest_path=manifest_path,
    ) == frozen


@pytest.mark.parametrize("mutation", ("add", "delete", "modify"))
def test_verify_rejects_any_docx_set_or_content_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs, 3)
    manifest = tmp_path / "manifest.json"
    freeze_corpus_manifest(
        docs_root=docs,
        corpus_id="frozen",
        output_path=manifest,
    )
    first = next(docs.rglob("*.docx"))
    if mutation == "add":
        (docs / "extra.docx").write_bytes(b"extra")
    elif mutation == "delete":
        first.unlink()
    else:
        first.write_bytes(b"modified")

    with pytest.raises(ValueError, match="exact set"):
        verify_corpus(docs_root=docs, manifest_path=manifest)


def test_freeze_rejects_symlink_and_zone_identifier(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs, 1)
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")
    (docs / "linked.docx").symlink_to(outside)

    with pytest.raises(ValueError, match="符号链接"):
        freeze_corpus_manifest(
            docs_root=docs,
            corpus_id="unsafe",
            output_path=tmp_path / "manifest.json",
        )

    (docs / "linked.docx").unlink()
    (docs / "doc.docx.Zone.Identifier").write_bytes(b"zone")
    with pytest.raises(ValueError, match=r"Zone\.Identifier"):
        freeze_corpus_manifest(
            docs_root=docs,
            corpus_id="unsafe",
            output_path=tmp_path / "manifest.json",
        )


def test_freeze_rejects_casefold_path_collision(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Report.docx").write_bytes(b"one")
    (docs / "report.DOCX").write_bytes(b"two")

    with pytest.raises(ValueError, match="case-fold"):
        freeze_corpus_manifest(
            docs_root=docs,
            corpus_id="collision",
            output_path=tmp_path / "manifest.json",
        )


def test_manifest_tamper_and_noncanonical_json_are_rejected(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs, 2)
    manifest = tmp_path / "manifest.json"
    freeze_corpus_manifest(
        docs_root=docs,
        corpus_id="tamper",
        output_path=manifest,
    )
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["documents"][0]["size"] += 1
    manifest.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"total_bytes|digest|canonical"):
        load_corpus_manifest(manifest)


def test_manifest_rejects_escape_path_even_with_valid_field_types(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    value = {
        "corpus_digest": "0" * 64,
        "corpus_id": "escape",
        "document_count": 1,
        "documents": [
            {
                "path": "../secret.docx",
                "sha256": "0" * 64,
                "size": 1,
            }
        ],
        "schema_version": "1",
        "total_bytes": 1,
    }
    manifest.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="越界"):
        load_corpus_manifest(manifest)
