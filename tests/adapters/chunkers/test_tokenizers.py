from pathlib import Path

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from rag_app.adapters.tokenizers import (
    ConservativeEstimatedTokenCounter,
    DeterministicUtf8TokenCounter,
    HuggingFaceJsonTokenCounter,
)


def test_deterministic_and_estimated_counters_are_explicit() -> None:
    deterministic = DeterministicUtf8TokenCounter().count("中文 a")
    estimated = ConservativeEstimatedTokenCounter(
        safety_margin=0.15,
        model_compatibility=("jina", "qwen"),
    ).count("中文 a")
    assert deterministic.exact is True
    assert estimated.exact is False
    assert estimated.count > deterministic.count
    assert estimated.model_compatibility == ("jina", "qwen")


def test_huggingface_counter_only_loads_explicit_local_json(
    tmp_path: Path,
) -> None:
    tokenizer = Tokenizer(
        WordLevel(
            vocab={"[UNK]": 0, "hello": 1},
            unk_token="[UNK]",  # noqa: S106
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    counter = HuggingFaceJsonTokenCounter(
        path,
        model_compatibility=("local-model",),
    )
    assert counter.count("hello hello").count == 2
    assert counter.tokenizer_id.startswith("huggingface-json-sha256:")
    with pytest.raises(FileNotFoundError):
        HuggingFaceJsonTokenCounter(
            tmp_path / "missing.json",
            model_compatibility=(),
        )
