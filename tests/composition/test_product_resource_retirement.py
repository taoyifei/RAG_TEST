"""Product Profile generation 租约与资源退役回归。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

from rag_app.adapters.stores.memory_retrieval_cache import (
    InMemoryRetrievalCache,
)
from rag_app.application.lifecycle import LifecycleService
from rag_app.application.retrieval import RetrievalService
from rag_app.application.retrieval.service import RetrievalExecutionIdentity
from rag_app.composition import product_runtime
from rag_app.composition.p09_runtime import P09Runtime
from rag_app.composition.product_runtime import ProductProfileResolver
from rag_app.core.models import (
    KnowledgeBaseScope,
    SearchAnswerResult,
    SearchRequest,
)
from rag_app.product.control_store import ProductControlStore
from rag_app.product.model_settings import ProductModelSettings
from rag_app.product.models import RetrievalProfileRevision
from rag_app.product.provider_runtime import ProviderRuntimeRegistry

_KNOWLEDGE_BASE_ID = f"kb_{'1' * 32}"
_PROJECT_ID = f"prj_{'2' * 32}"
_REVISION_ID = f"irev_{'3' * 32}"
_SERVING_FINGERPRINT = f"sha256:{'4' * 64}"


class _FakeQuality:
    def __init__(self) -> None:
        self.binding_version = 1

    def binding_identity(self, profile_revision_id: str) -> str:
        return f"binding:{profile_revision_id}:v{self.binding_version}"

    def states(self, profile_revision_id: str) -> dict[str, str]:
        return {"profile_revision_id": profile_revision_id}


class _FakeControl:
    def __init__(self, profile: RetrievalProfileRevision) -> None:
        self.quality = _FakeQuality()
        self.profile = profile

    def active_profile(
        self, knowledge_base_id: str
    ) -> RetrievalProfileRevision | None:
        if knowledge_base_id != self.profile.knowledge_base_id:
            return None
        return self.profile

    def get_profile(self, profile_revision_id: str) -> RetrievalProfileRevision:
        assert profile_revision_id == self.profile.profile_revision_id
        return self.profile


@dataclass(slots=True)
class _Closeable:
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeRetrieval:
    def __init__(self) -> None:
        self.on_search: object = None
        self.search_result = cast(SearchAnswerResult, object())
        self.generation: object = None
        self.resource_serving_fingerprint = ""

    def execution_identity(
        self, request: SearchRequest
    ) -> RetrievalExecutionIdentity:
        del request
        return RetrievalExecutionIdentity(
            key_hash=f"sha256:{'5' * 64}",
            active_revision_id=_REVISION_ID,
            serving_fingerprint=_SERVING_FINGERPRINT,
        )

    def with_generation(
        self,
        generator: object,
        *,
        serving_identity: str,
        interpreter: object = None,
        rewriter: object = None,
    ) -> _FakeRetrieval:
        del serving_identity, interpreter, rewriter
        self.generation = generator
        return self

    def search_and_answer(
        self, request: object, **kwargs: object
    ) -> SearchAnswerResult:
        del request, kwargs
        callback = self.on_search
        if callable(callback):
            callback()
        return self.search_result


class _FakeLifecycle:
    def __init__(self) -> None:
        self.on_ingestion: object = None

    def run_ingestion(self, job_id: str) -> None:
        del job_id
        callback = self.on_ingestion
        if callable(callback):
            callback()


@dataclass(slots=True)
class _BuiltServices:
    retrieval: _FakeRetrieval
    lifecycle: _FakeLifecycle
    cache: _Closeable
    remote: _Closeable


class _FakeModels:
    def __init__(self) -> None:
        self.connections = object()
        self.settings = SimpleNamespace(generation_connection_id="connection")

    def get(self, knowledge_base_id: str) -> object:
        assert knowledge_base_id == _KNOWLEDGE_BASE_ID
        return self.settings

    def serving_identity(self, settings: object) -> str:
        assert settings is self.settings
        return "generation-v1"


class _FakeGroundedModel(_Closeable):
    def __init__(self, *args: object) -> None:
        del args
        super().__init__()


def _profile(
    knowledge_base_id: str = _KNOWLEDGE_BASE_ID,
) -> RetrievalProfileRevision:
    return cast(
        RetrievalProfileRevision,
        SimpleNamespace(
            knowledge_base_id=knowledge_base_id,
            profile_revision_id=f"profile:{knowledge_base_id}",
            reranker_connection_id=None,
        ),
    )


def _resolver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: RetrievalProfileRevision | None = None,
    models: ProductModelSettings | None = None,
) -> tuple[ProductProfileResolver, list[_BuiltServices]]:
    selected = profile or _profile()
    control = _FakeControl(selected)
    resolver = ProductProfileResolver(
        cast(ProductControlStore, control),
        cast(ProviderRuntimeRegistry, object()),
        models=models,
    )
    built: list[_BuiltServices] = []

    def _build(
        _profile_revision: RetrievalProfileRevision,
        contract: product_runtime._ResolvedServiceGenerationContract,
    ) -> object:
        item = _BuiltServices(
            retrieval=_FakeRetrieval(),
            lifecycle=_FakeLifecycle(),
            cache=_Closeable(),
            remote=_Closeable(),
        )
        item.retrieval.resource_serving_fingerprint = (
            contract.serving_fingerprint
        )
        built.append(item)
        return product_runtime._ResolvedProductServices(
            lifecycle=cast(LifecycleService, item.lifecycle),
            retrieval=cast(RetrievalService, item.retrieval),
            cache=cast(InMemoryRetrievalCache, item.cache),
            remote_resources=(item.remote,),
        )

    monkeypatch.setattr(resolver, "_build", _build)
    monkeypatch.setattr(
        resolver,
        "serving_contract",
        lambda _profile_revision: (None, None, "serving-v1"),
    )
    return resolver, built


def _fallback_retrieval() -> RetrievalService:
    return cast(RetrievalService, object())


def _fallback_lifecycle() -> LifecycleService:
    return cast(LifecycleService, object())


def test_invalidate_retires_inflight_then_closes_on_last_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, built = _resolver(monkeypatch)

    with resolver.retrieval_service_lease(
        _KNOWLEDGE_BASE_ID, _fallback_retrieval()
    ) as first:
        resolver.invalidate(_KNOWLEDGE_BASE_ID)
        assert first is built[0].retrieval
        assert built[0].cache.close_calls == 0
        assert built[0].remote.close_calls == 0
        assert len(resolver._retired_services) == 1

        with resolver.retrieval_service_lease(
            _KNOWLEDGE_BASE_ID, _fallback_retrieval()
        ) as second:
            assert second is built[1].retrieval
            assert second is not first
        assert built[0].cache.close_calls == 0

    assert built[0].cache.close_calls == 1
    assert built[0].remote.close_calls == 1
    assert not resolver._retired_services
    resolver.invalidate(_KNOWLEDGE_BASE_ID)
    assert built[1].cache.close_calls == 1
    assert built[1].remote.close_calls == 1


def test_binding_rotation_changes_serving_identity_before_old_lease_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新代际不得沿用旧连接/凭据资源的 singleflight/cache 身份。"""
    resolver, built = _resolver(monkeypatch)
    control = cast(_FakeControl, resolver._control)

    with resolver.retrieval_service_lease(
        _KNOWLEDGE_BASE_ID, _fallback_retrieval()
    ) as first:
        first_identity = cast(
            _FakeRetrieval, first
        ).resource_serving_fingerprint
        control.quality.binding_version += 1
        resolver.invalidate(_KNOWLEDGE_BASE_ID)

        with resolver.retrieval_service_lease(
            _KNOWLEDGE_BASE_ID, _fallback_retrieval()
        ) as second:
            second_identity = cast(
                _FakeRetrieval, second
            ).resource_serving_fingerprint
            assert second_identity != first_identity
            assert built[0].remote.close_calls == 0

    assert built[0].remote.close_calls == 1


