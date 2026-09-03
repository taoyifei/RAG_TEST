from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.adapters.stores import SqliteConnectionFactory, SqliteControlStore
from rag_app.core.errors import Conflict
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import IndexRevisionRef, IndexRevisionState
from tests.persistence.helpers import runtime_with_kb


def test_same_revision_has_one_writer_and_stale_owner_is_fenced(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    connections = SqliteConnectionFactory(tmp_path / "universal-rag.sqlite3")
    second = SqliteControlStore(connections)
    revision_id = deterministic_id("irev", "lease-target")
    first_job = deterministic_id("job", "lease-first")
    second_job = deterministic_id("job", "lease-second")
    started = datetime.now(UTC)
    try:
        runtime.control.create_job(
            first_job,
            project_id,
            knowledge_base_id,
            revision_id,
            "lease-first",
        )
        second.create_job(
            second_job,
            project_id,
            knowledge_base_id,
            revision_id,
            "lease-second",
        )
        token = runtime.control.acquire_revision_lease(
            revision_id,
            first_job,
            now=started,
            lease_seconds=60,
        )
        assert token == 1
        assert (
            runtime.control.acquire_revision_lease(
                revision_id,
                first_job,
                now=started + timedelta(seconds=1),
                lease_seconds=60,
            )
            == token
        )
        with pytest.raises(Conflict, match="已有未过期"):
            second.acquire_revision_lease(
                revision_id,
                second_job,
                now=started + timedelta(seconds=2),
                lease_seconds=60,
            )
        revision = IndexRevisionRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            index_revision_id=revision_id,
            index_fingerprint=runtime.components.index_fingerprint,
            state=IndexRevisionState.CREATED,
        )
        runtime.control.create_revision(
            revision,
            physical_namespace=revision_id,
            expected_document_count=1,
            slots=runtime.components.embedding_topology.slots,
            resolved_contracts=runtime.builder._resolved_contracts,
        )

        takeover_token = second.acquire_revision_lease(
            revision_id,
            second_job,
            now=started + timedelta(seconds=62),
            lease_seconds=60,
        )

        assert takeover_token == 2
        with pytest.raises(Conflict, match="fencing token"):
            runtime.control.set_revision_state(
                revision_id,
                IndexRevisionState.CREATED,
                IndexRevisionState.PARSING,
            )
        second.set_revision_state(
            revision_id,
            IndexRevisionState.CREATED,
            IndexRevisionState.PARSING,
        )
        lease = second.revision_lease(revision_id)
        assert lease is not None
        assert lease["owner_job_id"] == second_job
        assert lease["fencing_token"] == 2
    finally:
        second.release_revision_lease(revision_id)
        second.close()
        runtime.close()
