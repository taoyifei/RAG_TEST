import { referencedCitations } from "./nativeCitationDisplay";
import type { PublicTurn } from "./usePublicChat";

export function answerEvidence(turn: PublicTurn) {
  if (!turn.nativeProtocol) return turn.citations;
  if (
    ["model_insufficient", "model_unverified"].includes(turn.answerOrigin ?? "")
  )
    return [];
  return referencedCitations(
    turn.answer ?? turn.provisionalAnswer ?? "",
    turn.citations,
  );
}

export function answerOriginNotice(turn: PublicTurn): string | undefined {
  const active = turn.status === "streaming";
  if (
    (turn.status !== "completed" && !active) ||
    !(turn.answer ?? turn.provisionalAnswer)?.trim()
  )
    return undefined;
  if (turn.answerOrigin === "model_insufficient")
    return "检索到了资料，但未找到能直接支持本次回答的依据。以下内容由大模型生成，仅供参考。";
  if (answerEvidence(turn).length > 0) return undefined;
  let found = false;
  let zero = false;
  for (const event of turn.nativeEvents ?? []) {
    const result = event.payload.data as Record<string, unknown> | undefined;
    if (
      event.responseType !== "tool_result" ||
      !result ||
      result.tool_name !== "knowledge_search" ||
      result.success !== true
    )
      continue;
    const nested = result.data as Record<string, unknown> | undefined;
    const count = result.count ?? nested?.count;
    if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 0)
      continue;
    found ||= count > 0;
    zero ||= count === 0;
  }
  if (
    turn.answerOrigin === "model_unverified" ||
    (turn.status === "completed" &&
      turn.nativeProtocol &&
      (turn.citations.length > 0 || found))
  )
    return "已检索知识库，但尚未确认回答的资料依据。以下内容由大模型生成，仅供参考。";
  if (!found && (turn.answerOrigin === "model_no_results" || zero))
    return "未找到相关资料，以下内容由大模型生成，仅供参考。";
  if (turn.status === "completed")
    return "未引用知识库资料，以下内容由大模型生成，仅供参考。";
  return undefined;
}
