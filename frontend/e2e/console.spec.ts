async function navigate(page: Page, name: string) {
  const nav = page.getByRole("navigation", { name: "主导航" });
  const button = nav.getByRole("button", { name, exact: true });
  if (!(await button.isVisible())) {
    const menu = page.getByRole("button", { name: "打开导航" });
    if (
      (await menu.isVisible()) &&
      !(await page
        .locator(".sidebar")
        .evaluate((element) => element.classList.contains("open")))
    )
      await menu.click();
    if (!(await button.isVisible())) await nav.locator("summary").click();
  }
  await button.click();
}
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { strToU8, zipSync } from "fflate";
import { createServer, type ServerResponse } from "node:http";
import type { ChunkPage, QueryResponse } from "../src/api/client";

test("相关内容 unit_synthetic 提示与真实授权原文入口", async ({
  page,
}, testInfo) => {
  await authenticate(page);
  await createScope(page, `related-${testInfo.project.name}`);
  await uploadAndWait(
    page,
    "隐私手册.docx",
    "隐私专员负责处理数据访问请求与投诉。",
  );
  const scopeUrl = new URL(page.url());
  const project = scopeUrl.searchParams.get("project")!;
  const kb = scopeUrl.searchParams.get("knowledgeBase")!;
  const base = `/api/v1/projects/${project}/knowledge-bases/${kb}`;
  const session = (await (
    await page.request.get("/api/v1/console/session")
  ).json()) as { csrf_token: string };
  const headers = {
    "X-CSRF-Token": session.csrf_token,
    Origin: scopeUrl.origin,
  };
  const kbResponse = (await (await page.request.get(base)).json()) as {
    active_index_revision_id: string;
  };
  const revision = kbResponse.active_index_revision_id;
  const chunkResponse = await page.request.get(
    `${base}/revisions/${revision}/chunks`,
  );
  expect(chunkResponse.ok(), await chunkResponse.text()).toBeTruthy();
  const chunk = ((await chunkResponse.json()) as ChunkPage).items[0];
  const baselineResponse = await page.request.post(`${base}:answer`, {
    headers,
    data: { query: "隐私专员" },
  });
  expect(baselineResponse.ok(), await baselineResponse.text()).toBeTruthy();
  const baseline = (await baselineResponse.json()) as QueryResponse;
  const related = {
    related_id: "related_unit_synthetic",
    document_id: chunk.version.document_id,
    document_version_id: chunk.version.document_version_id,
    index_revision_id: revision,
    chunk_id: chunk.chunk_id,
    document_name: "隐私手册.docx",
    heading_path: chunk.heading_path,
    excerpt: chunk.citation_text,
    source_spans: chunk.source_spans,
    is_answer_evidence: false as const,
    relevance_reason: "RELEVANCE_UNVERIFIED" as const,
    rerank_verified: false,
  };
  const notice =
    "本次检索未找到足以直接回答这个问题的依据。下面这些内容可能相关，供你查阅。";
  const dependency =
    "重排服务暂时不可用，这次未能可靠确认答案。你可以先查看下面检索到的内容。";
  const empty =
    "本次检索未找到足够相关的内容。可以补充关键词，或检查当前知识库是否包含所需资料。";
  const outcomes: Partial<QueryResponse>[] = [
    {
      status: "INSUFFICIENT_EVIDENCE",
      answer: null,
      evidence: [],
      related_contents: [related],
      display_message: notice,
    },
    {
      status: "PROVIDER_UNAVAILABLE",
      answer: null,
      evidence: [],
      related_contents: [related],
      display_message: dependency,
    },
    {
      status: "INSUFFICIENT_EVIDENCE",
      answer: null,
      evidence: [],
      related_contents: [],
      display_message: empty,
    },
    {
      status: "ANSWERABLE",
      answer: "隐私专员负责处理数据访问请求与投诉。",
      related_contents: [],
      display_message: null,
    },
    {
      status: "INSUFFICIENT_EVIDENCE",
      answer: null,
      evidence: [],
      related_contents: [related],
      display_message: notice,
    },
  ];
  let answerRequests = 0;
  await page.route(`**${base}:answer`, async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      include_related_content: true,
      stream: true,
      stream_protocol: "rag-answer-sse-v1",
    });
    const outcome = outcomes[answerRequests++];
    expect(outcome).toBeDefined();
    const payload = {
      ...baseline,
      ...outcome,
      type: "final",
      protocol: "rag-answer-sse-v1",
      sequence: 0,
      evidence_count: outcome.evidence?.length ?? baseline.evidence_count,
    };
    await route.fulfill({
      body: `event: final\ndata: ${JSON.stringify(payload)}\n\n`,
      headers: {
        "Cache-Control": "no-store, no-transform",
        "Content-Type": "text/event-stream; charset=utf-8",
        "X-Trace-Id": baseline.trace_id,
      },
    });
  });
  await navigate(page, "问答");
  const submit = async () => {
    await page.getByLabel("查询文本").fill("隐私专员电话");
    await page.getByRole("button", { name: "执行", exact: true }).click();
  };
  await submit();
  await expect(page.getByText(notice)).toBeVisible();
  await testInfo.attach("related-content-preview", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
  await expect(
    page.getByRole("region", { name: "正式答案", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "查看原文" }).click();
  await expect(
    page.getByRole("region", { name: "相关原文详情" }),
  ).toContainText(chunk.citation_text);
  await page.getByRole("button", { name: "关闭原文" }).click();
  await submit();
  await expect(page.getByText(dependency)).toBeVisible();
  await expect(page.getByText("未完成相关性复核，仅供查阅。")).toBeVisible();
  await submit();
  await expect(page.getByText(empty)).toBeVisible();
  await expect(
    page.getByRole("region", { name: "相关内容", exact: true }),
  ).toHaveCount(0);
  await submit();
  await expect(
    page.getByRole("region", { name: "正式答案", exact: true }),
  ).toHaveAttribute("data-raw-answer", "隐私专员负责处理数据访问请求与投诉。");
  await submit();
  await expect(
    page.getByRole("region", { name: "相关内容", exact: true }),
  ).toBeVisible();
  const deleted = await page.request.delete(
    `${base}/documents/${chunk.version.document_id}`,
    { headers },
  );
  expect(deleted.ok(), await deleted.text()).toBeTruthy();
  await page.getByRole("button", { name: "查看原文" }).click();
  await expect(page.getByText("原文当前不可读取。")).toBeVisible();
  await expect(
    page.getByRole("region", { name: "相关内容", exact: true }),
  ).toHaveCount(0);
  expect(answerRequests).toBe(5);
  const persisted = await page.evaluate(() =>
    JSON.stringify({ ...localStorage, ...sessionStorage }),
  );
  expect(persisted).not.toContain(chunk.citation_text);
});

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
  await expect(page.getByRole("dialog")).toBeHidden();
}

