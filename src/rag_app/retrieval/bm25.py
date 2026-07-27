"""Qdrant 内置离线 BM25 的确定性文档与查询编码。"""

from __future__ import annotations

import hashlib
import json

from qdrant_client.http import models

__all__ = ["QdrantBm25Encoder"]

_SUPPORTED_TOKENIZERS = frozenset({"multilingual", "word"})


class QdrantBm25Encoder:
    """用完全相同的选项构造 ingest/query BM25 Document。"""

    def __init__(self, *, tokenizer: str, language: str) -> None:
        """冻结中文 BM25 文本处理选项。

        Args:
            tokenizer: 当前消融允许 multilingual 或 word。
            language: 当前中文基线必须为 none，禁用英文词干和停用词。

        Raises:
            ValueError: 使用未经本轮基准覆盖的选项。

        """
        if tokenizer not in _SUPPORTED_TOKENIZERS:
            raise ValueError("tokenizer 未纳入当前 BM25 基准。")
        if language != "none":
            raise ValueError("中文 BM25 基线必须禁用英文词干和停用词。")
        self._options: dict[str, object] = {
            "language": language,
            "tokenizer": tokenizer,
        }

    def embed_document(self, text: str) -> models.Document:
        """构造由 Qdrant server 离线编码的文档输入。

        Args:
            text: chunk 的 embedding 文本。

        Returns:
            带冻结处理选项的 BM25 Document。

        """
        return self._document(text)

    def embed_query(self, text: str) -> models.Document:
        """构造与 ingest 选项一致的查询输入。

        Args:
            text: 原始或改写查询。

        Returns:
            带冻结处理选项的 BM25 Document。

        """
        return self._document(text)

    def revision(self) -> str:
        """返回模型与处理选项的规范化 SHA256。"""
        serialized = json.dumps(
            {
                "model": "qdrant/bm25",
                "options": self._options,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"

    def _document(self, text: str) -> models.Document:
        if not text.strip():
            raise ValueError("BM25 输入不能为空。")
        return models.Document(
            text=text,
            model="qdrant/bm25",
            options=dict(self._options),
        )
