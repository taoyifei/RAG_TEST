"""Profile、Registry 与 Composition Root 公共入口。"""

from rag_app.composition.factory import RagComponents, build_components
from rag_app.composition.p06_runtime import P06Runtime, build_p06_runtime
from rag_app.composition.p07_runtime import P07Runtime, build_p07_runtime
from rag_app.composition.profiles import (
    ComponentsProfile,
    EmbeddingSlotProfile,
    EmbeddingTopologyProfile,
    LocalDataProfile,
    RagProfile,
    RerankerProfile,
    default_hot_standby_profile,
    default_offline_profile,
    load_profile,
    profile_from_mapping,
)
from rag_app.composition.provider_profiles import (
    load_named_provider_profile,
    load_provider_catalog,
)
from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)

__all__ = [
    "ComponentRegistry",
    "ComponentsProfile",
    "EmbeddingSlotProfile",
    "EmbeddingTopologyProfile",
    "LocalDataProfile",
    "P06Runtime",
    "P07Runtime",
    "RagComponents",
    "RagProfile",
    "RerankerProfile",
    "build_components",
    "build_p06_runtime",
    "build_p07_runtime",
    "default_hot_standby_profile",
    "default_offline_profile",
    "load_named_provider_profile",
    "load_profile",
    "load_provider_catalog",
    "profile_from_mapping",
    "register_builtin_components",
]
