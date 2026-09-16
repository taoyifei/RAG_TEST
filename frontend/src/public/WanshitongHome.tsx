import { PublicChatComposer } from "./PublicChatComposer";
import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";

export function WanshitongHome({
  busy,
  onRefresh,
  onStop,
  onSubmit,
  questions,
}: {
  busy: boolean;
  onRefresh: () => void;
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
          onSubmit={onSubmit}
          questions={questions}
        />
        <PublicChatComposer busy={busy} onStop={onStop} onSubmit={onSubmit} />
      </div>
    </main>
  );
}
