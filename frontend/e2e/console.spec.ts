import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { strToU8, zipSync } from "fflate";

const contentTypes = `<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>`;
const relationships = `<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdOfficeDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`;
const styles = `<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>`;

function docx(text: string): Buffer {
  return docxBlocks(`<w:p><w:r><w:t>${text}</w:t></w:r></w:p>`);
}

function docxBlocks(blocks: string): Buffer {
  const zipOptions = { mtime: new Date("1980-01-01T00:00:00Z") };
  const document = `<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>${blocks}<w:sectPr/></w:body>
</w:document>`;
  return Buffer.from(
    zipSync({
      "[Content_Types].xml": [strToU8(contentTypes), zipOptions],
      "_rels/.rels": [strToU8(relationships), zipOptions],
      "word/document.xml": [strToU8(document), zipOptions],
      "word/styles.xml": [strToU8(styles), zipOptions],
    }),
  );
}

async function authenticate(page: Page) {
  await page.goto("/");
  await page.getByLabel("管理口令").fill("offline-bootstrap-credential");
  await page.getByRole("button", { name: "进入工作台" }).click();
}

async function createScope(page: Page, suffix: string) {
  await page.getByRole("button", { name: "知识库", exact: true }).click();
  await page.getByLabel("项目名称").fill(`离线项目 ${suffix}`);
  await page.getByRole("button", { name: "创建" }).click();
  const projectCard = page.getByRole("article").filter({
    hasText: `离线项目 ${suffix}`,
  });
  await projectCard.getByRole("heading").waitFor();
  await projectCard.getByRole("button", { name: "进入" }).click();
  await page.getByLabel("知识库名称").fill(`中文知识库 ${suffix}`);
  await page.getByRole("button", { name: "创建" }).click();
  const knowledgeBaseCard = page.getByRole("article").filter({
    hasText: `中文知识库 ${suffix}`,
  });
  await knowledgeBaseCard.getByRole("heading").waitFor();
  await knowledgeBaseCard.getByRole("button", { name: "进入" }).click();
}

async function uploadAndWait(page: Page, name: string, content: string) {
  await page.getByTestId("new-document-file").setInputFiles({
    name,
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: docx(content),
  });
  await expect(page.getByRole("article").first()).toContainText("已完成", {
    timeout: 20_000,
  });
}

async function validateConnection(
  page: Page,
  connection: Locator,
  buttonName: string,
) {
  const validationResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/v1/provider-connections/") &&
      response.url().endsWith(":validate"),
  );
  await connection.getByRole("button", { name: buttonName }).click();
  const response = await validationResponse;
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { status: string };
  expect(payload.status).toBe("succeeded");
}

async function configureModelServices(page: Page) {
  await page.getByRole("button", { name: "模型服务" }).click();
  await page
    .getByLabel("服务密钥", { exact: true })
    .fill("synthetic-jina-browser-value");
  await page.getByRole("button", { name: "保存连接" }).click();
  const jina = page.getByRole("article").filter({ hasText: "Jina 主连接" });
  await validateConnection(page, jina, "测试文档向量");
  await validateConnection(page, jina, "测试查询向量");
  await validateConnection(page, jina, "测试结果重排");

  await page.getByLabel("服务商").selectOption("aliyun-model-studio");
  await page
    .getByLabel("服务密钥", { exact: true })
    .fill("synthetic-aliyun-browser-value");
  await page.getByLabel("工作空间标识").fill("llm-syntheticworkspace");
  await page.getByRole("button", { name: "保存连接" }).click();
  const aliyun = page.getByRole("article").filter({ hasText: "百炼备用连接" });
  await validateConnection(page, aliyun, "测试文档向量");
  await validateConnection(page, aliyun, "测试查询向量");
}

async function createRetrievalProfile(
  page: Page,
  instruction = "为检索查询生成准确表示",
) {
  await page.getByRole("button", { name: "检索方案" }).click();
  await page.getByLabel("主向量连接").selectOption({ label: "Jina 主连接" });
  await page.getByLabel("备用向量连接").selectOption({ label: "百炼备用连接" });
  await page.getByLabel("查询指令").fill(instruction);
  await page.getByRole("button", { name: "创建并预览影响" }).click();
}

function documentRow(page: Page, name: string) {
  return page.getByRole("row").filter({ hasText: name });
}

