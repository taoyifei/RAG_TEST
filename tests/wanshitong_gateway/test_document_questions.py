"""指定文件问答、版本检索边界与来源标识的 HTTP 合同。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import wanshitong_gateway.app as gateway_app
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.document_catalogue import DocumentCatalogue
from wanshitong_gateway.engine_settings import EngineSettings
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import WeKnoraClient
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner


class _Auth:
    """只替换身份入口，文件授权仍调用真实 LibraryService。"""

    def __init__(self, database: Path) -> None:
        self.settings = SimpleNamespace(
            database_path=database, deployment_id="test", root_path="/kb"
        )

    def require_public(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del csrf
        return SimpleNamespace(user_id=request.headers.get("x-test-user", "u1"))

    def require_profile(
        self, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        del csrf
        user = request.headers.get("x-test-user", "u1")
        return UserProfile(
            user,
            user,
            "测试用户",
            None,
            "admin" if request.headers.get("x-test-admin") else "user",
            request.headers.get("x-test-super") == "true",
        )


class _Native:
    """保留真实 HTTP 客户端与资料接口，记录送入原生的限定参数。"""

    def __init__(self) -> None:
        self.chats: list[dict[str, Any]] = []
        self.citations: list[dict[str, str]] = []
        self.sessions = 0
        self.documents = {
            identity: {
                "id": identity,
                "knowledge_base_id": kb,
                "file_name": title,
                "parse_status": "completed",
                "enable_status": "enabled",
            }
            for identity, kb, title in (
                ("doc-public", "public", "公共制度.docx"),
                ("doc-other", "public", "另一份制度.docx"),
                ("doc-personal", "personal-u1", "我的资料.docx"),
                ("doc-foreign", "personal-u2", "他人的资料.docx"),
                ("doc-hr", "hr-secret", "人力内部.docx"),
            )
        }

    @staticmethod
    def ok(data: object) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": data})

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "test-only"}
            )
        if path == "/api/v1/sessions" and request.method == "POST":
            self.sessions += 1
            return self.ok({"id": f"native-{self.sessions}"})
        if path.startswith("/api/v1/knowledge-chat/"):
            body = json.loads(request.content)
            self.chats.append(body)
            count = 0 if body["query"] == "没有对应资料" else 1
            events = [
                {
                    "response_type": "agent_query",
                    "assistant_message_id": "answer-1",
                },
                {
                    "response_type": "tool_result",
                    "data": {
                        "tool_name": "knowledge_search",
                        "success": True,
                        "count": count,
                    },
                },
                {
                    "response_type": "answer",
                    "content": "测试回答",
                    "done": True,
                },
                {
                    "response_type": "references",
                    "knowledge_references": self.citations,
                },
                {"response_type": "complete", "done": True},
            ]
            data = "".join(
                "event: message\ndata: "
                + json.dumps({"id": "request-1", "content": "", **event})
                + "\n\n"
                for event in events
            )
            return httpx.Response(
                200, content=data, headers={"content-type": "text/event-stream"}
            )
        if path.startswith("/api/v1/messages/") and path.endswith("/load"):
            return self.ok(
                [
                    {
                        "id": "answer-1",
                        "content": "测试回答",
                        "created_at": "2026-11-04T00:00:00Z",
                        "is_completed": True,
                    }
                ]
            )
        if path.startswith("/api/v1/knowledge-bases/") and path.endswith(
            "/knowledge"
        ):
            kb = path.split("/")[4]
            return self.ok(
                [
                    item
                    for item in self.documents.values()
                    if item["knowledge_base_id"] == kb
                ]
            )
        if path.startswith("/api/v1/knowledge/"):
            identity = path.rsplit("/", 1)[1]
            if identity in self.documents:
                return self.ok(self.documents[identity])
        raise AssertionError(f"unexpected native route: {path}")


@pytest.fixture
def document_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, _Native, DocumentCatalogue]]:
    """两份公共资料、两个独立私库和人力内部区。"""
    database = tmp_path / "gateway.sqlite3"
    auth = _Auth(database)
    state = _Native()
    store = GatewayStore(database)
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda *_args, **_kwargs: None
    )
    native = WeKnoraClient(
        base_url="http://native.test",
        api_key="synthetic-key",
        signer=ExternalPrincipalSigner(
            secret="test-only-signing",  # noqa: S106
            tenant_id=9,
            deployment_id="test",
        ),
        transport=httpx.MockTransport(state.respond),
    )
    admin = NativeAdminClient(
        base_url="http://native.test",
        email="admin@example.test",
        password="synthetic-password",  # noqa: S106
        tenant_id=9,
        transport=httpx.MockTransport(state.respond),
    )
    app = gateway_app.create_app(
        auth=cast(GatewayAuth, auth),
        store=store,
        native=native,
        admin_native=admin,
        engine=EngineSettings(
            base_url="http://native.test",
            tenant_id=9,
            public_kb_ids=("public",),
            api_key_file=tmp_path / "unused",
            external_signing_key_file=tmp_path / "unused",
            admin_email="admin@example.test",
            admin_password_file=tmp_path / "unused",
        ),
    )
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO library_kbs(kb_id,scope,owner_id) VALUES (?,?,?)",
            [
                ("personal-u1", "personal", "u1"),
                ("personal-u2", "personal", "u2"),
                ("hr-secret", "hr_internal", ""),
            ],
        )
    with TestClient(app) as browser:
        yield browser, state, DocumentCatalogue(database)


def test_file_target_is_enforced_upstream_and_survives_history(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
) -> None:
    browser, state, _catalogue = document_chat
    base = "/kb/api/public/chat"
    assert (
        browser.post(
            base, json={"conversation_id": "public-chat", "query": "公共问题"}
        ).status_code
        == 200
    )
    assert set(state.chats[-1]["knowledge_ids"]) == {"doc-public", "doc-other"}
    assert "knowledge_base_ids" not in state.chats[-1]
    assert state.chats[-1]["knowledge_ids_use_kb_thresholds"] is True
    selected = {
        "conversation_id": "file-chat",
        "query": "我的资料有什么",
        "search_scopes": ["personal"],
        "knowledge_ids": ["doc-personal"],
    }
    assert browser.post(base, json=selected).status_code == 200
    assert state.chats[-1]["knowledge_ids"] == ["doc-personal"]
    assert (
        state.chats[-1].get("knowledge_ids_use_kb_thresholds", False) is False
    )
    assert "knowledge_base_ids" not in state.chats[-1]
    history = browser.get("/kb/api/public/conversations/file-chat").json()
    assert history["turns"][0]["knowledge_ids"] == ["doc-personal"]
    assert history["turns"][0]["target_documents"] == [
        {"id": "doc-personal", "title": "我的资料.docx"}
    ]
    assert (
        browser.post(
            base, json={"conversation_id": "file-chat", "query": "继续"}
        ).status_code
        == 200
    )
    assert state.chats[-1]["knowledge_ids"] == ["doc-personal"]
    assert "knowledge_base_ids" not in state.chats[-1]
    before = len(state.chats)
    assert (
        browser.post(base, json={**selected, "knowledge_ids": []}).status_code
        == 409
    )
    assert (
        browser.post(
            base,
            json={
                "conversation_id": "public-chat",
                "query": "改变已有上下文",
                "knowledge_ids": ["doc-public"],
            },
        ).status_code
        == 409
    )
    assert len(state.chats) == before
    state.documents["doc-personal"]["knowledge_base_id"] = "personal-u2"
    hidden = browser.get("/kb/api/public/conversations/file-chat")
    assert hidden.status_code == 403
    assert "测试回答" not in hidden.text


def test_template_lookup_clarifies_in_same_conversation_and_restores(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
) -> None:
    """确认当前可编辑原件和采购核对清单用途不同。"""
    browser, state, catalogue = document_chat
    state.documents["doc-public"]["file_name"] = (
        "附件2：关于XXXX项目采购需求的汇报-模板（2026年11月版）.pptx"
    )
    state.documents["doc-other"]["file_name"] = (
        "湾区研究院采购需求标准化清单.xlsx"
    )
    catalogue.annotate("doc-public", "public", kind="template")
    catalogue.annotate("doc-other", "public", kind="policy")

    def ask(query: str, conversation: str = "template-chat") -> dict[str, Any]:
        response = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": conversation, "query": query},
        )
        assert response.status_code == 200
        return json.loads(
            response.text.strip().split("\n\n")[-1].split("data: ", 1)[1]
        )

    first = ask("要走采购，能发我个表吗？")
    assert first["template_lookup"] == {"file": None, "topic": "采购"}
    assert "核对采购需求" in first["answer"]
    second = ask("就是拿去上会讲的那份，我要能改的。")
    file = second["template_lookup"]["file"]
    assert file["document_id"] == "doc-public"
    assert file["kind"] == "template" and file["format"] == "PPTX"
    assert first["native_session_id"] == second["native_session_id"]
    assert state.chats == []
    history = browser.get("/kb/api/public/conversations/template-chat").json()
    assert history["turns"][1]["template_lookup"]["file"] == file
    assert not history["turns"][1]["native_message_id"]

    checklist = ask("采购提交前，能给我一张核对的表吗？", "checklist")
    assert checklist["template_lookup"]["file"]["kind"] == "checklist"
    ordinary = ask("采购需求汇报模板应该包含哪些内容？", "business-qa")
    assert "template_lookup" not in ordinary and len(state.chats) == 1
    has_template = ask(
        "我拿到采购的汇报PPT了，上会主要要讲什么？", "template-in-use"
    )
    assert "template_lookup" not in has_template and len(state.chats) == 2

    catalogue.stage("doc-new", "public", "doc-public")
    catalogue.publish("doc-new", "public", "doc-public")
    history = browser.get("/kb/api/public/conversations/template-chat").json()
    assert history["turns"][1]["template_lookup"]["file"] is None
    assert "重新提问" in history["turns"][1]["answer"]


def test_template_lookup_respects_selected_documents_and_permissions(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
) -> None:
    """选中别的资料或只有个人区时，不发送范围外的模板。"""
    browser, state, catalogue = document_chat
    for identity in ("doc-public", "doc-foreign", "doc-hr"):
        item = state.documents[identity]
        item["file_name"] = "工程项目考核成绩汇总表填写模板.xlsx"
        catalogue.annotate(identity, item["knowledge_base_id"], kind="template")

    question = "工程那边要把考核的分数合到一起，能发个表吗？"
    for conversation, selection in (
        ("selected", {"knowledge_ids": ["doc-other"]}),
        ("personal", {"search_scopes": ["personal"]}),
    ):
        response = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": conversation,
                "query": question,
                **selection,
            },
        )
        assert (
            response.status_code == 200
            and "template_lookup" not in response.text
        )
    public = browser.post(
        "/kb/api/public/chat",
        json={"conversation_id": "all-public", "query": question},
    )
    assert (
        public.status_code == 200
        and '"document_id":"doc-public"' in public.text
    )
    assert all(
        identifier not in public.text
        for identifier in ("doc-foreign", "doc-hr")
    )


@pytest.mark.parametrize(
    ("scope", "document", "headers"),
    [
        ("personal", "doc-foreign", {}),
        (
            "personal",
            "doc-foreign",
            {"x-test-admin": "true", "x-test-super": "true"},
        ),
        ("hr_internal", "doc-hr", {"x-test-admin": "true"}),
        ("public", "doc-personal", {}),
    ],
)
def test_file_targets_never_bypass_owner_or_hr_access(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
    scope: str,
    document: str,
    headers: dict[str, str],
) -> None:
    browser, state, _catalogue = document_chat
    response = browser.post(
        "/kb/api/public/chat",
        json={
            "conversation_id": "denied",
            "query": "越权问题",
            "search_scopes": [scope],
            "knowledge_ids": [document],
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert state.chats == []
    assert "他人的资料" not in response.text


def test_staged_and_archived_versions_are_excluded_and_old_target_stops(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
) -> None:
    browser, state, catalogue = document_chat
    base = "/kb/api/public/chat"
    selected = {
        "conversation_id": "old-file",
        "query": "旧版本",
        "knowledge_ids": ["doc-public"],
    }
    assert browser.post(base, json=selected).status_code == 200
    catalogue.stage("doc-other", "public", "doc-public")
    assert (
        browser.post(
            base, json={"conversation_id": "stage-public", "query": "制度问题"}
        ).status_code
        == 200
    )
    assert state.chats[-1]["knowledge_ids"] == ["doc-public"]
    assert (
        browser.post(
            base,
            json={
                **selected,
                "conversation_id": "staged-file",
                "knowledge_ids": ["doc-other"],
            },
        ).status_code
        == 409
    )
    catalogue.publish("doc-other", "public", "doc-public")
    # 旧版退出新检索后，仍可在本人历史中查看当时的回答。
    assert (
        browser.get("/kb/api/public/conversations/old-file").status_code == 200
    )
    assert (
        browser.post(
            base, json={"conversation_id": "new-public", "query": "制度问题"}
        ).status_code
        == 200
    )
    assert state.chats[-1]["knowledge_ids"] == ["doc-other"]
    assert browser.post(base, json=selected).status_code == 409
    state.documents.pop("doc-other")
    assert (
        browser.post(
            base, json={"conversation_id": "no-fallback", "query": "不应全库"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    ("question", "origin"),
    [
        ("没有对应资料", "model_no_results"),
        ("有命中但未引用", "model_unreferenced"),
    ],
)
def test_model_origin_is_identical_in_stream_and_restored_history(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
    question: str,
    origin: str,
) -> None:
    browser, _state, _catalogue = document_chat
    response = browser.post(
        "/kb/api/public/chat",
        json={"conversation_id": "origin", "query": question},
    )
    assert response.status_code == 200
    frames = [
        json.loads(frame.split("data: ", maxsplit=1)[1])
        for frame in response.text.strip().split("\n\n")
    ]
    assert frames[-1]["type"] == "final"
    assert frames[-1]["answer_origin"] == origin
    history = browser.get("/kb/api/public/conversations/origin").json()
    assert history["turns"][0]["answer_origin"] == origin


def test_full_scope_old_citation_requires_new_chat_but_keeps_history(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
) -> None:
    browser, state, catalogue = document_chat
    catalogue.stage("doc-other", "public", "doc-public")
    state.citations = [
        {
            "id": "old-chunk",
            "knowledge_id": "doc-public",
            "knowledge_filename": "公共制度.docx",
            "content": "先前有效的制度内容",
        }
    ]
    body = {"conversation_id": "full-scope", "query": "当时的制度"}
    assert browser.post("/kb/api/public/chat", json=body).status_code == 200
    catalogue.publish("doc-other", "public", "doc-public")
    before = len(state.chats)
    response = browser.post(
        "/kb/api/public/chat", json={**body, "query": "现在应该怎么办"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "资料版本已更新，请新建对话。"
    assert len(state.chats) == before
    history = browser.get("/kb/api/public/conversations/full-scope")
    assert history.status_code == 200
    assert history.json()["turns"][0]["answer"] == "测试回答"
    assert (
        history.json()["turns"][0]["citations"][0]["native_knowledge_id"]
        == "doc-public"
    )
    state.citations = []
    assert (
        browser.post(
            "/kb/api/public/chat",
            json={**body, "conversation_id": "fresh-scope"},
        ).status_code
        == 200
    )
    assert state.chats[-1]["knowledge_ids"] == ["doc-other"]


@pytest.mark.parametrize("knowledge_id", [None, "doc-public"])
def test_unidentified_or_removed_citation_does_not_infer_archived_version(
    document_chat: tuple[TestClient, _Native, DocumentCatalogue],
    knowledge_id: str | None,
) -> None:
    browser, state, _catalogue = document_chat
    state.citations = [
        {
            "id": "historic-chunk",
            "content": "历史片段",
            **({"knowledge_id": knowledge_id} if knowledge_id else {}),
        }
    ]
    body = {"conversation_id": "no-archive", "query": "首轮"}
    assert browser.post("/kb/api/public/chat", json=body).status_code == 200
    state.documents.pop("doc-public")
    state.citations = []
    # 普通删除或无法定位资料的片段，不等同于被明确替换的旧版本。
    assert (
        browser.post(
            "/kb/api/public/chat", json={**body, "query": "继续"}
        ).status_code
        == 200
    )
