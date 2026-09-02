"""演示宿主无需 FastAPI 即可装配并管理 RagEngine。"""

from rag_app.application.engine import ComponentBundle, RagEngine
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_offline_profile,
    register_builtin_components,
)
from rag_app.composition.profiles import RagProfile


def main() -> None:
    """构造离线组件并打印安全组件名。

    Args:
        无参数；使用内置离线 Profile。

    Returns:
        无返回值。

    """
    registry = ComponentRegistry()
    register_builtin_components(registry)
    profile = default_offline_profile()

    def _build(selected: object) -> ComponentBundle:
        if not isinstance(selected, RagProfile):
            raise TypeError("示例只接受已验证 RagProfile。")
        return build_components(selected, registry)

    with RagEngine.from_profile(
        profile,
        builder=_build,
    ) as engine:
        print(",".join(item.name for item in engine.component_info()))


if __name__ == "__main__":
    main()
