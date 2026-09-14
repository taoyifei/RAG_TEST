import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleProvider } from "../state/console-context";
import { KnowledgeBasesPage } from "./KnowledgeBasesPage";
import { ProjectsPage } from "./ProjectsPage";

const now = "2026-09-14T00:00:00Z";

function jsonResponse(value: object, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function requestMethod(init?: RequestInit): string {
  return init?.method ?? "GET";
}

function setScope(projectId: string, kbId: string, revisionId: string) {
  sessionStorage.setItem(
    "rag.console.scope",
    JSON.stringify({ projectId, kbId, revisionId }),
  );
  window.history.replaceState({}, "", "/projects");
}

function project(projectId: string, name: string, status = "active") {
  return {
    project_id: projectId,
    name,
    status,
    created_at: now,
    updated_at: now,
  };
}

function knowledgeBase(kbId: string, name: string, status = "active") {
  return {
    project_id: "prj_current",
    knowledge_base_id: kbId,
    name,
    description: "",
    profile_id: "default",
    status,
    active_index_revision_id: "irev_current",
    created_at: now,
    updated_at: now,
  };
}

afterEach(() => {
  sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

describe("项目与知识库逻辑删除", () => {
  it("归档当前项目时防止重复提交并清空完整 scope", async () => {
    setScope("prj_current", "kb_current", "irev_current");
    let finishDelete: ((response: Response) => void) | undefined;
    const pendingDelete = new Promise<Response>((resolve) => {
      finishDelete = resolve;
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input, init) => {
        const path = requestPath(input);
        if (path.endsWith("/api/v1/console/session")) {
          return Promise.resolve(
            jsonResponse({ authenticated: true, csrf_token: "csrf" }),
          );
        }
        if (requestMethod(init) === "DELETE") return pendingDelete;
        return Promise.resolve(
          jsonResponse({
            items: [
              project("prj_current", "当前项目"),
              project("prj_other", "保留项目"),
              project("prj_archived", "已归档项目", "archived"),
            ],
            offset: 0,
            page_size: 50,
          }),
        );
      });
    const go = vi.fn();
    render(
      <ConsoleProvider>
        <ProjectsPage go={go} />
      </ConsoleProvider>,
    );
    const current = (await screen.findAllByRole("article")).find((item) =>
      item.textContent?.includes("当前项目"),
    );
    expect(current).toBeDefined();
    expect(screen.queryByText("已归档项目")).not.toBeInTheDocument();

    await userEvent
      .setup()
      .click(within(current!).getByRole("button", { name: "删除" }));
    expect(screen.getByText("将从当前 Demo 列表移除此项目。")).toBeVisible();
    expect(
      screen.getByText("本轮采用逻辑归档，不执行物理数据清理。"),
    ).toBeVisible();
    const confirm = screen.getByRole("button", { name: "确认归档项目" });
    fireEvent.click(confirm);
    expect(screen.getByRole("button", { name: "归档中…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "归档中…" }));
    expect(
      fetchMock.mock.calls.filter(
        ([, init]) => requestMethod(init) === "DELETE",
      ),
    ).toHaveLength(1);

    await act(async () => {
      finishDelete?.(
        jsonResponse(project("prj_current", "当前项目", "archived")),
      );
      await pendingDelete;
    });
    await waitFor(() =>
      expect(screen.queryByText("当前项目")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("保留项目")).toBeVisible();
    expect(
      JSON.parse(sessionStorage.getItem("rag.console.scope") ?? "{}"),
    ).toEqual({
      projectId: "",
      kbId: "",
      revisionId: "",
    });
    expect(go).toHaveBeenCalledWith("/projects");
  });

  it("删除当前知识库时保留项目并清空知识库与索引 scope", async () => {
    setScope("prj_current", "kb_current", "irev_current");
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = requestPath(input);
      if (path.endsWith("/api/v1/console/session")) {
        return Promise.resolve(
          jsonResponse({ authenticated: true, csrf_token: "csrf" }),
        );
      }
      if (requestMethod(init) === "DELETE") {
        return Promise.resolve(
          jsonResponse(knowledgeBase("kb_current", "当前知识库", "deleting")),
        );
      }
      return Promise.resolve(
        jsonResponse({
          items: [
            knowledgeBase("kb_current", "当前知识库"),
            knowledgeBase("kb_other", "保留知识库"),
            knowledgeBase("kb_deleting", "删除中的知识库", "deleting"),
          ],
          offset: 0,
          page_size: 50,
        }),
      );
    });
    const go = vi.fn();
    render(
      <ConsoleProvider>
        <KnowledgeBasesPage go={go} />
      </ConsoleProvider>,
    );
    const current = (await screen.findAllByRole("article")).find((item) =>
      item.textContent?.includes("当前知识库"),
    );
    expect(screen.queryByText("删除中的知识库")).not.toBeInTheDocument();

    await userEvent
      .setup()
      .click(within(current!).getByRole("button", { name: "删除" }));
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "确认删除知识库" }));

    await waitFor(() =>
      expect(screen.queryByText("当前知识库")).not.toBeInTheDocument(),
    );
    expect(
      JSON.parse(sessionStorage.getItem("rag.console.scope") ?? "{}"),
    ).toEqual({
      projectId: "prj_current",
      kbId: "",
      revisionId: "",
    });
    expect(go).toHaveBeenCalledWith("/knowledge-bases");
  });

  it("归档失败时保留条目并显示服务端错误", async () => {
    setScope("", "", "");
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = requestPath(input);
      if (path.endsWith("/api/v1/console/session")) {
        return Promise.resolve(
          jsonResponse({ authenticated: true, csrf_token: "csrf" }),
        );
      }
      if (requestMethod(init) === "DELETE") {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: "PROJECT_BUSY",
                message: "项目仍有运行任务，暂不可归档。",
                stage: "project.archive",
                retryable: true,
              },
            },
            409,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({
          items: [project("prj_busy", "忙碌项目")],
          offset: 0,
          page_size: 50,
        }),
      );
    });
    render(
      <ConsoleProvider>
        <ProjectsPage go={() => undefined} />
      </ConsoleProvider>,
    );
    const card = (await screen.findAllByRole("article"))[0];

    await userEvent
      .setup()
      .click(within(card).getByRole("button", { name: "删除" }));
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "确认归档项目" }));

    expect(
      await screen.findByText("项目仍有运行任务，暂不可归档。"),
    ).toBeVisible();
    expect(screen.getByText("忙碌项目")).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
  });
});
