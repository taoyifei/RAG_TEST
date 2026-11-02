import {
  AlignmentType,
  BorderStyle,
  Document,
  ExternalHyperlink,
  HeadingLevel,
  LevelFormat,
  Packer,
  Paragraph,
  Table,
  TableCell,
  TableLayoutType,
  TableRow,
  TextRun,
  WidthType,
} from "docx";
import type { ILevelsOptions, IParagraphOptions, ParagraphChild } from "docx";

import { buildAnswerExport } from "./answerExport";
import type { AnswerExportBlock, AnswerExportRun } from "./answerExport";
import type { PublicTurn } from "./usePublicChat";

const FONT = {
  ascii: "Calibri",
  hAnsi: "Calibri",
  eastAsia: "宋体",
  cs: "Calibri",
};
const CONTENT_WIDTH = 9638;
const HEADINGS = [
  HeadingLevel.HEADING_1,
  HeadingLevel.HEADING_2,
  HeadingLevel.HEADING_3,
  HeadingLevel.HEADING_4,
  HeadingLevel.HEADING_5,
  HeadingLevel.HEADING_6,
];

function runs(
  values: readonly AnswerExportRun[],
  header = false,
): ParagraphChild[] {
  return values.flatMap<ParagraphChild>((value) => {
    const children = value.text.split("\n").map(
      (text, index) =>
        new TextRun({
          text,
          break: index ? 1 : undefined,
          bold: header || value.bold,
          italics: value.italic,
          strike: value.strike,
          color: "000000",
          font: value.code
            ? { ...FONT, ascii: "Consolas", hAnsi: "Consolas" }
            : FONT,
        }),
    );
    return value.href
      ? [new ExternalHyperlink({ link: value.href, children })]
      : children;
  });
}

function tableFor(block: Extract<AnswerExportBlock, { type: "table" }>): Table {
  const columns = Math.max(1, ...block.rows.map((row) => row.length));
  const weights = Array.from({ length: columns }, (_, column) =>
    Math.max(
      6,
      Math.min(
        30,
        Math.max(
          ...block.rows.map((row) =>
            (row[column] ?? []).reduce(
              (count, run) => count + run.text.length,
              0,
            ),
          ),
        ),
      ),
    ),
  );
  const sum = weights.reduce((total, weight) => total + weight, 0);
  const widths = weights.map((weight) =>
    Math.floor((CONTENT_WIDTH * weight) / sum),
  );
  widths[widths.length - 1] +=
    CONTENT_WIDTH - widths.reduce((total, width) => total + width, 0);
  const border = { style: BorderStyle.SINGLE, size: 4, color: "C7CDD2" };
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: widths,
    layout: TableLayoutType.FIXED,
    borders: {
      top: border,
      bottom: border,
      left: border,
      right: border,
      insideHorizontal: border,
      insideVertical: border,
    },
    rows: block.rows.map(
      (row, index) =>
        new TableRow({
          tableHeader: index === 0 ? true : undefined,
          cantSplit: false,
          children: Array.from(
            { length: columns },
            (_, column) =>
              new TableCell({
                width: { size: widths[column], type: WidthType.DXA },
                shading: index === 0 ? { fill: "E9EEF2" } : undefined,
                margins: { top: 100, bottom: 100, left: 110, right: 110 },
                children: [
                  new Paragraph({
                    children: runs(row[column] ?? [], index === 0),
                    spacing: { after: 70, line: 300 },
                  }),
                ],
              }),
          ),
        }),
    ),
  });
}