def test_retrieval_proxy_holds_lease_for_complete_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, built = _resolver(monkeypatch)
    proxy = resolver.retrieval_service(
        _KNOWLEDGE_BASE_ID, _fallback_retrieval()
    )

    def _invalidate_during_search() -> None:
        resolver.invalidate(_KNOWLEDGE_BASE_ID)
        assert built[0].cache.close_calls == 0
        assert built[0].remote.close_calls == 0

    expected = cast(SearchAnswerResult, object())

    def _prepare() -> None:
        built[0].retrieval.on_search = _invalidate_during_search
        built[0].retrieval.search_result = expected

    original_build = resolver._build

    def _build_with_callback(
        profile_revision: RetrievalProfileRevision,
        contract: product_runtime._ResolvedServiceGenerationContract,
    ) -> object:
        result = original_build(profile_revision, contract)
        _prepare()
        return result

    monkeypatch.setattr(resolver, "_build", _build_with_callback)
    result = proxy.search_and_answer(
        SearchRequest(
            scope=KnowledgeBaseScope(
                project_id=_PROJECT_ID,
                knowledge_base_id=_KNOWLEDGE_BASE_ID,
            ),
            text="验证完整查询租约",
            singleflight_enabled=False,
        )
    )

    assert result is expected
    assert built[0].cache.close_calls == 1
    assert built[0].remote.close_calls == 1


