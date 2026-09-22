"""从环境变量解析湾事通产品模式。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from rag_app.wanshitong.mode import ProductMode
from rag_app.wanshitong.sso_settings import SsoSettings

_PRODUCT_MODE_ENVIRONMENT_KEY = "RAG_PRODUCT_MODE"
_DEMO_ALLOW_HTTP_ENVIRONMENT_KEY = "RAG_WANSHITONG_DEMO_ALLOW_HTTP"
_DEPARTMENT_SHADOW_ENVIRONMENT_KEY = (
    "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
)


@dataclass(frozen=True, slots=True)
class WanshitongSettings:
    """控制湾事通壳层是否随 Product API 启动。"""

    product_mode: ProductMode = ProductMode.UNIVERSAL
    demo_allow_http: bool = False
    department_shadow_enabled: bool = False
    sso: SsoSettings = field(default_factory=SsoSettings)

    @property
    def enabled(self) -> bool:
        """仅在显式湾事通模式下启用壳层。"""
        return self.product_mode is ProductMode.WANSHITONG

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> WanshitongSettings:
        """读取产品模式，缺省时保持 Universal 行为。

        Args:
            environment: 可选环境变量映射；默认读取当前进程环境。

        Returns:
            已校验的湾事通模式设置。

        Raises:
            ValueError: `RAG_PRODUCT_MODE` 不是受支持的模式。

        """
        source = os.environ if environment is None else environment
        raw_mode = source.get(
            _PRODUCT_MODE_ENVIRONMENT_KEY, ProductMode.UNIVERSAL.value
        )
        try:
            product_mode = ProductMode(raw_mode.strip().casefold())
        except ValueError:
            raise ValueError(
                "RAG_PRODUCT_MODE 仅支持 universal 或 wanshitong。"
            ) from None
        raw_allow_http = (
            source.get(_DEMO_ALLOW_HTTP_ENVIRONMENT_KEY, "false")
            .strip()
            .casefold()
        )
        if raw_allow_http not in {"true", "false"}:
            raise ValueError(
                "RAG_WANSHITONG_DEMO_ALLOW_HTTP 仅支持 true 或 false。"
            )
        raw_department_shadow = (
            source.get(_DEPARTMENT_SHADOW_ENVIRONMENT_KEY, "false")
            .strip()
            .casefold()
        )
        if raw_department_shadow not in {"true", "false"}:
            raise ValueError(
                "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED "
                "仅支持 true 或 false。"
            )
        return cls(
            product_mode=product_mode,
            demo_allow_http=(
                product_mode is ProductMode.WANSHITONG
                and raw_allow_http == "true"
            ),
            department_shadow_enabled=(
                product_mode is ProductMode.WANSHITONG
                and raw_department_shadow == "true"
            ),
            sso=(
                SsoSettings.from_environment(
                    source,
                    root_path=source.get("RAG_ROOT_PATH", "").strip(),
                )
                if product_mode is ProductMode.WANSHITONG
                else SsoSettings()
            ),
        )


__all__ = ["WanshitongSettings"]
