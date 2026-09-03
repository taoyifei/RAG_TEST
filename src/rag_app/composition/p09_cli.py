"""P09 离线 SDK、API 与 OpenAPI 验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from time import monotonic, sleep
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from rag_app.api.p09 import create_p09_app
from rag_app.composition.p09_runtime import P09Runtime, build_p09_runtime
from rag_app.core.models import Job

P09_COMMANDS = frozenset({"api-smoke", "sdk-smoke", "openapi-check"})
_DEFAULT_PROFILE = Path("configs/profiles/dev-offline.json")
_OPENAPI_SNAPSHOT = Path("docs/public/openapi-v1.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OPENAPI_QUERY_TOKEN = "openapi-query-token"  # noqa: S105
_OPENAPI_ADMIN_TOKEN = "openapi-admin-token"  # noqa: S105
_API_SMOKE_QUERY_TOKEN = "api-smoke-query"  # noqa: S105
_API_SMOKE_ADMIN_TOKEN = "api-smoke-admin"  # noqa: S105
_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_ACCEPTED = 202


def p09_command(arguments: Sequence[str]) -> int:
    """运行一个默认离线的 P09 验收命令。

    Args:
        arguments: 从命令名开始的参数序列。

    Returns:
        验收通过返回 0；合同漂移返回 1。

    """
    parser = argparse.ArgumentParser(prog=f"scripts/dev.py {arguments[0]}")
    parser.add_argument("--profile", type=Path, default=_DEFAULT_PROFILE)
    parsed = parser.parse_args(arguments[1:])
    command = arguments[0]
    if command == "sdk-smoke":
        return _sdk_smoke(parsed.profile)
    if command == "api-smoke":
        return _api_smoke(parsed.profile)
    return _openapi_check(parsed.profile)


def openapi_snapshot(profile: Path = _DEFAULT_PROFILE) -> dict[str, object]:
    """生成不依赖外部服务的权威 OpenAPI mapping。

    Args:
        profile: 用于构造 P09 组合根的离线 Profile。

    Returns:
        可稳定 JSON 序列化的 OpenAPI schema。

    """
    with (
        tempfile.TemporaryDirectory(prefix="rag-p09-openapi-") as temporary,
        build_p09_runtime(profile, data_dir=temporary) as runtime,
    ):
        app = create_p09_app(
            runtime,
            query_token=_OPENAPI_QUERY_TOKEN,
            admin_token=_OPENAPI_ADMIN_TOKEN,
            debug_enabled=False,
        )
        return dict(app.openapi())


def _sdk_smoke(profile: Path) -> int:
    with (
        tempfile.TemporaryDirectory(prefix="rag-p09-sdk-") as temporary,
        build_p09_runtime(profile, data_dir=temporary) as runtime,
    ):
        project = runtime.sdk.create_project("P09 SDK smoke")
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id, "离线知识库"
        )
        job = _wait_job(
            runtime,
            runtime.sdk.create_document(
                project.project_id,
                knowledge_base.knowledge_base_id,
                display_name="离线验收.docx",
                content=_docx("中文普通短语 P09 离线检索验收"),
                media_type=_MEDIA_TYPE,
                idempotency_key="sdk-smoke-document",
            ),
        )
        result = runtime.sdk.search(
            project.project_id,
            knowledge_base.knowledge_base_id,
            "中文普通短语",
        )
        if job.state.value != "succeeded" or not result.evidence:
            print("FAIL P09 SDK smoke 未产生成功 Job 或检索证据。")
            return 1
        print(
            "OK P09 SDK smoke "
            f"revision={job.revision_id} cache_hit={result.cache_hit} "
            "network_calls=0"
        )
    return 0


def _api_smoke(profile: Path) -> int:
    admin = {"Authorization": f"Bearer {_API_SMOKE_ADMIN_TOKEN}"}
    query = {"Authorization": f"Bearer {_API_SMOKE_QUERY_TOKEN}"}
    with (
        tempfile.TemporaryDirectory(prefix="rag-p09-api-") as temporary,
        build_p09_runtime(profile, data_dir=temporary) as runtime,
    ):
        client = TestClient(
            create_p09_app(
                runtime,
                query_token=_API_SMOKE_QUERY_TOKEN,
                admin_token=_API_SMOKE_ADMIN_TOKEN,
            )
        )
        project_response = client.post(
            "/api/v1/projects",
            json={"name": "P09 API smoke"},
            headers={**admin, "Idempotency-Key": "api-smoke-project"},
        )
        if project_response.status_code != _HTTP_CREATED:
            return _api_failure("project.create", project_response.text)
        project_id = project_response.json()["project_id"]
        kb_response = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases",
            json={"name": "离线知识库"},
            headers={**admin, "Idempotency-Key": "api-smoke-kb"},
        )
        if kb_response.status_code != _HTTP_CREATED:
            return _api_failure("knowledge_base.create", kb_response.text)
        kb_id = kb_response.json()["knowledge_base_id"]
        upload_headers = {
            **admin,
            "Idempotency-Key": "api-smoke-document",
            "Content-Type": _MEDIA_TYPE,
        }
        upload = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/{kb_id}/documents",
            params={"display_name": "offline-smoke.docx"},
            content=_docx("中文普通短语 P09 API 离线检索验收"),
            headers=upload_headers,
        )
        if upload.status_code != _HTTP_ACCEPTED:
            return _api_failure("document.create", upload.text)
        completed = _wait_api_job(client, str(upload.json()["job_id"]), admin)
        if completed.get("state") != "succeeded":
            return _api_failure("job.wait", json.dumps(completed))
        search = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/{kb_id}:search",
            json={"query": "中文普通短语", "limit": 5},
            headers=query,
        )
        if search.status_code != _HTTP_OK or not search.json().get("evidence"):
            return _api_failure("query.search", search.text)
        print(
            "OK P09 API smoke "
            f"revision={upload.json()['revision_id']} network_calls=0"
        )
    return 0


def _openapi_check(profile: Path) -> int:
    observed = _canonical_json(openapi_snapshot(profile))
    try:
        expected = _canonical_json(
            json.loads(_OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"FAIL OpenAPI snapshot 无法读取：{type(error).__name__}")
        return 1
    if observed != expected:
        print("FAIL OpenAPI snapshot 与当前路由不一致。")
        return 1
    print("OK OpenAPI snapshot 与当前路由一致。")
    return 0


def _docx(text: str) -> bytes:
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            "vnd.openxmlformats-officedocument.wordprocessingml.document."
            'main+xml"/>'
            "</Types>",
        )
        package.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        package.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return output.getvalue()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _api_failure(stage: str, detail: str) -> int:
    print(f"FAIL P09 API smoke stage={stage} detail={detail[:300]}")
    return 1


def _wait_job(runtime: P09Runtime, job: Job) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.01)
    raise RuntimeError(f"Job 未在期限内结束：{job.job_id}")


def _wait_api_job(
    client: TestClient, job_id: str, headers: dict[str, str]
) -> dict[str, object]:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        if response.status_code != _HTTP_OK:
            raise RuntimeError(f"Job 查询失败：{response.text[:300]}")
        job = dict(response.json())
        if job.get("state") not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise RuntimeError(f"Job 未在期限内结束：{job_id}")


__all__ = ["P09_COMMANDS", "openapi_snapshot", "p09_command"]
