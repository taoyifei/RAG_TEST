"""权威离线测试使用的确定性 UTF-8 计数器。"""

from __future__ import annotations

from rag_app.core.models import TokenCountResult


class DeterministicUtf8TokenCounter:
    """把每个 UTF-8 字节视为一个离线测试 token。"""

    tokenizer_id = "deterministic-utf8-v1"
    exact = True

    def count(self, text: str) -> TokenCountResult:
        """返回可跨进程复现的 UTF-8 字节计数。

        Args:
            text: 待计数文本。

        Returns:
            仅与 deterministic 测试模型兼容的精确结果。

        """
        return TokenCountResult(
            count=len(text.encode("utf-8")),
            tokenizer_id=self.tokenizer_id,
            exact=self.exact,
            model_compatibility=("deterministic-sha256-v1",),
        )
