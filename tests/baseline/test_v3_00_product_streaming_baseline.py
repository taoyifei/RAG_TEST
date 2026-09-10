"""V3-00 默认 Product 回答端点的真实 TCP 缓冲基线。"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from rag_app.product.auth import SESSION_COOKIE
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
    document_id: str


@dataclass(frozen=True, slots=True)
class _TcpSession:
    """已登录的 loopback Product 会话。"""

    client: httpx.Client
    csrf: str


@dataclass(frozen=True, slots=True)
class _ReadSignals:
    """区分响应头、首协议字节与首条核验 claim。"""

    headers_received: threading.Event
    first_bytes_received: threading.Event
    first_claim_received: threading.Event


@dataclass(frozen=True, slots=True)
class _SlowProviderStream(httpx.SyncByteStream):
    """首条 claim 后阻塞尾部，直到双闸门完成检查。"""

    entered: threading.Event
    claim_delta_sent: threading.Event
    release: threading.Event
    finished: threading.Event
    prefix: str
    suffix: str
    prefix_release: threading.Event | None = None

    def __iter__(self) -> Iterator[bytes]:
        self.entered.set()
        if self.prefix_release is not None and not self.prefix_release.wait(
            timeout=10
        ):
            raise httpx.ReadTimeout("synthetic provider prefix timeout")
        yield _provider_event(content=self.prefix)
        self.claim_delta_sent.set()
        if not self.release.wait(timeout=10):
            raise httpx.ReadTimeout("synthetic provider tail timeout")
        yield _provider_event(content=self.suffix)
        yield _provider_event(
            finish="stop",
            usage={
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        )
        yield b"data: [DONE]\r\n\r\n"
        self.finished.set()


def _provider_event(
    *,
    content: str | None = None,
    finish: str | None = None,
    usage: object = None,
) -> bytes:
    choice = {"index": 0, "delta": {}, "finish_reason": finish}
    if content is not None:
        choice["delta"] = {"content": content}
    payload: dict[str, object] = {
        "model": "qwen3.7-flash",
        "choices": [choice],
    }
    if usage is not None:
        payload["usage"] = usage
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\r\n\r\n"
    ).encode()


@dataclass(frozen=True, slots=True)
class _SlowGenerationResponder:
    """返回真实 SSE 响应，Provider 尾部由测试显式释放。"""

    entered: threading.Event
    claim_delta_sent: threading.Event
    release: threading.Event
    finished: threading.Event
    prefix_release: threading.Event | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
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
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=_SlowProviderStream(
                entered=self.entered,
                claim_delta_sent=self.claim_delta_sent,
                release=self.release,
                finished=self.finished,
                prefix=content[:-2],
                suffix=content[-2:],
                prefix_release=self.prefix_release,
            ),
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


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待流式资源状态超时。")
        time.sleep(0.01)


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


def _authorize_generation(
    scope: _GenerationScope,
    *,
    request_limit: int = 1,
) -> None:
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
            request_limit=request_limit,
            estimated_token_limit=10_000,
            scope_mode="knowledge_base",
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            approved_source_hashes=(version.content_sha256,),
            allowed_models=("qwen3.7-flash",),
            allowed_operations=("generation",),
            operation_request_limits={"generation": request_limit},
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
    *,
    generation_request_limit: int = 1,
) -> _StreamingCase:
    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(responder),
    )
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    preliminary = _StreamingCase(harness, project_id, knowledge_base_id, "")
    document_id = _upload_document(preliminary)
    case = _StreamingCase(
        harness,
        project_id,
        knowledge_base_id,
        document_id,
    )
    _, _, _, connection_id = create_provider_connections(harness)
    _authorize_generation(
        _GenerationScope(
            tmp_path=tmp_path,
            runtime=harness.runtime,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            connection_id=connection_id,
        ),
        request_limit=generation_request_limit,
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
    return _read_answer_request(
        session.client,
        case,
        signals,
        headers={"X-CSRF-Token": session.csrf},
    )


def _read_answer_request(
    client: httpx.Client,
    case: _StreamingCase,
    signals: _ReadSignals,
    *,
    headers: dict[str, str],
) -> bytes:
    """通过指定身份读取真实 TCP 流并记录公开交付边界。"""
    response_bytes: list[bytes] = []
    with client.stream(
        "POST",
        (
            f"/api/v1/projects/{case.project_id}/knowledge-bases/"
            f"{case.knowledge_base_id}:answer"
        ),
        headers=headers,
        json={
            "query": "MX-41 的维护周期是多少？",
            "stream": True,
            "stream_protocol": "rag-answer-sse-v1",
        },
    ) as response:
        response.raise_for_status()
        signals.headers_received.set()
        observed = b""
        for chunk in response.iter_bytes():
            if chunk:
                response_bytes.append(chunk)
                observed += chunk
                signals.first_bytes_received.set()
                if b"event: claim\n" in observed:
                    signals.first_claim_received.set()
    return b"".join(response_bytes)


def test_tcp_streams_headers_and_validated_claim_before_provider_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    provider_entered = threading.Event()
    claim_delta_sent = threading.Event()
    provider_release = threading.Event()
    provider_finished = threading.Event()
    responder = _SlowGenerationResponder(
        provider_entered,
        claim_delta_sent,
        provider_release,
        provider_finished,
    )
    case = _prepare_case(tmp_path, responder)
    signals = _ReadSignals(
        threading.Event(), threading.Event(), threading.Event()
    )
    try:
        with (
            _serve_product(case) as session,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(_read_answer, session, case, signals)
            try:
                assert provider_entered.wait(timeout=5)
                # 闸门 A：Provider 尚未结束，真实 TCP 已收到响应头和协议事件。
                assert signals.headers_received.wait(timeout=2)
                assert signals.first_bytes_received.wait(timeout=2)
                assert not provider_finished.is_set()
                # 闸门 B：完整 claim 已产生，Provider 尾部仍阻塞时已经交付。
                assert claim_delta_sent.wait(timeout=2)
                assert signals.first_claim_received.wait(timeout=2)
                assert not provider_finished.is_set()
            finally:
                provider_release.set()
            body = future.result(timeout=10).decode()
        assert body.startswith("event: meta\n")
        assert "event: stage\n" in body
        assert "event: claim\n" in body
        assert "event: final\n" in body
        assert body.index("event: claim\n") < body.index("event: final\n")
        assert provider_finished.is_set()
    finally:
        provider_release.set()
        case.harness.close()


def test_real_tcp_disconnect_keeps_slot_and_never_commits_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    provider_entered = threading.Event()
    claim_delta_sent = threading.Event()
    provider_release = threading.Event()
    provider_finished = threading.Event()
    responder = _SlowGenerationResponder(
        provider_entered,
        claim_delta_sent,
        provider_release,
        provider_finished,
    )
    case = _prepare_case(
        tmp_path,
        responder,
        generation_request_limit=2,
    )
    endpoint = (
        f"/api/v1/projects/{case.project_id}/knowledge-bases/"
        f"{case.knowledge_base_id}:answer"
    )
    request_body = {
        "query": "MX-41 的维护周期是多少？",
        "stream": True,
        "stream_protocol": "rag-answer-sse-v1",
    }
    trace_id = ""
    try:
        with _serve_product(case) as session:
            with session.client.stream(
                "POST",
                endpoint,
                headers={"X-CSRF-Token": session.csrf},
                json=request_body,
            ) as response:
                response.raise_for_status()
                trace_id = response.headers["X-Trace-Id"]
                observed = b""
                for chunk in response.iter_bytes():
                    observed += chunk
                    if b"event: claim\n" in observed:
                        break
                assert b"event: claim\n" in observed
                assert provider_entered.is_set()
                assert not provider_finished.is_set()
            # TCP 响应已关闭，但不可中断的同步 Provider 尚未返回，槽位仍占用。
            assert case.harness.runtime.p09.query_executor.in_flight == 1
            assert len(responder.requests) == 1
            provider_release.set()
            _wait_for(
                lambda: case.harness.runtime.p09.query_executor.in_flight == 0
            )
            detail = case.harness.runtime.history.detail(trace_id)
            assert detail["status"] == "CANCELLED"
            assert detail["reason_code"] == "REQUEST_CANCELLED"
            generation = next(
                item
                for item in detail["provider_usage"]
                if item["operation"] == "generation"
            )
            assert generation["call_count"] == 1
            # 该夹具刻意不可中断；断连后 Provider 仍返回完整 usage，预算与
            # Trace 必须保留实际值，但查询整体仍只能结算为 CANCELLED。
            assert generation["usage"] == 30
            assert generation["reason_code"] == "OK"
            case.harness.runtime.traces.recorder.flush()
            trace = session.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}"
            )
            trace.raise_for_status()
            trace_payload = trace.json()
            assert trace_payload["trace"]["status"] == "CANCELLED"
            provider_span = next(
                item
                for item in trace_payload["spans"]
                if item["name"] == "provider.generation"
            )
            assert provider_span["status"] == "OK"
            assert provider_span["attributes"]["observed_tokens"] == 30

            # 同一个问题必须再次进入 Provider，证明断连前缀未写成功缓存。
            second = session.client.post(
                endpoint,
                headers={"X-CSRF-Token": session.csrf},
                json=request_body,
            )
            second.raise_for_status()
            assert b"event: final\n" in second.content
            assert len(responder.requests) == 2
    finally:
        provider_release.set()
        case.harness.close()


def test_source_deleted_during_provider_stream_blocks_claim_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    prefix_release = threading.Event()
    responder = _SlowGenerationResponder(
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
        prefix_release,
    )
    case = _prepare_case(tmp_path, responder)
    signals = _ReadSignals(
        threading.Event(), threading.Event(), threading.Event()
    )
    try:
        with (
            _serve_product(case) as session,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(_read_answer, session, case, signals)
            assert responder.entered.wait(timeout=5)
            assert signals.headers_received.wait(timeout=2)
            assert signals.first_bytes_received.wait(timeout=2)
            deleted = case.harness.client.delete(
                f"/api/v1/projects/{case.project_id}/knowledge-bases/"
                f"{case.knowledge_base_id}/documents/{case.document_id}",
                headers=case.harness.write_headers,
            )
            assert deleted.status_code in {200, 202, 204}
            prefix_release.set()
            responder.release.set()
            body = future.result(timeout=10).decode()
        assert "event: error\n" in body
        assert '"partial":false' in body
        assert "event: claim\n" not in body
        assert "event: final\n" not in body
    finally:
        prefix_release.set()
        responder.release.set()
        case.harness.close()


def test_session_revoked_during_provider_stream_blocks_claim_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在途会话失效后，下一条完整 claim 必须重新鉴权并失败关闭。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    prefix_release = threading.Event()
    responder = _SlowGenerationResponder(
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
        prefix_release,
    )
    case = _prepare_case(tmp_path, responder)
    signals = _ReadSignals(
        threading.Event(), threading.Event(), threading.Event()
    )
    try:
        with (
            _serve_product(case) as session,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(_read_answer, session, case, signals)
            assert responder.entered.wait(timeout=5)
            assert signals.headers_received.wait(timeout=2)
            assert signals.first_bytes_received.wait(timeout=2)
            cookie = session.client.cookies.get(SESSION_COOKIE)
            assert cookie is not None
            case.harness.runtime.auth.revoke_session(cookie)
            prefix_release.set()
            responder.release.set()
            body = future.result(timeout=10).decode()
        assert "event: error\n" in body
        assert '"code":"POLICY_DENIED"' in body
        assert '"partial":false' in body
        assert "event: claim\n" not in body
        assert "event: final\n" not in body
    finally:
        prefix_release.set()
        responder.release.set()
        case.harness.close()


def test_access_token_revoked_during_provider_stream_blocks_claim_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 Token 在途吊销后，下一发布边界必须停止无权正文。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    prefix_release = threading.Event()
    responder = _SlowGenerationResponder(
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
        prefix_release,
    )
    case = _prepare_case(tmp_path, responder)
    token = case.harness.runtime.auth.create_access_token(
        name="合成流式查询",
        scopes=("query:read",),
        project_id=case.project_id,
        knowledge_base_id=case.knowledge_base_id,
    )
    signals = _ReadSignals(
        threading.Event(), threading.Event(), threading.Event()
    )
    try:
        with (
            _serve_product(case) as session,
            httpx.Client(
                base_url=session.client.base_url, timeout=15
            ) as client,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(
                _read_answer_request,
                client,
                case,
                signals,
                headers={"Authorization": f"Bearer {token.token}"},
            )
            assert responder.entered.wait(timeout=5)
            assert signals.headers_received.wait(timeout=2)
            assert signals.first_bytes_received.wait(timeout=2)
            case.harness.runtime.auth.revoke_access_token(token.token_id)
            prefix_release.set()
            responder.release.set()
            body = future.result(timeout=10).decode()
        assert "event: error\n" in body
        assert '"code":"POLICY_DENIED"' in body
        assert '"partial":false' in body
        assert "event: claim\n" not in body
        assert "event: final\n" not in body
    finally:
        prefix_release.set()
        responder.release.set()
        case.harness.close()
