import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AppShell from "./AppShell";
import { ConsoleProvider } from "../state/console-context";

const now = "2026-09-14T00:00:00Z";

function jsonResponse(value: object): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function setScope(projectId: string, kbId: string, revisionId: string) {
  sessionStorage.setItem(
    "rag.console.scope",
    JSON.stringify({ projectId, kbId, revisionId }),
  );
  window.history.replaceState(
    {},
    "",
    `/knowledge-bases?project=${projectId}&knowledgeBase=${kbId}&revision=${revisionId}`,
  );
}

function activeProject(projectId: string) {
  return {
    project_id: projectId,
    name: "活动项目",
    status: "active",
    created_at: now,
    updated_at: now,
  };
}

function mockShell(projectIds: string[], knowledgeBaseIds: string[]) {
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const path = requestPath(input);
    if (path.endsWith("/api/v1/console/session")) {
      return Promise.resolve(
        jsonResponse({ authenticated: true, csrf_token: "csrf" }),
      );
    }
    if (path.includes("/knowledge-bases?")) {
      return Promise.resolve(
        jsonResponse({
          items: knowledgeBaseIds.map((knowledgeBaseId) => ({
            project_id: "prj_current",
            knowledge_base_id: knowledgeBaseId,
            name: "活动知识库",
            description: "",
            profile_id: "default",
            status: "active",
            active_index_revision_id: "irev_active",
            created_at: now,
            updated_at: now,
          })),
          offset: 0,
          page_size: 50,
        }),
      );
    }
    if (path.includes("/api/v1/projects")) {
      return Promise.resolve(
        jsonResponse({
          items: projectIds.map(activeProject),
          offset: 0,
          page_size: 50,
        }),
      );
    }
    if (path.endsWith("/api/v1/system/components")) {
      return Promise.resolve(jsonResponse({ profile_id: "product-runtime" }));
    }
    throw new Error(`未处理的测试请求：${path}`);
  });
}

afterEach(() => {
  sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

describe("失效生命周期 scope 恢复", () => {
  it("旧 URL 指向删除中知识库时清空知识库和索引 scope", async () => {
    setScope("prj_current", "kb_deleted", "irev_deleted");
    mockShell(["prj_current"], []);

    render(
      <ConsoleProvider>
        <AppShell />
      </ConsoleProvider>,
    );

    await waitFor(() =>
      expect(
        JSON.parse(sessionStorage.getItem("rag.console.scope") ?? "{}"),
      ).toEqual({
        projectId: "prj_current",
        kbId: "",
        revisionId: "",
      }),
    );
    expect(window.location.search).toBe("?project=prj_current");
  });

  it("旧 URL 指向归档项目时清空完整 scope", async () => {
    setScope("prj_archived", "kb_current", "irev_current");
    mockShell([], []);

    render(
      <ConsoleProvider>
        <AppShell />
      </ConsoleProvider>,
    );

    await waitFor(() =>
      expect(
        JSON.parse(sessionStorage.getItem("rag.console.scope") ?? "{}"),
      ).toEqual({ projectId: "", kbId: "", revisionId: "" }),
    );
    expect(window.location.search).toBe("");
  });
});
