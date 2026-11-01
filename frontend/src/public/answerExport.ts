import type { PhrasingContent, RootContent } from "mdast";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

import { nativeCitationDisplay } from "./nativeCitationDisplay";
import type { PublicSearchScope } from "./publicApi";
import type { PublicTurn } from "./usePublicChat";

export interface AnswerExportRun {
  text: string;
  bold?: boolean;
  italic?: boolean;
  strike?: boolean;
  code?: boolean;
  href?: string;
}

export type AnswerExportBlock =
  | { type: "paragraph"; runs: AnswerExportRun[]; quote?: boolean }
  | { type: "heading"; level: number; runs: AnswerExportRun[] }
  | { type: "code"; text: string }
  | {
      type: "list";
      ordered: boolean;
      start: number;
      items: AnswerExportBlock[][];
    }
  | { type: "table"; rows: AnswerExportRun[][][] }
  | { type: "separator" };

export interface AnswerExportReference {
  number: number;
  title: string;
  detail: string;
  quote: string;
  unavailable: boolean;
}

export interface AnswerExportContent {
  question: string;
  askedAt: string;
  scopes: string[];
  blocks: AnswerExportBlock[];
  references: AnswerExportReference[];
  containsImages: boolean;
  truncated: boolean;
}

const SCOPE_LABELS: Record<PublicSearchScope, string> = {
  public: "公共知识库",
  hr_public: "公开人力制度",
  personal: "我的资料",
  hr_internal: "人力内部资料",
};
const parser = unified().use(remarkParse).use(remarkGfm);
const PRIVATE_RESOURCE =
  /(?:resource|privatefile|file|blob|data):\/\/[^\s<>\])（），。；、]+/gi;

/** 导出只保留可见内容，不把鉴权资源句柄变成可分享链接。 */
function visibleText(value: string): string {
  return value
    .replace(PRIVATE_RESOURCE, "（资源请在湾小度中查看）")
    .replaceAll(String.fromCharCode(0), "");
}

function publicLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (!["http:", "https:", "mailto:"].includes(url.protocol))
      return undefined;
    if (/\/(?:kb\/)?(?:api|files|uploads|storage)\//i.test(url.pathname)) {
      return undefined;
    }
    if (
      [...url.searchParams.keys()].some((key) =>
        /token|secret|credential|signature|file_path|trace_id|reference_id/i.test(
          key,
        ),
      )
    ) {
      return undefined;
    }
    return value;
  } catch {
    // 相对路径不能脱离当前登录态单独对外传播。
    return undefined;
  }
}

function inlineRuns(
  nodes: readonly PhrasingContent[],
  format: Omit<AnswerExportRun, "text" | "href"> = {},
): AnswerExportRun[] {
  return nodes.flatMap((node): AnswerExportRun[] => {
    switch (node.type) {
      case "text":
        return [{ ...format, text: visibleText(node.value) }];
      case "strong":
        return inlineRuns(node.children, { ...format, bold: true });
      case "emphasis":
        return inlineRuns(node.children, { ...format, italic: true });
      case "delete":
        return inlineRuns(node.children, { ...format, strike: true });
      case "inlineCode":
        return [{ ...format, code: true, text: visibleText(node.value) }];
      case "break":
        return [{ ...format, text: "\n" }];
      case "image":
      case "imageReference":
        return [
          {
            ...format,
            text: `图片：${visibleText(node.alt || "图示")}（请在湾小度原回答中查看）`,
          },
        ];
      case "link": {
        const href = publicLink(node.url);
        return inlineRuns(node.children, format).map((run) => ({
          ...run,
          href,
        }));
      }
      case "linkReference":
        return inlineRuns(node.children, format);
      case "footnoteReference":
        return [{ ...format, text: `〔注${visibleText(node.identifier)}〕` }];
      case "html":
        return [
          {
            ...format,
            text: /<br\s*\/?>/i.test(node.value)
              ? "\n"
              : visibleText(node.value.replace(/<[^>]*>/g, "")),
          },
        ];
      default:
        return [];
    }
  });
}

function blocksFromNodes(nodes: readonly RootContent[]): AnswerExportBlock[] {
  return nodes.flatMap((node): AnswerExportBlock[] => {
    switch (node.type) {
      case "paragraph":
        return [{ type: "paragraph", runs: inlineRuns(node.children) }];
      case "heading":
        return [
          {
            type: "heading",
            level: node.depth,
            runs: inlineRuns(node.children),
          },
        ];
      case "code":
        return [{ type: "code", text: visibleText(node.value) }];
      case "thematicBreak":
        return [{ type: "separator" }];
      case "blockquote":
        return blocksFromNodes(node.children).map((block) =>
          block.type === "paragraph" ? { ...block, quote: true } : block,
        );
      case "list":
        return [
          {
            type: "list",
            ordered: node.ordered === true,
            start: node.start ?? 1,
            items: node.children.map((item) => {
              const children = blocksFromNodes(item.children);
              if (item.checked !== null && item.checked !== undefined) {
                const first = children[0];
                if (first?.type === "paragraph") {
                  first.runs.unshift({ text: item.checked ? "☑ " : "☐ " });
                }
              }
              return children;
            }),
          },
        ];
      case "table":
        return [
          {
            type: "table",
            rows: node.children.map((row) =>
              row.children.map((cell) => inlineRuns(cell.children)),
            ),
          },
        ];
      case "footnoteDefinition":
        return [
          {
            type: "paragraph",
            runs: [{ text: `注${visibleText(node.identifier)}` }],
          },
          ...blocksFromNodes(node.children),
        ];
      case "html": {
        const text = visibleText(node.value.replace(/<[^>]*>/g, "")).trim();
        return text ? [{ type: "paragraph", runs: [{ text }] }] : [];
      }
      default:
        return [];
    }
  });
}

