import {
  PublicChatComposer,
  type PublicSearchSelectionProps,
} from "./PublicChatComposer";
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
  placeholder,
  searchScopes,
  searchScopeOptions,
  scopeNotice,
  onSearchScopesChange,
  welcomeText,
  showRecommendations,
  onRefresh,
  onPopularQuestionSubmit,
  onSuggestedQuestionSubmit,
  onStop,
  onSubmit,
  questions,
  popular,
}: PublicSearchSelectionProps & {
  busy: boolean;
  conversationId: string;
  initialQuestion?: string;
  placeholder: string;
  welcomeText: string;
  showRecommendations: boolean;
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
          <span className="wst-wordmark">湾小度</span>
          <h1>{welcomeText}</h1>
        </div>
        <PublicChatComposer
          busy={busy}
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
        {showRecommendations && (
          <PublicQuestionTabs
            busy={busy}
            onPopularSubmit={onPopularQuestionSubmit}
            onRefresh={onRefresh}
            onSuggestedSubmit={onSuggestedQuestionSubmit}
            popular={popular}
            suggestions={questions}
          />
        )}
      </div>
    </main>
  );
}
