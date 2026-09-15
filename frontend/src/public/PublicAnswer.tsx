import { RotateCcw } from "lucide-react";

import { PublicCitations } from "./PublicCitations";
import { PublicFeedback } from "./PublicFeedback";
import type { PublicTurn } from "./usePublicChat";

export function PublicAnswer({
  onFeedback,
  onRetry,
  turn,
}: {
  onFeedback: (useful: boolean) => void;
  onRetry: () => void;
  turn: PublicTurn;
}) {
  const active = turn.status === "submitting" || turn.status === "streaming";
  return (
    <section className="wst-answer" aria-label="湾事通回答">
      <div className="wst-answer-brand" aria-hidden="true">
        湾
      </div>
      <div className="wst-answer-body">
        {active && turn.stageMessage && (
          <p className="wst-stage">
            <span aria-hidden="true" />
            {turn.stageMessage}
          </p>
        )}
        {turn.answer !== undefined ? (
          <div className="wst-final-answer">{turn.answer}</div>
        ) : (
          turn.claims.length > 0 && (
            <div className="wst-claims">
              <p>正在生成 · 已验证片段（暂非最终答案）</p>
              {turn.claims.map((claim) => (
                <div className="wst-claim" key={claim.claimIndex}>
                  {claim.text}
                </div>
              ))}
            </div>
          )
        )}
        {turn.errorMessage && (
          <div
            className={`wst-answer-notice ${turn.partial ? "is-partial" : ""}`}
          >
            <p>{turn.errorMessage}</p>
            {turn.status === "failed" && (
              <button onClick={onRetry} type="button">
                <RotateCcw aria-hidden="true" size={15} />
                重新尝试
              </button>
            )}
          </div>
        )}
        {turn.status === "completed" && (
          <>
            <PublicCitations citations={turn.citations} />
            {turn.traceId && (
              <PublicFeedback onSubmit={onFeedback} status={turn.feedback} />
            )}
          </>
        )}
      </div>
    </section>
  );
}
