import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { PublicAnswer } from "./PublicAnswer";
import type { PublicTurn } from "./usePublicChat";

afterEach(() => vi.restoreAllMocks());

it("生成等待期间只报告真实的连接活动，不伪造阶段进度", async () => {
  const now = Date.now();
  vi.spyOn(Date, "now").mockReturnValue(now);
  const turn: PublicTurn = {
    id: "turn-test",
    conversationId: "wst-test",
    question: "测试问题",
    status: "streaming",
    stageMessage: "正在组织回答",
    stageHistory: ["已收到问题", "正在组织回答"],
    startedAt: now - 25000,
    stageStartedAt: now - 22000,
    lastSignalAt: now - 1000,
    claims: [],
    citations: [],
    partial: false,
    feedback: "idle",
  };
  const { rerender } = render(
    <PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />,
  );
  expect(
    await screen.findByText(
      "连接正常，仍在等待回答服务返回结果；如需可停止后重试。",
    ),
  ).toBeInTheDocument();
  expect(screen.getByText("25 秒")).toBeInTheDocument();
  expect(screen.queryByText("正在核对回答与来源")).not.toBeInTheDocument();

  rerender(
    <PublicAnswer
      onFeedback={vi.fn()}
      onRetry={vi.fn()}
      turn={{ ...turn, lastSignalAt: now - 7000 }}
    />,
  );
  expect(
    screen.getByText("暂未收到新数据，仍在等待回答服务；如需可停止后重试。"),
  ).toBeInTheDocument();
});

it("安全渲染原生 Markdown 的代码、表格和链接，并展示截断提示", () => {
  const now = Date.now();
  const answer = [
    "# 原生标题\n",
    "```js",
    "const value = 1;",
    "```\n",
    "|项目|值|",
    "|---|---|",
    "|甲|乙|\n",
    "[安全链接](https://example.test/docs)",
    "[危险链接](javascript:alert(1))",
    "<script>alert('xss')</script>",
  ].join("\n");
  const turn: PublicTurn = {
    id: "turn-markdown",
    conversationId: "wst-test",
    question: "测试 Markdown",
    status: "completed",
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    answer,
    citations: [],
    partial: false,
    truncated: true,
    feedback: "idle",
  };
  const { container } = render(
    <PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />,
  );
  expect(screen.getByRole("heading", { name: "原生标题" })).toBeInTheDocument();
  expect(screen.getByText("const value = 1;")).toBeInTheDocument();
  expect(screen.getByRole("table")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute(
    "href",
    "https://example.test/docs",
  );
  expect(screen.getByText("危险链接").closest("a")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText("知识库提示本次回答已截断。")).toBeInTheDocument();
});

it("实时正文与引用可同时显示，且不宣称内容已核验", () => {
  const now = Date.now();
  const turn: PublicTurn = {
    id: "turn-live",
    conversationId: "wst-test",
    question: "测试引用",
    status: "streaming",
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    provisionalAnswer: "**实时正文**",
    citations: [{ document_name: "政策.md", quote: "原生片段" }],
    partial: false,
    feedback: "idle",
  };
  render(<PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />);
  expect(screen.getByText("实时正文").tagName).toBe("STRONG");
  expect(screen.getByText("引用依据（1段）")).toBeInTheDocument();
  expect(screen.queryByText(/已核验/)).not.toBeInTheDocument();
});

it("原生流只显示检索过程和实时答案，不暴露事件正文", () => {
  const now = Date.now();
  const turn: PublicTurn = {
    id: "turn-native",
    conversationId: "wst-test",
    question: "开发中心是干嘛的",
    status: "streaming",
    nativeProtocol: true,
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    provisionalAnswer: "开发中心负责**研发项目**。",
    nativeEvents: [
      {
        sequence: 1,
        responseType: "tool_result",
        payload: {
          content: "不应显示的原生理解详情",
          data: { tool_name: "query_understand", success: true },
        },
      },
      {
        sequence: 2,
        responseType: "tool_call",
        payload: { data: { tool_name: "knowledge_search" } },
      },
      {
        sequence: 3,
        responseType: "tool_result",
        payload: {
          content: "不应显示的检索详情",
          data: { tool_name: "knowledge_search", success: true, count: 6 },
        },
      },
    ],
    citations: [
      { document_name: "职责.docx", native_knowledge_id: "doc-1" },
      { document_name: "职责.docx", native_knowledge_id: "doc-1" },
      { document_name: "流程.docx", native_knowledge_id: "doc-2" },
    ],
    partial: false,
    feedback: "idle",
  };
  render(<PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />);
  expect(screen.getByText("检索完成 · 引用了 2 篇文档")).toBeVisible();
  expect(screen.getByText("已完成问题理解")).toBeVisible();
  expect(screen.getByText("检索知识库 · 找到 6 个结果")).toBeVisible();
  expect(screen.getByText("研发项目").tagName).toBe("STRONG");
  expect(screen.queryByText("原生事件详情")).not.toBeInTheDocument();
  expect(screen.queryByText("不应显示的检索详情")).not.toBeInTheDocument();
  expect(screen.getByText("引用依据（3段）")).toBeInTheDocument();
});

it("原生消息资源图片在完成后走同源会话鉴权地址", () => {
  const now = Date.now();
  const turn: PublicTurn = {
    id: "turn-image",
    conversationId: "session-1",
    traceId: "trace-1",
    question: "查看图表",
    status: "completed",
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    answer: "![原生图表](resource://figures/chart.png)",
    citations: [],
    partial: false,
    feedback: "idle",
  };
  render(<PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />);
  const image = screen.getByRole("img", { name: "原生图表" });
  expect(image).toHaveAttribute(
    "src",
    "/api/public/conversations/session-1/turns/trace-1/resources?file_path=resource%3A%2F%2Ffigures%2Fchart.png",
  );
  expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
});

it("把原生知识库引用标记显示为序号，保留原始引用和代码示例", () => {
  const now = Date.now();
  const tag = '<kb doc="制度.docx" chunk_id="chunk-1" kb_id="kb-1" />';
  const turn: PublicTurn = {
    id: "turn-citation-marker",
    conversationId: "wst-test",
    question: "何时折旧",
    status: "completed",
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    answer: `次月开始折旧。${tag}\n\n\`\`\`xml\n${tag}\n\`\`\``,
    citations: [
      {
        document_name: "制度.docx",
        native_chunk_id: "chunk-1",
        quote: "次月起计提",
      },
    ],
    partial: false,
    feedback: "idle",
  };
  const { container } = render(
    <PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />,
  );
  expect(screen.getByText(/次月开始折旧。〔1〕/)).toBeInTheDocument();
  expect(screen.getByText("引用依据（1段）")).toBeInTheDocument();
  expect(container.querySelector("code")?.textContent).toContain(tag);
  expect(container.querySelector(".wst-final-answer")?.textContent).not.toContain(
    `${tag}${tag}`,
  );
});
