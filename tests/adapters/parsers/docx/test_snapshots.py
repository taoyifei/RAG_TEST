from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_hot_standby_profile,
    default_offline_profile,
    load_profile,
    register_builtin_components,
)
from rag_app.composition.provider_profiles import PROFILE_DIRECTORY
from rag_app.core.errors import RagError
from rag_app.core.models import ParseSource, canonical_document_ir_json
from tests.adapters.parsers.docx.fixtures import parse_package, policy
from tests.fixtures.docx_v4.generate_fixtures import FixtureCase, _cases

_FIXTURE_ROOT = Path(__file__).parents[3] / "fixtures" / "docx_v4"


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _observed(case: FixtureCase) -> tuple[object, object, str]:
    name = case.name
    content = case.content
    updates = case.policy
    try:
        result = parse_package(content, name=name, **updates)
    except RagError as error:
        failure = {
            "status": "rejected",
            "error": {"code": error.code, "stage": error.stage},
        }
        return failure, failure, "rejected"
    document_ir = json.loads(
        canonical_document_ir_json(
            result.document_ir,
            include_content=True,
        )
    )
    report = result.report.model_dump(
        mode="json",
        exclude={"elapsed_seconds"},
    )
    return document_ir, report, "parsed"


def test_all_fixed_fixtures_match_binary_and_semantic_snapshots() -> None:
    manifest = _read_json(_FIXTURE_ROOT / "manifest.json")
    assert isinstance(manifest, list)
    by_name = {str(item["name"]): item for item in manifest}
    cases = _cases()

    assert len(cases) == 20
    assert set(by_name) == {case.name for case in cases}
    for case in cases:
        fixture_path = _FIXTURE_ROOT / case.name
        expected_dir = (
            _FIXTURE_ROOT / "expected" / case.name.removesuffix(".docx")
        )
        assert fixture_path.read_bytes() == case.content
        assert hashlib.sha256(case.content).hexdigest() == by_name[
            case.name
        ]["sha256"]
        observed_ir, observed_report, status = _observed(case)
        assert status == by_name[case.name]["status"]
        assert observed_ir == _read_json(expected_dir / "expected_ir.json")
        assert observed_report == _read_json(
            expected_dir / "expected_report.json"
        )


def test_restart_fixture_parses_with_v4() -> None:
    case = next(
        item for item in _cases()
        if item.name == "03-numbering-restart-override.docx"
    )

    _, report, status = _observed(case)

    assert status == "parsed"
    assert isinstance(report, dict)
    assert report["parser_id"] == "docx-ooxml-v4"


def test_external_relationship_fixture_never_opens_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def reject_network(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("离线 DOCX parser 禁止建立网络连接。")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    case = next(
        item for item in _cases()
        if item.name == "18-external-relations.docx"
    )

    _, _, status = _observed(case)

    assert status == "parsed"
    assert calls == []


def test_parser_output_is_identical_across_provider_profiles() -> None:
    case = next(
        item for item in _cases()
        if item.name == "03-numbering-restart-override.docx"
    )
    jina_only = load_profile(PROFILE_DIRECTORY / "dev-jina-only.json")
    hot_standby = default_hot_standby_profile()
    profiles = (
        default_offline_profile(),
        jina_only.model_copy(
            update={
                "components": jina_only.components.model_copy(
                    update={"parser": "docx-ooxml-v4"}
                )
            }
        ),
        hot_standby.model_copy(
            update={
                "components": hot_standby.components.model_copy(
                    update={"parser": "docx-ooxml-v4"}
                )
            }
        ),
    )
    observed: list[tuple[str, tuple[str, ...], object]] = []
    for profile in profiles:
        registry = ComponentRegistry()
        register_builtin_components(registry)
        with build_components(profile, registry) as components:
            result = components.parser.parse(
                ParseSource(
                    media_type="application/octet-stream",
                    display_name=case.name,
                    content=case.content,
                ),
                policy(),
            )
        canonical = canonical_document_ir_json(result.document_ir)
        observed.append(
            (
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                tuple(node.node_id for node in result.document_ir.nodes),
                result.report.model_dump(
                    mode="json",
                    exclude={"elapsed_seconds"},
                ),
            )
        )

    assert observed[0] == observed[1] == observed[2]