def test_job_proxy_holds_lease_for_complete_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, built = _resolver(monkeypatch)
    store = SimpleNamespace(
        ingestion_profile_revision_id=lambda _job_id: (
            f"profile:{_KNOWLEDGE_BASE_ID}"
        )
    )
    resolver._runtime = cast(P09Runtime, SimpleNamespace(store=store))
    proxy = resolver.job_lifecycle("job-1", _fallback_lifecycle())

    original_build = resolver._build

    def _build_with_callback(
        profile_revision: RetrievalProfileRevision,
        contract: product_runtime._ResolvedServiceGenerationContract,
    ) -> object:
        result = original_build(profile_revision, contract)

        def _invalidate_during_ingestion() -> None:
            resolver.invalidate(_KNOWLEDGE_BASE_ID)
            assert built[0].cache.close_calls == 0
            assert built[0].remote.close_calls == 0

        built[0].lifecycle.on_ingestion = _invalidate_during_ingestion
        return result

    monkeypatch.setattr(resolver, "_build", _build_with_callback)
    proxy.run_ingestion("job-1")

    assert built[0].cache.close_calls == 1
    assert built[0].remote.close_calls == 1


def test_one_hundred_rotations_keep_resources_bounded_and_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, built = _resolver(monkeypatch)

    for _ in range(100):
        with resolver.retrieval_service_lease(
            _KNOWLEDGE_BASE_ID, _fallback_retrieval()
        ):
            pass
        resolver.invalidate(_KNOWLEDGE_BASE_ID)
        assert not resolver._services
        assert not resolver._retired_services

    assert len(built) == 100
    assert all(item.cache.close_calls == 1 for item in built)
    assert all(item.remote.close_calls == 1 for item in built)
    resolver.close()
    resolver.close()
    assert all(item.cache.close_calls == 1 for item in built)
    with (
        pytest.raises(RuntimeError, match="已关闭"),
        resolver.retrieval_service_lease(
            _KNOWLEDGE_BASE_ID, _fallback_retrieval()
        ),
    ):
        pass


def test_grounded_model_waits_for_query_lease_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = _FakeModels()
    resolver, built = _resolver(
        monkeypatch,
        models=cast(ProductModelSettings, fake_models),
    )
    grounded: list[_FakeGroundedModel] = []

    def _grounded_factory(*args: object) -> _FakeGroundedModel:
        model = _FakeGroundedModel(*args)
        grounded.append(model)
        return model

    monkeypatch.setattr(
        product_runtime,
        "ProductGroundedModel",
        _grounded_factory,
    )

    with resolver.retrieval_service_lease(
        _KNOWLEDGE_BASE_ID, _fallback_retrieval()
    ):
        assert built[0].retrieval.generation is grounded[0]
        resolver.invalidate(_KNOWLEDGE_BASE_ID)
        assert grounded[0].close_calls == 0
        assert len(resolver._retired_models) == 1

    assert grounded[0].close_calls == 1
    assert not resolver._retired_models