/** 标准OOXML保留原生标题、列表及可编辑表格，不进行网络请求。 */
export function createAnswerWordDocument(
  turn: PublicTurn,
  exportedAt = new Date(),
): Document {
  const content = buildAnswerExport(turn);
  const numbering: { reference: string; levels: ILevelsOptions[] }[] = [];
  let listNumber = 0;

  function paragraphFor(
    block: Exclude<AnswerExportBlock, { type: "list" | "table" }>,
    options: Partial<IParagraphOptions> = {},
  ): Paragraph {
    if (block.type === "separator")
      return new Paragraph({ spacing: { after: 80 } });
    if (block.type === "code")
      return new Paragraph({
        children: runs([{ text: block.text, code: true }]),
        shading: { fill: "F5F6F7" },
        spacing: { after: 140, line: 280 },
        ...options,
      });
    return new Paragraph({
      children: runs(block.runs),
      heading:
        block.type === "heading"
          ? HEADINGS[Math.min(5, Math.max(0, block.level - 1))]
          : undefined,
      indent:
        block.type === "paragraph" && block.quote ? { left: 360 } : undefined,
      ...options,
    });
  }

  function renderBlocks(
    blocks: readonly AnswerExportBlock[],
    depth = 0,
  ): (Paragraph | Table)[] {
    return blocks.flatMap((block): (Paragraph | Table)[] => {
      if (block.type === "table")
        return [tableFor(block), new Paragraph({ spacing: { after: 80 } })];
      if (block.type !== "list") return [paragraphFor(block)];
      const reference = `answer-list-${++listNumber}`;
      const level = Math.min(depth, 8);
      numbering.push({
        reference,
        levels: Array.from({ length: 9 }, (_, index) => ({
          level: index,
          format: block.ordered ? LevelFormat.DECIMAL : LevelFormat.BULLET,
          text: block.ordered ? `%${index + 1}.` : "•",
          start: Math.max(0, block.start),
          alignment: AlignmentType.LEFT,
          style: {
            run: { font: FONT },
            paragraph: { indent: { left: 420 * (index + 1), hanging: 300 } },
          },
        })),
      });
      return block.items.flatMap((items) => {
        const [first, ...rest] = items;
        const options = {
          numbering: { reference, level },
          spacing: { after: 100, line: 340 },
        };
        if (first && first.type !== "list" && first.type !== "table") {
          return [
            paragraphFor(first, options),
            ...renderBlocks(rest, depth + 1),
          ];
        }
        return [
          new Paragraph({ ...options }),
          ...renderBlocks(items, depth + 1),
        ];
      });
    });
  }

  const body = renderBlocks(content.blocks);
  const children: (Paragraph | Table)[] = [
    new Paragraph({ text: "湾小度问答记录", heading: HeadingLevel.TITLE }),
    new Paragraph({
      children: [
        new TextRun({ text: "问题：", bold: true }),
        new TextRun(content.question),
      ],
      spacing: { before: 160, after: 140 },
    }),
    new Paragraph({
      text: `提问时间：${content.askedAt}`,
      style: "ExportMetadata",
    }),
    new Paragraph({
      text: `资料范围：${content.scopes.join("、")}`,
      style: "ExportMetadata",
    }),
    new Paragraph({
      text: `导出时间：${new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: "Asia/Shanghai",
      }).format(exportedAt)}（北京时间）`,
      style: "ExportMetadata",
    }),
  ];
  if (content.containsImages)
    children.push(
      new Paragraph({
        text: "图片说明：本文保留图示名称，图片请在湾小度原回答中查看。",
        style: "ExportMetadata",
      }),
    );
  children.push(
    new Paragraph({ text: "回答", heading: HeadingLevel.HEADING_1 }),
    ...body,
  );
  if (content.truncated)
    children.push(new Paragraph({ text: "说明：知识库提示本次回答已截断。" }));
  children.push(
    new Paragraph({ text: "参考来源", heading: HeadingLevel.HEADING_1 }),
  );
  if (!content.references.length)
    children.push(new Paragraph("本次回答未附参考来源。"));
  for (const reference of content.references) {
    children.push(
      new Paragraph({
        children: [
          new TextRun({
            text: `〔${reference.number}〕${reference.title}`,
            bold: true,
          }),
        ],
        keepNext: Boolean(reference.detail),
        spacing: { before: 100, after: 60 },
      }),
    );
    if (reference.detail)
      children.push(
        new Paragraph({ text: reference.detail, style: "ExportMetadata" }),
      );
    if (reference.unavailable)
      children.push(
        new Paragraph({ text: "原文暂不可用。", style: "ExportMetadata" }),
      );
  }
  if (content.references.length)
    children.push(
      new Paragraph({
        text: "完整引用原文可在湾小度的“引用依据”中查看。",
        style: "ExportMetadata",
      }),
    );
  return new Document({
    creator: "湾小度",
    lastModifiedBy: "湾小度",
    title: content.question,
    subject: "知识问答记录",
    description: "问答正文与参考来源",
    styles: {
      default: {
        document: {
          run: { font: FONT, size: 24, color: "000000" },
          paragraph: { spacing: { after: 140, line: 360 } },
        },
        title: {
          run: { font: FONT, size: 36, bold: true, color: "000000" },
          paragraph: { spacing: { after: 180 }, keepNext: true },
        },
        heading1: {
          run: { font: FONT, size: 29, bold: true, color: "000000" },
          paragraph: { spacing: { before: 240, after: 120 }, keepNext: true },
        },
        heading2: {
          run: { font: FONT, size: 26, bold: true, color: "000000" },
          paragraph: { spacing: { before: 200, after: 100 }, keepNext: true },
        },
        heading3: {
          run: { font: FONT, size: 24, bold: true, color: "000000" },
          paragraph: { spacing: { before: 160, after: 80 }, keepNext: true },
        },
        heading4: {
          run: { font: FONT, size: 24, bold: true, color: "000000" },
        },
        heading5: {
          run: { font: FONT, size: 24, bold: true, color: "000000" },
        },
        heading6: {
          run: { font: FONT, size: 24, bold: true, color: "000000" },
        },
      },
      paragraphStyles: [
        {
          id: "ExportMetadata",
          name: "记录信息",
          basedOn: "Normal",
          run: { font: FONT, size: 22, color: "424A51" },
          paragraph: { spacing: { after: 60, line: 300 } },
        },
      ],
    },
    numbering: { config: numbering },
    sections: [
      {
        properties: {
          page: {
            size: { width: 11906, height: 16838 },
            margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 },
          },
        },
        children,
      },
    ],
  });
}

export async function answerWordBlob(turn: PublicTurn): Promise<Blob> {
  return Packer.toBlob(createAnswerWordDocument(turn));
}
