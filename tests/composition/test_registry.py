import sys

import pytest
from pydantic import BaseModel, ConfigDict, Field

from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)
from rag_app.core.errors import (
    ComponentNotRegistered,
    ConfigurationError,
    Conflict,
)


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: int = Field(gt=0)


def test_registry_starts_empty_and_builtins_are_explicit() -> None:
    registry = ComponentRegistry()
    assert registry.list_components() == ()
    register_builtin_components(registry)
    names = {item.name for item in registry.list_components()}
    assert {
        "legacy-docx-ir",
        "jina-embedding",
        "aliyun-qwen37-embedding",
        "jina-reranker",
        "embedding-router-hot-standby",
        "embedding-router-single",
    }.issubset(names)


def test_duplicate_and_unknown_registration_fail_closed() -> None:
    registry = ComponentRegistry()
    registry.register_parser("safe-parser", object)
    with pytest.raises(Conflict):
        registry.register_parser("safe-parser", object)
    with pytest.raises(ComponentNotRegistered):
        registry.get_parser("missing")


@pytest.mark.parametrize(
    "name",
    ["../../x", "os.system(...)用户输入", "package.module", "UPPER", "a/b"],
)
def test_malicious_names_never_trigger_import(name: str) -> None:
    registry = ComponentRegistry()
    before = set(sys.modules)
    with pytest.raises(ConfigurationError):
        registry.get_parser(name)
    assert set(sys.modules) == before


def test_factory_configuration_is_validated_before_creation() -> None:
    calls: list[object] = []

    def _capture(config: object) -> None:
        calls.append(config)

    registry = ComponentRegistry()
    registry.register_parser(
        "strict-parser",
        _capture,
        config_model=_StrictConfig,
    )
    with pytest.raises(ConfigurationError) as captured:
        registry.get_parser("strict-parser", {"dimension": 0, "extra": True})
    assert calls == []
    assert dict(captured.value.details)["paths"]


def test_list_components_exposes_no_secret_values() -> None:
    registry = ComponentRegistry()
    register_builtin_components(registry)
    rendered = repr(registry.list_components())
    assert "JINA_API_KEY=" not in rendered
    assert "DASHSCOPE_API_KEY=" not in rendered
