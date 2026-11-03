import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { WanshitongApp } from "./WanshitongApp";

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

it("保存后同一页面的侧栏、顶栏和搜索同步更新，无需手动刷新", async () => {
  let customTitle: string | null = null;
  const conversationId = "rename-history";
  window.sessionStorage.setItem(
    "wst-natural-conversation:rename_8289:325",
    conversationId,
  );
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input, init) => {
      const url = new URL(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
        "http://localhost",
      );
      const title = customTitle ?? "旧会话名称";
      if (url.pathname === "/api/public/session")
        return Promise.resolve(
          Response.json({
            session_id: "wstsid_11111111111111111111111111111111",
            csrf_token: "a".repeat(64),
            deployment_id: "rename_8289",
            user: { user_id: "325", display_name: "测试用户", role: "user" },
          }),
        );
      if (url.pathname === "/api/public/capabilities")
        return Promise.resolve(
          Response.json({
            stream_protocol: "wanshitong-weknora-sse-v1",
            natural_stream_protocol: "wanshitong-weknora-sse-v1",
            feedback: false,
            page_settings: { show_recommendations: false },
          }),
        );
      if (url.pathname === "/api/public/conversations") {
        const query = url.searchParams.get("query");
        const items =
          !query || title.includes(query)
            ? [
                {
                  conversation_id: conversationId,
                  title,
                  updated_at: "2026-11-03T00:00:00Z",
                },
              ]
            : [];
        return Promise.resolve(
          Response.json({ items, has_more: false, total: items.length }),
        );
      }
      if (
        url.pathname === `/api/public/conversations/${conversationId}/rename`
      ) {
        customTitle = (
          JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
            title: string;
          }
        ).title.trim();
        return Promise.resolve(
          Response.json({
            conversation_id: conversationId,
            title: customTitle,
          }),
        );
      }
      if (url.pathname.startsWith("/api/public/conversations/"))
        return Promise.resolve(
          Response.json({
            turns: [
              {
                turn_id: "turn-rename",
                trace_id: "trace-rename",
                question: "原问题",
                answer: "原回答",
                status: "completed",
                citations: [],
                created_at: "2026-11-03T00:00:00Z",
                search_scopes: ["public"],
              },
            ],
            title: customTitle,
          }),
        );
      return Promise.resolve(Response.json({}, { status: 404 }));
    });
  render(<WanshitongApp />);
  expect(
    await screen.findByRole("button", { name: "旧会话名称" }),
  ).toHaveAttribute("aria-current", "page");
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "会话操作：旧会话名称" }),
  );
  await user.click(screen.getByRole("button", { name: "重命名会话" }));
  const dialog = screen.getByRole("dialog", { name: "重命名会话" });
  const field = within(dialog).getByRole("textbox", { name: "会话名称" });
  await user.clear(field);
  await user.type(field, "我的委托单资料");
  await user.click(within(dialog).getByRole("button", { name: "保存" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("dialog", { name: "重命名会话" }),
    ).not.toBeInTheDocument(),
  );
  expect(
    screen.getByRole("button", { name: "我的委托单资料" }),
  ).toHaveAttribute("aria-current", "page");
  expect(
    screen.getByText("我的委托单资料", { selector: ".wst-app-view-label" }),
  ).toBeVisible();
  const search = screen.getByRole("searchbox", { name: "搜索历史会话" });
  await user.type(search, "旧会话名称");
  expect(
    await screen.findByText("没有匹配的会话，试试其他关键词。"),
  ).toBeVisible();
  expect(
    screen.getByText("我的委托单资料", { selector: ".wst-app-view-label" }),
  ).toBeVisible();
  await user.clear(search);
  await user.type(search, "我的委托单");
  expect(
    await screen.findByRole("button", { name: "我的委托单资料" }),
  ).toBeVisible();
  expect(
    fetchMock.mock.calls.some(
      ([input]) =>
        (typeof input === "string"
          ? input
          : input instanceof URL
            ? input.pathname
            : new URL(input.url).pathname) === "/api/public/chat",
    ),
  ).toBe(false);
});
