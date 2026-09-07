"""删除之后 HTTP 缓存、证据、相关内容与来源访问立即失效。"""

from pathlib import Path

from rag_app.composition.p09_runtime import build_p09_runtime
from tests.api.test_p09_e2e import (
    _ADMIN,
    _PROFILE,
    _QUERY,
    _client,
    _scope,
    _upload,
)


def test_delete_excludes_cached_evidence_and_related_content(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project, kb = _scope(client)
        job = _upload(
            client, project, kb, text="合成规范编号 TEST-4242，保管期限为五年。"
        )
        base = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        payload = {"query": "TEST-4242", "include_related_content": True}
        first = client.post(base + ":search", json=payload, headers=_QUERY)
        cached = client.post(base + ":search", json=payload, headers=_QUERY)
        assert first.status_code == cached.status_code == 200
        assert first.json()["evidence_count"] > 0
        assert cached.json()["cache_hit"] is True
        path = base + f"/documents/{job['document_id']}"
        deleted = client.delete(path, headers=_ADMIN)
        repeated = client.delete(path, headers=_ADMIN)
        assert deleted.status_code == repeated.status_code == 200
        assert deleted.json() == repeated.json()
        assert (
            client.get(base + "/documents", headers=_ADMIN).json()["items"]
            == []
        )
        remaining = client.post(base + ":search", json=payload, headers=_QUERY)
        assert remaining.status_code == 200
        assert remaining.json()["cache_hit"] is False
        assert remaining.json()["evidence_count"] == 0
        assert remaining.json()["related_contents"] == []
        assert "五年" not in remaining.text
