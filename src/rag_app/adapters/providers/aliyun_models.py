"""百炼问答模型在本应用中已验证的协议能力。"""

from __future__ import annotations

from typing import Final

# 这里只维护通过当前 Chat Completions、非思考模式和 JSON Object 合同的
# 模型身份；实际优先级由每个知识库的设置决定。
ALIYUN_GROUNDED_CHAT_MODELS: Final = (
    "qwen3.8-flash",
    "qwen3.7-flash-2026-07-15",
    "qwen3.7-flash",
)
ALIYUN_JSON_OBJECT_MODELS: Final = frozenset(ALIYUN_GROUNDED_CHAT_MODELS)
ALIYUN_DISABLE_THINKING_MODELS: Final = frozenset(
    ALIYUN_GROUNDED_CHAT_MODELS
)


__all__ = [
    "ALIYUN_DISABLE_THINKING_MODELS",
    "ALIYUN_GROUNDED_CHAT_MODELS",
    "ALIYUN_JSON_OBJECT_MODELS",
]
