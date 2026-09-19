"""候选隔离快照的一次检索诊断，在原回答链的生成前确定性停止。"""

# 捕获既有私有函数的完整关键字合同；该脚本不成为运行时接口。
# ruff: noqa: ANN401

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from copy import copy
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import patch

from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.events import TraceEvent
from rag_app.core.models import SearchAnswerResult, SearchRequest


class _RetrievalCompleteError(Exception):
    """检索已完成；此异常只能由独立诊断入口捕获。"""


class _EmptyCache:
    def get(self, cache_key: str) -> None:
        """独立诊断不复用缓存。

        Args:
            cache_key: 仅为端口兼容接收的缓存键。

        Returns:
            空值，保证本次检索没有缓存命中。

        """
        del cache_key

    def put(
        self,
        cache_key: str,
        result: SearchAnswerResult,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        """检索停止前不应写入最终答案。

        Args:
            cache_key: 被禁止写入的缓存键。
            result: 被禁止发布的结果。
            ttl_seconds: 仅用于满足原缓存端口的参数。

        Returns:
            不正常返回；任何发布尝试都明确报错。

        """
        del cache_key, result, ttl_seconds
        raise RuntimeError("RETRIEVAL_DIAGNOSTIC_CANNOT_PUBLISH")

    def close(self) -> None:
        """没有待释放的缓存资源。

        Args:
            无参数；关闭当前空缓存。

        Returns:
            无返回值。

        """


class _TraceCollector:
    descriptor = ComponentDescriptor(
        kind=ComponentKind.TRACE_SINK,
        name="private-retrieval-diagnostic",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: TraceEvent) -> None:
        """保留该单次请求的真实事件，不经过产品展示层裁剪。

        Args:
            event: 应用产生的不含正文的 SAFE 事件。

        Returns:
            无返回值；事件加入当前受控集合。

        """
        self.events.append(event.model_dump(mode="json"))

    def close(self) -> None:
        """进程内事件集合没有外部资源。

        Args:
            无参数；关闭当前事件集合。

        Returns:
            无返回值。

        """


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def run_diagnostic(
    request: SearchRequest, settings: ProductRuntimeSettings
) -> dict[str, Any]:
    """使用原 Planner、授权、snapshot 和检索函数，禁止进入生成及发布。

    Args:
        request: 受控文件读取的单个诊断请求。
        settings: 指向隔离候选数据快照的现有运行配置。

    Returns:
        含真实调用、固定快照、候选及 SAFE 事件的私有诊断记录。

    """
    collector = _TraceCollector()
    result: dict[str, Any] = {
        "status": "STARTED",
        "request": request.model_dump(mode="json"),
    }
    with build_product_runtime(settings, recover_jobs=False) as runtime:  # noqa: SIM117
        with runtime.profiles.retrieval_service_lease(
            request.scope.knowledge_base_id, runtime.retrieval_runtime.retrieval
        ) as leased:
            service = copy(leased)
            service._trace = collector
            service._cache = _EmptyCache()
            original_rank = service._rank_and_select
            original_snapshot = service._query_snapshot

            def snapshot(_self: Any, actual_request: SearchRequest) -> Any:
                """记录实际请求级快照。

                Args:
                    _self: 绑定的隔离服务副本。
                    actual_request: 原调用链传入的请求。

                Returns:
                    原方法返回的同一个不可变快照。

                """
                frozen = original_snapshot(actual_request)
                result["snapshot"] = _json_value(frozen)
                return frozen

            def rank(_self: Any, **kwargs: Any) -> None:
                """原样执行一次检索并在生成前终止。

                Args:
                    _self: 绑定的隔离服务副本。
                    **kwargs: 原检索函数的真实关键字参数。

                Returns:
                    不正常返回；通过私有完成异常终止回答链。

                """
                selected = original_rank(**kwargs)
                result["selection"] = _json_value(selected)
                result["provider_calls"] = _json_value(kwargs["provider_calls"])
                result["stage_timings"] = _json_value(kwargs["stage_timings"])
                result["status"] = "RETRIEVAL_COMPLETE_GENERATION_NOT_RUN"
                raise _RetrievalCompleteError

            if service._adaptive_planner is not None:
                planner = service._adaptive_planner

                class _PlannerCapture:
                    def plan_adaptive(self, *args: Any, **kwargs: Any) -> Any:
                        """保留既有 Planner 的单次真实响应。

                        Args:
                            *args: 原 Planner 的位置参数。
                            **kwargs: 原 Planner 的关键字参数。

                        Returns:
                            原响应对象，不附加调用或改写计划。

                        """
                        actual = planner.plan_adaptive(*args, **kwargs)
                        result["planner_response"] = _json_value(actual)
                        return actual

                service._adaptive_planner = _PlannerCapture()
            with (
                patch.object(
                    service, "_query_snapshot", MethodType(snapshot, service)
                ),
                patch.object(
                    service, "_rank_and_select", MethodType(rank, service)
                ),
            ):
                try:
                    service.search_and_answer(request, cache_result=False)
                except _RetrievalCompleteError:
                    pass
                else:
                    raise RuntimeError(
                        "REQUEST_DID_NOT_REACH_RETRIEVAL_BOUNDARY"
                    )
    result["trace_events"] = collector.events
    result["generation_calls"] = 0
    result["cache_hit"] = False
    return result


def main() -> None:
    """请求正文和输出仅通过显式受控文件交换，不写终端。

    Args:
        无参数；通过命令行解析受控请求及输出路径。

    Returns:
        无返回值；终端只显示完成状态与零生成计数。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument("--original-data-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = ProductRuntimeSettings.from_environment()
    if settings.data_dir.resolve() == args.original_data_dir.resolve():
        raise ValueError("DIAGNOSTIC_REQUIRES_ISOLATED_DATA_SNAPSHOT")
    if args.output_file.exists() or not args.output_file.parent.is_dir():
        raise ValueError("OUTPUT_MUST_BE_NEW_FILE_IN_CONTROLLED_DIRECTORY")
    request = SearchRequest.model_validate_json(
        args.request_file.read_text("utf-8")
    )
    result = run_diagnostic(request, settings)
    descriptor = os.open(
        args.output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "generation_calls": 0}))


if __name__ == "__main__":
    main()
