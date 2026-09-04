import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ConsoleProvider } from "../../state/console-context";
import { QueryPage } from "./WorkspaceConsole";

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

it("诊断请求失败不会覆盖已经返回的查询结果", async () => {
  sessionStorage.setItem(
    "rag.console.scope",
    JSON.stringify({ projectId: "prj_1", kbId: "kb_1", revisionId: "irev_1" }),
  );
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const path = requestPath(input);
    if (path.endsWith("/api/v1/console/session")) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            error: {
              code: "AUTHENTICATION_REQUIRED",
              message: "请登录",
            },
          }),
          { status: 401, headers: { "Content-Type": "application/json" } },
        ),
      );
    }
    if (path.endsWith(":search")) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            trace_id: "trace_1",
            status: "ANSWERABLE",
            reason_code: "EVIDENCE_READY",
            route_reason_code: "LEXICAL_ONLY",
            selected_embedding_slot: null,
            evidence_count: 0,
            quality_profile_status: "offline",
            evidence: [],
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      );
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          error: { code: "DIAGNOSTICS_UNAVAILABLE", message: "诊断暂不可用" },
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
  });
  const user = userEvent.setup();
  render(
    <ConsoleProvider>
      <QueryPage mode="search" />
    </ConsoleProvider>,
  );
  await user.type(screen.getByLabelText("查询文本"), "青岛啤酒");
  await user.click(screen.getByRole("button", { name: "执行" }));

  expect(await screen.findByText("没有可发布证据")).toBeVisible();
  expect(await screen.findByText("诊断信息暂不可用")).toBeVisible();
});
