import { fireEvent, render, screen } from "@testing-library/react";
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

it("来源澄清显示明确解除操作，只有用户点击才触发", () => {
  const release = vi.fn();
  const now = Date.now();
  render(
    <PublicAnswer
      onFeedback={vi.fn()}
      onReleaseSource={release}
      onRetry={vi.fn()}
      turn={{
        id: "turn-source",
        conversationId: "wst-test",
        question: "《规范》是什么？",
        status: "completed",
        publicationStatus: "SOURCE_CLARIFICATION",
        stageHistory: [],
        startedAt: now,
        stageStartedAt: now,
        lastSignalAt: now,
        claims: [],
        answer: "请明确要查询的资料或对象后重试。",
        citations: [],
        partial: false,
        feedback: "idle",
      }}
    />,
  );
  expect(screen.getByText(/准确文件名/)).toBeInTheDocument();
  expect(release).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "解除来源限定，搜索本知识库" }));
  expect(release).toHaveBeenCalledTimes(1);
});
