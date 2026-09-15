import { expect, test, type Page } from "@playwright/test";
import { createServer } from "node:http";

type ObservedRequest = {
  path: string;
  payload: Record<string, unknown>;
  authorization?: string;
};

async function authenticate(page: Page) {
  await page.goto("/admin");
  await page.getByLabel("管理口令").fill("offline-bootstrap-credential");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
}

test("自定义模型页面向真实 loopback 发送三类兼容请求", async ({
  page,
}, testInfo) => {
  if (testInfo.project.name !== "chromium-desktop") test.skip();
  const observed: ObservedRequest[] = [];
  const server = createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk: string) => {
      body += chunk;
    });
    request.on("end", () => {
      const payload = JSON.parse(body) as Record<string, unknown>;
      observed.push({
        path: request.url ?? "",
        payload,
        authorization: request.headers.authorization,
      });
      let result: Record<string, unknown>;
      if (request.url === "/embeddings") {
        result = {
          data: [{ index: 0, embedding: [0.6, 0.8, 0] }],
        };
      } else if (request.url === "/rerank") {
        const texts = payload.texts as string[];
        result = {
          results: texts.map((_, index) => ({ index, score: 1 - index / 10 })),
        };
      } else if (request.url === "/chat/completions") {
        result = {
          choices: [
            {
              finish_reason: "stop",
              message: { content: "公开回环模型响应" },
            },
          ],
        };
      } else {
        response.writeHead(404, { "Content-Length": "0" });
        response.end();
        return;
      }
      const content = JSON.stringify(result);
      response.writeHead(200, {
        "Content-Length": Buffer.byteLength(content),
        "Content-Type": "application/json",
      });
      response.end(content);
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("自定义 Provider 测试服务未取得 loopback 端口");
  }

  try {
    await authenticate(page);
    await page.getByRole("button", { name: "模型服务" }).click();
    await page.getByRole("button", { name: "新增连接" }).click();
    await page.getByLabel("服务商").selectOption("openai-compatible");
    await page.getByLabel("连接名称").fill("浏览器自定义回环连接");
    await page
      .getByLabel("API Base URL")
      .fill(`http://127.0.0.1:${address.port}`);
    await page.getByRole("button", { name: "保存连接" }).click();

    const card = page.getByRole("article").filter({
      has: page.getByRole("heading", {
        name: "浏览器自定义回环连接",
        exact: true,
      }),
    });
    await expect(card).toContainText(`http://127.0.0.1:${address.port}`);
    await expect(card).toContainText("未配置（无鉴权）");

    const validate = async (
      operationLabel: string,
      model: string,
      dimension?: string,
    ) => {
      const capability = card.locator(".capability").filter({
        has: page.getByText(operationLabel, { exact: true }),
      });
      await capability.getByLabel(`${operationLabel}模型 ID`).fill(model);
      if (dimension !== undefined) {
        await capability.getByLabel("期望 Embedding 维度").fill(dimension);
      }
      await capability
        .getByRole("button", { name: `测试${operationLabel}` })
        .click();
      const dialog = page.getByRole("dialog", { name: "确认连接测试" });
      await dialog.getByRole("button", { name: "开始测试" }).click();
      await expect(capability.getByText("接口测试通过")).toBeVisible();
    };

    await validate("查询向量", "org/free-embedding:v2", "3");
    await validate("结果重排", "org/free-reranker:v4");
    await validate("回答生成", "org/free-chat:v7");

    expect(observed.map((item) => item.path)).toEqual([
      "/embeddings",
      "/rerank",
      "/chat/completions",
    ]);
    expect(observed.every((item) => item.authorization === undefined)).toBe(
      true,
    );
    expect(observed[0].payload).toEqual({
      encoding_format: "float",
      input: ["验收示例：审批完成后归档。"],
      model: "org/free-embedding:v2",
    });
    expect(observed[1].payload).toMatchObject({
      query: "验收示例：审批完成后归档。",
      truncate: false,
    });
    expect(observed[1].payload).toHaveProperty("texts");
    expect(observed[2].payload).toMatchObject({
      model: "org/free-chat:v7",
      stream: false,
      temperature: 0,
    });
    expect(observed[2].payload).not.toHaveProperty("response_format");
    expect(observed[2].payload).not.toHaveProperty("workspace_id");
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