async function createScope(page: Page, suffix: string) {
  await page.getByRole("button", { name: "管理项目", exact: true }).click();
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

async function configureModelServices(page: Page) {
  await page.getByRole("button", { name: "模型服务" }).click();
  await page.getByRole("button", { name: "新增连接" }).click();
  await page
    .getByLabel("服务密钥", { exact: true })
    .fill("synthetic-jina-browser-value");
  await page.getByRole("button", { name: "保存连接" }).click();

  await page.getByRole("button", { name: "新增连接" }).click();
  await page.getByLabel("服务商").selectOption("aliyun-model-studio");
  await page
    .getByLabel("服务密钥", { exact: true })
    .fill("synthetic-aliyun-browser-value");
  await page.getByLabel("工作空间标识").fill("llm-syntheticworkspace");
  await page
    .getByLabel("API Host", { exact: true })
    .fill("https://llm-syntheticworkspace.cn-beijing.maas.aliyuncs.com");
  await page.getByRole("button", { name: "保存连接" }).click();
}

async function createRetrievalProfile(page: Page, instruction = "") {
  await navigate(page, "检索方案");
  await page.getByLabel("主向量连接").selectOption({ label: "Jina 主连接" });
  await page.getByLabel("备用向量连接").selectOption({ label: "百炼备用连接" });
  if (!(await page.getByLabel("Qwen 查询指令").isVisible()))
    await page.getByText("高级设置", { exact: true }).click();
  await page.getByLabel("Qwen 查询指令").fill(instruction);
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

test("真实浏览器流式显示、停止与知识库切换保持隔离", async ({
  page,
}, testInfo) => {
  await authenticate(page);
  await createScope(page, `stream-${testInfo.project.name}-${Date.now()}`);
  await uploadAndWait(
    page,
    "浏览器流式合同.docx",
    "公开合成设备 ZX-9 的复核周期为 9 天。",
  );
  const scopeUrl = new URL(page.url());
  const projectId = scopeUrl.searchParams.get("project")!;
  const knowledgeBaseId = scopeUrl.searchParams.get("knowledgeBase")!;
  const base = `/api/v1/projects/${projectId}/knowledge-bases/${knowledgeBaseId}`;
  await navigate(page, "知识库");
  const nextKnowledgeBase = `隔离知识库 ${testInfo.project.name}`;
  await page.getByLabel("知识库名称").fill(nextKnowledgeBase);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  const nextKnowledgeBaseCard = page.getByRole("article").filter({
    hasText: nextKnowledgeBase,
  });
  await nextKnowledgeBaseCard.getByRole("heading").waitFor();
  await navigate(page, "问答");
  const session = (await (
    await page.request.get("/api/v1/console/session")
  ).json()) as { csrf_token: string };
  const baselineResponse = await page.request.post(`${base}:answer`, {
    headers: {
      "X-CSRF-Token": session.csrf_token,
      Origin: scopeUrl.origin,
    },
    data: { query: "ZX-9 的复核周期是多少？" },
  });
  expect(baselineResponse.ok(), await baselineResponse.text()).toBeTruthy();
  const baseline = (await baselineResponse.json()) as QueryResponse;
  expect(baseline.status).toBe("CONFIGURATION_REQUIRED");
  expect(baseline.answer).toBeNull();
  const modelFinal: QueryResponse = {
    ...baseline,
    status: "ANSWERABLE",
    reason_code: "ANSWERED",
    answer: "公开合成模型答案 [S1]",
    generation_mode: "llm",
    generation_reason_code: null,
    generation_called_this_request: true,
    confidence: {
      ...baseline.confidence,
      status: "ANSWERABLE",
    },
  };

  const responses = new Map<number, ServerResponse>();
  const openedResolvers = new Map<
    number,
    (response: ServerResponse) => void
  >();
  const closedResolvers = new Map<number, () => void>();
  const opened = new Map<number, Promise<ServerResponse>>();
  const closed = new Map<number, Promise<void>>();
  for (const slot of [1, 2, 3]) {
    opened.set(
      slot,
      new Promise((resolve) => openedResolvers.set(slot, resolve)),
    );
    closed.set(
      slot,
      new Promise((resolve) => closedResolvers.set(slot, resolve)),
    );
  }
  const claimTexts = new Map([
    [1, "首条已核验浏览器事实"],
    [2, "停止前已核验浏览器事实"],
    [3, "旧知识库暂存事实"],
  ]);
  const frame = (name: string, payload: object) =>
    `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
  const server = createServer((request, response) => {
    request.resume();
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const slot = Number(url.searchParams.get("slot"));
    const traceId = `trace_${slot.toString(16).repeat(32)}`;
    const common = {
      protocol: "rag-answer-sse-v1",
      trace_id: traceId,
      project_id: projectId,
      knowledge_base_id: knowledgeBaseId,
    };
    response.writeHead(200, {
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Origin": scopeUrl.origin,
      "Cache-Control": "no-store, no-transform",
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Accel-Buffering": "no",
      "X-Trace-Id": traceId,
    });
    response.flushHeaders();
    response.socket?.setNoDelay(true);
    response.write(
      frame("meta", {
        ...common,
        type: "meta",
        sequence: 0,
        delivery: "incremental_or_final_only",
      }),
    );
    response.write(
      frame("stage", {
        ...common,
        type: "stage",
        sequence: 1,
        stage: "generation",
        attributes: [],
      }),
    );
    response.write(
      frame("claim", {
        ...common,
        type: "claim",
        sequence: 2,
        claim_index: 0,
        provisional: true,
        active_index_revision_id: baseline.active_index_revision_id,
        claim: {
          text: claimTexts.get(slot),
          supports: [{ support_id: "S1", quote: "公开合成引用" }],
        },
      }),
    );
    responses.set(slot, response);
    response.on("close", () => closedResolvers.get(slot)?.());
    openedResolvers.get(slot)?.(response);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("浏览器 SSE 测试服务未取得 loopback 端口");
  }
  let requestSlot = 0;
  await page.route(`**${base}:answer`, async (route) => {
    requestSlot += 1;
    await route.continue({
      url: `http://127.0.0.1:${address.port}/answer?slot=${requestSlot}`,
    });
  });
  try {
    const input = page.getByLabel("查询文本");
    const execute = page.getByRole("button", { name: "执行", exact: true });
    await input.fill("ZX-9 的复核周期是多少？");
    await execute.click();
    const firstResponse = await opened.get(1);
    await expect(
      page.getByRole("region", { name: "已核验暂存内容" }),
    ).toContainText("首条已核验浏览器事实");
    firstResponse?.end(
      frame("final", {
        ...modelFinal,
        type: "final",
        protocol: "rag-answer-sse-v1",
        sequence: 3,
        trace_id: "trace_" + "1".repeat(32),
        query_id: "trace_" + "1".repeat(32),
        project_id: projectId,
        knowledge_base_id: knowledgeBaseId,
      }),
    );
    await expect(page.getByRole("region", { name: "正式答案" })).toBeVisible();
    await expect(
      page.getByRole("region", { name: "已核验暂存内容" }),
    ).toHaveCount(0);

    await execute.click();
    await opened.get(2);
    await expect(page.getByRole("button", { name: "停止" })).toBeVisible();
    await page.getByRole("button", { name: "停止" }).click();
    await closed.get(2);
    await expect(execute).toBeEnabled();
    await expect(page.getByText(/暂存内容不是最终答案/)).toBeVisible();
    responses.get(2)?.write("停止后的晚到正文");
    await expect(page.getByText("停止后的晚到正文")).toHaveCount(0);

    await execute.click();
    await opened.get(3);
    await expect(
      page.getByRole("region", { name: "已核验暂存内容" }),
    ).toContainText("旧知识库暂存事实");
    await navigate(page, "知识库");
    await closed.get(3);
    await nextKnowledgeBaseCard.getByRole("button", { name: "进入" }).click();
    await expect(page.locator(".scope-card")).toContainText(nextKnowledgeBase);
    await expect(page.getByText("旧知识库暂存事实")).toHaveCount(0);
  } finally {
    await page.unroute(`**${base}:answer`);
    for (const response of responses.values()) response.destroy();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

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
  await page.getByRole("button", { name: "验证方案所用参数" }).click();
  await page.getByRole("button", { name: "开始测试", exact: true }).click();
  await expect(
    page.getByText("方案参数连接验证通过；检索质量仍需独立验证。"),
  ).toBeVisible();
  const applied = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(":activate"),
  );
  await page.getByRole("button", { name: "建立新索引并切换" }).click();
  expect((await applied).ok()).toBeTruthy();
  await expect(
    page.getByRole("heading", { name: "当前方案", exact: true }),
  ).toBeVisible();
  await navigate(page, "文档管理");
  await uploadAndWait(
    page,
    "青岛啤酒采购流程.docx",
    "青岛啤酒采购流程需要采购申请审批，并由采购部门归档。 ",
  );
  await navigate(page, "文档管理");

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
  await navigate(page, "文档管理");
  await expect(
    documentRow(page, "青岛啤酒采购制度.docx").locator("td").nth(2),
  ).not.toHaveText(originalVersion);

  await uploadAndWait(
    page,
    "青岛啤酒采购流程.docx",
    "青岛啤酒采购流程需要采购申请审批，并由采购部门归档。 ",
  );
  await navigate(page, "文档管理");
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

  await navigate(page, "检索调试");
  await page.getByLabel("查询文本").fill("青岛啤酒");
  await page.getByRole("button", { name: "执行" }).click();

  const candidates = page.getByRole("region", { name: "检索候选" });
  await expect(candidates).toHaveAttribute(
    "data-content-role",
    "diagnostic-evidence",
  );
  await expect(page.getByRole("region", { name: "引用依据" })).toHaveCount(0);
  const evidence = candidates
    .getByRole("button", { name: /青岛啤酒采购流程/ })
    .first();
  await expect(evidence).toBeVisible();
  await expect(page.getByText("无关噪声.docx")).toHaveCount(0);
  await evidence.click();
  await expect(page.getByRole("dialog", { name: "证据详情" })).toContainText(
    "检索候选（未发布）",
  );
  await expect(page.getByRole("dialog", { name: "证据详情" })).toContainText(
    "青岛啤酒采购流程",
  );
  await expect(page.getByRole("dialog", { name: "证据详情" })).toContainText(
    "retrieval_candidate",
  );
  await page.getByRole("button", { name: "关闭证据详情" }).click();
  await page.getByText("本次检索诊断", { exact: true }).click();
  const fusionHeading = page.getByRole("heading", { name: "融合与证据决定" });
  await expect(fusionHeading).toBeVisible();
  const firstFusion = fusionHeading.locator("..").locator("details").first();
  await firstFusion.locator("summary").click();
  const contributions: unknown = JSON.parse(
    (await firstFusion.locator("pre").textContent()) ?? "null",
  );
  expect(contributions).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        channel: "lexical:fts5",
        rank: expect.any(Number),
        contribution: expect.any(Number),
      }),
    ]),
  );

  await page.reload();
  await expect(page.getByRole("heading", { name: "检索调试" })).toBeVisible();
  await expect(page.locator(".scope-card")).toContainText("kb_");
  await page.getByRole("button", { name: "模型服务" }).click();
  await page
    .getByRole("article")
    .filter({ hasText: "Jina 主连接" })
    .getByRole("button", { name: "编辑连接" })
    .click();
  await page.getByRole("button", { name: "更换密钥", exact: true }).click();
  await page.getByLabel("新服务密钥").fill("rotated-jina-browser-value");
  const rotationResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/v1/provider-credentials/") &&
      response.url().endsWith(":rotate"),
  );
  await page.getByRole("button", { name: "确认更换密钥" }).click();
  expect((await rotationResponse).ok()).toBeTruthy();
  await expect(page.getByText("密钥已更换，请重新测试。")).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await createRetrievalProfile(page);
  await expect(page.getByText("无需重建索引")).toBeVisible();
  await createRetrievalProfile(page, "为新版业务检索查询生成准确表示");
  await expect(page.getByText("需要构建新索引版本")).toBeVisible();

  if (await page.getByRole("dialog", { name: "编辑 Jina 连接" }).isVisible())
    await page.getByRole("button", { name: "取消", exact: true }).click();
  await navigate(page, "接口访问");
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
  await expect(tokenCard).toContainText("已吊销");
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
  await navigate(page, "系统状态");
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
