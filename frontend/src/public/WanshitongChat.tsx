import { PublicAnswer } from "./PublicAnswer";
import { PublicChatComposer } from "./PublicChatComposer";
import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";
import type { PublicFeedbackSubmission } from "./publicApi";
import type { PublicTurn } from "./usePublicChat";

export function WanshitongChat({
  busy,
  feedbackDetailsEnabled,
  initialQuestion,
  onFeedback,
  onFeedbackLogin,
  onRefresh,
  onRetry,
  onStop,
  onSubmit,
  onSuggestedQuestionSubmit,
  questions,
  turns,
}: {
  busy: boolean;
  feedbackDetailsEnabled: boolean;
  initialQuestion?: string;
  onFeedback: (
    turnId: string,
    traceId: string,
    feedback: PublicFeedbackSubmission,
  ) => void;
  onFeedbackLogin: () => void;
  onRefresh: () => void;
  onRetry: (turnId: string, question: string) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  onSuggestedQuestionSubmit: (question: SuggestedQuestion) => void;
  questions: readonly SuggestedQuestion[];
  turns: PublicTurn[];
}) {
  return (
    <div className="wst-chat-shell">
      <header className="wst-chat-header">
        <strong>湾事通</strong>
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
                onRetry={() => onRetry(turn.id, turn.question)}
                turn={turn}
              />
            </article>
          ))}
        </div>
        <div className="wst-chat-composer">
          {!busy &&
            turns.at(-1)?.status !== "submitting" &&
            turns.at(-1)?.status !== "streaming" && (
              <SuggestedQuestionCarousel
                busy={busy}
                onRefresh={onRefresh}
                onSubmit={onSuggestedQuestionSubmit}
                questions={questions}
              />
            )}
          <PublicChatComposer
            busy={busy}
            compact
            initialQuestion={initialQuestion}
            onStop={onStop}
            onSubmit={onSubmit}
          />
        </div>
      </main>
    </div>
  );
}
