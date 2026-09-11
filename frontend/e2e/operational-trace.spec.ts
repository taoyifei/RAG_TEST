import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Download, type Page } from "@playwright/test";
import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";

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
    data: { query: "TR-21 的维护周期是多少？", trace_mode: "FULL" },
  });
  expect(answer.ok(), await answer.text()).toBeTruthy();
  const result = (await answer.json()) as {
    trace_id: string;
    status: string;
    answer: string | null;
    requested_answer_type: string;
    generation_mode: string;
    data_plane: { retrieval_data_plane: string };
  };
  expect(result.status).toBe("CONFIGURATION_REQUIRED");
  expect(result.answer).toBeNull();
  expect(result.requested_answer_type).toBe("FACT");
  expect(result.generation_mode).toBe("none");
  expect(result.data_plane.retrieval_data_plane).toBe("default_local_fallback");
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

test("桌面与移动端实际下载支持包并验证会话、反馈与清理边界", async ({
  page,
}, testInfo) => {
  await authenticate(page);
  await createScope(page, `support-${testInfo.project.name}`);
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
    `${base}/documents?display_name=${encodeURIComponent("支持包与会话样例.docx")}`,
    {
      headers: {
        ...headers,
        "Content-Type": mediaType,
        "Idempotency-Key": `support-browser-${testInfo.project.name}`,
      },
      data: docx("设备 TR-21 的维护周期为 14 天。"),
    },
  );
  expect(upload.ok(), await upload.text()).toBeTruthy();
  await waitForJob(page, ((await upload.json()) as { job_id: string }).job_id);

  const chatUrl = new URL(scopeUrl);
  chatUrl.pathname = "/chat";
  await page.goto(chatUrl.toString());
  const conversation = page.getByRole("region", { name: "当前会话" });
  await expect(conversation).toBeVisible();
  const firstConversationId = await conversation.locator("code").innerText();
  await page.getByLabel("查询文本").fill("设备 TR-21 的维护周期是多少？");
  await page.getByRole("button", { name: "执行", exact: true }).click();
  await expect(
    page.getByText(/回答模型尚未完成配置/),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正式答案" }),
  ).toHaveCount(0);
  const traceCode = page
    .locator("code")
    .filter({ hasText: /^trace_[0-9a-f]{32}$/ });
  await expect(traceCode).toHaveCount(1);
  const primaryTraceId = (await traceCode.textContent()) ?? "";
  expect(primaryTraceId).toMatch(/^trace_[0-9a-f]{32}$/);
  await page
    .getByRole("region", { name: "回答反馈" })
    .getByRole("button", { name: "有用", exact: true })
    .click();
  await expect(page.getByText("当前反馈：有用", { exact: true })).toBeVisible();
  await conversation
    .getByRole("button", { name: "清空当前会话", exact: true })
    .click();
  await expect(conversation.getByRole("status")).toHaveText(
    "当前会话没有已保存轮次。",
  );
  await conversation.getByRole("button", { name: "新会话" }).click();
  await expect(conversation.locator("code")).not.toHaveText(
    firstConversationId,
  );

  await page.getByLabel("查询文本").fill("请再次核对 TR-21 的维护周期。");
  await page.getByRole("button", { name: "执行", exact: true }).click();
  await expect(
    page.getByText(/回答模型尚未完成配置/),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正式答案" }),
  ).toHaveCount(0);
  await expect(traceCode).toHaveCount(1);
  const secondaryTraceId = (await traceCode.textContent()) ?? "";
  expect(secondaryTraceId).toMatch(/^trace_[0-9a-f]{32}$/);
  expect(secondaryTraceId).not.toBe(primaryTraceId);

  const historyUrl = new URL(scopeUrl);
  historyUrl.pathname = "/history";
  await page.goto(historyUrl.toString());
  const primaryRow = page.getByRole("article").filter({
    has: page.getByText(primaryTraceId, { exact: true }),
  });
  await expect(primaryRow).toBeVisible();
  await expect(
    page.getByRole("article").filter({
      has: page.getByText(secondaryTraceId, { exact: true }),
    }),
  ).toBeVisible();

  await primaryRow.getByRole("button", { name: "下载本条支持包" }).click();
  const supportDialog = page.getByRole("dialog", {
    name: "下载问答与技术 Trace 支持包",
  });
  await supportDialog
    .getByLabel("包含当前仍获授权的敏感问题、答案与引用正文")
    .check();
  await expect(supportDialog.getByRole("alert")).toContainText("敏感问答");
  const singleDownloadPromise = page.waitForEvent("download");
  await supportDialog.getByRole("button", { name: "确认下载支持包" }).click();
  const singleDownload = await singleDownloadPromise;
  expect(singleDownload.suggestedFilename()).toBe(
    `history-trace-${primaryTraceId}.zip`,
  );
  const singleMembers = unzipSync(await downloadBytes(singleDownload));
  expect(Object.keys(singleMembers)).toEqual([
    "MANIFEST.json",
    `items/${primaryTraceId}/history.json`,
    `items/${primaryTraceId}/operational-trace.json`,
  ]);
  const singleManifest = jsonMember<SupportManifest>(
    singleMembers,
    "MANIFEST.json",
  );
  expect(singleManifest.schema_version).toBe("history-trace-support-v1");
  expect(singleManifest.items).toEqual([
    expect.objectContaining({
      trace_id: primaryTraceId,
      body_included: true,
      operational_trace_schema: "2",
    }),
  ]);
  for (const member of singleManifest.members) {
    expect(sha256(singleMembers[member.path])).toBe(member.sha256);
  }
  const body = jsonMember<Record<string, unknown>>(
    singleMembers,
    `items/${primaryTraceId}/history.json`,
  );
  expect(body.body_included).toBe(true);
  expect(body.question).toBe("设备 TR-21 的维护周期是多少？");
  expect(body.answer).toBeNull();
  expect(JSON.stringify(body)).toContain("14 天");

  await primaryRow.getByRole("button", { name: "查看详情与过程" }).click();
  const historyDetail = page.getByRole("dialog", {
    name: "问答与检索过程",
  });
  await expect(historyDetail).toContainText(primaryTraceId);
  const technicalDownloadPromise = page.waitForEvent("download");
  await historyDetail
    .getByRole("button", { name: "仅下载技术 Trace JSON" })
    .click();
  const technicalDownload = await technicalDownloadPromise;
  expect(technicalDownload.suggestedFilename()).toBe(`${primaryTraceId}.json`);
  expect(
    jsonBytes<{ trace: { trace_id: string } }>(
      await downloadBytes(technicalDownload),
    ).trace.trace_id,
  ).toBe(primaryTraceId);
  await historyDetail.getByRole("button", { name: "关闭" }).click();

  await page.getByRole("button", { name: "选择本页" }).click();
  await expect(page.getByText("已选择 2 条", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "下载已选支持包" }).click();
  const batchDialog = page.getByRole("dialog", {
    name: "下载问答与技术 Trace 支持包",
  });
  const batchDownloadPromise = page.waitForEvent("download");
  await batchDialog.getByRole("button", { name: "确认下载支持包" }).click();
  const batchDownload = await batchDownloadPromise;
  expect(batchDownload.suggestedFilename()).toBe("history-traces.zip");
  const batchMembers = unzipSync(await downloadBytes(batchDownload));
  const batchManifest = jsonMember<SupportManifest>(
    batchMembers,
    "MANIFEST.json",
  );
  expect(batchManifest.canonical_order).toEqual(
    [primaryTraceId, secondaryTraceId].sort(),
  );
  expect(batchManifest.items).toHaveLength(2);
  expect(batchManifest.items.every((item) => !item.body_included)).toBe(true);

  const tracesUrl = new URL(scopeUrl);
  tracesUrl.pathname = "/operational-traces";
  await page.goto(tracesUrl.toString());
  await page.getByLabel("用户反馈").selectOption("useful");
  await page.getByRole("button", { name: "筛选 Trace" }).click();
  const feedbackRow = page.getByRole("row").filter({
    has: page.getByRole("code").filter({ hasText: primaryTraceId }),
  });
  await expect(feedbackRow).toBeVisible();
  await expect(feedbackRow).toContainText("用户反馈：有用");

  const legacyUrl = new URL(tracesUrl);
  legacyUrl.searchParams.set("trace_id", primaryTraceId.slice("trace_".length));
  await page.goto(legacyUrl.toString());
  const detail = page.getByRole("region", {
    name: "Operational Trace 详情",
  });
  await expect(detail).toContainText(primaryTraceId);
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "清理已到期 Trace" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "已清理" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "清理已到期 Trace" }),
  ).toBeEnabled();

  await page.goto(historyUrl.toString());
  await page.getByRole("button", { name: "清理全部历史" }).click();
  const clearDialog = page.getByRole("dialog", {
    name: "清理全部问答历史",
  });
  await expect(clearDialog).toContainText("独立 Operational Trace");
  await clearDialog.getByRole("button", { name: "确认清理全部历史" }).click();
  await expect(
    page.getByRole("heading", { name: "没有匹配的历史" }),
  ).toBeVisible();

  await page.goto(legacyUrl.toString());
  await expect(
    page.getByRole("region", { name: "Operational Trace 详情" }),
  ).toContainText(primaryTraceId);
});

type SupportManifest = {
  schema_version: string;
  canonical_order: string[];
  members: { path: string; sha256: string }[];
  items: {
    trace_id: string;
    body_included: boolean;
    operational_trace_schema: string;
  }[];
};

async function downloadBytes(download: Download): Promise<Uint8Array> {
  const path = await download.path();
  if (!path) throw new Error("浏览器下载没有可读取的临时文件。");
  return new Uint8Array(await readFile(path));
}

function jsonMember<T>(members: Record<string, Uint8Array>, path: string): T {
  const payload = members[path];
  if (!payload) throw new Error(`支持包缺少成员：${path}`);
  return jsonBytes<T>(payload);
}

function jsonBytes<T>(payload: Uint8Array): T {
  return JSON.parse(strFromU8(payload)) as T;
}

function sha256(payload: Uint8Array): string {
  return createHash("sha256").update(payload).digest("hex");
}

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
