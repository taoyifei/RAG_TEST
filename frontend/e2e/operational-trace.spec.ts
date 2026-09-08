import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { strToU8, zipSync } from "fflate";

const mediaType =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

test("默认 Product Trace 支持诊断、惰性 Artifact、导出与关联导航", async ({
  page,
}, testInfo) => {
  await authenticate(page);
  await createScope(page, `trace-${testInfo.project.name}`);
  const scopeUrl = new URL(page.url());
  const projectId = scopeUrl.searchParams.get("project")!;
  const knowledgeBaseId = scopeUrl.searchParams.get("knowledgeBase")!;
  const csrf = (await (
    await page.request.get("/api/v1/console/session")
  ).json()) as { csrf_token: string };
  const headers = {
    "X-CSRF-Token": csrf.csrf_token,
    Origin: scopeUrl.origin,
  };
  const base = `/api/v1/projects/${projectId}/knowledge-bases/${knowledgeBaseId}`;
  const upload = await page.request.post(
    `${base}/documents?display_name=${encodeURIComponent("公开 Trace 浏览器样例.docx")}`,
    {
      headers: {
        ...headers,
        "Content-Type": mediaType,
        "Idempotency-Key": `trace-browser-${testInfo.project.name}`,
      },
      data: docx("设备 TR-21 的维护周期为 14 天。"),
    },
  );
  expect(upload.ok(), await upload.text()).toBeTruthy();
  const job = (await upload.json()) as { job_id: string };
  await waitForJob(page, job.job_id);

  const answer = await page.request.post(`${base}:answer`, {
    headers,
    data: { query: "TR-21 的维护周期", trace_mode: "FULL" },
  });
  expect(answer.ok(), await answer.text()).toBeTruthy();
  const result = (await answer.json()) as { trace_id: string };
  const traceUrl = new URL(scopeUrl);
  traceUrl.pathname = "/operational-traces";
  traceUrl.searchParams.set("trace_id", result.trace_id);
  let artifactRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/artifacts/")) artifactRequests += 1;
  });
  await page.goto(traceUrl.toString());

  const detail = page.getByRole("region", {
    name: "Operational Trace 详情",
  });
  await expect(detail).toContainText(result.trace_id);
  await expect(detail.getByLabel("Span waterfall")).toBeVisible();
  expect(artifactRequests).toBe(0);

  await detail.getByRole("tab", { name: "候选漏斗" }).click();
  await expect(
    detail.getByRole("tabpanel", { name: "候选漏斗" }),
  ).toContainText("FUSION_SELECTED");
  await detail.getByRole("tab", { name: "Provider / Usage" }).click();
  await expect(
    detail.getByRole("tabpanel", { name: "Provider 与 Usage" }),
  ).toBeVisible();
  await detail.getByRole("tab", { name: "Artifact" }).click();
  expect(artifactRequests).toBe(0);
  await detail.getByRole("button", { name: "按需读取" }).click();
  await expect(detail.getByText(/已校验内容/)).toBeVisible();
  expect(artifactRequests).toBe(1);

  const jsonDownload = page.waitForEvent("download");
  await detail.getByRole("button", { name: "导出 JSON" }).click();
  expect((await jsonDownload).suggestedFilename()).toBe(
    `${result.trace_id}.json`,
  );
  await detail.getByRole("button", { name: "关闭详情" }).click();
  await page.getByLabel(`选择 ${result.trace_id}`).check();
  const zipDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: /导出已选/ }).click();
  expect((await zipDownload).suggestedFilename()).toBe(
    "operational-traces.zip",
  );

  await page.getByRole("button", { name: "查看技术详情" }).first().click();
  const accessibility = await new AxeBuilder({ page })
    .disableRules(["color-contrast"])
    .analyze();
  expect(
    accessibility.violations.filter((item) =>
      ["serious", "critical"].includes(item.impact ?? ""),
    ),
  ).toEqual([]);
  await detail.getByRole("button", { name: "打开问答历史" }).click();
  await expect(page).toHaveURL(/\/history\?.*trace_id=/);
  await expect(
    page.getByRole("dialog", { name: "问答与检索过程" }),
  ).toBeVisible();
});

async function authenticate(page: Page) {
  await page.goto("/");
  await page.getByLabel("管理口令").fill("offline-bootstrap-credential");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
}

async function createScope(page: Page, suffix: string) {
  await page.getByRole("button", { name: "管理项目", exact: true }).click();
  await page.getByLabel("项目名称").fill(`离线项目 ${suffix}`);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  const project = page.getByRole("article").filter({
    hasText: `离线项目 ${suffix}`,
  });
  await project.getByRole("button", { name: "进入" }).click();
  await page.getByLabel("知识库名称").fill(`离线知识库 ${suffix}`);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  const knowledgeBase = page.getByRole("article").filter({
    hasText: `离线知识库 ${suffix}`,
  });
  await knowledgeBase.getByRole("button", { name: "进入" }).click();
}

async function waitForJob(page: Page, jobId: string) {
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/v1/jobs/${jobId}`);
        expect(response.ok(), await response.text()).toBeTruthy();
        return ((await response.json()) as { state: string }).state;
      },
      { timeout: 30_000 },
    )
    .toBe("succeeded");
}

function docx(text: string): Buffer {
  const zipOptions = { mtime: new Date("1980-01-01T00:00:00Z") };
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
  const document = `<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>${text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>`;
  return Buffer.from(
    zipSync({
      "[Content_Types].xml": [strToU8(contentTypes), zipOptions],
      "_rels/.rels": [strToU8(relationships), zipOptions],
      "word/document.xml": [strToU8(document), zipOptions],
      "word/styles.xml": [
        strToU8(
          `<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>`,
        ),
        zipOptions,
      ],
    }),
  );
}
