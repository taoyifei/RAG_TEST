"""生成包在真实 HTTP 序列化边界的 request 局部观测。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.generation_packet import PreparedGenerationPacket


@dataclass(slots=True)
class GenerationPacketCapture:
    """每次生成持有独立对象，不在 adapter 实例保存会话状态。"""

    packet: PreparedGenerationPacket


_CURRENT: ContextVar[GenerationPacketCapture | None] = ContextVar(
    "generation_packet_capture", default=None
)


@contextmanager
def generation_packet_scope(
    packet: PreparedGenerationPacket,
) -> Iterator[GenerationPacketCapture]:
    """将发送审计限定于当前同步调用及其流式回调。

    Args:
        packet: 完成预算和证据选择的本次生成包。

    Returns:
        管理当前调用观测生命周期的上下文管理迭代器。

    Yields:
        本次调用独享的传输观测，退出时恢复外层上下文。

    """
    capture = GenerationPacketCapture(packet)
    token = _CURRENT.set(capture)
    try:
        yield capture
    except (RagError, QueryCancelled) as error:
        # 内部异常附件只供 SAFE Trace 使用，不改变公开错误序列化。
        vars(error)["_prepared_generation_packet"] = capture.packet
        raise
    finally:
        _CURRENT.reset(token)


def observe_generation_transport(payload: Mapping[str, object]) -> None:
    """在 HTTP 调用前验证消息未变，并记录将要发送的完整 body 摘要。

    Args:
        payload: 即将交给同一 HTTP 请求的最终序列化字段。

    Returns:
        无返回值；只更新当前调用内的 SAFE 摘要。

    """
    capture = _CURRENT.get()
    if capture is None:
        return
    if canonical_sha256(payload.get("messages")) != (
        capture.packet.messages_sha256
    ):
        raise ValueError("PREPARED_PACKET_MESSAGES_CHANGED")
    capture.packet = capture.packet.model_copy(
        update={"transport_body_sha256": canonical_sha256(dict(payload))}
    )


def packet_failure(
    error: RagError, packet: PreparedGenerationPacket
) -> RagError:
    """解析失败同样保留发送身份，不向公开错误 details 增添协议字段。

    Args:
        error: 已按现有错误协议分类的内部异常。
        packet: 本次失败前实际准备或发送的生成包。

    Returns:
        原异常对象，仅新增不会公开序列化的包附件。

    """
    vars(error)["_prepared_generation_packet"] = packet
    return error


def complete_generation_transport(prompt_tokens: int | None) -> None:
    """收到 Provider 响应后才标 SENT，实际 usage 缺失保持未知。

    Args:
        prompt_tokens: Provider 实际报告的输入 token 数，未知时为 None。

    Returns:
        无返回值；更新当前包的发送状态及估计误差。

    """
    capture = _CURRENT.get()
    if capture is None:
        return
    capture.packet = capture.packet.model_copy(
        update={
            "evidence_level": "TRANSPORT_SENT",
            "observed_prompt_tokens": prompt_tokens,
            "estimate_error_tokens": (
                prompt_tokens - capture.packet.estimated_input_tokens
                if prompt_tokens is not None
                else None
            ),
        }
    )
