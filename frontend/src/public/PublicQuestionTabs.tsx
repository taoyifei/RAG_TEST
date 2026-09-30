import { ArrowUpRight } from "lucide-react";
import { useState } from "react";

import type {
  PublicPopularQuestion,
  PublicPopularQuestions,
} from "./publicApi";
import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";

export function PublicQuestionTabs({
  busy,
  compact = false,
  heading,
  onRefresh,
  onPopularSubmit,
  onSuggestedSubmit,
  popular,
  suggestions,
}: {
  busy: boolean;
  compact?: boolean;
  heading?: string;
  onRefresh: () => void;
  onPopularSubmit: (question: PublicPopularQuestion) => void;
  onSuggestedSubmit: (question: SuggestedQuestion) => void;
  popular: PublicPopularQuestions;
  suggestions: readonly SuggestedQuestion[];
}) {
  const [tab, setTab] = useState<"suggested" | "popular">("suggested");
  const [expanded, setExpanded] = useState(false);
  const [category, setCategory] = useState("all");
  const hasPopular = popular.mode === "POPULAR" && popular.items.length > 0;
  const hasSuggestions = suggestions.length > 0;
  const previewCount = compact ? 3 : 5;
  const categories = Array.from(
    new Set(popular.items.map((item) => item.topic_key.trim()).filter(Boolean)),
  );
  const selectedCategory = categories.includes(category) ? category : "all";
  const visibleItems = expanded
    ? popular.items.filter(
        (item) =>
          selectedCategory === "all" || item.topic_key.trim() === selectedCategory,
      )
    : popular.items.slice(0, previewCount);
  const activeTab = hasSuggestions
    ? hasPopular
      ? tab
      : "suggested"
    : "popular";

  if (!hasPopular && !hasSuggestions) return null;

  return (
    <section className="wst-question-tabs" aria-label={heading ?? "推荐问题"}>
      {heading && <h2>{heading}</h2>}
      <div className="wst-question-tab-heading">
        <div className="wst-question-tab-buttons" role="tablist">
          <button
            aria-selected={activeTab === "suggested"}
            disabled={!hasSuggestions}
            onClick={() => setTab("suggested")}
            role="tab"
            type="button"
          >
            你可能想问
          </button>
          <button
            aria-selected={activeTab === "popular"}
            disabled={!hasPopular}
            onClick={() => setTab("popular")}
            role="tab"
            type="button"
          >
            大家常问
          </button>
        </div>
        {activeTab === "popular" && popular.items.length > previewCount && (
          <button
            aria-expanded={expanded}
            className="wst-popular-more"
            onClick={() => {
              setExpanded((value) => !value);
              setCategory("all");
            }}
            type="button"
          >
            {expanded ? "收起" : "查看全部"}
          </button>
        )}
      </div>
      {activeTab === "suggested" ? (
        <SuggestedQuestionCarousel
          busy={busy}
          heading="你可能想问"
          onRefresh={onRefresh}
          onSubmit={onSuggestedSubmit}
          questions={suggestions}
        />
      ) : (
        <div aria-label="大家常问" className="wst-popular" role="tabpanel">
          {expanded && categories.length > 1 && (
            <div aria-label="问题分类" className="wst-popular-categories" role="group">
              {["all", ...categories].map((value) => (
                <button
                  aria-pressed={selectedCategory === value}
                  key={value}
                  onClick={() => setCategory(value)}
                  type="button"
                >
                  {value === "all" ? "全部" : value}
                </button>
              ))}
            </div>
          )}
          <div className="wst-popular-list">
            {visibleItems.map((item) => {
              const rank = popular.items.indexOf(item);
              return (
                <button
                  aria-label={item.question}
                  className="wst-popular-item"
                  disabled={busy}
                  key={item.id}
                  onClick={() => onPopularSubmit(item)}
                  type="button"
                >
                  <span
                    aria-hidden="true"
                    className={`wst-popular-rank${rank < 3 ? " is-top" : ""}`}
                  >
                    {String(rank + 1).padStart(2, "0")}
                  </span>
                  <span className="wst-popular-question">{item.question}</span>
                  <span aria-hidden="true" className="wst-popular-topic">
                    {item.topic_key}
                  </span>
                  <ArrowUpRight aria-hidden="true" size={16} />
                </button>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
