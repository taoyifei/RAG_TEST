import { describe, expect, it } from "vitest";

import {
  answerClipboardText,
  answerWordFilename,
  buildAnswerExport,
  canExportAnswer,
} from "./answerExport";
import type { PublicTurn } from "./usePublicChat";

function completedTurn(): PublicTurn {
  return {
    id: "turn-private",
    conversationId: "conversation-private",
    traceId: "trace-private",
    question: "线上委托单怎样申请？",
    status: "completed",
    stageHistory: [],
    startedAt: Date.parse("2026-11-01T08:00:00Z"),
    stageStartedAt: 0,
    lastSignalAt: 0,
    claims: [],
    partial: false,
    feedback: "idle",
    searchScopes: ["public", "personal"],
    answer:
      '# 新建委托单\n\n进入**供应链管理**。<kb doc="操作指引" chunk_id="chunk-private"/>\n\n3. 填写需求\n4. 提交审批\n\n| 项目 | 入口 |\n| --- | --- |\n| 委托单 | 订单管理 |\n\n![菜单示意](resource://private-files/screen.png)\n\n[系统入口](https://rdms.example/index)和[内部资源](https://example.com/kb/api/private?token=do-not-share)。',
    citations: [
      {
        document_name: "操作指引.docx",
        document_title: "线上委托单操作指引",
        native_chunk_id: "chunk-private",
        reference_id: "reference-private",
        native_knowledge_id: "knowledge-private",
        locator: "第2页",
        quote: "供应链管理 → 订单管理 → 新增。",
      },
    ],
  };
}

describe("完成回答导出模型", () => {
  it("复制可见中文内容、列表和表格，保留来源编号，不导出Markdown标记或私有句柄", () => {
    const turn = completedTurn();
    const text = answerClipboardText(turn, true);
    expect(text).toContain("进入供应链管理。〔1〕");
    expect(text).toContain("3、填写需求");
    expect(text).toContain("4、提交审批");
    expect(text).toContain("项目\t入口\n委托单\t订单管理");
    expect(text).toContain("图片：菜单示意（请在湾小度原回答中查看）");
    expect(text).toContain("系统入口（https://rdms.example/index）");
    expect(text).toContain("〔1〕线上委托单操作指引");
    expect(text).toContain("第2页");
    expect(text).not.toContain("供应链管理 → 订单管理 → 新增。");
    expect(text).toContain("完整引用原文可在湾小度的“引用依据”中查看。");
    expect(text).toContain("提问时间：2026/11/01 16:00（北京时间）");
    expect(text).toContain("资料范围：公共知识库、我的资料");
    expect(text).not.toMatch(
      /resource:\/\/|private-files|token=|chunk-private|trace-private|knowledge-private|reference-private|<kb|\*\*|^# /m,
    );
    expect(answerClipboardText(turn)).not.toContain("参考来源");
  });

  it("两个片段即使同文件也保留原始编号，不把出处重排为文档序号", () => {
    const turn = completedTurn();
    turn.citations.push({
      document_name: "操作指引.docx",
      native_chunk_id: "second-chunk",
      quote: "补充说明。",
    });
    turn.answer =
      '<kb chunk_id="second-chunk"/>之后是<kb chunk_id="chunk-private"/>';
    const text = answerClipboardText(turn, true);
    expect(text).toContain("〔2〕之后是〔1〕");
    expect(text).toContain("〔2〕操作指引.docx");
  });

  it("旧记录没有范围和时间时明确说明，不把当前默认范围或当前时间当原始信息", () => {
    const turn = completedTurn();
    turn.searchScopes = undefined;
    turn.startedAt = Number.NaN;
    const content = buildAnswerExport(turn);
    expect(content.scopes).toEqual(["本次记录未保存资料范围"]);
    expect(content.askedAt).toBe("未记录");
  });

  it("截断状态保留说明；文件名为真正docx且清除文件系统保留符号", () => {
    const turn = completedTurn();
    turn.truncated = true;
    turn.question = "资料/审批：怎么申请？";
    expect(answerClipboardText(turn)).toContain("本次回答已截断");
    expect(answerWordFilename(turn)).toMatch(
      /^湾小度回答_[^/\\:*?"<>|]+\.docx$/,
    );
  });

  it.each([
    "streaming",
    "failed",
    "cancelled",
    "pending_confirmation",
  ] as const)("%s不能导出暂存正文", (status) => {
    const turn = { ...completedTurn(), status };
    expect(canExportAnswer(turn)).toBe(false);
    expect(() => buildAnswerExport(turn)).toThrow("尚无可导出的最终回答");
  });

  it("completed仍缺final正文或只有partial时不当成最终答案", () => {
    expect(
      canExportAnswer({
        ...completedTurn(),
        answer: undefined,
        provisionalAnswer: "仅暂存文本",
      }),
    ).toBe(false);
    expect(canExportAnswer({ ...completedTurn(), partial: true })).toBe(false);
  });
});
