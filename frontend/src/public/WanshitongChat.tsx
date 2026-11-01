/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- 独立对话滚动区须可聚焦，供键盘滚动。 */
import { ArrowDown } from "lucide-react";
import { memo } from "react";

import { useChatScroll } from "./useChatScroll";
import { PublicAnswer } from "./PublicAnswer";
import {
  PublicChatComposer,
  type PublicSearchSelectionProps,
} from "./PublicChatComposer";
import { PublicQuestionTabs } from "./PublicQuestionTabs";
import type { SuggestedQuestion } from "./suggestedQuestions";
import type {
  PublicFeedbackSubmission,
  PublicPopularQuestion,
  PublicPopularQuestions,
} from "./publicApi";
import type { PublicTurn } from "./usePublicChat";

type ChatProps = PublicSearchSelectionProps & {
  busy: boolean;
  conversationId: string;
  feedbackDetailsEnabled: boolean;
  allowFeedback: boolean;
  allowSourceDownload: boolean;
  placeholder: string;
  showRecommendations: boolean;
  initialQuestion?: string;
  onFeedback: (
    turnId: string,
    traceId: string,
    feedback: PublicFeedbackSubmission,
  ) => void;
  onFeedbackLogin: () => void;
  onDownloadReference?: (
    conversationId: string,
    traceId: string,
    referenceId: string,
    documentName: string,
    original?: boolean,
  ) => Promise<void>;
  onReadReference?: (
    conversationId: string,
    traceId: string,
    referenceId: string,
  ) => Promise<string>;
  beforeExport?: (turn: PublicTurn) => Promise<void>;
  onPopularQuestionSubmit: (question: PublicPopularQuestion) => void;
  onRefresh: () => void;
  onRetry: (turnId: string, question: string, conversationId: string) => void;
  onRecover?: (
    turnId: string,
    question: string,
    conversationId: string,
  ) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  onSuggestedQuestionSubmit: (question: SuggestedQuestion) => void;
  questions: readonly SuggestedQuestion[];
  popular: PublicPopularQuestions;
  turns: PublicTurn[];
};

const ChatTurn = memo(function ChatTurn({
  turn,
  allowFeedback,
  allowSourceDownload,
  feedbackDetailsEnabled,
  onFeedback,
  onFeedbackLogin,
  onDownloadReference,
  onReadReference,
  beforeExport,
  onRetry,
  onRecover,
}: Pick<
  ChatProps,
  | "allowFeedback"
  | "allowSourceDownload"
  | "feedbackDetailsEnabled"
  | "onFeedback"
  | "onFeedbackLogin"
  | "onDownloadReference"
  | "onReadReference"
  | "beforeExport"
  | "onRetry"
  | "onRecover"
> & { turn: PublicTurn }) {
  return (
    <article className="wst-turn">
      <div className="wst-question">
        <span className="sr-only">你问：</span>
        {turn.question}
      </div>
      <PublicAnswer
        allowFeedback={allowFeedback}
        allowSourceDownload={allowSourceDownload}
        feedbackDetailsEnabled={feedbackDetailsEnabled}
        onFeedback={(feedback) => {
          if (turn.traceId) onFeedback(turn.id, turn.traceId, feedback);
        }}
        onFeedbackLogin={onFeedbackLogin}
        onDownloadReference={
          turn.traceId && onDownloadReference
            ? (referenceId, documentName, original) =>
                onDownloadReference(
                  turn.conversationId,
                  turn.traceId!,
                  referenceId,
                  documentName,
                  original,
                )
            : undefined
        }
        onReadReference={
          turn.traceId && onReadReference
            ? (referenceId) =>
                onReadReference(turn.conversationId, turn.traceId!, referenceId)
            : undefined
        }
        beforeExport={beforeExport ? () => beforeExport(turn) : undefined}
        onRetry={() => onRetry(turn.id, turn.question, turn.conversationId)}
        onRecover={
          onRecover
            ? () => onRecover(turn.id, turn.question, turn.conversationId)
            : undefined
        }
        turn={turn}
      />
    </article>
  );
});

export function WanshitongChat({
  busy,
  conversationId,
  feedbackDetailsEnabled,
  allowFeedback,
  allowSourceDownload,
  placeholder,
  searchScopes,
  searchScopeOptions,
  scopeNotice,
  onSearchScopesChange,
  showRecommendations,
  initialQuestion,
  onFeedback,
  onFeedbackLogin,
  onDownloadReference,
  onReadReference,
  beforeExport,
  onPopularQuestionSubmit,
  onRefresh,
  onRetry,
  onRecover,
  onStop,
  onSubmit,
  onSuggestedQuestionSubmit,
  questions,
  popular,
  turns,
}: ChatProps) {
  const { scrollRef, contentRef, onScroll, awayFromLatest, jumpToLatest } =
    useChatScroll(conversationId, turns.length);
  return (
    <div className="wst-chat-shell">
      <main className="wst-chat" id="main-content">
        <div
          className="wst-chat-scroll"
          ref={scrollRef}
          onScroll={onScroll}
          role="region"
          aria-label="对话内容"
          tabIndex={0}
        >
          <div className="wst-chat-scroll-content" ref={contentRef}>
            <div className="wst-turns">
              {turns.map((turn) => (
                <ChatTurn
                  key={turn.id}
                  turn={turn}
                  allowFeedback={allowFeedback}
                  allowSourceDownload={allowSourceDownload}
                  feedbackDetailsEnabled={feedbackDetailsEnabled}
                  onFeedback={onFeedback}
                  onFeedbackLogin={onFeedbackLogin}
                  onDownloadReference={onDownloadReference}
                  onReadReference={onReadReference}
                  beforeExport={beforeExport}
                  onRetry={onRetry}
                  onRecover={onRecover}
                />
              ))}
            </div>
            {showRecommendations &&
              !busy &&
              turns.at(-1)?.status !== "submitting" &&
              turns.at(-1)?.status !== "streaming" && (
                <div className="wst-chat-suggestions">
                  <PublicQuestionTabs
                    busy={busy}
                    compact
                    heading="换个话题"
                    onPopularSubmit={onPopularQuestionSubmit}
                    onRefresh={onRefresh}
                    onSuggestedSubmit={onSuggestedQuestionSubmit}
                    popular={popular}
                    suggestions={questions}
                  />
                </div>
              )}
          </div>
        </div>
        <div className="wst-chat-composer">
          {awayFromLatest && (
            <button
              className="wst-jump-latest"
              type="button"
              onClick={jumpToLatest}
              aria-label="回到最新回答"
            >
              <ArrowDown size={16} aria-hidden="true" />
              回到最新
            </button>
          )}
          <PublicChatComposer
            busy={busy}
            compact
            initialQuestion={initialQuestion}
            placeholder={placeholder}
            searchScopes={searchScopes}
            searchScopeOptions={searchScopeOptions}
            scopeNotice={scopeNotice}
            onSearchScopesChange={onSearchScopesChange}
            key={conversationId}
            onStop={onStop}
            onSubmit={onSubmit}
          />
        </div>
      </main>
    </div>
  );
}
