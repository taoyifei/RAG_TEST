"""没有本地 tokenizer 时使用的保守估算器。"""

from __future__ import annotations

import math

from rag_app.core.models import TokenCountResult


class ConservativeEstimatedTokenCounter:
    """用 UTF-8 字节数和安全余量提供明确标记的上界估算。"""

    exact = False

    def __init__(
        self,
        *,
        safety_margin: float = 0.15,
        model_compatibility: tuple[str, ...] = (),
    ) -> None:
        """冻结估算余量和兼容模型声明。

        Args:
            safety_margin: 在 UTF-8 字节计数上增加的比例。
            model_compatibility: 使用该保守合同的模型身份。

        Returns:
            无返回值。

        Raises:
            ValueError: 余量不是 `[0, 1)` 范围。

        """
        if not 0.0 <= safety_margin < 1.0:
            raise ValueError("estimated token safety margin 必须位于 [0, 1)。")
        self._safety_margin = safety_margin
        self._model_compatibility = model_compatibility
        basis_points = round(safety_margin * 10000)
        self.tokenizer_id = f"conservative-utf8-estimate-v1-{basis_points}bp"

    def count(self, text: str) -> TokenCountResult:
        """返回含安全余量的保守 token 估算。

        Args:
            text: 待估算文本。

        Returns:
            明确标记为非精确的计数结果。

        """
        byte_count = len(text.encode("utf-8"))
        estimated = math.ceil(byte_count * (1.0 + self._safety_margin))
        return TokenCountResult(
            count=estimated,
            tokenizer_id=self.tokenizer_id,
            exact=self.exact,
            model_compatibility=self._model_compatibility,
        )