async function sourceArtifact(page: Page): Promise<string> {
  const detail = page.getByRole("region", { name: "文档详情" });
  await expect(detail).toBeVisible();
  return (
    (await detail
      .locator("dt", { hasText: "来源文件指纹" })
      .first()
      .locator("xpath=following-sibling::dd[1]")
      .textContent()) ?? ""
  );
}

test("真实离线 DOCX 到中文 FTS V2 Evidence 流程", async ({
  page,
  request,
}, testInfo) => {
  if (testInfo.project.name !== "chromium-desktop") test.skip();
  await authenticate(page);
  await configureModelServices(page);
  await createScope(page, `${testInfo.project.name}-${Date.now()}`);
  await createRetrievalProfile(page);
  await expect(page.getByText("需要构建新索引版本")).toBeVisible();
  await page.getByRole("button", { name: "确认应用" }).click();
  await page.getByRole("button", { name: "文档管理" }).click();
  await uploadAndWait(
    page,
    "青岛啤酒采购流程.docx",
    "青岛啤酒采购流程需要采购申请审批，并由采购部门归档。 ",
  );
  await page.getByRole("button", { name: "文档管理" }).click();

  const originalRow = documentRow(page, "青岛啤酒采购流程.docx");
  const documentId = await originalRow.locator("td").nth(1).innerText();
  const originalVersion = await originalRow.locator("td").nth(2).innerText();
  await originalRow.getByRole("button", { name: "重命名" }).click();
  await expect(
    page.getByText("只改显示名，不创建新 dver 或重建索引。"),
  ).toBeVisible();
  await originalRow.getByLabel("新显示名").fill("青岛啤酒采购制度.docx");
  await originalRow.getByRole("button", { name: "保存" }).click();
  const renamedRow = documentRow(page, "青岛啤酒采购制度.docx");
  await expect(renamedRow).toBeVisible();
  await expect(renamedRow.locator("td").nth(2)).toHaveText(originalVersion);
  await renamedRow.getByRole("button", { name: "详情" }).click();
  const originalArtifact = await sourceArtifact(page);

  await renamedRow
    .getByTestId(new RegExp(`^version-${documentId}$`))
    .setInputFiles({
      name: "青岛啤酒采购制度-v2.docx",
      mimeType:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      buffer: docx("青岛啤酒采购流程第二版要求采购申请、复核和归档。"),
    });
  await expect(page.getByRole("article").first()).toContainText("已完成", {
    timeout: 20_000,
  });
  await page
    .getByRole("article")
    .first()
    .getByRole("button", { name: "检查版本" })
    .click();
  await expect(page.getByText("使用中", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "文档管理" }).click();
  await expect(
    documentRow(page, "青岛啤酒采购制度.docx").locator("td").nth(2),
  ).not.toHaveText(originalVersion);

  await uploadAndWait(
    page,
    "青岛啤酒采购流程.docx",
    "青岛啤酒采购流程需要采购申请审批，并由采购部门归档。 ",
  );
  await page.getByRole("button", { name: "文档管理" }).click();
  const duplicateRow = documentRow(page, "青岛啤酒采购流程.docx");
  await expect(duplicateRow.locator("td").nth(1)).not.toHaveText(documentId);
  await duplicateRow.getByRole("button", { name: "详情" }).click();
  expect(await sourceArtifact(page)).toBe(originalArtifact);
  await duplicateRow.getByRole("button", { name: "删除" }).click();
  await duplicateRow.getByRole("button", { name: "确认删除" }).click();
  await expect(documentRow(page, "青岛啤酒采购制度.docx")).toBeVisible();

  await uploadAndWait(
    page,
    "无关噪声.docx",
    "设备巡检记录包含空调滤芯更换和机房温度检查。 ",
  );

  await page.getByRole("button", { name: "检索调试" }).click();
  await page.getByLabel("查询文本").fill("青岛啤酒");
  await page.getByRole("button", { name: "执行" }).click();

  const evidence = page
    .getByRole("button", { name: /青岛啤酒采购流程/ })
    .first();
  await expect(evidence).toBeVisible();
  await expect(page.getByText("无关噪声.docx")).toHaveCount(0);
  await evidence.click();
  await expect(page.getByRole("dialog", { name: "证据详情" })).toContainText(
    "青岛啤酒采购流程",
  );
  await expect(page.getByRole("dialog", { name: "证据详情" })).toContainText(
    "retrieval_candidate",
  );
  await page.getByRole("button", { name: "关闭证据详情" }).click();
  await expect(page.getByText("RRF 融合贡献")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: "检索调试" })).toBeVisible();
  await expect(page.locator(".scope-card")).toContainText("kb_");
  await page.getByRole("button", { name: "模型服务" }).click();
  await page.getByLabel("待轮换凭据").selectOption({ index: 1 });
  await page.getByLabel("新服务密钥").fill("rotated-jina-browser-value");
  const rotationResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/v1/provider-credentials/") &&
      response.url().endsWith(":rotate"),
  );
  await page.getByRole("button", { name: "轮换密钥" }).click();
  expect((await rotationResponse).ok()).toBeTruthy();
  await createRetrievalProfile(page);
  await expect(page.getByText("无需重建索引")).toBeVisible();
  await createRetrievalProfile(page, "为新版业务检索查询生成准确表示");
  await expect(page.getByText("需要构建新索引版本")).toBeVisible();

  await page.getByRole("button", { name: "接口访问" }).click();
  await page.getByLabel("令牌名称").fill("浏览器验收令牌");
  await page.getByRole("button", { name: "创建令牌" }).click();
  const token =
    (await page.getByRole("alert").locator("code").textContent()) ?? "";
  const projectId =
    (await page.locator(".scope-card code").nth(1).textContent()) ?? "";
  const knowledgeBaseId =
    (await page.locator(".scope-card code").nth(2).textContent()) ?? "";
  expect(token).toMatch(/^ragk_/);
  const tokenQuery = await request.post(
    `/api/v1/projects/${projectId}/knowledge-bases/${knowledgeBaseId}:search`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: { query: "青岛啤酒", limit: 1 },
    },
  );
  expect(tokenQuery.ok()).toBeTruthy();
  const tokenCard = page
    .getByRole("article")
    .filter({ hasText: "浏览器验收令牌" });
  const [revokeResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(":revoke"),
    ),
    tokenCard.getByRole("button", { name: "吊销" }).click(),
  ]);
  expect(revokeResponse.ok()).toBeTruthy();
  await expect(tokenCard).toContainText("revoked");
  const deniedQuery = await request.post(
    `/api/v1/projects/${projectId}/knowledge-bases/${knowledgeBaseId}:search`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: { query: "青岛啤酒", limit: 1 },
    },
  );
  expect(deniedQuery.status()).toBe(403);

  const storageContainsSecret = await page.evaluate(() =>
    JSON.stringify({ ...localStorage, ...sessionStorage }),
  );
  expect(storageContainsSecret).not.toContain("browser-value");

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("375px 视口可通过导航进入系统状态", async ({ page }) => {
  await authenticate(page);
  if (page.viewportSize()?.width !== 375) test.skip();
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("button", { name: "系统状态" }).click();
  await expect(
    page.getByRole("heading", { name: "系统状态", exact: true, level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("离线评测证据")).toBeVisible();
});

test("表格空列与合并结构保持可定位且不伪造引用", async ({ page }, testInfo) => {
  if (testInfo.project.name !== "chromium-desktop") test.skip();
  await authenticate(page);
  await createScope(page, `table-${Date.now()}`);
  const table = `<w:tbl>
    <w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
    <w:tr><w:trPr><w:tblHeader/></w:trPr>
      <w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
      <w:tc><w:p/></w:tc>
      <w:tc><w:p><w:r><w:t>说明</w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>
      <w:tc><w:p><w:r><w:t>表格定位词</w:t></w:r></w:p></w:tc>
      <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>审批</w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr>
      <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合并备注</w:t></w:r></w:p></w:tc>
      <w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>
    </w:tr>
  </w:tbl>`;
  await page.getByTestId("new-document-file").setInputFiles({
    name: "表格结构.docx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: docxBlocks(table),
  });
  const job = page.getByRole("article").first();
  await expect(job).toContainText("已完成", { timeout: 20_000 });
  await job.getByRole("button", { name: "检查版本" }).click();
  const tableChunk = page
    .locator("details")
    .filter({ hasText: "table" })
    .first();
  await tableChunk.locator("summary").click();
  await expect(tableChunk).toContainText("表格定位词");
  await expect(
    tableChunk
      .locator("h4", { hasText: "引用文本" })
      .locator("xpath=following-sibling::p[1]"),
  ).not.toContainText("<EMPTY>");
  await expect(
    tableChunk
      .locator("h4", { hasText: "向量文本" })
      .locator("xpath=following-sibling::p[1]"),
  ).toContainText(/<EMPTY>|<OMITTED>/);
});
