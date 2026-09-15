import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConsoleProvider, useConsole } from "./console-context";

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function Probe() {
  const { scope, setProject, setKnowledgeBase } = useConsole();
  return (
    <>
      <output>{`${scope.projectId}|${scope.kbId}|${scope.revisionId}`}</output>
      <button onClick={() => setProject("prj_a")}>项目 A</button>
      <button onClick={() => setKnowledgeBase("kb_a", "irev_a")}>
        知识库 A
      </button>
      <button onClick={() => setProject("prj_b")}>项目 B</button>
    </>
  );
}

function AuthProbe() {
  const { login, session } = useConsole();
  return (
    <>
      <output>{session.authenticated ? "已登录" : "未登录"}</output>
      <button onClick={() => void login("synthetic-bootstrap-value")}>
        登录
      </button>
    </>
  );
}

function FixedScopeProbe() {
  const { fixedScope, recheckFixedScope, scope } = useConsole();
  return (
    <>
      <output>
        {`${fixedScope.state}|${scope.projectId}|${scope.kbId}|${fixedScope.reason}`}
      </output>
      <button onClick={recheckFixedScope}>重新检查 Scope</button>
    </>
  );
}

describe("内存范围", () => {
  it("切换项目时清空知识库和 Revision，避免串库", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          authenticated: false,
          error: { code: "AUTHENTICATION_REQUIRED" },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );
    const user = userEvent.setup();
    render(
      <ConsoleProvider>
        <Probe />
      </ConsoleProvider>,
    );
    await user.click(screen.getByRole("button", { name: "项目 A" }));
    await user.click(screen.getByRole("button", { name: "知识库 A" }));
    expect(screen.getByRole("status")).toHaveTextContent("prj_a|kb_a|irev_a");
    await user.click(screen.getByRole("button", { name: "项目 B" }));
    expect(screen.getByRole("status")).toHaveTextContent("prj_b||");
  });

  it("延迟返回的会话恢复失败不覆盖已成功登录", async () => {
    let resolveResume: (response: Response) => void = () => undefined;
    const resume = new Promise<Response>((resolve) => {
      resolveResume = resolve;
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) => {
        if (init?.method === "POST") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                authenticated: true,
                session_id: "sess_test",
                csrf_token: "csrf_test",
                expires_in: 3600,
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return resume;
      });
    const user = userEvent.setup();
    render(
      <ConsoleProvider>
        <AuthProbe />
      </ConsoleProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(screen.getByRole("status")).toHaveTextContent("已登录");
    await act(async () => {
      resolveResume(
        new Response(
          JSON.stringify({ error: { code: "AUTHENTICATION_REQUIRED" } }),
          {
            status: 401,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
      await resume;
    });

    expect(screen.getByRole("status")).toHaveTextContent("已登录");
  });
});

describe("湾事通固定范围", () => {
  it("忽略 URL 与 sessionStorage，并可从阻断状态重新检查", async () => {
    window.history.replaceState(
      {},
      "",
      "/admin?project=prj_wrong&knowledgeBase=kb_wrong",
    );
    sessionStorage.setItem(
      "rag.console.scope",
      JSON.stringify({ projectId: "prj_stale", kbId: "kb_stale" }),
    );
    let scopeCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      if (path === "/api/v1/console/session") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              authenticated: true,
              session_id: "sess_wst",
              csrf_token: "csrf_wst",
              expires_in: 3600,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (path === "/api/v1/admin/wanshitong/scope") {
        scopeCalls += 1;
        return Promise.resolve(
          new Response(
            JSON.stringify(
              scopeCalls === 1
                ? {
                    mode: "wanshitong",
                    ready: false,
                    project_id: null,
                    project_name: null,
                    knowledge_base_id: null,
                    knowledge_base_name: null,
                    blocker_code: "WANSHITONG_SCOPE_INVALID",
                    blocker_message: "固定知识范围已损坏。",
                  }
                : {
                    mode: "wanshitong",
                    ready: true,
                    project_id: "prj_fixed",
                    project_name: "湾事通",
                    knowledge_base_id: "kb_fixed",
                    knowledge_base_name: "湾事通知识库",
                    blocker_code: null,
                    blocker_message: null,
                  },
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      throw new Error(`unexpected fetch: ${path}`);
    });
    const user = userEvent.setup();
    render(
      <ConsoleProvider productMode="wanshitong">
        <FixedScopeProbe />
      </ConsoleProvider>,
    );

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "blocked|||固定知识范围已损坏。",
      ),
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("wrong");
    expect(screen.getByRole("status")).not.toHaveTextContent("stale");

    await user.click(screen.getByRole("button", { name: "重新检查 Scope" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "ready|prj_fixed|kb_fixed|",
      ),
    );
    expect(scopeCalls).toBe(2);
  });
});
