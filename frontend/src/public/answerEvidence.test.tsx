import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { answerEvidence, answerOriginNotice } from "./answerEvidence";
import { answerClipboardText } from "./answerExport";
import { PublicAnswer } from "./PublicAnswer";
import type { PublicTurn } from "./usePublicChat";

const citation = {
  document_name: "园区服务手册.docx",
  native_chunk_id: "chunk-a",
  native_reference: { knowledge_base_id: "kb-a" },
  quote: "员工食堂在服务中心。",
};
const turn: PublicTurn = {
  id: "t",
  conversationId: "c",
  question: "我家在哪里",
  status: "completed",
  nativeProtocol: true,
  answerOrigin: "model_insufficient",
  answer: "资料未提供你的家庭地址。",
  startedAt: 1,
  stageStartedAt: 1,
  lastSignalAt: 1,
  stageHistory: [],
  claims: [],
  citations: [citation],
  partial: false,
  feedback: "idle",
};

it("有检索候选但资料不足时提示，并把候选放在检索参考里", () => {
  render(<PublicAnswer turn={turn} onFeedback={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.getByRole("note")).toHaveTextContent(
    "未找到能直接支持本次回答的依据",
  );
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
  expect(screen.queryByText("引用依据（1段）")).not.toBeInTheDocument();
});

it.each([
  ['实际引用<kb chunk_id="chunk-a" kb_id="kb-a"/>', 1],
  ['<kb chunk_id="other"/>', 0],
  ['<kb chunk_id="chunk-a" kb_id="other-kb"/>', 0],
  ['<kb doc="园区服务手册.docx"/>', 0],
  ['`<kb chunk_id="chunk-a"/>`', 0],
  ['```xml\n<kb chunk_id="chunk-a"/>\n```', 0],
])("正文引用必须与本轮片段对应：%s", (answer, count) => {
  expect(
    answerEvidence({ ...turn, answerOrigin: "knowledge", answer }),
  ).toHaveLength(count);
});

it("明确资料不足时，即使正文附了引用也不能冒充充分依据", () => {
  const value = { ...turn, answer: '没有地址。<kb chunk_id="chunk-a"/>' };
  expect(answerEvidence(value)).toEqual([]);
  expect(answerOriginNotice(value)).toContain("未找到能直接支持");
});

it("充分依据显示引用；其他检索候选仍单独保留", () => {
  const value: PublicTurn = {
    ...turn,
    answerOrigin: "knowledge",
    answer: '食堂在服务中心。<kb chunk_id="chunk-a"/>',
    citations: [
      citation,
      { document_name: "发票指引.docx", native_chunk_id: "chunk-b" },
    ],
  };
  render(<PublicAnswer turn={value} onFeedback={vi.fn()} onRetry={vi.fn()} />);
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
  expect(screen.getByText("引用依据（1段）")).toBeInTheDocument();
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
});

it("复制含来源时保留提示，不把无关检索结果列为回答来源", () => {
  const text = answerClipboardText(turn, true);
  expect(text).toContain("未找到能直接支持本次回答的依据");
  expect(text).not.toContain("园区服务手册.docx");
});
