"""同步且禁止隐式下载的 TokenCounter 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import TokenCountResult


class TokenCounterPort(Protocol):
    """返回计数值、身份、精确性和模型兼容范围。"""

    @property
    def tokenizer_id(self) -> str:
        """返回稳定 tokenizer 身份。

        Args:
            无参数；读取当前计数器。

        Returns:
            不含机器绝对路径的稳定身份。

        """
        ...

    @property
    def exact(self) -> bool:
        """返回当前计数算法是否精确。

        Args:
            无参数；读取当前计数器。

        Returns:
            精确计数返回 True，保守估算返回 False。

        """
        ...

    def count(self, text: str) -> TokenCountResult:
        """计算一段 Provider 输入文本的 token 数。

        Args:
            text: 待计数文本。

        Returns:
            包含身份和兼容性的计数结果。

        """
        ...
