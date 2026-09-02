import json
import math
import os
import subprocess
import sys

import pytest

from rag_app.core.identifiers import (
    canonical_json,
    chunk_id,
    deterministic_id,
    document_version_id,
    new_id,
    validate_id,
)


def test_random_and_deterministic_ids_have_controlled_prefixes() -> None:
    random_id = new_id("prj")
    assert validate_id("prj", random_id) == random_id

    first = deterministic_id("doc", "logical-document")
    second = deterministic_id("doc", "logical-document")
    assert first == second
    assert validate_id("doc", first) == first


@pytest.mark.parametrize("prefix", ["../x", "src", "", "doc_"])
def test_invalid_prefixes_are_rejected(prefix: str) -> None:
    with pytest.raises(ValueError):
        new_id(prefix)


def test_document_version_and_chunk_ids_bind_content() -> None:
    content_hash = "a" * 64
    document_id = deterministic_id("doc", "logical-document")
    version = document_version_id(document_id, content_hash)
    assert version != document_version_id(
        deterministic_id("doc", "other-document"),
        content_hash,
    )
    first = chunk_id(
        version,
        f"sha256:{'b' * 64}",
        ({"node": "node-1", "start": 0, "end": 3},),
        "c" * 64,
    )
    second = chunk_id(
        version,
        f"sha256:{'b' * 64}",
        ({"node": "node-1", "start": 0, "end": 4},),
        "c" * 64,
    )
    assert first != second


def test_canonical_json_sorts_keys_and_preserves_bool_identity() -> None:
    assert canonical_json({"b": 2, "a": "中文"}) == '{"a":"中文","b":2}'
    assert canonical_json(True) != canonical_json(1)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})


def test_deterministic_hash_is_stable_across_processes() -> None:
    script = (
        "from rag_app.core.identifiers import deterministic_id; "
        "print(deterministic_id('node', {'b': 2, 'a': 1}))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "src"
    outputs = [
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1]
    assert json.dumps(outputs)
