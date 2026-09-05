import type { ProviderConnection } from "../src/api/client";
import { expect, test } from "@playwright/test";

test("R3 连接编辑保留密钥、并发保护和键盘导航", async ({
  page,
  request,
}, testInfo) => {
  await page.goto("/");
  await page.getByLabel("管理口令").fill("offline-bootstrap-credential");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  const session = (await (
    await request.post("/api/v1/console/session", {
      headers: { Origin: "http://127.0.0.1:8091" },
      data: { bootstrap_token: "offline-bootstrap-credential" },
    })
  ).json()) as { csrf_token: string };
  const headers = {
    "X-CSRF-Token": session.csrf_token,
    Origin: "http://127.0.0.1:8091",
  };
  const suffix = testInfo.project.name;
  const host =
    "https://synthetic-long-business-workspace-r3.cn-beijing.maas.aliyuncs.com";
  const seed = async (name: string) => {
    const response = await request.post("/api/v1/provider-connections", {
      headers,
      data: {
        display_name: name,
        provider_type: "aliyun-model-studio",
        credential: {
          provider_type: "aliyun-model-studio",
          source: "database_encrypted",
          secret_value: "synthetic-r3-browser-value",
        },
        endpoint_mode: "workspace_host",
        api_host: host,
        workspace_id: "ws-synthetic-r3",
        region: "cn-beijing",
      },
    });
    expect(response.ok()).toBeTruthy();
    return response.json() as Promise<ProviderConnection>;
  };
  const first = await seed(`合成连接甲 ${suffix}`);
  const second = await seed(`合成连接乙 ${suffix}`);
  await page.goto("/model-services");
  const row = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: first.display_name, exact: true }),
  });
  await expect(row).toBeVisible();
  expect(await page.getByRole("dialog").count()).toBe(0);
  await row.getByRole("button", { name: "编辑连接" }).click();
  const drawer = page.getByRole("dialog", { name: "编辑百炼连接" });
  await expect(drawer.getByText(/保持现有密钥/)).toBeVisible();
  expect(await drawer.locator('input[type="password"]').count()).toBe(0);
  await drawer.getByRole("button", { name: "关闭", exact: true }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(
    drawer.getByRole("button", { name: "更换密钥", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    drawer.getByRole("button", { name: "关闭", exact: true }),
  ).toBeFocused();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(page.viewportSize()!.width);
  const otherWindow = await request.patch(
    `/api/v1/provider-connections/${first.connection_id}`,
    {
      headers,
      data: {
        expected_version: 1,
        display_name: first.display_name + " 新版本",
      },
    },
  );
  expect(otherWindow.ok(), await otherWindow.text()).toBeTruthy();
  await drawer.getByLabel("工作空间标识").fill("llm-synthetic-new");
  const conflict = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith(first.connection_id),
  );
  await drawer.getByRole("button", { name: "保存修改" }).click();
  expect((await conflict).status()).toBe(409);
  await expect(drawer.getByText(/另一窗口已修改此连接/)).toBeVisible();
  await expect(drawer.getByLabel("工作空间标识")).toHaveValue(
    "llm-synthetic-new",
  );
  const cancellationRequests: string[] = [];
  const recordCancellation = (request: { url: () => string }) =>
    cancellationRequests.push(request.url());
  page.on("request", recordCancellation);
  await drawer.getByRole("button", { name: "取消", exact: true }).click();
  await expect(drawer).toBeHidden();
  page.off("request", recordCancellation);
  expect(cancellationRequests).toEqual([]);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  const freshRow = page
    .getByRole("article")
    .filter({ hasText: first.display_name + " 新版本" });
  await freshRow.getByRole("button", { name: "编辑连接" }).click();
  await drawer.getByLabel("工作空间标识").fill("llm-synthetic-new");
  let probes = 0;
  page.on("request", (request) => {
    if (request.url().endsWith(":validate")) probes++;
  });
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith(first.connection_id),
  );
  await drawer.getByRole("button", { name: "保存修改" }).click();
  const savedResponse = await saved;
  expect(savedResponse.ok()).toBeTruthy();
  const payload = (await savedResponse.json()) as ProviderConnection;
  expect(payload.connection_id).toBe(first.connection_id);
  expect(payload.credential_id).toBe(first.credential_id);
  expect(payload.workspace_id).toBe("llm-synthetic-new");
  expect(payload.configuration_version).toBe(3);
  await expect(drawer).toBeHidden();
  expect(probes).toBe(0);
  const connections = (await (
    await request.get("/api/v1/provider-connections")
  ).json()) as { items: ProviderConnection[] };
  expect(
    connections.items.find(
      (item: { connection_id: string }) =>
        item.connection_id === second.connection_id,
    )?.configuration_version,
  ).toBe(1);
  if (page.viewportSize()!.width === 375) {
    await page.getByRole("button", { name: "打开导航" }).click();
    await page.getByRole("button", { name: "知识库", exact: true }).click();
    expect(new URL(page.url()).pathname).toBe("/knowledge-bases");
    await expect(
      page.getByRole("heading", { name: "请先选择项目" }),
    ).toBeVisible();
  }
});
