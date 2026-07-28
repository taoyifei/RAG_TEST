import json
from pathlib import Path

import pytest

from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import (
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
)
from rag_app.corpus_policy import CorpusPolicy
from rag_app.runtime import load_pipeline


def _metadata(
    *,
    status: str = "active",
    authority: str = "official",
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> dict[str, object]:
    return {
        "document_status": status,
        "authority_level": authority,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def _policy_payload(
    overrides: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "defaults": _metadata(),
        "overrides": [] if overrides is None else overrides,
    }


def _write_policy(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_policy_defaults_apply_before_chunk_creation(tmp_path: Path) -> None:
    input_root = tmp_path / "docs"
    input_root.mkdir()
    (input_root / "a.docx").write_bytes(b"synthetic")
    policy = CorpusPolicy.load(
        _write_policy(tmp_path / "policy.json", _policy_payload())
    )

    resolved = policy.resolve(
        input_root=input_root,
        discovered_paths=("a.docx",),
    )
    chunks = Chunker(
        ChunkerConfig(32, 64, 8),
        Utf8TokenCounter(),
        pipeline_fingerprint="sha256:" + "f" * 64,
    ).chunk(
        "src_" + "a" * 32,
        "sha256:" + "a" * 64,
        [
            Element(
                element_id="element-1",
                kind=ElementKind.PARAGRAPH,
                text="公开合成证据",
                locator=Locator(
                    file_path="a.docx",
                    paragraph_index=1,
                    fragment="公开合成证据",
                ),
                content_sha256="a" * 64,
            )
        ],
        metadata=resolved["a.docx"],
    )

    assert chunks[0].document_status == "active"
    assert chunks[0].authority_level == "official"
    assert chunks[0].effective_from is None
    assert chunks[0].effective_to is None


@pytest.mark.parametrize(
    "override_path",
    (
        "/absolute.docx",
        "../escape.docx",
        "nested\\windows.docx",
    ),
)
def test_policy_rejects_unsafe_override_paths(
    tmp_path: Path,
    override_path: str,
) -> None:
    payload = _policy_payload(
        [{"path": override_path, **_metadata()}]
    )

    with pytest.raises(ValueError, match="路径"):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


@pytest.mark.parametrize(
    "paths",
    (
        ("a.docx", "a.docx"),
        ("A.docx", "a.docx"),
    ),
)
def test_policy_rejects_duplicate_and_casefold_paths(
    tmp_path: Path,
    paths: tuple[str, str],
) -> None:
    payload = _policy_payload(
        [{"path": path, **_metadata()} for path in paths]
    )

    with pytest.raises(ValueError, match=r"重复|大小写"):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


@pytest.mark.parametrize(
    "metadata",
    (
        _metadata(effective_from="2026-01-01T00:00:00"),
        _metadata(
            effective_from="2026-02-01T00:00:00Z",
            effective_to="2026-01-01T00:00:00Z",
        ),
    ),
)
def test_policy_rejects_naive_or_reversed_dates(
    tmp_path: Path,
    metadata: dict[str, object],
) -> None:
    payload = _policy_payload([{"path": "a.docx", **metadata}])

    with pytest.raises(ValueError, match=r"时区|晚于"):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


def test_policy_rejects_unknown_fields_and_unknown_overrides(
    tmp_path: Path,
) -> None:
    payload = _policy_payload(
        [{"path": "missing.docx", **_metadata()}]
    )
    payload["unknown"] = True
    path = _write_policy(tmp_path / "policy.json", payload)

    with pytest.raises(ValueError, match="unknown"):
        CorpusPolicy.load(path)

    payload.pop("unknown")
    policy = CorpusPolicy.load(_write_policy(path, payload))
    input_root = tmp_path / "docs"
    input_root.mkdir()
    (input_root / "a.docx").write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="未发现"):
        policy.resolve(
            input_root=input_root,
            discovered_paths=("a.docx",),
        )


def test_policy_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        (
            '{"schema_version":"1","schema_version":"1",'
            '"defaults":{"document_status":"active",'
            '"authority_level":"official","effective_from":null,'
            '"effective_to":null},"overrides":[]}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复"):
        CorpusPolicy.load(path)


def test_policy_override_requires_complete_metadata(tmp_path: Path) -> None:
    payload = _policy_payload([{"path": "a.docx"}])

    with pytest.raises(ValueError):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


@pytest.mark.parametrize(
    "effective_from",
    (
        1_700_000_000,
        1_700_000_000.5,
        "2026-01-01 00:00:00Z",
    ),
)
def test_policy_rejects_non_rfc3339_date_inputs(
    tmp_path: Path,
    effective_from: object,
) -> None:
    metadata = _metadata()
    metadata["effective_from"] = effective_from
    payload = _policy_payload([{"path": "a.docx", **metadata}])

    with pytest.raises(ValueError):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


@pytest.mark.parametrize(
    ("status", "authority"),
    (
        ("unspecified", "official"),
        ("active", "unspecified"),
    ),
)
def test_policy_rejects_unspecified_metadata(
    tmp_path: Path,
    status: str,
    authority: str,
) -> None:
    payload = _policy_payload(
        [
            {
                "path": "a.docx",
                **_metadata(status=status, authority=authority),
            }
        ]
    )

    with pytest.raises(ValueError, match="unspecified"):
        CorpusPolicy.load(_write_policy(tmp_path / "policy.json", payload))


def test_policy_rejects_symlink_override_boundary(tmp_path: Path) -> None:
    input_root = tmp_path / "docs"
    input_root.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"synthetic")
    (input_root / "link.docx").symlink_to(outside)
    policy = CorpusPolicy.load(
        _write_policy(
            tmp_path / "policy.json",
            _policy_payload(
                [{"path": "link.docx", **_metadata()}]
            ),
        )
    )

    with pytest.raises(ValueError, match=r"符号链接|越界"):
        policy.resolve(
            input_root=input_root,
            discovered_paths=("link.docx",),
        )


def test_policy_semantic_digest_is_canonical_and_sensitive(
    tmp_path: Path,
) -> None:
    first = CorpusPolicy.load(
        _write_policy(
            tmp_path / "first.json",
            _policy_payload(
                [
                    {
                        "path": "b.docx",
                        **_metadata(status="draft"),
                    },
                    {"path": "a.docx", **_metadata()},
                ]
            ),
        )
    )
    reordered = CorpusPolicy.load(
        _write_policy(
            tmp_path / "reordered.json",
            {
                "overrides": list(
                    reversed(
                        first.model_dump(mode="json")["overrides"]
                    )
                ),
                "defaults": first.model_dump(mode="json")["defaults"],
                "schema_version": "1",
            },
        )
    )
    changed = CorpusPolicy.load(
        _write_policy(
            tmp_path / "changed.json",
            _policy_payload(
                [
                    {
                        "path": "b.docx",
                        **_metadata(status="active"),
                    },
                    {"path": "a.docx", **_metadata()},
                ]
            ),
        )
    )

    assert first.semantic_sha256() == reordered.semantic_sha256()
    assert first.semantic_sha256() != changed.semantic_sha256()


def test_checked_in_policy_contains_no_private_overrides() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = CorpusPolicy.load(
        root / "deployment/config/corpus-policy.json"
    )
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")

    assert policy.defaults == DocumentMetadata(
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    assert policy.overrides == ()
    assert pipeline.corpus_policy_sha256 == policy.semantic_sha256()
