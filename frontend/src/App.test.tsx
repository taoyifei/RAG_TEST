import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import * as authNavigation from "./public/authNavigation";

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

  it("湾事通管理员路径只提供统一管理后台入口", async () => {
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
      throw new Error(`unexpected fetch: ${path}`);
    });
    render(<App />);

    expect(await screen.findByRole("link", { name: "进入湾事通管理后台" })).toHaveAttribute("href", "/admin/overview");
    expect(screen.queryByText("企业知识助手")).not.toBeInTheDocument();
    expect(screen.queryByText("管理员控制台")).not.toBeInTheDocument();
    expect(document.title).toBe("湾事通");
  });

  it("kb 前缀下旧 React 管理路径指向唯一 Vue 后台且不跳 RDMS SSO", async () => {
    window.history.replaceState({}, "", "/kb/admin");
    const ssoRedirect = vi.spyOn(authNavigation, "redirectToSso");
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = requestPath(input);
      if (path === "/kb/api/public/capabilities") {
        return Promise.resolve(
          response({ error: { code: "AUTHENTICATION_REQUIRED" } }, 401),
        );
      }
      throw new Error(`unexpected fetch: ${path}`);
    });

    render(<App />);

    expect(await screen.findByRole("link", { name: "进入湾事通管理后台" })).toHaveAttribute("href", "/kb/admin/overview");
    expect(ssoRedirect).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/kb/admin");
  });
});
