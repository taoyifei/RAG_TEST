import { expect, test } from "@playwright/test";

const traceId = "trace_11111111111111111111111111111111";

test("无登录公共问答完成流式回答、来源与反馈", async ({
  page,
}, testInfo) => {
  let feedbackCalls = 0;
  if (testInfo.project.name === "chromium-desktop") {
    await page.setViewportSize({ width: 1366, height: 768 });
  }
  await page.addInitScript(
    ({ publicTraceId }) => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const path = new URL(requestUrl, window.location.origin).pathname;
        if (path !== "/api/public/chat") return originalFetch(input, init);
        const frames = [
          [
            "meta",
            {
              type: "meta",
              trace_id: publicTraceId,
              sequence: 0,
            },
          ],
          [
            "stage",
            {
              type: "stage",
              trace_id: publicTraceId,
              sequence: 1,
              stage: "retrieval",
            },
          ],
          [
            "claim",
            {
              type: "claim",
              trace_id: publicTraceId,
              sequence: 2,
              claim_index: 0,
              claim: { text: "材料应在五个工作日内核验。" },
              provisional: true,
            },
          ],
          [
            "final",
            {
              type: "final",
              trace_id: publicTraceId,
              sequence: 3,
              status: "ANSWERED",
              answer: "办理材料应在五个工作日内完成核验。",
              citations: [
                {
                  document_name: "湾事通办事指南",
                  department: "政务服务部",
                  department_name: "政务服务部",
                  category_path: ["办事服务", "材料办理"],
                  document_title: "湾事通办事指南",
                  source_relative_path:
                    "政务服务部/办事服务/湾事通办事指南.docx",
                  locator: "申请指南 > 材料核验",
                  quote: "五个工作日内完成核验。",
                },
              ],
            },
          ],
        ] as const;
        const encoder = new TextEncoder();
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            frames.forEach(([name, payload], index) => {
              window.setTimeout(
                () => {
                  controller.enqueue(
                    encoder.encode(
                      `event: ${name}\ndata: ${JSON.stringify({
                        protocol: "wanshitong-public-sse-v1",
                        ...payload,
                      })}\n\n`,
                    ),
                  );
                  if (index === frames.length - 1) controller.close();
                },
                700 * (index + 1),
              );
            });
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      };
    },
    { publicTraceId: traceId },
  );
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
        trace_mode: "SAFE",
        history_mode: "full",
        document_visibility: "all_internal",
        conversation_delete: true,
        feedback: true,
        shortcuts: [],
      },
    });
  });
  await page.route("**/api/public/feedback", async (route) => {
    feedbackCalls += 1;
    const body = route.request().postDataJSON() as {
      trace_id: string;
      useful: boolean;
    };
    expect(body).toEqual({ trace_id: traceId, useful: true });
    await route.fulfill({ json: { useful: true } });
  });
  await page.route("**/api/v1/console/session", async (route) => {
    await route.fulfill({
      status: 401,
      json: { error: { code: "AUTHENTICATION_REQUIRED" } },
    });
  });

  await page.goto("/");
  await expect(page).toHaveTitle("湾事通");
  await expect(
    page.getByRole("heading", { name: "你的内部知识助手" }),
  ).toBeVisible();
  await expect(
    page.getByText("从已核验的内部资料中寻找答案，并把来源交代清楚。"),
  ).toHaveCount(0);
  await expect(page.getByText("管理员登录")).toHaveCount(0);
  await expect(page.getByText("制度政策")).toHaveCount(0);
  await expect(page.getByText("部门筛选")).toHaveCount(0);
  await expect(page.getByText("当前 Demo 仅支持 DOCX")).toHaveCount(0);

  const composer = page.getByRole("textbox", { name: "向湾事通提问" });
  await composer.fill("材料多久完成核验？");
  await composer.press("Enter");
  await expect(page.getByText("材料多久完成核验？")).toBeVisible();
  const answer = page.getByLabel("湾事通回答");
  await expect(answer.getByText("正在检索内部资料")).toBeVisible();
  await expect(answer.getByText("材料应在五个工作日内核验。")).toBeVisible();
  await expect(
    answer.getByText("暂非最终答案", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("办理材料应在五个工作日内完成核验。"),
  ).toBeVisible();
  await expect(page.getByText("材料应在五个工作日内核验。")).toHaveCount(0);

  await page.getByText("引用依据（1段）").click();
  await expect(page.getByText("湾事通办事指南", { exact: true })).toBeVisible();
  const citationGroupSummary = page.locator(".wst-citation-group > summary");
  await citationGroupSummary.press("Enter");
  await expect(citationGroupSummary).toBeFocused();
  await expect(page.getByText("片段 1")).toBeVisible();
  await expect(
    page.getByText("政务服务部 · 办事服务 / 材料办理"),
  ).toBeVisible();
  await expect(
    page.getByText("政务服务部/办事服务/湾事通办事指南.docx"),
  ).toBeVisible();
  await page.getByRole("button", { name: "有帮助" }).click();
  await expect(page.getByText("感谢你的反馈")).toBeVisible();
  expect(feedbackCalls).toBe(1);

  const noHorizontalScroll = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  expect(noHorizontalScroll).toBe(true);

  await expect(page.getByRole("button", { name: "新建会话" })).toHaveCount(0);
  await expect(page.getByText("材料多久完成核验？")).toBeVisible();
  if (testInfo.project.name === "chromium-desktop") {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expect(
      page.getByRole("textbox", { name: "向湾事通提问" }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }

  await page.goto("/admin");
  await expect(page).toHaveTitle("湾事通");
  await expect(page.getByLabel("管理口令")).toBeVisible();
  await expect(page.getByText("当前 Demo 仅支持 DOCX")).toHaveCount(0);
});
