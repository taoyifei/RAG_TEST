import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { answerEvidence, answerOriginNotice } from "./answerEvidence";
import { answerClipboardText } from "./answerExport";
import { PublicAnswer } from "./PublicAnswer";
import type { PublicTurn } from "./usePublicChat";

const citation = {
  document_name: "园区服务手册.docx",
  native_chunk_id: "chunk-a",
  native_reference: { knowledge_base_id: "kb-a", sub_chunk_id: ["child-a"] },
  quote: "员工食堂在服务中心。",
};
const turn: PublicTurn = {
  id: "t",
  conversationId: "c",
  question: "我家在哪里",
  status: "completed",
  nativeProtocol: true,
  answerOrigin: "model_unreferenced",
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
    "未引用知识库资料，以下内容由大模型生成，仅供参考。",
  );
  expect(screen.getByText("检索参考（1段）")).toBeInTheDocument();
  expect(screen.queryByText("引用依据（1段）")).not.toBeInTheDocument();
});

it.each([
  ['实际引用<kb chunk_id="chunk-a" kb_id="kb-a"/>', 1],
  ['<kb chunk_id="other"/>', 0],
  ['<kb chunk_id="child-a" kb_id="kb-a"/>', 1],
  ['<kb chunk_id="chunk-a" kb_id="other-kb"/>', 0],
  ['<kb doc="园区服务手册.docx"/>', 0],
  ['`<kb chunk_id="chunk-a"/>`', 0],
  ['```xml\n<kb chunk_id="chunk-a"/>\n```', 0],
])("正文引用必须与本轮片段对应：%s", (answer, count) => {
  expect(
    answerEvidence({ ...turn, answerOrigin: "knowledge", answer }),
  ).toHaveLength(count);
});

it("使用真实引用时不被旧的来源标记覆盖", () => {
  const value = { ...turn, answer: '食堂在服务中心。<kb chunk_id="chunk-a"/>' };
  expect(answerEvidence(value)).toEqual([citation]);
  expect(answerOriginNotice(value)).toBeUndefined();
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
  expect(text).toContain("未引用知识库资料，以下内容由大模型生成，仅供参考。");
  expect(text).not.toContain("园区服务手册.docx");
});

it("实际展示的知识库原图也属于引用，单独提到图片地址不算", () => {
  const picture = { ...citation, quote: "![原图](resource://source-picture)" };
  const value = {
    ...turn,
    citations: [picture],
    answer: "![原图](resource://source-picture)",
  };
  expect(answerEvidence(value)).toEqual([picture]);
  expect(answerOriginNotice(value)).toBeUndefined();
  expect(
    answerEvidence({ ...value, answer: "地址resource://source-picture" }),
  ).toEqual([]);
});
