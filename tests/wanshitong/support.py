"""湾事通公共 Facade 的离线测试句柄与结果构造器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    QueryKind,
    SearchAnswerResult,
)
from rag_app.wanshitong.public_api import PUBLIC_SESSION_PATH
from rag_app.wanshitong.public_session import (
    PUBLIC_SESSION_COOKIE,
    PublicSessionPrincipal,
    PublicSessionService,
)
from rag_app.wanshitong.scope_service import FixedScopeService
from tests.product_support import ProductHarness, build_product_harness


@dataclass(slots=True)
class PublicHarness:
    """共享唯一 Product Runtime 的管理员与匿名测试客户端。"""

    product: ProductHarness
    csrf: str
    session_id: str

    @property
    def app(self) -> FastAPI:
        """返回承载公共路由的同一个 Product App。"""
        return cast(FastAPI, self.product.client.app)

    @property
    def client(self) -> TestClient:
        """返回已经取得公共 Cookie 的主客户端。"""
        return self.product.client

    @property
    def headers(self) -> dict[str, str]:
        """返回当前页面内存 CSRF Header。"""
        return {"X-CSRF-Token": self.csrf}

    @property
    def sessions(self) -> PublicSessionService:
        """返回仅供白盒安全断言的公共 Session 服务。"""
        return cast(
            PublicSessionService,
            self.app.state.wanshitong_public_session_service,
        )

    @property
    def scope_service(self) -> FixedScopeService:
        """返回 WB-01 固定 Scope 服务。"""
        return cast(
            FixedScopeService, self.app.state.wanshitong_scope_service
        )

    def close(self) -> None:
        """关闭唯一测试客户端与 Product Runtime。"""
        self.product.close()


def build_public_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PublicHarness:
    """构建启用湾事通模式且不访问外网的真实 Product Runtime。"""
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    product = build_product_harness(tmp_path)
    response = product.client.post(PUBLIC_SESSION_PATH)
    response.raise_for_status()
    payload = response.json()
    return PublicHarness(
        product=product,
        csrf=str(payload["csrf_token"]),
        session_id=str(payload["session_id"]),
    )


def public_principal(
    harness: PublicHarness,
    client: TestClient,
    csrf: str,
) -> PublicSessionPrincipal:
    """从测试客户端 Cookie 取得仅供断言的内部匿名主体。"""
    cookie = client.cookies.get(PUBLIC_SESSION_COOKIE)
    if cookie is None:
        raise AssertionError("测试客户端缺少公共 Session Cookie。")
    return harness.sessions.authenticate(cookie, csrf)


def synthetic_answer(
    trace_id: str,
    *,
    include_evidence: bool = True,
) -> SearchAnswerResult:
    """构造已完成 Grounded 校验语义的有限权威结果。"""
    evidence = ()
    if include_evidence:
        evidence = (
            EvidenceItem(
                evidence_id="S1",
                chunk_id="chunk_" + "1" * 32,
                citation_text="办理材料应在五个工作日内完成核验。",
                source_label="申请指南 > 材料核验",
                display_name="湾事通办事指南.docx",
                metadata={
                    "department": "政务服务部",
                    "category_path": ["办事服务", "材料办理"],
                },
            ),
        )
    return SearchAnswerResult(
        trace_id=trace_id,
        status=ConfidenceStatus.ANSWERABLE,
        reason_code="ANSWER_SUPPORTED",
        answer="办理材料应在五个工作日内完成核验。",
        evidence=evidence,
        confidence=ConfidenceDecision(
            status=ConfidenceStatus.ANSWERABLE,
            score=1.0,
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        active_index_revision_id="irev_" + "2" * 32,
        index_fingerprint="sha256:" + "3" * 64,
        serving_fingerprint="sha256:" + "4" * 64,
        route_reason_code="LEXICAL_ONLY",
        rerank_execution_mode="bypass",
        generation_mode="llm",
        cache_key="sha256:" + "5" * 64,
    )


__all__ = [
    "PublicHarness",
    "build_public_harness",
    "public_principal",
    "synthetic_answer",
]
