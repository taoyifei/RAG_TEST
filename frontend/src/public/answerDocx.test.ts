import { Packer } from "docx";
import JSZip from "jszip";
import { expect, it } from "vitest";

import { createAnswerWordDocument } from "./answerDocx";
import type { PublicTurn } from "./usePublicChat";

it("真正OOXML包保留中文原生标题、列表、可编辑表格和编号来源，不带私有URL或外部图片关系", async () => {
  const turn: PublicTurn = {
    id: "private-turn",
    conversationId: "private-session",
    traceId: "private-trace",
    question: "我要新建一张线上委托单，应该进入哪个菜单？",
    status: "completed",
    stageHistory: [],
    startedAt: Date.parse("2026-11-01T08:00:00Z"),
    stageStartedAt: 0,
    lastSignalAt: 0,
    claims: [],
    partial: false,
    feedback: "idle",
    searchScopes: ["public"],
    answer:
      '# 办理步骤\n\n进入**供应链管理 → 订单管理 → 新增**。<kb chunk_id="private-chunk"/>\n\n1. 填写申请信息\n2. 上传附件\n\n| 场景 | 入口 |\n| --- | --- |\n| 线上委托单 | 订单管理 |\n\n![菜单示意图](resource://private-images/screen.png)',
    citations: [
      {
        document_name: "线上委托单操作指引.docx",
        native_chunk_id: "private-chunk",
        reference_id: "private-reference",
        quote: "选择订单管理，点击新增。",
        locator: "第2页",
      },
    ],
  };
  const bytes = await Packer.toBuffer(
    createAnswerWordDocument(turn, new Date("2026-11-02T08:00:00Z")),
  );
  const zip = await JSZip.loadAsync(bytes);
  const xml = await zip.file("word/document.xml")?.async("string");
  expect(xml).toBeTruthy();
  const doc = new DOMParser().parseFromString(xml ?? "", "application/xml");
  const ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
  expect(doc.getElementsByTagName("parsererror")).toHaveLength(0);
  expect(doc.getElementsByTagNameNS(ns, "tbl")).toHaveLength(1);
  expect(doc.getElementsByTagNameNS(ns, "tr")).toHaveLength(2);
  expect(doc.getElementsByTagNameNS(ns, "tc")).toHaveLength(4);
  expect(doc.getElementsByTagNameNS(ns, "tblHeader")).toHaveLength(1);
  expect(doc.getElementsByTagNameNS(ns, "numPr")).toHaveLength(2);
  expect(xml).toContain('w:val="Heading1"');
  expect(xml).toContain('w:val="Title"');
  expect(doc.documentElement.textContent).toContain(
    "供应链管理 → 订单管理 → 新增",
  );
  expect(doc.documentElement.textContent).toContain(
    "〔1〕线上委托单操作指引.docx",
  );
  expect(doc.documentElement.textContent).toContain("第2页");
  expect(doc.documentElement.textContent).not.toContain(
    "选择订单管理，点击新增。",
  );
  expect(doc.documentElement.textContent).toContain(
    "完整引用原文可在湾小度的“引用依据”中查看。",
  );
  expect(doc.documentElement.textContent).toContain(
    "提问时间：2026/11/01 16:00",
  );
  expect(doc.documentElement.textContent).toContain(
    "导出时间：2026/11/02 16:00",
  );
  expect(xml).toContain('w:w="11906"');
  expect(xml).not.toMatch(
    /resource:\/\/|private-chunk|private-trace|private-reference|private-images|\*\*|# 办理/,
  );
  const relationships = await zip
    .file("word/_rels/document.xml.rels")
    ?.async("string");
  expect(relationships).not.toContain('/image"');
  expect(zip.file("[Content_Types].xml")).not.toBeNull();
});
