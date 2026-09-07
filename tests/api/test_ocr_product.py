"""完整产品接口中的图片识别、来源访问及部分失败重试。"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx
from PIL import Image, ImageDraw

from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.adapters.providers.budget_transport import (
    provider_request_identity,
)
from rag_app.core.models import DocumentIR, ParseResult
from tests.adapters.parsers.docx_fixtures import IMAGE, build_docx
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OCR_TEXT = "设备 OCR-731 的温度为 27 ℃。"
_NATIVE_TEXT = "本文件说明设备巡检，图示如下。"


@dataclass
class _Scenario:
    harness: ProductHarness
    project: str
    kb: str
    document: str
    image: bytes
    source: bytes
    calls: list[httpx.Request]
    failures: list[int]
    during_call: list[Callable[[], None]]

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project}/knowledge-bases/{self.kb}"

    @property
    def ocr_path(self) -> str:
        return (
            f"/api/v1/knowledge-bases/{self.kb}/documents/{self.document}/ocr"
        )

    @property
    def model_path(self) -> str:
        return f"/api/v1/knowledge-bases/{self.kb}/model-settings"

    def answer(self) -> dict[str, Any]:
        response = self.harness.client.post(
            self.base + ":answer",
            json={"query": "OCR-731的温度是多少"},
            headers=self.harness.write_headers,
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def recognize(self) -> dict[str, Any]:
        response = self.harness.client.post(
            self.ocr_path,
            json={
                "confirmed_media_hashes": [
                    hashlib.sha256(self.image).hexdigest()
                ]
            },
            headers=self.harness.write_headers,
        )
        assert response.status_code == 202, response.text
        return _wait_job(self.harness, str(response.json()["job_id"]))


def _wait_job(
    harness: ProductHarness, job_id: str, *, expected_state: str = "succeeded"
) -> dict[str, Any]:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = dict(response.json())
        if job["state"] not in {"queued", "running"}:
            assert job["state"] == expected_state, job
            return job
        sleep(0.01)
    raise AssertionError("图片识别产品作业超时。")


def _prepare(tmp_path: Path) -> _Scenario:
    stream = io.BytesIO()
    picture = Image.new("RGB", (320, 96), color="white")
    ImageDraw.Draw(picture).text(
        (10, 25), "OCR-731 temperature: 27 Celsius", fill="black"
    )
    picture.save(stream, format="PNG")
    image = stream.getvalue()
    source = build_docx(
        f"<w:p><w:r><w:t>{_NATIVE_TEXT}</w:t></w:r></w:p>" + IMAGE,
        relationships=(
            '<Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/image" '
            'Target="media/ocr-source.png"/>'
        ),
        extra_entries={"word/media/ocr-source.png": image},
    )
    calls: list[httpx.Request] = []
    failures = [0]
    during_call: list[Callable[[], None]] = []

    def transport(_connection: object) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            assert payload["model"] == "qwen3.5-ocr"
            assert request.extensions["rag_chat_operation"] == "image.ocr"
            if during_call:
                during_call.pop(0)()
            if failures[0]:
                failures[0] -= 1
                return httpx.Response(
                    503, json={"error": {"code": "ServiceUnavailable"}}
                )
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": _OCR_TEXT,
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 12,
                        "total_tokens": 42,
                    },
                },
            )

        return httpx.MockTransport(handler)

    harness = build_product_harness(tmp_path, transport_factory=transport)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        base = f"/api/v1/projects/{project}/knowledge-bases/{kb}"
        uploaded = harness.client.post(
            base + "/documents",
            params={"display_name": "合成图片巡检手册.docx"},
            content=source,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "ocr-source",
                "Content-Type": _MEDIA_TYPE,
            },
        )
        assert uploaded.status_code == 202, uploaded.text
        job = _wait_job(harness, str(uploaded.json()["job_id"]))
        credential = harness.client.post(
            "/api/v1/provider-credentials",
            headers=harness.write_headers,
            json={
                "provider_type": "aliyun-model-studio",
                "source": "database_encrypted",
                "secret_value": "synthetic-provider-secret",
            },
        )
        assert credential.status_code == 201, credential.text
        connection = harness.client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "display_name": "合成 OCR 连接",
                "provider_type": "aliyun-model-studio",
                "credential_id": credential.json()["credential_id"],
                "workspace_id": "synthetic",
                "endpoint_mode": "beijing_dashscope",
                "region": "cn-beijing",
            },
        )
        assert connection.status_code == 201, connection.text
        connection_id = str(connection.json()["connection_id"])
        stored_connection = harness.runtime.control.get_connection(
            connection_id
        )
        configuration_version = stored_connection.configuration_version
        credential_version = harness.runtime.control.credential_version(
            stored_connection.credential_id
        )
        ledger = ProviderBudgetLedger(tmp_path / "data/provider-budget.sqlite3")
        ledger.create_campaign(
            BudgetCampaign(
                campaign_id="ocr-product",
                authorization_id="synthetic-ocr-product",
                scope="ocr-product",
                request_limit=8,
                estimated_token_limit=50000,
                approved_request_identities=(
                    provider_request_identity(
                        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                        "qwen3.5-ocr",
                        {
                            "connection_id": connection_id,
                            "configuration_version": configuration_version,
                            "credential_key_version": credential_version,
                        },
                    ),
                ),
                scope_mode="knowledge_base",
                project_id=project,
                knowledge_base_id=kb,
                approved_source_hashes=(hashlib.sha256(source).hexdigest(),),
                approved_media_hashes=(hashlib.sha256(image).hexdigest(),),
                allowed_models=("qwen3.5-ocr",),
                allowed_operations=("image.ocr",),
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                operation_request_limits={"image.ocr": 8},
            )
        )
        configured = harness.client.put(
            f"/api/v1/knowledge-bases/{kb}/model-settings",
            headers=harness.write_headers,
            json={
                "ocr_enabled": True,
                "ocr_connection_id": connection.json()["connection_id"],
                "ocr_model": "qwen3.5-ocr",
                "budget_campaign_id": "ocr-product",
            },
        )
        assert configured.status_code == 200, configured.text
        return _Scenario(
            harness,
            project,
            kb,
            str(job["document_id"]),
            image,
            source,
            calls,
            failures,
            during_call,
        )
    except BaseException:
        harness.close()
        raise


def _active_ir(scenario: _Scenario) -> DocumentIR:
    with scenario.harness.runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT rd.document_ir_json FROM revision_documents rd "
            "JOIN knowledge_bases kb ON kb.active_revision_id=rd.revision_id "
            "WHERE kb.knowledge_base_id=? AND rd.document_id=?",
            (scenario.kb, scenario.document),
        ).fetchone()
    assert row is not None
    return DocumentIR.model_validate_json(str(row[0]))


def test_ocr_product_answers_image_fact_and_opens_the_scoped_source(
    tmp_path: Path,
) -> None:
    scenario = _prepare(tmp_path)
    harness = scenario.harness
    try:
        before = scenario.answer()
        assert before["answer"] is None and before["evidence"] == []
        scan = harness.client.get(scenario.ocr_path)
        assert scan.status_code == 200
        item = scan.json()["media"][0]
        assert item["supported"] and item["approved"] and not item["cached"]
        assert scenario.calls == []
        job = scenario.recognize()
        assert len(scenario.calls) == 1
        after = scenario.answer()
        assert after["confidence"]["status"] == "ANSWERABLE"
        assert (
            "27" in after["answer"] and after["generation_mode"] == "extractive"
        )
        evidence = after["evidence"][0]
        metadata = dict(evidence["metadata"])
        assert metadata["origin"] == "ocr"
        assert metadata["media_part_uri"] == "/word/media/ocr-source.png"
        assert all(
            dict(span["metadata"])["origin"] == "ocr"
            for span in evidence["source_spans"]
        )
        assert all(
            "bbox" not in dict(span["metadata"])
            for span in evidence["source_spans"]
        )
        image_path = (
            scenario.base
            + f"/documents/{scenario.document}/images/{metadata['artifact_id']}"
        )
        opened = harness.client.get(image_path)
        assert opened.status_code == 200 and opened.content == scenario.image, (
            opened.text
        )
        active = _active_ir(scenario)
        assert _NATIVE_TEXT in [node.text for node in active.nodes]
        assert _OCR_TEXT in [node.text for node in active.nodes]
        assert (
            active.source.content_sha256
            == hashlib.sha256(scenario.source).hexdigest()
        )
        # ParseResult 的报告与 IR 仍可按完整模型重新校验。
        ParseResult(document_ir=active, report=active.parse_report)
        repeated = scenario.recognize()
        assert repeated["job_id"] == job["job_id"]
        assert len(scenario.calls) == 1
        assert (
            harness.client.get(scenario.model_path).json()["ocr_revision"] == 1
        )
        deleted = harness.client.delete(
            scenario.base + f"/documents/{scenario.document}",
            headers=harness.write_headers,
        )
        assert deleted.status_code in {200, 202, 204}
        assert harness.client.get(image_path).status_code == 404
        assert scenario.answer()["evidence"] == []
    finally:
        harness.close()


def test_ocr_product_retries_partial_failure_in_a_new_revision(
    tmp_path: Path,
) -> None:
    scenario = _prepare(tmp_path)
    harness = scenario.harness
    try:
        scenario.failures[0] = 2
        failed_image_job = scenario.recognize()
        assert len(scenario.calls) == 2
        failed_ir = _active_ir(scenario)
        assert _NATIVE_TEXT in [node.text for node in failed_ir.nodes]
        assert _OCR_TEXT not in [node.text for node in failed_ir.nodes]
        assert (
            dict(dict(failed_ir.metadata)["ocr_enrichment"])["status"]
            == "PARTIAL"
        )
        assert (
            harness.client.get(scenario.ocr_path).json()["pending_count"] == 1
        )
        assert scenario.answer()["answer"] is None
        retried = scenario.recognize()
        assert retried["job_id"] != failed_image_job["job_id"]
        assert retried["revision_id"] != failed_image_job["revision_id"]
        assert len(scenario.calls) == 3
        assert (
            harness.client.get(scenario.model_path).json()["ocr_revision"] == 2
        )
        assert (
            harness.client.get(scenario.ocr_path).json()["pending_count"] == 0
        )
        assert "27" in scenario.answer()["answer"]
        assert _NATIVE_TEXT in [
            node.text for node in _active_ir(scenario).nodes
        ]
        scenario.recognize()
        assert len(scenario.calls) == 3
    finally:
        harness.close()


def test_ocr_configuration_drift_preserves_old_active_revision(
    tmp_path: Path,
) -> None:
    scenario = _prepare(tmp_path)
    harness = scenario.harness
    try:
        before = scenario.answer()

        def change_configuration() -> None:
            settings = harness.runtime.models.get(scenario.kb)
            changed = harness.client.put(
                scenario.model_path,
                headers=harness.write_headers,
                json={
                    **settings.model_dump(),
                    "ocr_revision": settings.ocr_revision + 1,
                },
            )
            assert changed.status_code == 200, changed.text

        scenario.during_call.append(change_configuration)
        response = harness.client.post(
            scenario.ocr_path,
            headers=harness.write_headers,
            json={
                "confirmed_media_hashes": [
                    hashlib.sha256(scenario.image).hexdigest()
                ]
            },
        )
        assert response.status_code == 202, response.text
        failed = _wait_job(
            harness,
            str(response.json()["job_id"]),
            expected_state="failed_terminal",
        )
        after = scenario.answer()
        assert (
            after["active_index_revision_id"]
            == before["active_index_revision_id"]
        )
        assert after["answer"] is None
        assert len(scenario.calls) == 1
        # 已识别的安全缓存不重复出网，新配置重新构建后才发布 OCR 引用。
        completed = scenario.recognize()
        assert completed["revision_id"] != failed["revision_id"]
        assert "27" in scenario.answer()["answer"]
        assert len(scenario.calls) == 1
    finally:
        harness.close()


def test_image_access_requires_the_exact_document_version(
    tmp_path: Path,
) -> None:
    scenario = _prepare(tmp_path)
    harness = scenario.harness
    try:
        artifact = "sha256:" + hashlib.sha256(scenario.image).hexdigest()
        image_path = (
            scenario.base + f"/documents/{scenario.document}/images/{artifact}"
        )
        assert harness.client.get(image_path).status_code == 200
        no_image = build_docx(
            "<w:p><w:r><w:t>该文档只有原生文字。</w:t></w:r></w:p>"
        )
        uploaded = harness.client.post(
            scenario.base + "/documents",
            params={"display_name": "另一个文档.docx"},
            content=no_image,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "other-document",
                "Content-Type": _MEDIA_TYPE,
            },
        )
        assert uploaded.status_code == 202, uploaded.text
        other = _wait_job(harness, str(uploaded.json()["job_id"]))
        # 同一 Revision 中的另一个文档不能继承图片的访问权限。
        assert (
            harness.client.get(
                scenario.base
                + f"/documents/{other['document_id']}/images/{artifact}"
            ).status_code
            == 404
        )
        updated = harness.client.post(
            scenario.base + f"/documents/{scenario.document}/versions",
            content=no_image,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "replace-with-text",
                "Content-Type": _MEDIA_TYPE,
            },
        )
        assert updated.status_code == 202, updated.text
        _wait_job(harness, str(updated.json()["job_id"]))
        # 同一逻辑文档的新版本也不能访问仅属于旧版本的图片。
        assert harness.client.get(image_path).status_code == 404
        assert scenario.calls == []
    finally:
        harness.close()
