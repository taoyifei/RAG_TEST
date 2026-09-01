from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

import rag_app.runtime as runtime_module
import rag_app.worker_runtime as worker_runtime_module
from rag_app.clients.resilience import ExternalServiceUnavailableError
from rag_app.runtime import build_runtime
from rag_app.settings import AccessMode, RuntimeSettings
from rag_app.worker_runtime import build_worker_runtime


@pytest.fixture(autouse=True)
def _installed_source_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "SOURCE_REVISION", "1" * 40)


class _TrackedResource:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def close(self) -> None:
        self._calls.append(self._name)


class _TrackedReadiness(_TrackedResource):
    def __init__(
        self,
        calls: list[str],
        *,
        fail_start: bool,
    ) -> None:
        super().__init__("readiness-close", calls)
        self._calls = calls
        self._fail_start = fail_start

    def start(self) -> None:
        self._calls.append("readiness-start")
        if self._fail_start:
            raise RuntimeError("synthetic readiness start failure")


class _TrackedExecutor(_TrackedResource):
    retry_after_seconds = 5

    def __init__(self, calls: list[str]) -> None:
        super().__init__("executor-close", calls)


class _FailingStateStore:
    def __init__(self, path: Path) -> None:
        del path

    def initialize(self) -> None:
        raise RuntimeError("synthetic state initialize failure")


def _noop(*args: object, **kwargs: object) -> None:
    del args, kwargs


def _empty_metadata(
    *args: object,
    **kwargs: object,
) -> dict[str, object]:
    del args, kwargs
    return {}


def _settings(tmp_path: Path) -> RuntimeSettings:
    root = Path(__file__).resolve().parents[1]
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    return RuntimeSettings(
        access_mode=AccessMode.SHARED_CORPUS,
        query_token=SecretStr(uuid.uuid4().hex),
        admin_token=SecretStr(uuid.uuid4().hex),
        qdrant_api_key=SecretStr(uuid.uuid4().hex),
        qdrant_url="http://qdrant:6333",
        qdrant_alias="rag-active",
        release_revision="1" * 40,
        state_database=tmp_path / "state.sqlite3",
        manifest_database=tmp_path / "manifest.sqlite3",
        trace_database=tmp_path / "traces.sqlite3",
        pipeline_path=root / "deployment/config/pipeline.json",
        retrieval_path=root / "deployment/config/retrieval.json",
        corpus_policy_path=root / "deployment/config/corpus-policy.json",
        intent_router_path=(
            root / "deployment/config/intent-router.json"
        ),
        intent_router_calibration_path=(
            root / "deployment/config/intent-router-calibration.json"
        ),
        frontend_dir=root / "frontend",
        llm_tokenizer_path=(
            root / "deployment/assets/tokenizers/llm/tokenizer.json"
        ),
        embedding_tokenizer_path=(
            root / "deployment/assets/tokenizers/embedding/tokenizer.json"
        ),
        input_root=docs,
        index_state_dir=tmp_path / "indexes",
        embedding_endpoints='["http://embedding:80"]',
        reranker_endpoints='["http://reranker:80"]',
        llm_endpoints='["http://llm:80"]',
        ocr_endpoints='["http://ocr:8090"]',
        embedding_model="Qwen3-Embedding-0.6B",
        reranker_model="Qwen3-Reranker-0.6B",
        llm_model="Qwen/Qwen3-8B-AWQ",
    )


def test_pool_override_caps_transport_failover_to_two(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        return httpx.Response(503)

    pool = runtime_module._pool(
        tuple(f"http://llm-{index}" for index in range(4)),
        httpx.Client(transport=httpx.MockTransport(unavailable)),
        _settings(tmp_path),
        max_concurrency=4,
        max_attempts=2,
    )

    with pytest.raises(ExternalServiceUnavailableError):
        pool.request_json("POST", "/v1/chat/completions", payload={})

    assert len(calls) == 2
    assert len(set(calls)) == 2


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    fail_readiness_start: bool,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_validate_runtime_contract",
        _noop,
    )
    monkeypatch.setattr(
        runtime_module,
        "_reject_incompatible_active_index",
        _noop,
    )

    def qdrant_client(**kwargs: object) -> _TrackedResource:
        del kwargs
        return _TrackedResource("qdrant-close", calls)

    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        qdrant_client,
    )

    def http_client(*args: object) -> _TrackedResource:
        del args
        name = f"http-{sum(item.startswith('http-new') for item in calls)}"
        calls.append(f"http-new-{name}")
        return _TrackedResource(name + "-close", calls)

    monkeypatch.setattr(runtime_module, "_http_client", http_client)
    def readiness(probes: object) -> _TrackedReadiness:
        del probes
        return _TrackedReadiness(
            calls,
            fail_start=fail_readiness_start,
        )

    monkeypatch.setattr(runtime_module, "ReadinessService", readiness)
    monkeypatch.setattr(
        runtime_module,
        "QueryExecutor",
        lambda: _TrackedExecutor(calls),
    )


