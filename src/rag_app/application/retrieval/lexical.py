"""P07 版本化 FTS5 lexical 通道。"""

from __future__ import annotations

import re

from rag_app.application.retrieval.answer_support import descriptive_request
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChannelHit,
    LexicalSearchRequest,
    QueryVariant,
)
from rag_app.core.ports import LexicalStorePort

_QUESTION_SYNTAX = re.compile(
    r"请问|请列举|请列出|请介绍|请说明|告诉我|我想知道|"
    r"是什么|有哪些|哪几种|分别是|哪些|什么|是多少|怎么|如何|"
    r"需要承担|主要负责|负责什么|采用|包括|的|里|[？?]"
)


def question_search_terms(query: str) -> str:
    """仅分隔问句语法，保留对象、数字和否定供原检索与支持校验。

    Args:
        query: 保留业务对象及关系的原始问句。

    Returns:
        至多用于一次补充字面检索的对象词或分隔后的词组。

    """
    descriptive = descriptive_request(query)
    if descriptive is not None:
        target, relation, answer_type = descriptive
        return target if answer_type == "DUTIES" else f"{target} {relation}"
    return " ".join(_QUESTION_SYNTAX.sub(" ", query).split())


class LexicalChannel:
    """安全处理空 token、特殊字符和至多两个 query 变体。"""

    def __init__(self, store: LexicalStorePort) -> None:
        self._store = store

    def search(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        variant: QueryVariant,
        *,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """返回不携带正文的 FTS5 候选。

        Args:
            snapshot: 请求级 immutable Active Revision。
            variant: 原始或唯一 normalized 变体。
            limit: 最大候选数。

        Returns:
            绑定变体通道名的 FTS5 身份候选。

        """
        hits = self._store.search_candidates(
            LexicalSearchRequest(
                revision=snapshot.revision,
                query=variant.text,
                limit=limit,
            )
        )
        normalized_terms = question_search_terms(variant.text)
        used_question_terms = False
        if normalized_terms and normalized_terms != variant.text:
            supplemental = self._store.search_candidates(
                LexicalSearchRequest(
                    revision=snapshot.revision,
                    query=normalized_terms,
                    limit=limit,
                )
            )
            # 原问句仍实际检索；补充对象词命中避免英文标题独占候选窗口。
            merged = {hit.chunk_id: hit for hit in (*supplemental, *hits)}
            hits = tuple(
                hit.model_copy(update={"rank": rank})
                for rank, hit in enumerate(tuple(merged.values())[:limit], 1)
            )
            used_question_terms = True
        channel = (
            "lexical:fts5"
            if variant.kind == "original"
            else f"lexical:fts5:{variant.kind}"
        )
        if used_question_terms:
            channel += ":question_terms"
        return tuple(
            hit.model_copy(update={"channel": channel}) for hit in hits
        )


__all__ = ["LexicalChannel"]
