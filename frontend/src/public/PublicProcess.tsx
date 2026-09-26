import { useState } from "react";

import type { PublicCitation } from "./publicSse";
import type { PublicTurn } from "./usePublicChat";

function documentCount(citations: PublicCitation[]): number {
  return new Set(
    citations.map(
      (citation) =>
        citation.native_knowledge_id ??
        citation.source_relative_path ??
        citation.document_name,
    ),
  ).size;
}

function toolResult(
  turn: PublicTurn,
  name: string,
): Record<string, unknown> | undefined {
  for (let index = (turn.nativeEvents?.length ?? 0) - 1; index >= 0; index--) {
    const event = turn.nativeEvents?.[index];
    const data = event?.payload.data;
    if (
      event?.responseType === "tool_result" &&
      typeof data === "object" &&
      data !== null &&
      (data as Record<string, unknown>).tool_name === name
    ) {
      return data as Record<string, unknown>;
    }
  }
  return undefined;
}

function calledTool(turn: PublicTurn, name: string): boolean {
  return Boolean(
    turn.nativeEvents?.some(
      (item) =>
        item.responseType === "tool_call" &&
        typeof item.payload.data === "object" &&
        item.payload.data !== null &&
        (item.payload.data as Record<string, unknown>).tool_name === name,
    ),
  );
}

export function PublicProcess({ turn }: { turn: PublicTurn }) {
  const active = turn.status === "submitting" || turn.status === "streaming";
  const [open, setOpen] = useState(active);
  const understanding = toolResult(turn, "query_understand");
  const searching = calledTool(turn, "knowledge_search");
  const searchResult = toolResult(turn, "knowledge_search");
  const found = searchResult?.count;
  const foundLabel =
    typeof found === "number" && Number.isSafeInteger(found) && found >= 0
      ? `找到 ${found} 个结果`
      : "检索完成";
  const citationsCount = documentCount(turn.citations);
  const searched = searchResult?.success === true || citationsCount > 0;
  const summary =
    turn.status === "failed"
      ? "处理未完成"
      : turn.status === "cancelled"
        ? "已停止"
        : turn.status === "stop_requested"
          ? "停止请求已接收"
        : searched
          ? citationsCount > 0
            ? `检索完成 · 引用了 ${citationsCount} 篇文档`
            : "检索完成"
          : turn.status === "completed"
            ? "回答完成"
            : searching
              ? "正在检索知识库"
              : "正在处理问题";

  return (
    <details
      className="wst-native-process"
      onToggle={(event) => setOpen(event.currentTarget.open)}
      open={open}
    >
      <summary>{summary}</summary>
      <ol aria-label="知识库处理进度">
        {understanding?.success === true && <li>已完成问题理解</li>}
        {searching && (
          <li>
            检索知识库
            {searchResult?.success === true ? ` · ${foundLabel}` : "中"}
          </li>
        )}
        {turn.status === "completed" && <li>完成</li>}
        {turn.status === "failed" && <li>本次处理失败</li>}
        {turn.status === "cancelled" && <li>已停止处理</li>}
        {!understanding && !searching && active && <li>等待知识库响应</li>}
      </ol>
    </details>
  );
}
