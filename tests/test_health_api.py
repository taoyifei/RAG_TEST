import uuid
from dataclasses import dataclass
from threading import Event

from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.health import ComponentStatus, ReadinessService


@dataclass(frozen=True, slots=True)
class _Probe:
    status: ComponentStatus
    calls: int = 0

    def check(self) -> ComponentStatus:
        object.__setattr__(self, "calls", self.calls + 1)
        return self.status


@dataclass(slots=True)
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(slots=True)
class _SignallingProbe:
    checked_twice: Event
    calls: int = 0

    def check(self) -> ComponentStatus:
        self.calls += 1
        if self.calls >= 2:
            self.checked_twice.set()
        return ComponentStatus("local", True, "ready", 1, 1)


class _FailingProbe:
    def check(self) -> ComponentStatus:
        raise RuntimeError("private dependency details")


def _services(*, llm_ready: bool) -> tuple[ApiServices, tuple[_Probe, ...]]:
    probes = (
        _Probe(ComponentStatus("qdrant", True, "ready", 1, 1)),
        _Probe(ComponentStatus("embedding", True, "ready", 1, 1)),
        _Probe(ComponentStatus("reranker", True, "ready", 1, 1)),
        _Probe(
            ComponentStatus(
                "llm",
                llm_ready,
                "ready" if llm_ready else "no healthy endpoint",
                1 if llm_ready else 0,
                4,
            )
        ),
    )
    readiness = ReadinessService(probes)
    readiness.refresh_once()
    return ApiServices(
        readiness=readiness,
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
    ), probes


def test_live_is_independent_and_ready_returns_503_when_all_llms_bad() -> None:
    services, _ = _services(llm_ready=False)
    client = TestClient(create_app(services))

    assert client.get("/live").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert next(
        item
        for item in response.json()["components"]
        if item["name"] == "llm"
    )["healthy_endpoints"] == 0


def test_ready_is_200_with_at_least_one_healthy_llm() -> None:
    services, _ = _services(llm_ready=True)
    client = TestClient(create_app(services))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_ready_and_chat_only_read_cached_snapshot() -> None:
    clock = _Clock()
    probe = _Probe(ComponentStatus("local", True, "ready", 1, 1))
    readiness = ReadinessService(
        (probe,),
        refresh_interval_seconds=10,
        max_staleness_seconds=20,
        clock=clock,
    )
    services = ApiServices(
        readiness=readiness,
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
    )
    client = TestClient(create_app(services))

    assert client.get("/ready").status_code == 503
    assert probe.calls == 0
    readiness.refresh_once()
    assert probe.calls == 1

    assert client.get("/ready").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {services.query_token}"},
        json={"conversation_id": "c", "question": "问题"},
    ).status_code == 503
    assert probe.calls == 1

    clock.advance(21)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert probe.calls == 1


def test_readiness_background_failure_is_safe_and_close_joins() -> None:
    failed = ReadinessService((_FailingProbe(),))
    failed.refresh_once()
    report = failed.check()
    assert report.ready is False
    assert report.components[0].detail == "readiness refresh failed"

    checked_twice = Event()
    probe = _SignallingProbe(checked_twice)
    service = ReadinessService(
        (probe,),
        refresh_interval_seconds=0.01,
        max_staleness_seconds=1,
    )
    service.start()
    assert checked_twice.wait(timeout=1)
    assert service.is_running()

    service.close()

    assert not service.is_running()
