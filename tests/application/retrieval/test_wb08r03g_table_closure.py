"""V9：规范表头必须属于目标表，完整阅读单元使用同一有界预算。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import JsonValue

from rag_app.adapters.stores.sqlite_control import SqliteControlStore
from rag_app.application.retrieval.neighbors import (
    ExpansionOutcome,
    NeighborExpander,
)
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from rag_app.core.ports import EvidenceSourcePort
from tests.application.retrieval.helpers import make_ranked_chunk
from tests.application.retrieval.test_generation_evidence_pack import (
    _pack,
    _plan,
)
from tests.application.retrieval.test_generation_reading_units import (
    _reading_keys,
    _sources,
    _table_cell,
)


class _CanonicalSource:
    """运行真实 SQL 端口，再由当前规范对象执行 hydration。"""

    def __init__(self, candidates: tuple[RankedChunk, ...]) -> None:
        self.chunks = {
            item.hydrated.chunk.chunk_id: item for item in candidates
        }
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "CREATE TABLE chunks(row_id INTEGER PRIMARY KEY, chunk_id TEXT, "
            "revision_id TEXT, document_version_id TEXT, document_id TEXT, "
            "chunk_json TEXT);"
            "CREATE TABLE index_revisions(index_revision_id TEXT, "
            "project_id TEXT, knowledge_base_id TEXT);"
            "CREATE TABLE documents(document_id TEXT, deleted_at TEXT, "
            "status TEXT, lifecycle_status TEXT);"
        )
        chunk = candidates[0].hydrated.chunk
        self.snapshot = cast(
            ActiveRevisionQuerySnapshot,
            SimpleNamespace(
                revision=SimpleNamespace(
                    index_revision_id=chunk.index_revision_id,
                    project_id=chunk.project_id,
                    knowledge_base_id=chunk.knowledge_base_id,
                )
            ),
        )
        self.connection.execute(
            "INSERT INTO index_revisions VALUES (?, ?, ?)",
            (
                chunk.index_revision_id,
                chunk.project_id,
                chunk.knowledge_base_id,
            ),
        )
        documents = set()
        for item in candidates:
            chunk = item.hydrated.chunk
            self.connection.execute(
                "INSERT INTO chunks(chunk_id, revision_id, "
                "document_version_id, "
                "document_id, chunk_json) VALUES (?, ?, ?, ?, ?)",
                (
                    chunk.chunk_id,
                    chunk.index_revision_id,
                    chunk.version.document_version_id,
                    chunk.version.document_id,
                    chunk.model_dump_json(),
                ),
            )
            documents.add(chunk.version.document_id)
        self.connection.executemany(
            "INSERT INTO documents VALUES (?, NULL, 'active', 'active')",
            ((document,) for document in documents),
        )
        self.store = object.__new__(SqliteControlStore)
        self.store._connections = self  # type: ignore[assignment]
        self.table_context_chunk_ids = self.store.table_context_chunk_ids
        self.section_calls = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        yield self.connection

    def hydrate_chunks(
        self, snapshot: ActiveRevisionQuerySnapshot, chunk_ids: tuple[str, ...]
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(self.chunks[key].hydrated for key in chunk_ids)

    def section_chunk_ids(
        self, snapshot: ActiveRevisionQuerySnapshot, **kwargs: object
    ) -> tuple[str, ...]:
        del snapshot, kwargs
        self.section_calls += 1
        return ()


def _atom_metadata(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """合成 fixture 的单 atom 形状必须显式通过结构断言。"""
    atoms = metadata["atoms"]
    assert isinstance(atoms, list)
    atom = atoms[0]
    assert isinstance(atom, dict)
    value = atom["metadata"]
    assert isinstance(value, dict)
    return value


def _expand(
    source: _CanonicalSource, seeds: tuple[RankedChunk, ...], **limits: int
) -> ExpansionOutcome:
    return NeighborExpander(cast(EvidenceSourcePort, source)).expand(
        source.snapshot,
        tuple(seeds),
        "section",
        RetrievalPolicy.model_validate(limits),
    )


@pytest.mark.parametrize("rank", (1, 20))
def test_exact_sql_closes_remote_header_even_when_prose_ranks_first(
    rank: int,
) -> None:
    row = _sources()
    prose = tuple(make_ranked_chunk(i, "普通文本") for i in range(1, rank))
    source = _CanonicalSource((*prose, *row))
    outcome = _expand(source, (*prose, row[0]))
    assert {item.hydrated.chunk.chunk_id for item in row} <= {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }
    assert outcome.degraded_reason_codes == ()
    assert source.section_calls == len(prose)


def test_unmarked_short_first_row_is_hydrated_and_forms_physical_fact() -> (
    None
):
    row = _sources()
    header = row[-1]
    chunk = header.hydrated.chunk
    metadata = dict(chunk.metadata)
    _atom_metadata(metadata)["header_strategy"] = "none"
    unmarked = header.model_copy(
        update={
            "hydrated": header.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"metadata": freeze_json_object(metadata)}
                    )
                }
            )
        }
    )
    source = _CanonicalSource((*row[:-1], unmarked))
    outcome = _expand(source, (row[0],))
    assert unmarked.hydrated.chunk.chunk_id in {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }

    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    pack = _pack(_plan(atom), outcome.candidates)
    assert any(
        fact.value_column_index == 2 for fact in pack.physical_table_facts
    )


def test_same_section_other_table_and_same_named_column_are_not_borrowed() -> (
    None
):
    row = _sources()
    wrong = _table_cell(90, "输入", 0, 2)
    chunk = wrong.hydrated.chunk
    metadata = dict(chunk.metadata)
    _atom_metadata(metadata)["table_node_id"] = f"node_{901:032x}"
    wrong = wrong.model_copy(
        update={
            "hydrated": wrong.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"metadata": freeze_json_object(metadata)}
                    )
                }
            )
        }
    )
    source = _CanonicalSource((*row[:-1], wrong))
    outcome = _expand(source, (row[0],))
    assert "TABLE_HEADER_MISSING" in outcome.degraded_reason_codes
    assert wrong.hydrated.chunk.chunk_id not in {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }


@pytest.mark.parametrize("limit,complete", ((6, True), (5, False)))
def test_exact_read_never_returns_half_a_relation_unit(
    limit: int, complete: bool
) -> None:
    row = _sources()
    source = _CanonicalSource(row)
    outcome = _expand(source, (row[0],), group_member_chunk_limit=limit)
    assert (len(outcome.candidates) == len(row)) is complete
    if not complete:
        assert outcome.candidates == (row[0],)
        assert outcome.degraded_reason_codes == (
            "TABLE_RELATION_UNIT_BUDGET_EXCEEDED",
        )


def test_multilevel_header_and_split_row_ignore_neighbor_distance() -> None:
    row = _sources()
    upper = _table_cell(95, "准备事项", 1, 2)
    chunk = upper.hydrated.chunk
    metadata = dict(chunk.metadata)
    _atom_metadata(metadata)["header_strategy"] = "tblHeader"
    upper = upper.model_copy(
        update={
            "hydrated": upper.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"metadata": freeze_json_object(metadata)}
                    )
                }
            )
        }
    )
    continuation = _table_cell(96, "并提交审核。", 2, 2)
    source = _CanonicalSource((*row, upper, continuation))
    outcome = _expand(source, (row[3],))
    assert len(outcome.candidates) == 8
    assert not outcome.degraded_reason_codes


def test_repeated_display_header_does_not_satisfy_missing_original_header() -> (
    None
):
    row = _sources()
    header = row[-1]
    chunk = header.hydrated.chunk
    header = header.model_copy(
        update={
            "hydrated": header.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "source_spans": tuple(
                                span.model_copy(update={"is_repeated": True})
                                for span in chunk.source_spans
                            )
                        }
                    )
                }
            )
        }
    )
    source = _CanonicalSource((*row[:-1], header))
    outcome = _expand(source, (row[0],))
    assert outcome.degraded_reason_codes == ("TABLE_HEADER_MISSING",)


def test_missing_header_does_not_certify_complete_reading_unit() -> None:
    candidates = _sources()[:-1]
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={"original_query": "工装试制的输入是什么？"}
    )
    pack = _pack(plan, candidates)
    assert "TABLE_HEADER_MISSING" in pack.reading_unit_reason_codes
    assert not _reading_keys(pack)


@pytest.mark.parametrize("delta", (0, -1))
def test_reading_token_budget_keeps_or_drops_whole_unit(delta: int) -> None:
    candidates = _sources()
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={"original_query": "工装试制的输入是什么？"}
    )
    baseline = _pack(plan, candidates)
    priority = _reading_keys(baseline)

    cost = sum(
        max(1, (len(item.citation_text) + 3) // 4)
        for item in baseline.evidence
        if stable_support_key(item) in priority
    )
    pack = _pack(
        plan,
        candidates,
        policy=RetrievalPolicy(generation_evidence_token_budget=cost + delta),
    )
    assert bool(_reading_keys(pack)) is (delta == 0)
    if delta:
        assert (
            "TABLE_RELATION_UNIT_BUDGET_EXCEEDED"
            in pack.reading_unit_reason_codes
        )


def test_repeated_header_resolves_only_to_available_original() -> None:
    row = _sources()
    repeated = _table_cell(98, "输入", 0, 2)
    chunk = repeated.hydrated.chunk
    original_span = row[-1].hydrated.chunk.source_spans[2]
    metadata = dict(chunk.metadata)
    _atom_metadata(metadata)["cell_source_node_ids"] = {
        "2": [original_span.node_id]
    }
    repeated = repeated.model_copy(
        update={
            "hydrated": repeated.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "metadata": freeze_json_object(metadata),
                            "source_spans": (
                                original_span.model_copy(
                                    update={"is_repeated": True}
                                ),
                            ),
                        }
                    )
                }
            )
        }
    )
    source = _CanonicalSource((*row, repeated))
    outcome = _expand(source, (row[0],))
    assert not outcome.degraded_reason_codes
    ids = {item.hydrated.chunk.chunk_id for item in outcome.candidates}
    assert row[-1].hydrated.chunk.chunk_id in ids
    assert repeated.hydrated.chunk.chunk_id not in ids


def test_derived_numbering_without_offsets_is_not_a_reading_header() -> None:

    candidates = _sources()
    header = candidates[-1]
    chunk = header.hydrated.chunk
    header = header.model_copy(
        update={
            "hydrated": header.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={
                            "source_spans": tuple(
                                span.model_copy(
                                    update={
                                        "source_start_char": None,
                                        "source_end_char": None,
                                        "span_type": (
                                            SourceSpanKind.DERIVED_NUMBERING
                                        ),
                                    }
                                )
                                for span in chunk.source_spans
                            )
                        }
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={"original_query": "工装试制的输入是什么？"}
    )
    pack = _pack(plan, (*candidates[:-1], header))
    assert not _reading_keys(pack)


def test_requested_column_keeps_its_proven_merged_ancestor_header() -> None:
    candidates = _sources()
    ancestor = _table_cell(99, "准备材料", 1, 1)
    chunk = ancestor.hydrated.chunk
    metadata = dict(chunk.metadata)
    _atom_metadata(metadata).update(
        {
            "header_strategy": "tblHeader",
            "cell_coordinates": ["r1:c1:rs1:cs2"],
        }
    )
    ancestor = ancestor.model_copy(
        update={
            "hydrated": ancestor.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"metadata": freeze_json_object(metadata)}
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={"original_query": "工装试制的输入是什么？"}
    )
    pack = _pack(plan, (*candidates, ancestor))
    texts = {
        item.citation_text
        for item in pack.evidence
        if stable_support_key(item) in _reading_keys(pack)
    }
    assert {"工装试制", "输入", "准备材料", "图纸基线、材料清单。"} <= texts
    assert "输出" not in texts


def test_ranked_value_protects_closed_row_label_without_forging_rank() -> None:
    row = _sources()
    seed = row[2].model_copy(update={"rerank_rank": 20})
    source = _CanonicalSource((*row[:2], seed, *row[3:]))
    outcome = _expand(source, (seed,))
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    plan = _plan(atom).model_copy(
        update={"original_query": "工装试制的输入是什么？"}
    )
    pack = _pack(plan, outcome.candidates)
    labels = [
        item for item in pack.evidence if item.citation_text == "工装试制"
    ]
    assert len(labels) == 1
    assert labels[0].rerank_rank is None
    assert stable_support_key(labels[0]) in _reading_keys(pack)


@pytest.mark.parametrize("boundary", ("part", "table_path", "revision"))
def test_header_cannot_cross_canonical_identity_boundaries(
    boundary: str,
) -> None:
    row = _sources()
    header = row[-1]
    chunk = header.hydrated.chunk
    if boundary == "revision":
        chunk = chunk.model_copy(
            update={"index_revision_id": f"irev_{'f' * 32}"}
        )
    else:
        spans = []
        for span in chunk.source_spans:
            assert span.source_anchor is not None
            path = span.structural_path
            if boundary == "table_path":
                path = tuple(
                    "tbl:99" if part.startswith("tbl:") else part
                    for part in path
                )
            anchor = span.source_anchor.model_copy(
                update={
                    "part_uri": "/word/header1.xml"
                    if boundary == "part"
                    else span.source_anchor.part_uri,
                    "structural_path": path,
                }
            )
            spans.append(
                span.model_copy(
                    update={"source_anchor": anchor, "structural_path": path}
                )
            )
        chunk = chunk.model_copy(update={"source_spans": tuple(spans)})
    header = header.model_copy(
        update={"hydrated": header.hydrated.model_copy(update={"chunk": chunk})}
    )
    source = _CanonicalSource((*row[:-1], header))
    outcome = _expand(source, (row[0],))
    assert outcome.degraded_reason_codes == (
        "TABLE_HEADER_MISSING"
        if boundary == "revision"
        else "NEIGHBOR_INDEX_CORRUPT",
    )
    assert header.hydrated.chunk.chunk_id not in {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }
