"""相关内容的可选 HTTP 合同与原文读取边界。"""

import json
from pathlib import Path

import pytest

from rag_app.composition.p09_runtime import build_p09_runtime
from tests.api.test_p09_e2e import (
    _ADMIN,
    _PROFILE,
    _QUERY,
    _client,
    _scope,
    _upload,
)


def test_related_option_is_optional_and_sdk_preserves_answer(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project, kb = _scope(client)
        _upload(client, project, kb)
        path = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        for operation in ("search", "answer"):
            old = client.post(
                path + ":" + operation,
                json={"query": "财务制度"},
                headers=_QUERY,
            )
            new = client.post(
                path + ":" + operation,
                json={"query": "财务制度", "include_related_content": True},
                headers=_QUERY,
            )
            assert new.status_code == old.status_code == 200
            assert "related_contents" not in old.json()
            assert "display_message" not in old.json()
            assert "related_contents" in new.json()
            for field in ("status", "answer", "confidence", "evidence"):
                assert new.json()[field] == old.json()[field]
        sdk = runtime.sdk.answer(
            project, kb, "财务制度", include_related_content=True
        )
        assert sdk.answer == new.json()["answer"]
        schemas = client.app.openapi()["components"]["schemas"]
        assert "include_related_content" not in schemas["QueryRequest"].get(
            "required", []
        )
        assert "related_contents" not in schemas["QueryResponse"].get(
            "required", []
        )


def test_related_source_read_rechecks_active_revision_and_deletion(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project, kb = _scope(client)
        job = _upload(client, project, kb)
        base = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        path = base + f"/revisions/{job['revision_id']}/chunks"
        original = client.get(path, headers=_ADMIN).json()["items"][0]
        params = {
            "chunk_id": original["chunk_id"],
            "document_id": job["document_id"],
        }
        assert (
            client.get(path, params=params, headers=_QUERY).status_code == 403
        )
        assert client.get(path, params=params, headers=_ADMIN).json()[
            "items"
        ] == [original]
        assert (
            client.get(
                path,
                params={**params, "chunk_id": "chunk_" + "0" * 32},
                headers=_ADMIN,
            ).json()["items"]
            == []
        )
        client.delete(base + f"/documents/{job['document_id']}", headers=_ADMIN)
        denied = client.get(path, params=params, headers=_ADMIN)
        assert denied.status_code == 404
        assert original["citation_text"] not in denied.text


def test_related_source_rejects_retired_revision(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project, kb = _scope(client)
        job = _upload(client, project, kb)
        base = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        path = base + f"/revisions/{job['revision_id']}/chunks"
        original = client.get(path, headers=_ADMIN).json()["items"][0]
        _upload(client, project, kb, key="second-document", text="新的内容")
        denied = client.get(
            path,
            params={
                "chunk_id": original["chunk_id"],
                "document_id": job["document_id"],
            },
            headers=_ADMIN,
        )
        assert denied.status_code == 404
        assert original["citation_text"] not in denied.text


@pytest.mark.parametrize("corruption", ["scope", "span"])
def test_related_source_fails_closed_on_canonical_corruption(
    tmp_path: Path,
    corruption: str,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project, kb = _scope(client)
        job = _upload(client, project, kb)
        base = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        path = base + f"/revisions/{job['revision_id']}/chunks"
        original = client.get(path, headers=_ADMIN).json()["items"][0]
        invalid = dict(original)
        if corruption == "scope":
            invalid["knowledge_base_id"] = "kb_" + "0" * 32
        else:
            invalid["source_spans"] = []
        connections = runtime.retrieval_runtime.persistence.control._connections
        with connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE chunks SET chunk_json=? WHERE chunk_id=?",
                (json.dumps(invalid), original["chunk_id"]),
            )
        denied = client.get(
            path,
            params={
                "chunk_id": original["chunk_id"],
                "document_id": job["document_id"],
            },
            headers=_ADMIN,
        )
        assert denied.status_code == 500
        assert denied.json()["error"]["code"] == "INDEX_CORRUPT"
        assert original["citation_text"] not in denied.text
