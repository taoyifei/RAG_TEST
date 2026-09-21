"""统一 Grounded Wire 协议的根失败与逐项隔离回归。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from rag_app.adapters.providers.grounded_wire import (
    GroundedWireError,
    grounded_wire_schema,
    parse_grounded_wire,
)

_ATOMS = frozenset({"A1", "A2"})
_REFS = {"A1": frozenset({"E1", "E2"}), "A2": frozenset({"E3"})}


def _schema_atom_enum(schema: dict[str, object]) -> list[str]:
    claim = schema["$defs"]["GroundedWireClaim"]  # type: ignore[index]
    properties = claim["properties"]  # type: ignore[index]
    return properties["atom_id"]["enum"]  # type: ignore[index,return-value]


def test_request_scoped_schema_mutates_ref_target_without_shared_state() -> (
    None
):
    with ThreadPoolExecutor(max_workers=3) as executor:
        schemas = tuple(
            executor.map(
                grounded_wire_schema,
                (("A1",), ("A1", "A3"), ("A2", "A4")),
            )
        )

    assert [_schema_atom_enum(schema) for schema in schemas] == [
        ["A1"],
        ["A1", "A3"],
        ["A2", "A4"],
    ]
    assert schemas[0]["properties"]["claims"]["items"] == {  # type: ignore[index]
        "$ref": "#/$defs/GroundedWireClaim"
    }
    _schema_atom_enum(schemas[0]).append("A4")
    assert _schema_atom_enum(grounded_wire_schema(("A1",))) == ["A1"]
    assert "uniqueItems" not in json.dumps(schemas, ensure_ascii=False)


@pytest.mark.parametrize("atom_ids", [(), ("A5",), ("A1", "A5")])
def test_request_scoped_schema_rejects_invalid_atom_set(
    atom_ids: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="Atom 集合无效"):
        grounded_wire_schema(atom_ids)


def test_complete_json_fence_is_the_only_tolerated_wrapper() -> None:
    result = parse_grounded_wire(
        '  ```json\n{"claims":[{"atom_id":"A1","text":"事实",'
        '"refs":["E1"]}]}\n```  ',
        allowed_atom_ids=_ATOMS,
        allowed_refs_by_atom=_REFS,
    )

    assert tuple(claim.text for claim in result.claims) == ("事实",)
    assert result.diagnostics == ()


def test_complete_bare_fence_and_crlf_are_safe_normalization() -> None:
    result = parse_grounded_wire(
        '```\r\n{"claims":[{"atom_id":"A1","text":"事实",'
        '"refs":["E1"]}]}\r\n```',
        allowed_atom_ids=_ATOMS,
        allowed_refs_by_atom=_REFS,
    )

    assert tuple(claim.text for claim in result.claims) == ("事实",)


@pytest.mark.parametrize(
    "content,stage,code",
    [
        ("not-json", "json_decode", "JSON_DECODE_FAILED"),
        ('{"claims":[],"answer":"x"}', "wire_schema", "ROOT_FIELDS"),
        ('{"claims":{}}', "wire_schema", "CLAIMS_TYPE"),
        ('{"claims":[],"claims":[]}', "json_decode", "DUPLICATE_OBJECT_KEY"),
    ],
)
def test_root_failure_is_precise_and_does_not_echo_content(
    content: str, stage: str, code: str
) -> None:
    with pytest.raises(GroundedWireError) as captured:
        parse_grounded_wire(
            content,
            allowed_atom_ids=_ATOMS,
            allowed_refs_by_atom=_REFS,
        )

    error = captured.value
    assert error.failure_stage == stage
    assert error.failure_code == code
    assert content not in str(error.safe_details)


def test_bad_items_do_not_delete_independent_valid_claim() -> None:
    result = parse_grounded_wire(
        """{
          "claims": [
            {"atom_id": "A1", "text": "保留", "refs": ["E1"]},
            {"atom_id": "A1", "text": "缺字段"},
            {"atom_id": "A2", "text": "越界引用", "refs": ["E1"]}
          ]
        }""",
        allowed_atom_ids=_ATOMS,
        allowed_refs_by_atom=_REFS,
    )

    assert tuple(claim.text for claim in result.claims) == ("保留",)
    assert tuple(item.failure_code for item in result.diagnostics) == (
        "FIELD_REQUIRED",
        "UNKNOWN_OR_OUT_OF_SCOPE_REF",
    )
    assert tuple(item.json_path for item in result.diagnostics) == (
        "claims[1].refs",
        "claims[2].refs",
    )
    assert all("保留" not in repr(item) for item in result.diagnostics)


def test_duplicate_item_keeps_first_and_records_rejection() -> None:
    result = parse_grounded_wire(
        '{"claims":['
        '{"atom_id":"A1","text":"事实","refs":["E1"]},'
        '{"atom_id":"A1","text":"事实","refs":["E1"]}'
        "]}",
        allowed_atom_ids=_ATOMS,
        allowed_refs_by_atom=_REFS,
    )

    assert len(result.claims) == 1
    assert tuple(item.failure_code for item in result.diagnostics) == (
        "DUPLICATE_ITEM",
    )
