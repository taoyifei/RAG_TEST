import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { WanshitongOpsApp } from "./WanshitongOpsApp";

afterEach(() => vi.unstubAllGlobals());

const trace = {
  trace_id: "trace_" + "a".repeat(32),
  created_at: "2026-09-26T01:00:00+00:00",
  status: "completed",
  asker_id: "1001",
  asker_name: "张三",
  question: "开发中心是干嘛的？",
  feedback_useful: 0,
  review_status: null,
};

it("管理员口令进入运营页后可看用户问题、高频榜、原生入口和 Trace", async () => {
  let recommendation: Record<string, unknown> | undefined;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    await Promise.resolve();
    const target =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const path = new URL(target, "http://localhost").pathname;
    if (path === "/api/v1/console/session" && init?.method === "POST") {
      return Response.json({ csrf_token: "csrf-1" });
    }
    if (path === "/api/v1/console/session") return new Response(null, { status: 401 });
    if (path === "/api/admin/ops/summary") {
      return Response.json({ turns: 2, users: 2, completed: 1, failed: 0, negative_feedback: 1 });
    }
    if (path === "/api/admin/ops/migration") return Response.json({ items: [{}] });
    if (path === "/api/admin/ops/questions") {
      return Response.json({ items: [{ question: trace.question, count: 2, user_count: 2, negative_feedback: 1, last_asked_at: trace.created_at }] });
    }
    if (path === "/api/admin/ops/recommendations") {
      if (init?.method === "POST") {
        recommendation = {
          ...(JSON.parse(String(init.body)) as Record<string, unknown>),
          recommendation_id: "pq_" + "b".repeat(32),
          version: 1,
          updated_at: trace.created_at,
        };
        return Response.json(recommendation);
      }
      return Response.json({ items: recommendation ? [recommendation] : [] });
    }
    if (path === "/api/admin/ops/traces") {
      return Response.json({ items: [trace], total: 1 });
    }
    if (path === `/api/admin/ops/traces/${trace.trace_id}`) {
      return Response.json({
        bridge_trace_id: trace.trace_id,
        native_session_id: "native-session",
        native_message_id: "native-message",
        native_request_id: "native-request",
        status: "completed",
        asker_id: "1001",
        asker_name: "张三",
        question: trace.question,
        answer: "原生回答正文",
        events: [{ sequence: 1, event_type: "answer_delta", created_at: trace.created_at }],
        references: [{ reference_id: "ref-1" }],
        feedback: { useful: false, reason_detail: "不准确" },
        review: null,
      });
    }
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(<WanshitongOpsApp />);

  expect(await screen.findByRole("heading", { name: "管理员登录" })).toBeVisible();
  await user.type(screen.getByLabelText("管理员口令"), "test-token");
  await user.click(screen.getByRole("button", { name: "登录" }));
  expect(await screen.findByRole("heading", { name: "能力入口" })).toBeVisible();
  expect(screen.getByRole("link", { name: "选择和配置模型" })).toHaveAttribute(
    "href",
    "/admin/platform/settings?section=models",
  );
  expect(screen.getByRole("link", { name: "管理知识库、文件与分块" })).toBeVisible();

  await user.click(screen.getByRole("button", { name: "问题运营榜" }));
  expect(await screen.findByRole("cell", { name: "开发中心是干嘛的？" })).toBeVisible();
  expect(screen.getAllByRole("cell", { name: "2" })).toHaveLength(2);
  await user.click(screen.getByRole("button", { name: "建推荐草稿" }));
  expect(screen.getByLabelText("可公开题面")).toHaveValue(trace.question);
  await user.type(screen.getByLabelText("主题"), "研发");
  await user.click(screen.getByRole("button", { name: "保存草稿" }));
  expect(await screen.findByRole("button", { name: `${trace.question} · DRAFT · v1` })).toBeVisible();

  await user.click(screen.getByRole("button", { name: "问答历史" }));
  expect(await screen.findByRole("cell", { name: "张三（1001）" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "查看" }));
  expect(await screen.findByText("原生回答正文")).toBeVisible();
  expect(screen.getByText("native-session")).toBeVisible();
  expect(screen.getByRole("button", { name: "保存复核" })).toBeVisible();
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
});
