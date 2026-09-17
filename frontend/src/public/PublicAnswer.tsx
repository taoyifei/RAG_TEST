import { RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";

import { PublicCitations } from "./PublicCitations";
import { PublicFeedback } from "./PublicFeedback";
import { PUBLIC_STAGE_LABELS } from "./publicSse";
import type { PublicTurn } from "./usePublicChat";

const PROGRESS_STAGES = [
  { label: PUBLIC_STAGE_LABELS.accepted, short: "已接收" },
  { label: PUBLIC_STAGE_LABELS.snapshot, short: "确认资料" },
  { label: PUBLIC_STAGE_LABELS.retrieval, short: "检索依据" },
  { label: PUBLIC_STAGE_LABELS.generation, short: "组织回答" },
  { label: PUBLIC_STAGE_LABELS.validation, short: "核对来源" },
] as const;

function progressHint(
  stageMessage: string | undefined,
  claimCount: number,
  stageSeconds: number,
  signalAgeSeconds: number,
): string | undefined {
  if (stageMessage === PUBLIC_STAGE_LABELS.validation) {
    return "正在核对回答与来源，完成后会显示最终答复。";
  }
  if (stageMessage !== PUBLIC_STAGE_LABELS.generation) return undefined;
  if (claimCount > 0) {
    return `已先显示 ${claimCount} 条已核验内容，完整答案仍在生成。`;
  }
  if (stageSeconds >= 20) {
    return signalAgeSeconds <= 5
      ? "连接正常，仍在等待回答服务返回结果；如需可停止后重试。"
      : "暂未收到新数据，仍在等待回答服务；如需可停止后重试。";
  }
  if (stageSeconds >= 6) {
    return signalAgeSeconds <= 5
      ? "连接正常，正在等待回答服务返回可核对的内容。"
      : "正在等待回答服务返回可核对的内容。";
  }
  return "正在生成答复；有可核对的内容会先显示。";
}

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
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const stageSeconds = Math.max(
    0,
    elapsedSeconds - Math.floor((turn.stageStartedAt - turn.startedAt) / 1000),
  );
  const signalAgeSeconds = Math.max(
    0,
    elapsedSeconds - Math.floor((turn.lastSignalAt - turn.startedAt) / 1000),
  );
  const waitingForGeneration = turn.stageMessage === PUBLIC_STAGE_LABELS.generation;
  const hint = progressHint(
    turn.stageMessage,
    turn.claims.length,
    stageSeconds,
    signalAgeSeconds,
  );

  useEffect(() => {
    if (!active) return;
    const update = () => {
      setElapsedSeconds(
        Math.max(0, Math.floor((Date.now() - turn.startedAt) / 1000)),
      );
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [active, turn.startedAt]);

  return (
    <section className="wst-answer" aria-label="湾事通回答">
      <div className="wst-answer-brand" aria-hidden="true">
        湾
      </div>
      <div className="wst-answer-body">
        {active && turn.stageMessage && (
          <div className="wst-progress">
            <p className="wst-stage">
              <span aria-hidden="true" className="wst-stage-dot" />
              <span>{turn.stageMessage}</span>
              <span aria-hidden="true" className="wst-elapsed">
                {elapsedSeconds} 秒
              </span>
            </p>
            {waitingForGeneration && (
              <div aria-hidden="true" className="wst-progress-motion" />
            )}
            {turn.stageHistory.length > 0 && (
              <ol className="wst-stage-history" aria-label="处理进度">
                {PROGRESS_STAGES.map((stage) => (
                  <li
                    className={
                      stage.label === turn.stageMessage
                        ? "is-current"
                        : turn.stageHistory.includes(stage.label)
                          ? "is-complete"
                          : "is-upcoming"
                    }
                    aria-current={
                      stage.label === turn.stageMessage ? "step" : undefined
                    }
                    key={stage.label}
                  >
                    <span aria-hidden="true" />
                    {stage.short}
                  </li>
                ))}
              </ol>
            )}
            {hint && (
              <p aria-live="polite" className="wst-progress-hint">
                {hint}
              </p>
            )}
          </div>
        )}
        {turn.answer !== undefined ? (
          <div className="wst-final-answer">{turn.answer}</div>
        ) : (
          turn.claims.length > 0 && (
            <div className="wst-claims">
              <p>已核验 {turn.claims.length} 条内容 · 暂非最终答案</p>
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