@pytest.mark.parametrize(
    "failure_point",
    ("create_app", "readiness_start"),
)
def test_runtime_construction_failure_closes_all_owned_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    calls: list[str] = []
    _install_runtime_fakes(
        monkeypatch,
        calls,
        fail_readiness_start=failure_point == "readiness_start",
    )

    def create_app(services: object) -> object:
        del services
        calls.append("create-app")
        if failure_point == "create_app":
            raise RuntimeError("synthetic create app failure")
        return object()

    monkeypatch.setattr(runtime_module, "create_app", create_app)

    with pytest.raises(RuntimeError, match="synthetic"):
        build_runtime(_settings(tmp_path))

    close_calls = tuple(
        item
        for item in calls
        if item.endswith("-close")
    )
    assert close_calls[:2] == ("executor-close", "readiness-close")
    assert close_calls[2:-1] == tuple(
        f"http-{index}-close" for index in reversed(range(6))
    )
    assert close_calls[-1] == "qdrant-close"


def test_runtime_http_construction_failure_closes_partial_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "_validate_runtime_contract",
        _noop,
    )
    monkeypatch.setattr(
        runtime_module,
        "_reject_incompatible_active_index",
        _noop,
    )

    def qdrant_client(**kwargs: object) -> _TrackedResource:
        del kwargs
        return _TrackedResource("qdrant-close", calls)

    monkeypatch.setattr(
        runtime_module,
        "QdrantClient",
        qdrant_client,
    )
    created = 0

    def http_client(*args: object) -> _TrackedResource:
        nonlocal created
        del args
        if created == 2:
            raise RuntimeError("synthetic HTTP construction failure")
        resource = _TrackedResource(f"http-{created}-close", calls)
        created += 1
        return resource

    monkeypatch.setattr(runtime_module, "_http_client", http_client)

    with pytest.raises(RuntimeError, match="HTTP construction"):
        build_runtime(_settings(tmp_path))

    assert calls == [
        "http-1-close",
        "http-0-close",
        "qdrant-close",
    ]


def test_runtime_uses_service_specific_readiness_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    probes: list[tuple[str, str | None]] = []
    _install_runtime_fakes(
        monkeypatch,
        calls,
        fail_readiness_start=False,
    )

    def endpoint_probe(**kwargs: object) -> object:
        expected_model = kwargs["expected_model"]
        assert expected_model is None or isinstance(expected_model, str)
        probes.append(
            (
                str(kwargs["name"]),
                expected_model,
            )
        )
        return object()

    monkeypatch.setattr(runtime_module, "HttpEndpointProbe", endpoint_probe)

    def create_app(services: object) -> object:
        del services
        return object()

    monkeypatch.setattr(
        runtime_module,
        "create_app",
        create_app,
    )

    bundle = build_runtime(_settings(tmp_path))
    bundle.close()

    assert probes == [
        ("embedding", None),
        ("reranker", None),
        ("llm", "Qwen/Qwen3-8B-AWQ"),
    ]


def test_worker_initialize_failure_closes_network_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        worker_runtime_module,
        "require_indexable_configuration",
        _noop,
    )
    monkeypatch.setattr(
        worker_runtime_module,
        "_validate_worker_contract",
        _empty_metadata,
    )

    def qdrant_client(**kwargs: object) -> _TrackedResource:
        del kwargs
        return _TrackedResource("qdrant-close", calls)

    monkeypatch.setattr(
        worker_runtime_module,
        "QdrantClient",
        qdrant_client,
    )
    created = 0

    def http_client(*args: object, **kwargs: object) -> _TrackedResource:
        nonlocal created
        del args, kwargs
        name = "http-close" if created == 0 else "ocr-http-close"
        created += 1
        return _TrackedResource(name, calls)

    monkeypatch.setattr(
        httpx,
        "Client",
        http_client,
    )
    monkeypatch.setattr(
        worker_runtime_module,
        "StateStore",
        _FailingStateStore,
    )

    with pytest.raises(RuntimeError, match="state initialize"):
        build_worker_runtime(_settings(tmp_path))

    assert calls == ["ocr-http-close", "http-close", "qdrant-close"]
