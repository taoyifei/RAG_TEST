import { Plus } from "lucide-react";

import { PublicAnswer } from "./PublicAnswer";
import { PublicChatComposer } from "./PublicChatComposer";
import { PublicQuestionTabs } from "./PublicQuestionTabs";
import type { SuggestedQuestion } from "./suggestedQuestions";
import type {
  PublicFeedbackSubmission,
  PublicPopularQuestion,
  PublicPopularQuestions,
} from "./publicApi";
import type { PublicTurn } from "./usePublicChat";

export function WanshitongChat({
  busy,
  conversationId,
  feedbackDetailsEnabled,
  initialQuestion,
  onFeedback,
  onFeedbackLogin,
  onDownloadReference,
  onNewTopic,
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
}: {
  busy: boolean;
  conversationId: string;
  feedbackDetailsEnabled: boolean;
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
  onNewTopic: () => void;
  onPopularQuestionSubmit: (question: PublicPopularQuestion) => void;
  onRefresh: () => void;
  onRetry: (turnId: string, question: string, conversationId: string) => void;
  onRecover?: (turnId: string, question: string, conversationId: string) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  onSuggestedQuestionSubmit: (question: SuggestedQuestion) => void;
  questions: readonly SuggestedQuestion[];
  popular: PublicPopularQuestions;
  turns: PublicTurn[];
}) {
  return (
    <div className="wst-chat-shell">
      <header className="wst-chat-header">
        <strong>湾事通</strong>
        <button
          className="wst-new-topic-button"
          disabled={busy}
          onClick={onNewTopic}
          type="button"
        >
          <Plus aria-hidden="true" size={17} />
          新问题
        </button>
      </header>
      <main className="wst-chat" id="main-content">
        <div className="wst-turns">
          {turns.map((turn) => (
            <article className="wst-turn" key={turn.id}>
              <div className="wst-question">
                <span className="sr-only">你问：</span>
                {turn.question}
              </div>
              <PublicAnswer
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
                onRetry={() =>
                  onRetry(turn.id, turn.question, turn.conversationId)
                }
                onRecover={
                  onRecover
                    ? () => onRecover(turn.id, turn.question, turn.conversationId)
                    : undefined
                }
                turn={turn}
              />
            </article>
          ))}
        </div>
        {!busy &&
          turns.at(-1)?.status !== "submitting" &&
          turns.at(-1)?.status !== "streaming" && (
            <div className="wst-chat-suggestions">
              <PublicQuestionTabs
                busy={busy}
                heading="换个话题"
                onPopularSubmit={onPopularQuestionSubmit}
                onRefresh={onRefresh}
                onSuggestedSubmit={onSuggestedQuestionSubmit}
                popular={popular}
                suggestions={questions}
              />
            </div>
          )}
        <div className="wst-chat-composer">
          <PublicChatComposer
            busy={busy}
            compact
            initialQuestion={initialQuestion}
            key={conversationId}
            onStop={onStop}
            onSubmit={onSubmit}
          />
        </div>
      </main>
    </div>
  );
}
