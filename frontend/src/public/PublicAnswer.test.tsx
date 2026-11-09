import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToString } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";

import { PublicAnswer } from "./PublicAnswer";
import * as libraryApi from "./libraryApi";
import type { PublicTurn } from "./usePublicChat";

afterEach(() => vi.restoreAllMocks());

it("把可填写原件放在回答开头并使用资料下载入口", async () => {
  const now = Date.now();
  const filename = "项目采购需求汇报（2026年11月版）.pptx";
  const download = vi.spyOn(libraryApi, "downloadDocument").mockResolvedValue();
  render(
    <PublicAnswer
      onFeedback={vi.fn()}
      onRetry={vi.fn()}
      turn={{
        id: "template",
        conversationId: "chat",
        question: "上会要讲采购，有现成的能改吗？",
        status: "completed",
        nativeProtocol: true,
        stageHistory: [],
        startedAt: now,
        stageStartedAt: now,
        lastSignalAt: now,
        claims: [],
        answer: "这是当前发布的原件，可以直接下载。",
        citations: [],
        partial: false,
        feedback: "idle",
        templateLookup: {
          file: {
            document_id: "pptx-1",
            filename,
            kind: "template",
            format: "PPTX",
          },
          topic: null,
        },
      }}
    />,
  );
  const card = screen.getByRole("region", { name: "文件直达" });
  expect(card).toHaveTextContent(filename);
  expect(card.nextElementSibling).toHaveTextContent("这是当前发布的原件");
  expect(screen.queryByRole("note")).toBeNull();
  await userEvent.click(within(card).getByRole("button", { name: "下载原件" }));
  expect(download).toHaveBeenCalledWith("pptx-1", filename);
});

