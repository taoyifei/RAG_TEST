import { referencedCitations } from "./nativeCitationDisplay";
import type { PublicTurn } from "./usePublicChat";

export function answerEvidence(turn: PublicTurn) {
  if (!turn.nativeProtocol) return turn.citations;
  return referencedCitations(
    turn.answer ?? turn.provisionalAnswer ?? "",
    turn.citations,
  );
}

export function answerOriginNotice(turn: PublicTurn): string | undefined {
  if (
    (turn.status !== "completed" &&
      !(
        turn.status === "streaming" && turn.answerOrigin === "model_no_results"
      )) ||
    !(turn.answer ?? turn.provisionalAnswer)?.trim()
  )
    return undefined;
  if (answerEvidence(turn).length > 0) return undefined;
  return "未引用知识库资料，以下内容由大模型生成，仅供参考。";
}
