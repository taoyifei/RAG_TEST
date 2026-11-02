import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AnswerActions } from "./AnswerActions";
import { PublicApiError } from "./publicApi";
import type { PublicTurn } from "./usePublicChat";

function turn(overrides: Partial<PublicTurn> = {}): PublicTurn {
  return {
    id: "turn",
    conversationId: "conversation",
    question: "怎么申请？",
    status: "completed",
    stageHistory: [],
    startedAt: 0,
    stageStartedAt: 0,
    lastSignalAt: 0,
    claims: [],
    citations: [{ document_name: "办理指引.docx", quote: "办理原文。" }],
    answer: "**先填写申请**。",
    partial: false,
    feedback: "idle",
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it.each([
  { status: "streaming" },
  { status: "failed" },
  { partial: true },
  { answer: "" },
] as Partial<PublicTurn>[])("未完成最终回答时没有回答操作 %j", (overrides) => {
  render(<AnswerActions turn={turn(overrides)} />);
  expect(
    screen.queryByRole("button", { name: "导出Word" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "复制回答" }),
  ).not.toBeInTheDocument();
});

it("完成回答只保留两种复制操作，不再提供Word导出", () => {
  render(<AnswerActions turn={turn()} />);
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "复制回答" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "复制含来源" })).toBeEnabled();
  expect(
    screen.queryByRole("button", { name: "导出Word" }),
  ).not.toBeInTheDocument();
});

it("复制前先等待权限复核，复核中不允许重复点击", async () => {
  let release: (() => void) | undefined;
  const beforeExport = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        release = resolve;
      }),
  );
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<AnswerActions turn={turn()} beforeExport={beforeExport} />);
  fireEvent.click(screen.getByRole("button", { name: "复制回答" }));
  expect(beforeExport).toHaveBeenCalledOnce();
  expect(writeText).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "正在复制…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "复制含来源" })).toBeDisabled();
  release?.();
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("先填写申请。"));
  expect(await screen.findByRole("status")).toHaveTextContent("已复制回答");
});

it.each(["复制回答", "复制含来源"])(
  "%s遇到权限撤回时不执行复制",
  async (label) => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(
      <AnswerActions
        turn={turn()}
        beforeExport={() =>
          Promise.reject(
            new PublicApiError("内部信息不应展示", { status: 403 }),
          )
        }
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "资料权限已变化",
    );
    expect(writeText).not.toHaveBeenCalled();
    expect(screen.queryByText("内部信息不应展示")).not.toBeInTheDocument();
  },
);

it("HTTP环境没有Clipboard API时采用本地复制并清理临时输入框", async () => {
  vi.stubGlobal("navigator", { clipboard: undefined });
  const previous = Object.getOwnPropertyDescriptor(document, "execCommand");
  const copy = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: copy,
  });
  try {
    render(<AnswerActions turn={turn()} />);
    fireEvent.click(screen.getByRole("button", { name: "复制含来源" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "已复制回答和来源",
    );
    expect(copy).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
  } finally {
    if (previous) Object.defineProperty(document, "execCommand", previous);
    else Reflect.deleteProperty(document, "execCommand");
  }
});