it.each([
  {
    name: "检索零结果的实时回答",
    status: "streaming" as const,
    origin: "model_no_results" as const,
    count: 0,
    citation: false,
    notice: "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  },
  {
    name: "恢复的无引用历史回答",
    status: "completed" as const,
    origin: "model_unreferenced" as const,
    count: undefined,
    citation: false,
    notice: "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  },
  {
    name: "检索命中但没有引用的回答",
    status: "completed" as const,
    origin: "model_unreferenced" as const,
    count: 3,
    citation: false,
    notice: "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  },
  {
    name: "明确命中时不沿用旧的零结果标识",
    status: "completed" as const,
    origin: "model_no_results" as const,
    count: 3,
    citation: false,
    notice: "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  },
  {
    name: "正在生成且尚未收到引用时不猜测零结果",
    status: "streaming" as const,
    origin: "unknown" as const,
    count: undefined,
    citation: false,
    notice: undefined,
  },
  {
    name: "有检索候选但没有正文依据时仍提示引用待核对",
    status: "completed" as const,
    origin: "model_no_results" as const,
    count: 0,
    citation: true,
    notice: "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  },
  {
    name: "失败的部分回答不冒充完整模型回答",
    status: "failed" as const,
    origin: "model_no_results" as const,
    count: 0,
    citation: false,
    notice: undefined,
  },
])("$name", ({ status, origin, count, citation, notice }) => {
  const now = Date.now();
  const turn: PublicTurn = {
    id: "turn-origin",
    conversationId: "origin-test",
    question: "测试问题",
    status,
    answerOrigin: origin,
    nativeProtocol: true,
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    ...(status === "streaming"
      ? { provisionalAnswer: "这是一段回答。" }
      : { answer: "这是一段回答。" }),
    citations: citation ? [{ document_name: "制度.docx" }] : [],
    nativeEvents:
      count === undefined
        ? []
        : [
            {
              sequence: 1,
              responseType: "tool_result",
              payload: {
                data: { tool_name: "knowledge_search", success: true, count },
              },
            },
          ],
    partial: status === "failed",
    feedback: "idle",
  };
  const { container } = render(
    <PublicAnswer onFeedback={vi.fn()} onRetry={vi.fn()} turn={turn} />,
  );
  if (notice) {
    expect(screen.getByRole("note")).toHaveTextContent(notice);
  } else {
    expect(container.querySelector(".wst-answer-origin")).toBeNull();
  }
});

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
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
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
  expect(screen.getByText("检索完成 · 找到 2 篇参考资料")).toBeVisible();
  expect(screen.getByText("已完成问题理解")).toBeVisible();
  expect(screen.getByText("检索知识库 · 找到 6 个结果")).toBeVisible();
  expect(screen.getByText("研发项目").tagName).toBe("STRONG");
  expect(screen.queryByText("原生事件详情")).not.toBeInTheDocument();
  expect(screen.queryByText("不应显示的检索详情")).not.toBeInTheDocument();
  expect(screen.getByText("检索参考（3段）")).toBeInTheDocument();
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
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
  expect(container.querySelector("code")?.textContent).toContain(tag);
  expect(
    container.querySelector(".wst-final-answer")?.textContent,
  ).not.toContain(`${tag}${tag}`);
});

function completedTurn(answer: string): PublicTurn {
  const now = Date.now();
  return {
    id: "turn-reading",
    conversationId: "conversation-reading",
    traceId: "trace-reading",
    question: "查看操作图",
    status: "completed",
    stageHistory: [],
    startedAt: now,
    stageStartedAt: now,
    lastSignalAt: now,
    claims: [],
    answer,
    citations: [],
    partial: false,
    feedback: "idle",
  };
}

it.each([
  [
    "普通图片链接",
    "[![委托单示意图](resource://figures/guide.png)](https://example.test/guide)",
  ],
  [
    "带加粗的图片链接",
    "[**![委托单示意图](resource://figures/guide.png)**](https://example.test/guide)",
  ],
])("%s可键盘放大并复用kb前缀鉴权地址，不嵌套交互元素", async (_, markdown) => {
  const previous = window.location.pathname;
  window.history.replaceState({}, "", "/kb/");
  try {
    const user = userEvent.setup();
    const { container } = render(
      <div className="wst-root">
        <PublicAnswer
          onFeedback={vi.fn()}
          onRetry={vi.fn()}
          turn={completedTurn(markdown)}
        />
      </div>,
    );
    const trigger = screen.getByRole("button", {
      name: "放大图片：委托单示意图",
    });
    const inline = within(trigger).getByRole("img");
    expect(inline.getAttribute("src")).toBe(
      "/kb/api/public/conversations/conversation-reading/turns/trace-reading/resources?file_path=resource%3A%2F%2Ffigures%2Fguide.png",
    );
    expect(container.querySelector("a button")).toBeNull();
    trigger.focus();
    await user.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog", { name: "图片预览" });
    expect(within(dialog).getByRole("img")).toHaveAttribute(
      "src",
      inline.getAttribute("src"),
    );
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  } finally {
    window.history.replaceState({}, "", previous);
  }
});

it("答案把来源读取回调透传到阅读抽屉", async () => {
  const reader = vi.fn().mockResolvedValue("来自受保护接口的引用原文");
  const user = userEvent.setup();
  render(
    <PublicAnswer
      onFeedback={vi.fn()}
      onRetry={vi.fn()}
      onReadReference={reader}
      turn={{
        ...completedTurn("根据制度办理。"),
        citations: [
          {
            document_name: "现行制度",
            reference_id: "ref-1",
            source_available: true,
            source_kind: "weknora",
            quote: "应按制度办理",
          },
        ],
      }}
    />,
  );
  await user.click(screen.getByText("检索参考（1段）"));
  await user.click(screen.getByRole("button", { name: "阅读来源：现行制度" }));
  expect(await screen.findByText("来自受保护接口的引用原文")).toBeVisible();
  expect(reader).toHaveBeenCalledExactlyOnceWith("ref-1");
});

it("回答没有配图时，引用独有图片仍透传本条会话身份并可放大", async () => {
  const user = userEvent.setup();
  const reader = vi
    .fn()
    .mockResolvedValue("![引用独有示意图](resource://citation-only-figure)");
  render(
    <PublicAnswer
      onFeedback={vi.fn()}
      onRetry={vi.fn()}
      onReadReference={reader}
      turn={{
        ...completedTurn("请按指引操作。"),
        conversationId: "citation-session",
        traceId: "citation-trace",
        citations: [
          {
            document_name: "操作说明",
            reference_id: "source-reference",
            source_available: true,
            source_kind: "weknora",
          },
        ],
      }}
    />,
  );
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  await user.click(screen.getByText("检索参考（1段）"));
  await user.click(screen.getByRole("button", { name: "阅读来源：操作说明" }));
  const source = screen.getByRole("dialog", { name: "来源阅读：操作说明" });
  const thumb = await within(source).findByRole("img", {
    name: "引用独有示意图",
  });
  const url =
    "/api/public/conversations/citation-session/turns/citation-trace/resources?file_path=resource%3A%2F%2Fcitation-only-figure";
  expect(thumb).toHaveAttribute("src", url);
  await user.click(
    within(source).getByRole("button", { name: "放大图片：引用独有示意图" }),
  );
  expect(
    within(screen.getByRole("dialog", { name: "图片预览" })).getByRole("img"),
  ).toHaveAttribute("src", url);
  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog")).toBe(source);
  expect(reader).toHaveBeenCalledExactlyOnceWith("source-reference");
});

it("完成答案的静态渲染不挂载浮层或启动原文读取", () => {
  const reader = vi.fn().mockResolvedValue("不应读取");
  const html = renderToString(
    <PublicAnswer
      onFeedback={vi.fn()}
      onRetry={vi.fn()}
      onReadReference={reader}
      turn={completedTurn("![示意图](resource://figures/guide.png)")}
    />,
  );
  expect(html).toContain("放大图片");
  expect(html).not.toContain('role="dialog"');
  expect(reader).not.toHaveBeenCalled();
});