function runText(runs: readonly AnswerExportRun[]): string {
  return runs
    .map(
      (run) =>
        run.text + (run.href && run.href !== run.text ? `（${run.href}）` : ""),
    )
    .join("");
}

function blockText(block: AnswerExportBlock, level = 0): string {
  switch (block.type) {
    case "paragraph":
    case "heading":
      return runText(block.runs);
    case "code":
      return block.text;
    case "separator":
      return "";
    case "table":
      return block.rows.map((row) => row.map(runText).join("\t")).join("\n");
    case "list":
      return block.items
        .map((items, index) => {
          const prefix = block.ordered ? `${block.start + index}、` : "• ";
          return (
            "  ".repeat(level) +
            prefix +
            items.map((item) => blockText(item, level + 1)).join("\n")
          );
        })
        .join("\n");
  }
}

function humanMarkdown(value: string): string {
  return blocksFromNodes(parser.parse(value).children)
    .map((block) => blockText(block))
    .join("\n")
    .trim();
}

export function canExportAnswer(turn: PublicTurn): boolean {
  return (
    turn.status === "completed" && !turn.partial && Boolean(turn.answer?.trim())
  );
}

/** 从完成的可见回答建立导出模型，不读取任何原生事件、令牌或内部ID。 */
export function buildAnswerExport(turn: PublicTurn): AnswerExportContent {
  if (!canExportAnswer(turn)) throw new Error("尚无可导出的最终回答。");
  const content = nativeCitationDisplay(
    turn.answer ?? "",
    turn.citations,
  ).replace(/<kb\b[^>]*\/?\s*>/gi, (tag) =>
    nativeCitationDisplay(tag, turn.citations),
  );
  const askedAt = Number.isFinite(turn.startedAt)
    ? new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: "Asia/Shanghai",
      }).format(new Date(turn.startedAt)) + "（北京时间）"
    : "未记录";
  return {
    question: visibleText(turn.question),
    askedAt,
    scopes: turn.searchScopes?.length
      ? turn.searchScopes.map((scope) => SCOPE_LABELS[scope])
      : ["本次记录未保存资料范围"],
    blocks: blocksFromNodes(parser.parse(content).children),
    references: turn.citations.map((citation, index) => ({
      number: index + 1,
      title: humanMarkdown(
        citation.document_title?.trim() || citation.document_name || "资料来源",
      ),
      detail: [
        citation.department_name || citation.department,
        Array.isArray(citation.category_path)
          ? citation.category_path.join(" / ")
          : citation.category_path,
        citation.locator,
      ]
        .filter(Boolean)
        .map((value) => humanMarkdown(value ?? ""))
        .join(" · "),
      quote: humanMarkdown(citation.quote ?? ""),
      unavailable: citation.source_available === false,
    })),
    containsImages: /!\[|<img\b/i.test(content),
    truncated: turn.truncated === true,
  };
}

export function answerClipboardText(
  turn: PublicTurn,
  includeSources = false,
): string {
  const content = buildAnswerExport(turn);
  const answer = content.blocks
    .map((block) => blockText(block))
    .join("\n\n")
    .trim();
  const notice = content.truncated
    ? "\n\n说明：知识库提示本次回答已截断。"
    : "";
  if (!includeSources) return answer + notice;
  const sources = content.references.map(
    (reference) =>
      `〔${reference.number}〕${reference.title}` +
      (reference.detail ? `\n${reference.detail}` : "") +
      (reference.quote ? `\n${reference.quote}` : "") +
      (reference.unavailable ? "\n原文暂不可用。" : ""),
  );
  return [
    `问题：${content.question}`,
    `提问时间：${content.askedAt}`,
    `资料范围：${content.scopes.join("、")}`,
    answer + notice,
    "参考来源",
    ...(sources.length ? sources : ["本次回答未附参考来源。"]),
  ].join("\n\n");
}

export function answerWordFilename(turn: PublicTurn): string {
  const title = Array.from(visibleText(turn.question), (character) =>
    character.charCodeAt(0) < 32 ? " " : character,
  )
    .join("")
    .replace(/[<>:"/\\|?*]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 48)
    .replace(/[. ]+$/, "");
  return `湾小度回答_${title || "知识问答"}.docx`;
}
