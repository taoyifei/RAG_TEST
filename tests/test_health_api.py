import uuid
from dataclasses import dataclass

from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.health import ComponentStatus, ReadinessService


@dataclass(frozen=True, slots=True)
class _Probe:
    status: ComponentStatus

    def check(self) -> ComponentStatus:
        return self.status


def _services(*, llm_ready: bool) -> ApiServices:
    return ApiServices(
        readiness=ReadinessService(
            (
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
        ),
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
    )


def test_live_is_independent_and_ready_returns_503_when_all_llms_bad() -> None:
    client = TestClient(create_app(_services(llm_ready=False)))

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
    client = TestClient(create_app(_services(llm_ready=True)))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
