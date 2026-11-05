import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { answerOriginNotice } from "./answerEvidence";
import { answerClipboardText } from "./answerExport";
import { MODEL_GENERATED_NOTICE } from "./nativeCitationDisplay";
import { PublicAnswer } from "./PublicAnswer";
import type { PublicTurn } from "./usePublicChat";

const turn: PublicTurn = {
  id: "t",
  conversationId: "c",
  question: "测试问题",
  status: "completed",
  nativeProtocol: true,
  answerOrigin: "knowledge",
  answer: "食堂位于服务中心A座1楼。\n\n来源：知识库",
  startedAt: 1,
  stageStartedAt: 1,
  lastSignalAt: 1,
  stageHistory: [],
  claims: [],
  citations: [
    { document_name: "服务手册.docx", quote: "食堂位于服务中心A座1楼。" },
  ],
  partial: false,
  feedback: "idle",
};

it("正常业务回答没有正文引用标号时也不误加模型生成提示", () => {
  expect(answerOriginNotice(turn)).toBeUndefined();
  render(<PublicAnswer turn={turn} onFeedback={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
  expect(screen.queryByText("来源：知识库")).not.toBeInTheDocument();
  expect(
    screen.getByText("食堂位于服务中心A座1楼。", {
      selector: ".wst-final-answer p",
    }),
  ).toBeInTheDocument();
});

it("明确无依据时统一显示一次提示，并保留检索参考及下载入口", () => {
  const value: PublicTurn = {
    ...turn,
    answerOrigin: "model_unreferenced",
    answer: "当前资料没有提供所问的信息。\n\n来源：模型生成",
  };
  render(<PublicAnswer turn={value} onFeedback={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.getByRole("note")).toHaveTextContent(MODEL_GENERATED_NOTICE);
  expect(screen.getAllByText(MODEL_GENERATED_NOTICE)).toHaveLength(1);
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
  expect(screen.getByText("当前资料没有提供所问的信息。")).toBeInTheDocument();
  expect(screen.queryByText("来源：模型生成")).not.toBeInTheDocument();
});

it("复制模型答复保留提示一次，不附上无关检索候选作为答复来源", () => {
  const value: PublicTurn = {
    ...turn,
    answerOrigin: "model_unreferenced",
    answer: "这是一句友好的答复。\n\n来源：模型生成",
  };
  const text = answerClipboardText(value, true);
  expect(text.split(MODEL_GENERATED_NOTICE)).toHaveLength(2);
  expect(text).not.toContain("服务手册.docx");
  expect(text).not.toContain("来源：模型生成");
});
