import { expect, test, type Page } from "@playwright/test";

const adminNavigation = [
  "概览",
  "文档管理",
  "处理任务",
  "问答历史",
  "Operational Trace",
  "模型与服务状态",
  "系统状态",
] as const;

async function openAdminNavigation(page: Page) {
  const navigation = page.getByRole("navigation", { name: "管理员导航" });
  const toggle = page.getByRole("button", { name: "打开导航" });
  if (
    (await toggle.isVisible()) &&
    (await toggle.getAttribute("aria-expanded")) !== "true"
  ) {
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  }
  return navigation;
}

async function navigate(page: Page, label: (typeof adminNavigation)[number]) {
  const navigation = await openAdminNavigation(page);
  await navigation.getByRole("button", { name: label, exact: true }).click();
}

test("湾事通管理员控制台固定 Scope、DOCX-only 与只读状态页", async ({
  page,
}) => {
  await page.goto("/admin");
  await expect(
    page.getByRole("dialog", { name: "连接管理控制台" }),
  ).toBeVisible();
  await page.getByLabel("管理口令").fill("offline-bootstrap-credential");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(
    page.getByRole("dialog", { name: "连接管理控制台" }),
  ).toBeHidden();

  const navigation = await openAdminNavigation(page);
  await expect(page.getByText("湾事通知识库", { exact: true })).toBeVisible();
  await expect(navigation.getByRole("button")).toHaveCount(7);
  for (const label of adminNavigation) {
    await expect(
      navigation.getByRole("button", { name: label, exact: true }),
    ).toBeVisible();
  }
  await expect(page.getByText("当前空间", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "管理项目" })).toHaveCount(0);

  await navigate(page, "文档管理");
  await expect(page).toHaveURL(/\/admin\/documents/);
  await expect(
    page.getByText(
      "当前湾事通 Demo 仅开放 DOCX 文档。PDF、旧 DOC、Excel 和 ZIP 将在后续版本接入。",
    ),
  ).toBeVisible();
  await expect(page.getByTestId("wst-document-files")).toHaveAttribute(
    "accept",
    ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  );
  await expect(page.getByTestId("wst-document-files")).toHaveAttribute(
    "multiple",
    "",
  );
  await expect(page.getByTestId("wst-document-directory")).toHaveAttribute(
    "webkitdirectory",
    "",
  );
  await expect(
    page.getByText("最终业务资料将在 WB-07 阶段统一导入。"),
  ).toBeVisible();

  await navigate(page, "处理任务");
  await expect(page).toHaveURL(/\/admin\/jobs/);
  await expect(page.getByRole("heading", { name: "暂无处理任务" })).toBeVisible();

  await navigate(page, "问答历史");
  await expect(page).toHaveURL(/\/admin\/history/);
  await expect(
    page.getByRole("button", { name: "清理湾事通知识库历史" }),
  ).toBeVisible();
  await expect(page.getByLabel("知识库")).toHaveCount(0);

  await navigate(page, "Operational Trace");
  await expect(page).toHaveURL(/\/admin\/operational-traces/);
  await expect(page.getByText("正在读取 Trace…")).toBeHidden();
  await expect(page.getByRole("heading", { name: "没有匹配的 Trace" })).toBeVisible();

  await navigate(page, "模型与服务状态");
  await expect(page).toHaveURL(/\/admin\/models/);
  await expect(page.getByText(/模型配置已锁定/)).toBeVisible();
  for (const label of ["新增", "编辑", "删除", "激活", "验证"] as const) {
    await expect(page.getByRole("button", { name: new RegExp(label) })).toHaveCount(0);
  }

  await navigate(page, "系统状态");
  await expect(page).toHaveURL(/\/admin\/system/);
  await expect(page.getByText("QueryExecutor capacity")).toBeVisible();
  await expect(page.getByText("公共 Session")).toBeVisible();

  await page.goto("/model-services");
  await expect(page).toHaveURL(/\/admin\/models/);
  await expect(page.getByText(/模型配置已锁定/)).toBeVisible();

  await page.getByRole("button", { name: "退出" }).click();
  await expect(
    page.getByRole("dialog", { name: "连接管理控制台" }),
  ).toBeVisible();
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "你的内部知识助手" }),
  ).toBeVisible();
});
