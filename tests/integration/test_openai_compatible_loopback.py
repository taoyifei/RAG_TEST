"""OpenAI-compatible 适配器的真实回环 HTTP 功能与对抗性合同。"""

from __future__ import annotations

import json
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx
import pytest

from rag_app.adapters.providers.aliyun_chat import ChatMessage
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleEmbeddingConfig,
    OpenAICompatibleRerankerAdapter,
    OpenAICompatibleRerankerConfig,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import (
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderUnavailable,
)
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    EvidenceItem,
    RerankRequest,
)
from rag_app.core.ports import GenerationRequest
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class _State:
    """保存服务端实际收到的请求，测试不依赖进程内 Transport。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object], dict[str, str]]] = []


@contextmanager
def _loopback_server() -> Iterator[tuple[str, _State]]:
    state = _State()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: PLR0911, PLR0912
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            state.requests.append(
                (
                    self.path,
                    payload,
                    {
                        key.casefold(): value
                        for key, value in self.headers.items()
                    },
                )
            )
            if self.path in {"/embeddings", "/gateway/v1/embeddings"}:
                response = {
                    "data": [
                        {"index": index, "embedding": [3.0, 4.0, index + 1.0]}
                        for index in reversed(range(len(payload["input"])))
                    ],
                    "usage": {"total_tokens": 9},
                }
                self._json(response)
                return
            if self.path == "/wrong-model/embeddings":
                self._json(
                    {
                        "model": "unexpected-model",
                        "data": [{"index": 0, "embedding": [1.0, 1.0, 1.0]}],
                    }
                )
                return
            if self.path == "/bad-dimension/embeddings":
                self._json({"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
                return
            if self.path == "/auth/embeddings":
                if self.headers.get("Authorization") != "Bearer expected-key":
                    self._status(401)
                    return
                self._json(
                    {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}
                )
                return
            if self.path == "/slow/embeddings":
                sleep(0.2)
                with suppress(BrokenPipeError):
                    self._json(
                        {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}
                    )
                return
            if self.path == "/redirect/embeddings":
                self.send_response(307)
                self.send_header("Location", "/embeddings")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path in {"/rerank", "/v1/rerank"}:
                documents = payload.get("texts", payload.get("documents", []))
                self._json(
                    {
                        "results": [
                            {
                                "index": index,
                                "score": (index + 1) / (len(documents) + 1),
                            }
                            for index in reversed(range(len(documents)))
                        ]
                    }
                )
                return
            if self.path == "/duplicate/rerank":
                self._json(
                    {
                        "results": [
                            {"index": 0, "score": 0.2},
                            {"index": 0, "score": 0.9},
                        ]
                    }
                )
                return
            if self.path == "/missing/rerank":
                self._json({"results": [{"index": 0, "score": 0.7}]})
                return
            if self.path == "/no-stream/chat/completions" and payload["stream"]:
                self._status(405)
                return
            if self.path == "/no-stream/chat/completions":
                self._json(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": _chat_answer(payload)},
                            }
                        ]
                    }
                )
                return
            if self.path == "/bad-grounded/chat/completions":
                self._json(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "不是 Grounded JSON"},
                            }
                        ]
                    }
                )
                return
            if self.path == "/chat/completions" and payload["stream"]:
                answer = _chat_answer(payload)
                midpoint = max(1, len(answer) // 2)
                content = (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": answer[:midpoint]},
                                    "finish_reason": None,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                    + "\n\ndata: "
                    + json.dumps(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": answer[midpoint:]},
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                    + "\n\ndata: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if self.path == "/chat/completions":
                self._json(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": _chat_answer(payload)},
                            }
                        ]
                    }
                )
                return
            self.send_error(404)

        def _json(self, payload: dict[str, Any]) -> None:
            content = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _status(self, status: int) -> None:
            content = b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format: str, *args: object) -> None:
            del _format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chat_answer(payload: dict[str, object]) -> str:  # noqa: PLR0911
    """让回环模型仅复述请求中真实提供的一条证据。"""
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return "回环回答"
    candidate = messages[1]
    if not isinstance(candidate, dict):
        return "回环回答"
    content = candidate.get("content")
    if not isinstance(content, str):
        return "回环回答"
    try:
        grounded = json.loads(content)
    except json.JSONDecodeError:
        return "回环回答"
    if not isinstance(grounded, dict):
        return "回环回答"
    question = grounded.get("question")
    evidence = grounded.get("evidence")
    if not isinstance(question, str) or not isinstance(evidence, list):
        return "回环回答"
    candidates = [
        item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    if not candidates:
        return "回环回答"
    question_bigrams = _character_bigrams(question)
    selected = max(
        candidates,
        key=lambda item: len(
            question_bigrams & _character_bigrams(str(item["text"]))
        ),
    )
    support_id = selected.get("support_id")
    text = selected.get("text")
    if not isinstance(support_id, str) or not isinstance(text, str):
        return "回环回答"
    return json.dumps(
        {
            "claims": [
                {
                    "text": text,
                    "supports": [{"support_id": support_id, "quote": text}],
                }
            ]
        },
        ensure_ascii=False,
    )


def _character_bigrams(value: str) -> frozenset[str]:
    """以通用文本重合选择回环候选，不内置测试实体或业务词表。"""
    normalized = "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )
    if len(normalized) < 2:
        return frozenset({normalized}) if normalized else frozenset()
    return frozenset(
        normalized[index : index + 2] for index in range(len(normalized) - 1)
    )


def _http(base_url: str) -> ProviderHttpClient:
    return ProviderHttpClient(
        base_url,
        max_attempts=1,
        allow_http=True,
        use_budget_transport=False,
        defer_success_observation=True,
    )


def _generation_request() -> GenerationRequest:
    """构造不含私有语料的最小引用回答请求。"""
    return GenerationRequest(
        query="设备的维护周期是多少？",
        citation_protocol="grounded-support-v1",
        evidence=(
            EvidenceItem(
                evidence_id="support-1",
                chunk_id="chunk_" + "1" * 32,
                source_label="公开合成手册",
                citation_text="设备 MX-41 的维护周期为 14 天。",
            ),
        ),
    )


def test_real_loopback_embedding_rerank_and_chat_protocols() -> None:
    with _loopback_server() as (base_url, state):
        embedding = OpenAICompatibleEmbeddingAdapter(
            OpenAICompatibleEmbeddingConfig(
                slot_id="primary",
                model="team/free-form-embedding-v2",
                dimension=3,
                request_policy_identity="both",
                document_request_policy_identity="document-role",
                query_request_policy_identity="query-role",
                document_egress_allowed=True,
                query_egress_allowed=True,
            ),
            http_client=_http(base_url),
            api_key_resolver=lambda: "",
        )
        vectors = embedding.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("第一条", "第二条"),
            )
        )
        embedding.close()
        assert len(vectors.vectors) == 2
        assert vectors.request_policy_identity == "query-role"
        assert vectors.calls[0].observed_tokens == 9
        assert vectors.vectors[0][2] < vectors.vectors[1][2]

        protocols = (
            ("tei", "/rerank"),
            ("jina-compatible", "/v1/rerank"),
        )
        for protocol, path in protocols:
            reranker = OpenAICompatibleRerankerAdapter(
                OpenAICompatibleRerankerConfig(
                    model="自由重排模型",
                    protocol=protocol,
                    path=path,
                    egress_allowed=True,
                ),
                http_client=_http(base_url),
                api_key_resolver=lambda: "",
            )
            ranked = reranker.rerank(
                RerankRequest(
                    query="哪条更相关",
                    candidates=(("a", "第一条"), ("b", "第二条")),
                    limit=2,
                )
            )
            reranker.close()
            assert [item.candidate_id for item in ranked.items] == ["b", "a"]

        chat = OpenAICompatibleChatAdapter(
            OpenAICompatibleChatConfig(
                model="internal/chat-model:latest", egress_allowed=True
            ),
            http_client=_http(base_url),
            api_key_resolver=lambda: "",
        )
        messages = (ChatMessage(role="user", content="真实回环请求"),)
        synchronous = chat.complete(messages, operation="query.interpret")
        deltas: list[str] = []
        streamed = chat.complete_stream(
            messages,
            on_delta=deltas.append,
            cancellation=StreamCancellation(),
        )
        chat.close()
        assert synchronous.content == streamed.content == "回环回答"
        assert synchronous.usage.total_tokens is None
        assert streamed.usage.total_tokens is None
        assert deltas == ["回环", "回答"]

        paths = [item[0] for item in state.requests]
        assert paths == [
            "/embeddings",
            "/rerank",
            "/v1/rerank",
            "/chat/completions",
            "/chat/completions",
        ]
        embedding_payload = state.requests[0][1]
        assert set(embedding_payload) == {"model", "input", "encoding_format"}
        assert embedding_payload["encoding_format"] == "float"
        assert "authorization" not in state.requests[0][2]
        assert state.requests[1][1] == {
            "query": "哪条更相关",
            "texts": ["第一条", "第二条"],
            "truncate": False,
        }
        assert set(state.requests[3][1]) == {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "stream",
        }
        assert state.requests[3][1]["temperature"] == 0


def test_embedding_base_url_path_prefix_is_preserved() -> None:
    """常见 ``/v1`` 部署前缀不能被以斜杠开头的操作路径覆盖。"""
    with _loopback_server() as (base_url, state):
        embedding = OpenAICompatibleEmbeddingAdapter(
            OpenAICompatibleEmbeddingConfig(
                slot_id="primary",
                model="自由向量模型",
                dimension=3,
                request_policy_identity="both",
                document_request_policy_identity="document-role",
                query_request_policy_identity="query-role",
                document_egress_allowed=True,
                query_egress_allowed=True,
            ),
            http_client=_http(base_url + "/gateway/v1"),
            api_key_resolver=lambda: "",
        )
        try:
            result = embedding.embed(
                EmbeddingRequest(
                    slot_id="primary",
                    role=EmbeddingRequestRole.QUERY,
                    texts=("路径前缀回归",),
                )
            )
        finally:
            embedding.close()

        assert len(result.vectors) == 1
        assert [item[0] for item in state.requests] == [
            "/gateway/v1/embeddings"
        ]


@pytest.mark.parametrize(
    ("base_suffix", "kind"),
    (
        ("/wrong-model", "model"),
        ("/bad-dimension", "dimension"),
        ("/redirect", "redirect"),
    ),
)
def test_embedding_adversarial_responses_fail_closed(
    base_suffix: str, kind: str
) -> None:
    with _loopback_server() as (base_url, _):
        adapter = OpenAICompatibleEmbeddingAdapter(
            OpenAICompatibleEmbeddingConfig(
                slot_id="primary",
                model="expected-model",
                dimension=3,
                request_policy_identity="both",
                document_request_policy_identity="document",
                query_request_policy_identity="query",
                document_egress_allowed=True,
                query_egress_allowed=True,
            ),
            http_client=_http(base_url + base_suffix),
            api_key_resolver=lambda: "",
        )
        del kind
        try:
            with pytest.raises(ProviderInvalidResponse):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="primary",
                        role=EmbeddingRequestRole.DOCUMENT,
                        texts=("不会静默通过",),
                    )
                )
        finally:
            adapter.close()


@pytest.mark.parametrize("path", ("/duplicate/rerank", "/missing/rerank"))
def test_reranker_incomplete_or_duplicate_index_is_rejected(path: str) -> None:
    with _loopback_server() as (base_url, _):
        adapter = OpenAICompatibleRerankerAdapter(
            OpenAICompatibleRerankerConfig(
                model="free-reranker",
                protocol="jina-compatible",
                path=path,
                egress_allowed=True,
            ),
            http_client=_http(base_url),
            api_key_resolver=lambda: "",
        )
        try:
            with pytest.raises(ProviderInvalidResponse):
                adapter.rerank(
                    RerankRequest(
                        query="对抗重复索引",
                        candidates=(("a", "第一条"), ("b", "第二条")),
                        limit=2,
                    )
                )
        finally:
            adapter.close()


def test_wrong_key_and_timeout_are_not_silent_success() -> None:
    with _loopback_server() as (base_url, _):
        unauthorized = OpenAICompatibleEmbeddingAdapter(
            OpenAICompatibleEmbeddingConfig(
                slot_id="primary",
                model="free-embedding",
                dimension=3,
                request_policy_identity="both",
                document_request_policy_identity="document",
                query_request_policy_identity="query",
                document_egress_allowed=True,
                query_egress_allowed=True,
            ),
            http_client=_http(base_url + "/auth"),
            api_key_resolver=lambda: "wrong-key",
        )
        request = EmbeddingRequest(
            slot_id="primary",
            role=EmbeddingRequestRole.QUERY,
            texts=("公开测试文本",),
        )
        try:
            with pytest.raises(ProviderAuthenticationError):
                unauthorized.embed(request)
        finally:
            unauthorized.close()

        timeout_client = httpx.Client(
            timeout=httpx.Timeout(0.05),
            follow_redirects=False,
            trust_env=False,
        )
        timed = OpenAICompatibleEmbeddingAdapter(
            OpenAICompatibleEmbeddingConfig(
                slot_id="primary",
                model="free-embedding",
                dimension=3,
                request_policy_identity="both",
                document_request_policy_identity="document",
                query_request_policy_identity="query",
                document_egress_allowed=True,
                query_egress_allowed=True,
            ),
            http_client=ProviderHttpClient(
                base_url + "/slow",
                client=timeout_client,
                max_attempts=1,
                allow_http=True,
                use_budget_transport=False,
                defer_success_observation=True,
            ),
            api_key_resolver=lambda: "",
        )
        started = monotonic()
        try:
            with pytest.raises(ProviderUnavailable):
                timed.embed(request)
            assert monotonic() - started < 1
        finally:
            timed.close()


def test_invalid_grounded_json_uses_custom_provider_stage() -> None:
    with _loopback_server() as (base_url, _):
        adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleChatConfig(
                model="free-chat-model", egress_allowed=True
            ),
            http_client=_http(base_url + "/bad-grounded"),
            api_key_resolver=lambda: "",
        )
        try:
            with pytest.raises(ProviderInvalidResponse) as captured:
                adapter.generate(_generation_request())
            assert captured.value.stage == (
                "provider.openai_compatible.generation"
            )
            assert dict(captured.value.details)["reason_code"] == (
                "GENERATION_CLAIMS_INVALID"
            )
        finally:
            adapter.close()


def test_stream_unsupported_falls_back_once_without_fake_claim_deltas() -> None:
    with _loopback_server() as (base_url, state):
        adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleChatConfig(
                model="free-chat-model", egress_allowed=True
            ),
            http_client=_http(base_url + "/no-stream"),
            api_key_resolver=lambda: "",
        )
        emitted = []
        try:
            draft = adapter.generate_stream(
                _generation_request(),
                on_claim=emitted.append,
                cancellation=StreamCancellation(),
            )
        finally:
            adapter.close()
        assert emitted == []
        assert draft.claims[0].text == "设备 MX-41 的维护周期为 14 天。"
        assert len(draft.provider_calls) == 2
        assert draft.provider_calls[0].reason_code == "HTTP_405"
        assert draft.provider_calls[1].reason_code == "OK"
        assert [path for path, _, _ in state.requests] == [
            "/no-stream/chat/completions",
            "/no-stream/chat/completions",
        ]


def test_product_api_real_loopback_grounded_qa_and_trace(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """默认 Product 经真实 TCP 回环完成配置、入库、检索和引用回答。"""
    with _loopback_server() as (base_url, state):
        harness = build_product_harness(tmp_path, transport_factory=None)
        try:
            catalog = harness.client.get("/api/v1/provider-catalog")
            catalog.raise_for_status()
            compatible = next(
                item
                for item in catalog.json()["providers"]
                if item["provider_type"] == "openai-compatible"
            )
            assert compatible["models"] == []

            created = harness.client.post(
                "/api/v1/provider-connections",
                headers=harness.write_headers,
                json={
                    "display_name": "本机兼容网关",
                    "provider_type": "openai-compatible",
                    "credential": {
                        "provider_type": "openai-compatible",
                        "source": "database_encrypted",
                        "secret_value": "",
                    },
                    "api_base_url": base_url + "/",
                    "rerank_protocol": "tei",
                    "rerank_path": "/rerank",
                    "request_budget": 100,
                    "token_budget": 100_000,
                },
            )
            assert created.status_code == 201, created.text
            connection = created.json()
            connection_id = connection["connection_id"]
            assert connection["api_base_url"] == base_url
            assert connection["endpoint_mode"] == "custom"
            assert connection["rerank_protocol"] == "tei"
            assert "secret" not in created.text.casefold()
            credentials = harness.client.get(
                "/api/v1/provider-credentials"
            ).json()["items"]
            assert credentials == [
                {
                    **credentials[0],
                    "provider_type": "openai-compatible",
                    "masked_hint": "未配置（无鉴权）",
                }
            ]

            models = {
                "embedding.document": "team/free-form-embedding-v2",
                "embedding.query": "team/free-form-embedding-v2",
                "reranking": "自由重排模型",
                "generation": "internal/chat-model:latest",
                "query.interpret": "internal/chat-model:latest",
                "query.rewrite": "internal/chat-model:latest",
            }
            for index, (operation, model) in enumerate(models.items()):
                request: dict[str, object] = {
                    "operation": operation,
                    "model": model,
                }
                if operation.startswith("embedding."):
                    request["expected_dimension"] = 3
                    request["request_policy"] = {
                        "role": operation.removeprefix("embedding.")
                    }
                if index < 5:
                    validation = harness.client.post(
                        f"/api/v1/provider-connections/"
                        f"{connection_id}:validate",
                        headers=harness.write_headers,
                        json=request,
                    )
                    assert validation.status_code == 200, validation.text
                    result = validation.json()
                else:
                    # HTTP 层既有固定窗每分钟只接收五次连接测试；第六项
                    # 仍从同一 Product Registry 发出真实 TCP 请求。
                    result = harness.runtime.providers.validate(
                        connection_id,
                        operation=operation,
                        model=model,
                    ).model_dump(mode="json")
                assert result["status"] == "succeeded"
                assert result["validation_mode"] == "live"

            project_id, knowledge_base_id = create_project_and_knowledge_base(
                harness
            )
            retrieval_configuration = {
                "primary_connection_id": connection_id,
                "primary_embedding_model": models["embedding.document"],
                "primary_dimension": 3,
                "primary_document_policy": {"role": "document"},
                "primary_query_policy": {"role": "query"},
                "reranker_connection_id": connection_id,
                "reranker_model": models["reranking"],
            }
            profile = harness.client.post(
                f"/api/v1/knowledge-bases/{knowledge_base_id}/"
                "retrieval-profiles",
                headers=harness.write_headers,
                json=retrieval_configuration,
            )
            assert profile.status_code == 201, profile.text
            profile_id = profile.json()["profile_revision_id"]
            profile_status = harness.client.get(
                f"/api/v1/retrieval-profiles/{profile_id}/authorization"
            )
            assert profile_status.status_code == 200, profile_status.text
            assert profile_status.json()["authorization_state"] == (
                "NOT_REQUIRED"
            )
            activated = harness.client.post(
                f"/api/v1/retrieval-profiles/{profile_id}:activate",
                headers=harness.write_headers,
                json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
            )
            assert activated.status_code == 200, activated.text
            assert activated.json()["status"] == "active"

            before_settings = harness.client.get(
                f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings"
            ).json()["retrieval_data_plane"]["active_index_revision_id"]
            settings = harness.client.put(
                f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings",
                headers=harness.write_headers,
                json={
                    "generation_connection_id": connection_id,
                    "generation_model": models["generation"],
                    "generation_fallback_models": ["备用模型/自由版本"],
                    "rewrite_enabled": False,
                },
            )
            assert settings.status_code == 200, settings.text
            assert settings.json()["generation_fallback_models"] == [
                "备用模型/自由版本"
            ]
            authorization = settings.json()["corpus_authorization"]
            assert authorization["corpus_authorization_state"] == (
                "NOT_REQUIRED"
            )
            assert authorization["budget_state"] == "NOT_REQUIRED"
            assert (
                settings.json()["retrieval_data_plane"][
                    "active_index_revision_id"
                ]
                == before_settings
            )

            impact_variants = (
                (retrieval_configuration, "NO_REINDEX"),
                (
                    {
                        **retrieval_configuration,
                        "reranker_model": "另一自由重排模型",
                    },
                    "SERVING_RELOAD",
                ),
                (
                    {
                        **retrieval_configuration,
                        "primary_embedding_model": "另一自由向量模型",
                    },
                    "NEW_INDEX_REVISION_REQUIRED",
                ),
                (
                    {**retrieval_configuration, "primary_dimension": 4},
                    "NEW_INDEX_REVISION_REQUIRED",
                ),
            )
            for payload, expected_impact in impact_variants:
                variant = harness.client.post(
                    f"/api/v1/knowledge-bases/{knowledge_base_id}/"
                    "retrieval-profiles",
                    headers=harness.write_headers,
                    json=payload,
                )
                assert variant.status_code == 201, variant.text
                impact = harness.client.get(
                    f"/api/v1/retrieval-profiles/"
                    f"{variant.json()['profile_revision_id']}:preview"
                )
                assert impact.status_code == 200, impact.text
                assert impact.json()["impact"] == expected_impact

            # 两个命名向量空间各自保存维度，不应由 Primary 维度限制 Standby。
            distinct_spaces = harness.client.post(
                f"/api/v1/knowledge-bases/{knowledge_base_id}/"
                "retrieval-profiles",
                headers=harness.write_headers,
                json={
                    **retrieval_configuration,
                    "standby_connection_id": connection_id,
                    "standby_embedding_model": "备用自由向量模型",
                    "standby_dimension": 4,
                    "standby_document_policy": {"role": "document"},
                    "standby_query_policy": {"role": "query"},
                    "failover_enabled": True,
                },
            )
            assert distinct_spaces.status_code == 201, distinct_spaces.text
            assert distinct_spaces.json()["primary_resolved"]["dimension"] == 3
            assert distinct_spaces.json()["standby_resolved"]["dimension"] == 4

            job = harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开兼容模型手册.docx",
                content=build_package(
                    "<w:p><w:r><w:t>设备 MX-41 的维护周期为 14 天。</w:t>"
                    "</w:r></w:p>"
                ),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="openai-compatible-loopback",
            )
            deadline = monotonic() + 10
            while monotonic() < deadline:
                current = harness.runtime.sdk.get_job(job.job_id)
                if current.state.value not in {"queued", "running"}:
                    break
                sleep(0.01)
            assert current.state.value == "succeeded", current

            answered = harness.client.post(
                f"/api/v1/projects/{project_id}/knowledge-bases/"
                f"{knowledge_base_id}:answer",
                headers=harness.write_headers,
                json={"query": "设备 MX-41 的维护周期是多少？"},
            )
            assert answered.status_code == 200, answered.text
            result = answered.json()
            assert result["status"] == "ANSWERABLE", result
            assert result["generation_mode"] == "llm"
            assert "设备 MX-41 的维护周期为 14 天。" in result["answer"]
            assert result["evidence"]
            assert result["evidence"][0]["source_spans"]
            assert result["evidence"][0]["citation_text"] in result["answer"]

            history = harness.client.get(
                f"/api/v1/history/{result['trace_id']}"
            )
            assert history.status_code == 200, history.text
            usage = {
                item["operation"]: item
                for item in history.json()["provider_usage"]
            }
            assert usage["embedding.query"]["call_count"] == 1
            assert usage["reranking"]["call_count"] == 1
            assert usage["generation"]["call_count"] == 1
            assert usage["generation"]["usage"] == "unknown"

            trace = harness.client.get(
                f"/api/v1/admin/operational-traces/{result['trace_id']}"
            )
            assert trace.status_code == 200, trace.text
            provider_spans = [
                item
                for item in trace.json()["spans"]
                if item["name"].startswith("provider.")
            ]
            assert provider_spans
            assert {
                item["attributes"]["provider_id"] for item in provider_spans
            } == {"openai-compatible"}
            assert all(
                item["attributes"]["model"] in set(models.values())
                for item in provider_spans
            )

            paths = [item[0] for item in state.requests]
            assert paths.count("/embeddings") >= 4
            assert paths.count("/rerank") >= 2
            assert paths.count("/chat/completions") >= 4
            assert all(
                "authorization" not in headers
                for _, _, headers in state.requests
            )

            updated = harness.client.patch(
                f"/api/v1/provider-connections/{connection_id}",
                headers=harness.write_headers,
                json={
                    "expected_version": connection["configuration_version"],
                    "api_base_url": base_url + "/alternate",
                },
            )
            assert updated.status_code == 200, updated.text
            validations = harness.client.get(
                f"/api/v1/provider-connections/{connection_id}/validations"
            ).json()["items"]
            assert len(validations) == 6
            assert all(item["is_current"] is False for item in validations)
            endpoint_variant = harness.client.post(
                f"/api/v1/knowledge-bases/{knowledge_base_id}/"
                "retrieval-profiles",
                headers=harness.write_headers,
                json=retrieval_configuration,
            )
            assert endpoint_variant.status_code == 201, endpoint_variant.text
            endpoint_impact = harness.client.get(
                f"/api/v1/retrieval-profiles/"
                f"{endpoint_variant.json()['profile_revision_id']}:preview"
            )
            assert endpoint_impact.status_code == 200, endpoint_impact.text
            assert endpoint_impact.json()["impact"] == (
                "NEW_INDEX_REVISION_REQUIRED"
            )
        finally:
            harness.close()
