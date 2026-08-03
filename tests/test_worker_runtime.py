"""单索引 worker 冻结配置门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.runtime import load_pipeline
from rag_app.settings import RetrievalSettings
from rag_app.worker_runtime import require_indexable_configuration


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_provisional_retrieval_refuses_indexing() -> None:
    root = _project_root()
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")
    retrieval = RetrievalSettings.load(
        root / "deployment/config/retrieval.json"
    )

    with pytest.raises(ValueError, match="冻结集"):
        require_indexable_configuration(pipeline, retrieval, None)
