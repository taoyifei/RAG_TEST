"""从环境变量解析湾事通产品模式。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from rag_app.wanshitong.mode import ProductMode
from rag_app.wanshitong.sso_settings import SsoSettings

_PRODUCT_MODE_ENVIRONMENT_KEY = "RAG_PRODUCT_MODE"
_DEMO_ALLOW_HTTP_ENVIRONMENT_KEY = "RAG_WANSHITONG_DEMO_ALLOW_HTTP"
_DEPARTMENT_SHADOW_ENVIRONMENT_KEY = "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED"
_POPULAR_QUESTIONS_ENVIRONMENT_KEY = "RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED"
_NATURAL_PUBLIC_ENVIRONMENT_KEY = "RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED"
_NATURAL_ENGINE_ENVIRONMENT_KEY = "RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE"


@dataclass(frozen=True, slots=True)
class WanshitongSettings:
    """控制湾事通壳层是否随 Product API 启动。"""

    product_mode: ProductMode = ProductMode.UNIVERSAL
    demo_allow_http: bool = False
    department_shadow_enabled: bool = False
    popular_questions_enabled: bool = True
    natural_public_enabled: bool = False
    natural_public_engine: Literal["wk-standard-v1", "wk-standard-pc-v1"] = (
        "wk-standard-pc-v1"
    )
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
        raw_popular = (
            source.get(_POPULAR_QUESTIONS_ENVIRONMENT_KEY, "true")
            .strip()
            .casefold()
        )
        if raw_popular not in {"true", "false"}:
            raise ValueError(
                "RAG_WANSHITONG_POPULAR_QUESTIONS_ENABLED "
                "仅支持 true 或 false。"
            )
        raw_natural = (
            source.get(_NATURAL_PUBLIC_ENVIRONMENT_KEY, "false")
            .strip()
            .casefold()
        )
        if raw_natural not in {"true", "false"}:
            raise ValueError(
                "RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED 仅支持 true 或 false。"
            )
        natural_engine = source.get(
            _NATURAL_ENGINE_ENVIRONMENT_KEY, "wk-standard-pc-v1"
        ).strip()
        if natural_engine not in {"wk-standard-v1", "wk-standard-pc-v1"}:
            raise ValueError(
                "RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE 不是受支持的引擎。"
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
            popular_questions_enabled=(
                product_mode is ProductMode.WANSHITONG and raw_popular == "true"
            ),
            natural_public_enabled=(
                product_mode is ProductMode.WANSHITONG and raw_natural == "true"
            ),
            natural_public_engine=natural_engine,
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
