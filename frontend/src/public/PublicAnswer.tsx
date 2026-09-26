import { RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { withAppBase } from "../app/basePath";
import { PublicCitations } from "./PublicCitations";
import { PublicFeedback } from "./PublicFeedback";
import { PublicProcess } from "./PublicProcess";
import { PUBLIC_STAGE_LABELS } from "./publicSse";
import type { PublicFeedbackSubmission } from "./publicApi";
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
    return `已收到 ${claimCount} 段实时内容，完整答案仍在生成。`;
  }
  if (stageSeconds >= 20) {
    return signalAgeSeconds <= 5
      ? "连接正常，仍在等待回答服务返回结果；如需可停止后重试。"
      : "暂未收到新数据，仍在等待回答服务；如需可停止后重试。";
  }
  if (stageSeconds >= 6) {
    return signalAgeSeconds <= 5
      ? "连接正常，正在等待回答服务返回内容。"
      : "正在等待回答服务返回内容。";
  }
  return "正在生成答复；收到内容后会实时显示。";
}

function MarkdownAnswer({
  content,
  turn,
}: {
  content: string;
  turn: PublicTurn;
}) {
  return (
    <div className="wst-final-answer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url, key, node) =>
          key === "src" &&
          node.tagName === "img" &&
          url.startsWith("resource://")
            ? url
            : defaultUrlTransform(url)
        }
        components={{
          a: ({ children, href, title }) =>
            href ? (
              <a
                href={href}
                rel="noopener noreferrer"
                target="_blank"
                title={title}
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt, src }) => {
            if (!src) return <span>{alt}</span>;
            if (src.startsWith("resource://")) {
              if (!turn.traceId || turn.status !== "completed") {
                return <span>{alt}</span>;
              }
              const path =
                `/api/public/conversations/${encodeURIComponent(turn.conversationId)}` +
                `/turns/${encodeURIComponent(turn.traceId)}/resources` +
                `?file_path=${encodeURIComponent(src)}`;
              return (
                <img
                  alt={alt ?? ""}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  src={withAppBase(path)}
                />
              );
            }
            return (
              <img
                alt={alt ?? ""}
                loading="lazy"
                referrerPolicy="no-referrer"
                src={src}
              />
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export function PublicAnswer({
  feedbackDetailsEnabled = true,
  onFeedback,
  onFeedbackLogin,
  onDownloadReference,
  onRetry,
  turn,
}: {
  feedbackDetailsEnabled?: boolean;
  onFeedback: (feedback: PublicFeedbackSubmission) => void;
  onFeedbackLogin?: () => void;
  onDownloadReference?: (
    referenceId: string,
    documentName: string,
    original?: boolean,
  ) => Promise<void>;
  onRetry: () => void;
  turn: PublicTurn;
}) {
  const active = turn.status === "submitting" || turn.status === "streaming";
  const nativeProcess =
    turn.nativeProtocol === true || Boolean(turn.nativeEvents?.length);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const stageSeconds = Math.max(
    0,
    elapsedSeconds - Math.floor((turn.stageStartedAt - turn.startedAt) / 1000),
  );
  const signalAgeSeconds = Math.max(
    0,
    elapsedSeconds - Math.floor((turn.lastSignalAt - turn.startedAt) / 1000),
  );
  const waitingForGeneration =
    turn.stageMessage === PUBLIC_STAGE_LABELS.generation;
  const hint = progressHint(
    turn.stageMessage,
    turn.claims.length + (turn.provisionalAnswer ? 1 : 0),
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
        {nativeProcess && <PublicProcess turn={turn} />}
        {!nativeProcess && active && turn.stageMessage && (
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
          <MarkdownAnswer content={turn.answer} turn={turn} />
        ) : turn.provisionalAnswer ? (
          <div
            className={nativeProcess && active ? "wst-streaming-answer" : "wst-claims"}
          >
            {!nativeProcess && (
              <p>
                {active
                  ? "回答生成中 · 以下为实时正文"
                  : "回答已中断 · 以下为已收到的正文片段"}
              </p>
            )}
            <MarkdownAnswer content={turn.provisionalAnswer} turn={turn} />
          </div>
        ) : (
          turn.claims.length > 0 && (
            <div className="wst-claims">
              <p>已收到 {turn.claims.length} 条内容 · 暂非最终答案</p>
              {turn.claims.map((claim) => (
                <div className="wst-claim" key={claim.claimIndex}>
                  {claim.text}
                </div>
              ))}
            </div>
          )
        )}
        {turn.truncated && turn.status === "completed" && (
          <p className="wst-answer-notice">知识库提示本次回答已截断。</p>
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
        {turn.citations.length > 0 && (
          <>
            <PublicCitations
              citations={turn.citations}
              onDownloadReference={onDownloadReference}
            />
          </>
        )}
        {(turn.status === "completed" ||
          (turn.status === "failed" && !turn.partial)) &&
          turn.traceId && (
            <PublicFeedback
              detailsEnabled={feedbackDetailsEnabled}
              errorMessage={turn.feedbackError}
              onLogin={onFeedbackLogin}
              onSubmit={onFeedback}
              status={turn.feedback}
            />
          )}
      </div>
    </section>
  );
}
