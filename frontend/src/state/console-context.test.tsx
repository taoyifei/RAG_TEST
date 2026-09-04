import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConsoleProvider, useConsole } from "./console-context";

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
