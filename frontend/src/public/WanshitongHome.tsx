import { PublicChatComposer } from "./PublicChatComposer";
import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";

export function WanshitongHome({
  busy,
  conversationId,
  initialQuestion,
  onRefresh,
  onSuggestedQuestionSubmit,
  onStop,
  onSubmit,
  questions,
}: {
  busy: boolean;
  conversationId: string;
  initialQuestion?: string;
  onRefresh: () => void;
  onSuggestedQuestionSubmit: (question: SuggestedQuestion) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  questions: readonly SuggestedQuestion[];
}) {
  return (
    <main className="wst-home" id="main-content">
      <div className="wst-home-content">
        <div className="wst-hero-copy">
          <span className="wst-wordmark">湾事通</span>
          <h1>你的内部知识助手</h1>
        </div>
        <SuggestedQuestionCarousel
          busy={busy}
          onRefresh={onRefresh}
          onSubmit={onSuggestedQuestionSubmit}
          questions={questions}
        />
        <PublicChatComposer
          busy={busy}
          initialQuestion={initialQuestion}
          key={conversationId}
          onStop={onStop}
          onSubmit={onSubmit}
        />
      </div>
    </main>
  );
}
