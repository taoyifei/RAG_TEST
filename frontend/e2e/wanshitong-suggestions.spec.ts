import { expect, test } from "@playwright/test";

test("推荐首屏只展示审核题目，双击只提交原题一次", async ({ page }) => {
  const chatBodies: Record<string, unknown>[] = [];
  const reviewed = Array.from({ length: 8 }, (_, index) => ({
    id: `pq_${String(index).padStart(32, "0")}`,
    question: `审核通过的问题 ${index + 1}？`,
    topic_key: "制度",
  }));
  await page.route("**/api/public/session", async (route) => {
    await route.fulfill({
      json: {
        session_id: "wstsid_11111111111111111111111111111111",
        csrf_token: "a".repeat(64),
        expires_in: 3600,
      },
    });
  });
  await page.route("**/api/public/capabilities", async (route) => {
    await route.fulfill({
      json: {
        mode: "wanshitong",
        stream: true,
        stream_protocol: "wanshitong-public-sse-v1",
        document_visibility: "all_internal",
        feedback: true,
        feedback_details: true,
        shortcuts: [],
      },
    });
  });
  await page.route("**/api/public/popular-questions", async (route) => {
    await route.fulfill({
      json: {
        mode: "POPULAR",
        generated_at: "2026-09-25T00:00:00Z",
        window_days: 7,
        items: reviewed,
      },
    });
  });
  await page.route("**/api/public/chat", async (route) => {
    chatBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      body: `event: final\ndata: ${JSON.stringify({
        protocol: "wanshitong-public-sse-v1",
        type: "final",
        trace_id: "trace_11111111111111111111111111111111",
        sequence: 0,
        answer: "已完成回答。",
        citations: [],
      })}\n\n`,
      contentType: "text/event-stream",
      status: 200,
    });
  });

  await page.goto("/");
  const suggestions = page.getByRole("region", { name: "你可能想问" });
  const cards = suggestions.locator("button.wst-suggestion");
  await expect(cards).toHaveCount(5);
  const texts = await cards.allTextContents();
  expect(
    texts.every((question) =>
      reviewed.some((item) => item.question === question),
    ),
  ).toBe(true);
  await expect(page.getByText("未审核的问题？")).toHaveCount(0);

  const question = texts[0];
  await cards.first().evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(page.getByText("已完成回答。")).toBeVisible();
  await expect(
    page.locator(".wst-question", { hasText: question }),
  ).toHaveCount(1);
  expect(chatBodies).toHaveLength(1);
  expect(Object.keys(chatBodies[0]).sort()).toEqual([
    "conversation_id",
    "query",
  ]);
  expect(chatBodies[0].query).toBe(question);
});

test("没有审核题目时保持推荐区为空", async ({ page }) => {
  await page.route("**/api/public/session", async (route) => {
    await route.fulfill({
      json: {
        session_id: "wstsid_11111111111111111111111111111111",
        csrf_token: "a".repeat(64),
        expires_in: 3600,
      },
    });
  });
  await page.route("**/api/public/capabilities", async (route) => {
    await route.fulfill({
      json: {
        mode: "wanshitong",
        stream: true,
        stream_protocol: "wanshitong-public-sse-v1",
        document_visibility: "all_internal",
        feedback: true,
        shortcuts: [],
      },
    });
  });
  await page.route("**/api/public/popular-questions", async (route) => {
    await route.fulfill({
      json: { mode: "EMPTY", generated_at: null, window_days: 7, items: [] },
    });
  });

  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "你的内部知识助手" }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "你可能想问" })).toHaveCount(0);
  await expect(page.getByText("当前展示示例问题")).toHaveCount(0);
});
