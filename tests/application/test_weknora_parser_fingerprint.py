"""候选格式解析器的实际身份必须进入索引指纹。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.parsers.document_router import (
    wrap_weknora_document_parser,
)
from rag_app.composition.p06_runtime import (
    build_p06_runtime,
    resolved_contracts,
)
from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    _product_profile,
)
from rag_app.core.models import WeKnoraChunkingPolicy


def test_enabled_formats_change_index_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = (
        Path(__file__).resolve().parents[2]
        / "configs/profiles/dev-p06-memory.json"
    )
    with build_p06_runtime(profile, data_dir=tmp_path / "old") as old:
        old_fingerprint = old.components.index_fingerprint
        assert (
            old.components.parser.descriptor.name != "weknora-document-router"
        )

    monkeypatch.setenv("RAG_WK_DOCUMENT_FORMATS", "md,txt")
    with build_p06_runtime(
        profile,
        data_dir=tmp_path / "candidate",
        parser_resolver=wrap_weknora_document_parser,
    ) as candidate:
        assert candidate.components.index_fingerprint != old_fingerprint
        assert candidate.components.parser.descriptor.name == (
            "weknora-document-router"
        )
        assert resolved_contracts(candidate.components)["parser_identity"] == (
            candidate.components.parser.descriptor.model_dump(mode="json")
        )


def test_product_candidate_profile_selects_go_chunker(tmp_path: Path) -> None:
    settings = ProductRuntimeSettings(
        data_dir=tmp_path / "data",
        frontend_dir=tmp_path / "frontend",
        bootstrap_token_file=tmp_path / "bootstrap-token",
        weknora_chunker_mode="parent-child",
    )

    profile = _product_profile(settings)

    assert profile.components.chunker == "weknora-adaptive-parent-child-v1"
    assert isinstance(profile.chunking, WeKnoraChunkingPolicy)
