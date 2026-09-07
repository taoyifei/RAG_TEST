"""V3-00 默认 Product 回答端点的真实 TCP 缓冲基线。"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from rag_app.adapters.providers.budget_ledger import (
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import (
    provider_request_identity,
)
from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import ProductRuntime
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@dataclass(frozen=True, slots=True)
class _GenerationScope:
    """一次合成回答授权所需的完整隔离范围。"""

    tmp_path: Path
    runtime: ProductRuntime
    project_id: str
    knowledge_base_id: str
    document_id: str
    connection_id: str


@dataclass(frozen=True, slots=True)
class _StreamingCase:
    """真实 TCP 请求使用的 Product 合成案例。"""

    harness: ProductHarness
    project_id: str
    knowledge_base_id: str


@dataclass(frozen=True, slots=True)
class _TcpSession:
    """已登录的 loopback Product 会话。"""

    client: httpx.Client
    csrf: str


@dataclass(frozen=True, slots=True)
class _ReadSignals:
    """区分响应头到达与正文首字节到达。"""

    headers_received: threading.Event
    first_bytes_received: threading.Event


@dataclass(frozen=True, slots=True)
class _SlowGenerationResponder:
    """只有显式释放后才返回的离线 Provider Transport。"""

    entered: threading.Event
    release: threading.Event

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.entered.set()
        if not self.release.wait(timeout=10):
            return httpx.Response(504, json={"error": {"code": "timeout"}})
        payload = json.loads(request.content)
        prompt = json.loads(payload["messages"][1]["content"])
        evidence = prompt["evidence"][0]
        content = json.dumps(
            {
                "claims": [
                    {
                        "text": evidence["text"],
                        "supports": [
                            {
                                "support_id": evidence["support_id"],
                                "quote": evidence["text"],
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_live(client: httpx.Client, server: uvicorn.Server) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            try:
                if client.get("/live").status_code == 200:
                    return
            except httpx.RequestError:
                pass
        time.sleep(0.02)
    raise AssertionError("V3-00 loopback Product 服务未在时限内启动。")


def _upload_document(case: _StreamingCase) -> str:
    job = case.harness.runtime.sdk.create_document(
        case.project_id,
        case.knowledge_base_id,
        display_name="公共设备手册.docx",
        content=build_package(
            "<w:p><w:r><w:t>设备 MX-41 的维护周期为 14 天。</w:t></w:r></w:p>"
        ),
        media_type=_DOCX_MEDIA_TYPE,
        idempotency_key="v3-00-stream-fixture",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = case.harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            assert current.document_id
            return current.document_id
        time.sleep(0.01)
    raise AssertionError("V3-00 合成文档上传超时。")


def _authorize_generation(scope: _GenerationScope) -> None:
    control = scope.runtime.control
    sdk = scope.runtime.sdk
    connection = control.get_connection(scope.connection_id)
    document = sdk.get_document(
        scope.project_id,
        scope.knowledge_base_id,
        scope.document_id,
    )
    if document.current_version_id is None:
        raise AssertionError("合成文档缺少活动版本。")
    version = sdk.get_document_version(
        scope.project_id,
        scope.knowledge_base_id,
        scope.document_id,
        document.current_version_id,
    )
    ProviderBudgetLedger(
        scope.tmp_path / "data" / "provider-budget.sqlite3"
    ).create_campaign(
        BudgetCampaign(
            campaign_id="v3-00-stream-baseline",
            authorization_id="v3-00-synthetic-only",
            scope="synthetic-stream-baseline",
            request_limit=1,
            estimated_token_limit=10_000,
            scope_mode="knowledge_base",
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            approved_source_hashes=(version.content_sha256,),
            allowed_models=("qwen3.7-flash",),
            allowed_operations=("generation",),
            operation_request_limits={"generation": 1},
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            approved_request_identities=(
                provider_request_identity(
                    "https://llm-syntheticworkspace.cn-beijing.maas."
                    "aliyuncs.com/compatible-mode/v1/chat/completions",
                    "qwen3.7-flash",
                    {
                        "connection_id": scope.connection_id,
                        "configuration_version": (
                            connection.configuration_version
                        ),
                        "credential_key_version": control.credential_version(
                            connection.credential_id
                        ),
                    },
                ),
            ),
        )
    )


def _prepare_case(
    tmp_path: Path,
    responder: _SlowGenerationResponder,
) -> _StreamingCase:
    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(responder),
    )
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    case = _StreamingCase(harness, project_id, knowledge_base_id)
    document_id = _upload_document(case)
    _, _, _, connection_id = create_provider_connections(harness)
    _authorize_generation(
        _GenerationScope(
            tmp_path=tmp_path,
            runtime=harness.runtime,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            connection_id=connection_id,
        )
    )
    settings = harness.client.put(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings",
        headers=harness.write_headers,
        json={
            "generation_connection_id": connection_id,
            "generation_model": "qwen3.7-flash",
            "budget_campaign_id": "v3-00-stream-baseline",
        },
    )
    settings.raise_for_status()
    return case


@contextmanager
def _serve_product(case: _StreamingCase) -> Iterator[_TcpSession]:
    port = _free_loopback_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_product_app(case.harness.runtime),
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="warning",
            access_log=False,
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15)
    try:
        _wait_until_live(client, server)
        login = client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": case.harness.bootstrap_token},
        )
        login.raise_for_status()
        yield _TcpSession(client, str(login.json()["csrf_token"]))
    finally:
        client.close()
        server.should_exit = True
        server_thread.join(timeout=10)


def _read_answer(
    session: _TcpSession,
    case: _StreamingCase,
    signals: _ReadSignals,
) -> bytes:
    response_bytes: list[bytes] = []
    with session.client.stream(
        "POST",
        (
            f"/api/v1/projects/{case.project_id}/knowledge-bases/"
            f"{case.knowledge_base_id}:answer"
        ),
        headers={"X-CSRF-Token": session.csrf},
        json={"query": "MX-41 的维护周期是多少？", "stream": True},
    ) as response:
        response.raise_for_status()
        signals.headers_received.set()
        for chunk in response.iter_bytes():
            if chunk:
                response_bytes.append(chunk)
                signals.first_bytes_received.set()
    return b"".join(response_bytes)


def test_tcp_baseline_records_answer_headers_waiting_for_slow_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    provider_entered = threading.Event()
    provider_release = threading.Event()
    responder = _SlowGenerationResponder(provider_entered, provider_release)
    case = _prepare_case(tmp_path, responder)
    signals = _ReadSignals(threading.Event(), threading.Event())
    try:
        with (
            _serve_product(case) as session,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(_read_answer, session, case, signals)
            try:
                assert provider_entered.wait(timeout=5)
                # 已知缺陷基线：Provider 未释放时连响应头都不可见。
                assert not signals.headers_received.wait(timeout=0.25)
                assert not signals.first_bytes_received.is_set()
            finally:
                provider_release.set()
            body = future.result(timeout=10).decode()
        assert body.startswith("event: meta\n")
        assert "event: retrieval\n" in body
        assert "event: final\n" in body
    finally:
        provider_release.set()
        case.harness.close()
