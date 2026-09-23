import { PublicChatComposer } from "./PublicChatComposer";
import { PublicQuestionTabs } from "./PublicQuestionTabs";
import type {
  PublicPopularQuestion,
  PublicPopularQuestions,
} from "./publicApi";
import type { SuggestedQuestion } from "./suggestedQuestions";

export function WanshitongHome({
  busy,
  conversationId,
  initialQuestion,
  onRefresh,
  onPopularQuestionSubmit,
  onSuggestedQuestionSubmit,
  onStop,
  onSubmit,
  questions,
  popular,
}: {
  busy: boolean;
  conversationId: string;
  initialQuestion?: string;
  onRefresh: () => void;
  onPopularQuestionSubmit: (question: PublicPopularQuestion) => void;
  onSuggestedQuestionSubmit: (question: SuggestedQuestion) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  questions: readonly SuggestedQuestion[];
  popular: PublicPopularQuestions;
}) {
  return (
    <main className="wst-home" id="main-content">
      <div className="wst-home-content">
        <div className="wst-hero-copy">
          <span className="wst-wordmark">湾事通</span>
          <h1>你的内部知识助手</h1>
        </div>
        <PublicChatComposer
          busy={busy}
          initialQuestion={initialQuestion}
          key={conversationId}
          onStop={onStop}
          onSubmit={onSubmit}
        />
        <PublicQuestionTabs
          busy={busy}
          onPopularSubmit={onPopularQuestionSubmit}
          onRefresh={onRefresh}
          onSuggestedSubmit={onSuggestedQuestionSubmit}
          popular={popular}
          suggestions={questions}
        />
      </div>
    </main>
  );
}
