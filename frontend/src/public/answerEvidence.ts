import {
  hasModelNotice,
  MODEL_GENERATED_NOTICE,
} from "./nativeCitationDisplay";
import type { PublicTurn } from "./usePublicChat";

export function answerEvidence(turn: PublicTurn) {
  return hasModelNotice(turn.answer ?? turn.provisionalAnswer ?? "") ||
    turn.answerOrigin === "model_unreferenced" ||
    turn.answerOrigin === "model_no_results"
    ? []
    : turn.citations;
}

export function answerOriginNotice(turn: PublicTurn): string | undefined {
  if (
    (turn.status !== "completed" && turn.status !== "streaming") ||
    !(turn.answer ?? turn.provisionalAnswer)?.trim()
  )
    return undefined;
  if (
    hasModelNotice(turn.answer ?? turn.provisionalAnswer ?? "") ||
    turn.answerOrigin === "model_unreferenced" ||
    turn.answerOrigin === "model_no_results" ||
    (turn.status === "completed" && turn.citations.length === 0)
  )
    return MODEL_GENERATED_NOTICE;
  return undefined;
}
