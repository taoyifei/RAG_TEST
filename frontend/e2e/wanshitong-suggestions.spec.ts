import { expect, test } from "@playwright/test";

import { SUGGESTED_QUESTIONS } from "../src/public/suggestedQuestions";

test("推荐首屏保持三短一标准一复合且双击只提交原题一次", async ({ page }) => {
  const chatBodies: Record<string, unknown>[] = [];
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
  const items = texts.map((question) =>
    SUGGESTED_QUESTIONS.find((item) => item.question === question),
  );
  expect(items.every((item) => item !== undefined)).toBe(true);
  expect(items.filter((item) => item?.style === "SHORT")).toHaveLength(3);
  expect(items.filter((item) => item?.style === "STANDARD")).toHaveLength(1);
  expect(items.filter((item) => item?.style === "COMPOUND")).toHaveLength(1);

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
