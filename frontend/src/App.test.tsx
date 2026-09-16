import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

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

afterEach(() => vi.restoreAllMocks());

describe("产品壳隔离", () => {
  it("Universal 默认根路径继续打开原控制台而不是公共湾事通", async () => {
    window.history.replaceState({}, "", "/");
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      if (path === "/api/public/capabilities") {
        return Promise.resolve(response({ error: { code: "NOT_FOUND" } }, 404));
      }
      if (path === "/api/v1/console/session") {
        return Promise.resolve(
          response({ error: { code: "AUTHENTICATION_REQUIRED" } }, 401),
        );
      }
      throw new Error(`unexpected fetch: ${path}`);
    });
    render(<App />);

    expect(await screen.findByText("企业知识助手")).toBeVisible();
    expect(
      await screen.findByRole("dialog", { name: "连接管理控制台" }),
    ).toBeVisible();
    expect(screen.queryByText("你的内部知识助手")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(document.title).toBe("Universal RAG 控制台");
  });

  it("湾事通管理员路径使用独立收敛壳和同一登录页", async () => {
    window.history.replaceState({}, "", "/admin");
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      if (path === "/api/public/capabilities") {
        return Promise.resolve(
          response({
            mode: "wanshitong",
            stream: true,
            stream_protocol: "wanshitong-public-sse-v1",
            document_visibility: "all_internal",
            feedback: true,
          }),
        );
      }
      if (path === "/api/v1/console/session") {
        return Promise.resolve(
          response({ error: { code: "AUTHENTICATION_REQUIRED" } }, 401),
        );
      }
      throw new Error(`unexpected fetch: ${path}`);
    });
    render(<App />);

    expect(await screen.findByText("管理员控制台")).toBeVisible();
    expect(
      await screen.findByRole("dialog", { name: "连接管理控制台" }),
    ).toBeVisible();
    expect(screen.queryByText("企业知识助手")).not.toBeInTheDocument();
    expect(document.title).toBe("湾事通");
  });
});
