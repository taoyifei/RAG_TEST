import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleProvider } from "../../state/console-context";
import { WanshitongAdminShell } from "./WanshitongAdminShell";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

const session = {
  authenticated: true,
  session_id: "sess_wst_admin",
  csrf_token: "csrf_wst_admin",
  expires_in: 3600,
};

const scope = {
  mode: "wanshitong",
  ready: true,
  system_key: "wanshitong",
  project_id: "prj_fixed",
  project_name: "湾事通",
  knowledge_base_id: "kb_fixed",
  knowledge_base_name: "湾事通知识库",
  blocker_code: null,
  blocker_message: null,
};

function installReadyApi() {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const path = requestPath(input);
    calls.push(path);
    if (path === "/api/v1/console/session") return Promise.resolve(response(session));
    if (path === "/api/v1/admin/wanshitong/scope") {
      return Promise.resolve(response(scope));
    }
    if (path === "/api/v1/admin/wanshitong/overview") {
      return Promise.resolve(
        response({
          scope,
          documents: { total: 0, retrievable: 0 },
          jobs: {
            queued: 0,
            running: 0,
            failed_retryable: 0,
            failed_terminal: 0,
          },
          active_index_revision_id: null,
          providers: {
            embedding: { configured: false },
            reranker: { configured: false },
            llm: { configured: false },
          },
          public_query_ready: false,
          supported_formats: { docx: true },
          history_retention_days: 7,
          recent_history: [],
          recent_errors: [],
        }),
      );
    }
    if (path.startsWith("/api/v1/admin/wanshitong/history")) {
      return Promise.resolve(
        response({
          items: [],
          total: 0,
          total_is_exact: true,
          offset: 0,
          page_size: 20,
          body_enabled: true,
          retention_days: 7,
          storage: "sqlite",
          search_complete: true,
          next_cursor: null,
        }),
      );
    }
    if (path.startsWith("/api/v1/admin/wanshitong/operational-traces")) {
      return Promise.resolve(
        response({ items: [], page: 1, page_size: 30, total: 0 }),
      );
    }
    throw new Error(`unexpected fetch: ${path}`);
  });
  return calls;
}

afterEach(() => vi.restoreAllMocks());

describe("湾事通管理员壳", () => {
  it("未登录复用现有 Console Session 登录页", async () => {
    window.history.replaceState({}, "", "/admin");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ error: { code: "AUTHENTICATION_REQUIRED" } }, 401),
    );
    render(
      <ConsoleProvider productMode="wanshitong">
        <WanshitongAdminShell />
      </ConsoleProvider>,
    );

    expect(
      await screen.findByRole("dialog", { name: "连接管理控制台" }),
    ).toBeVisible();
    expect(screen.getByLabelText("管理口令")).toBeVisible();
    expect(screen.getByText("登录后自动检查并绑定湾事通固定知识范围。")).toBeVisible();
  });

  it("固定 Scope 就绪后只显示七项导航且没有空间选择器", async () => {
    window.history.replaceState({}, "", "/admin");
    installReadyApi();
    render(
      <ConsoleProvider productMode="wanshitong">
        <WanshitongAdminShell />
      </ConsoleProvider>,
    );

    const navigation = await screen.findByRole("navigation", {
      name: "管理员导航",
    });
    await waitFor(() => expect(screen.getByText("湾事通知识库")).toBeVisible());
    expect(within(navigation).getAllByRole("button")).toHaveLength(7);
    for (const label of [
      "概览",
      "文档管理",
      "处理任务",
      "问答历史",
      "Operational Trace",
      "模型与服务状态",
      "系统状态",
    ]) {
      expect(within(navigation).getByRole("button", { name: label })).toBeVisible();
    }
    expect(screen.queryByText("当前空间")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "管理项目" })).not.toBeInTheDocument();
    expect(screen.queryByText("Access Token")).not.toBeInTheDocument();
    expect(screen.queryByText("检索方案")).not.toBeInTheDocument();
  });

  it("固定 Scope 未就绪时显示阻断原因且不加载业务页面", async () => {
    window.history.replaceState({}, "", "/admin");
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      calls.push(path);
      if (path === "/api/v1/console/session") return Promise.resolve(response(session));
      if (path === "/api/v1/admin/wanshitong/scope") {
        return Promise.resolve(
          response({
            mode: "wanshitong",
            ready: false,
            project_id: null,
            project_name: null,
            knowledge_base_id: null,
            knowledge_base_name: null,
            blocker_code: "WANSHITONG_SCOPE_INVALID",
            blocker_message: "固定 Project 已被删除。",
          }),
        );
      }
      throw new Error(`unexpected fetch: ${path}`);
    });
    render(
      <ConsoleProvider productMode="wanshitong">
        <WanshitongAdminShell />
      </ConsoleProvider>,
    );

    expect(await screen.findByText("固定 Project 已被删除。")).toBeVisible();
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
    expect(calls).not.toContain("/api/v1/admin/wanshitong/overview");
    expect(screen.queryByText("固定知识范围概览")).not.toBeInTheDocument();
  });

  it("History 与 Trace 只调用 fixed-scope Facade", async () => {
    window.history.replaceState({}, "", "/admin");
    const calls = installReadyApi();
    const user = userEvent.setup();
    render(
      <ConsoleProvider productMode="wanshitong">
        <WanshitongAdminShell />
      </ConsoleProvider>,
    );
    const navigation = await screen.findByRole("navigation", {
      name: "管理员导航",
    });
    await waitFor(() => expect(screen.getByText("湾事通知识库")).toBeVisible());

    await user.click(within(navigation).getByRole("button", { name: "问答历史" }));
    await waitFor(() =>
      expect(calls.some((path) => path.startsWith("/api/v1/admin/wanshitong/history"))).toBe(true),
    );
    expect(screen.queryByLabelText("知识库")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "清理湾事通知识库历史" }),
    ).toBeVisible();

    await user.click(
      within(navigation).getByRole("button", { name: "Operational Trace" }),
    );
    await waitFor(() =>
      expect(
        calls.some((path) =>
          path.startsWith("/api/v1/admin/wanshitong/operational-traces"),
        ),
      ).toBe(true),
    );
    expect(screen.queryByRole("button", { name: /清理已到期/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出已选技术 Trace" })).toBeVisible();
    expect(screen.getByRole("button", { name: "前往问答历史批量下载" })).toBeVisible();
  });
});
