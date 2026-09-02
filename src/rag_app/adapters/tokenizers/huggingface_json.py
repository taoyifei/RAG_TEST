"""只加载本地 tokenizer.json 的 Hugging Face 计数器。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tokenizers import Tokenizer

from rag_app.core.models import TokenCountResult


class HuggingFaceJsonTokenCounter:
    """绑定本地 tokenizer.json 内容摘要的精确计数器。"""

    exact = True

    def __init__(
        self,
        tokenizer_path: Path,
        *,
        model_compatibility: tuple[str, ...],
    ) -> None:
        """从现有普通文件加载 tokenizer，禁止网络下载。

        Args:
            tokenizer_path: 用户显式提供的本地 tokenizer.json。
            model_compatibility: 已核对的模型身份。

        Returns:
            无返回值。

        Raises:
            FileNotFoundError: 路径不是现有非 symlink 普通文件。

        """
        if tokenizer_path.is_symlink() or not tokenizer_path.is_file():
            raise FileNotFoundError(
                "tokenizer.json 必须是现有非 symlink 文件。"
            )
        content = tokenizer_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self.tokenizer_id = f"huggingface-json-sha256:{digest}"
        self._model_compatibility = model_compatibility
        self._tokenizer = Tokenizer.from_buffer(content)

    def count(self, text: str) -> TokenCountResult:
        """精确计算不含额外 special token 的 token 数。

        Args:
            text: 待计数文本。

        Returns:
            绑定 tokenizer 内容摘要的精确结果。

        """
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        return TokenCountResult(
            count=len(encoding.ids),
            tokenizer_id=self.tokenizer_id,
            exact=self.exact,
            model_compatibility=self._model_compatibility,
        )
