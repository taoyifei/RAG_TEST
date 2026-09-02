"""不依赖 FastAPI 或基础设施实现的最小同步 RagEngine。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.errors import CapabilityUnavailable


class ComponentBundle(Protocol):
    """Composition Root 返回给应用层的最小生命周期视图。"""

    def component_info(self) -> tuple[ComponentDescriptor, ...]:
        """返回安全组件描述符。

        Args:
            无参数；读取当前组件集合。

        Returns:
            不含 secret 的组件描述符。

        """
        ...

    def close(self) -> None:
        """幂等关闭所有资源。

        Args:
            无参数；关闭当前组件集合。

        Returns:
            无返回值。

        """
        ...


class RagEngine:
    """宿主可嵌入的同步应用外壳。"""

    def __init__(self, components: ComponentBundle) -> None:
        """保存显式装配结果。

        Args:
            components: 由宿主或 Composition Root 提供的组件集合。

        Returns:
            无返回值。

        """
        self._components = components
        self._closed = False

    @classmethod
    def from_components(cls, components: ComponentBundle) -> RagEngine:
        """从宿主直接注入的组件集合构造引擎。

        Args:
            components: 已完成能力验证的组件集合。

        Returns:
            新同步 RagEngine。

        """
        return cls(components)

    @classmethod
    def from_profile(
        cls,
        profile: object,
        *,
        builder: Callable[[object], ComponentBundle],
    ) -> RagEngine:
        """通过宿主注入的 builder 从 Profile 构造引擎。

        应用层不反向 import composition；宿主负责把 Profile 交给唯一
        Composition Root。

        Args:
            profile: 宿主认可的严格 Profile 对象或路径。
            builder: 宿主注入的同步 Composition Root callback。

        Returns:
            新同步 RagEngine。

        """
        return cls(builder(profile))

    def component_info(self) -> tuple[ComponentDescriptor, ...]:
        """返回不含 secret 的装配清单。

        Args:
            无参数；读取当前引擎。

        Returns:
            组件来源、版本、mode 与 capability。

        """
        return self._components.component_info()

    def health(self) -> tuple[tuple[str, str], ...]:
        """执行不联网的受控组件存在性检查。

        Args:
            无参数；不会触发 Provider 网络探测。

        Returns:
            每个组件名及 `configured` 状态。

        """
        return tuple(
            (descriptor.name, "configured")
            for descriptor in self.component_info()
        )

    def ingest(self, request: object) -> None:
        """拒绝尚未迁移的 ingest 用例。

        Args:
            request: 未执行的宿主请求。

        Returns:
            此方法不会返回。

        Raises:
            CapabilityUnavailable: P01 尚未迁移 ingest。

        """
        del request
        raise CapabilityUnavailable(
            "ingest 用例尚未迁移到通用 RagEngine。",
            stage="application.ingest",
        )

    def search(self, request: object) -> None:
        """拒绝尚未迁移的 search 用例。

        Args:
            request: 未执行的宿主请求。

        Returns:
            此方法不会返回。

        Raises:
            CapabilityUnavailable: P01 尚未迁移 search。

        """
        del request
        raise CapabilityUnavailable(
            "search 用例尚未迁移到通用 RagEngine。",
            stage="application.search",
        )

    def answer(self, request: object) -> None:
        """拒绝尚未迁移的 answer 用例。

        Args:
            request: 未执行的宿主请求。

        Returns:
            此方法不会返回。

        Raises:
            CapabilityUnavailable: P01 尚未迁移 answer。

        """
        del request
        raise CapabilityUnavailable(
            "answer 用例尚未迁移到通用 RagEngine。",
            stage="application.answer",
        )

    def close(self) -> None:
        """幂等关闭引擎拥有的组件集合。

        Args:
            无参数；关闭当前引擎。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._components.close()

    def __enter__(self) -> RagEngine:
        """进入引擎生命周期作用域。"""
        return self

    def __exit__(self, *_: object) -> None:
        """离开作用域并幂等关闭引擎。"""
        self.close()
