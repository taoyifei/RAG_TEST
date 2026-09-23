import { useState } from "react";

import type {
  PublicPopularQuestion,
  PublicPopularQuestions,
} from "./publicApi";
import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";

export function PublicQuestionTabs({
  busy,
  heading,
  onRefresh,
  onPopularSubmit,
  onSuggestedSubmit,
  popular,
  suggestions,
}: {
  busy: boolean;
  heading?: string;
  onRefresh: () => void;
  onPopularSubmit: (question: PublicPopularQuestion) => void;
  onSuggestedSubmit: (question: SuggestedQuestion) => void;
  popular: PublicPopularQuestions;
  suggestions: readonly SuggestedQuestion[];
}) {
  const [tab, setTab] = useState<"suggested" | "popular">("suggested");
  const hasPopular = popular.mode === "POPULAR" && popular.items.length > 0;
  const activeTab = hasPopular ? tab : "suggested";

  return (
    <section className="wst-question-tabs" aria-label={heading ?? "推荐问题"}>
      {heading && <h2>{heading}</h2>}
      <div className="wst-question-tab-buttons" role="tablist">
        <button
          aria-selected={activeTab === "suggested"}
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
      {!hasPopular && (
        <p className="wst-question-note">
          当前展示示例问题，暂无符合条件的真实热榜。
        </p>
      )}
      {activeTab === "suggested" ? (
        <SuggestedQuestionCarousel
          busy={busy}
          heading="你可能想问"
          onRefresh={onRefresh}
          onSubmit={onSuggestedSubmit}
          questions={suggestions}
        />
      ) : (
        <div className="wst-popular-list" role="tabpanel">
          <p className="wst-question-note">
            根据最近 {popular.window_days} 天已完成统计排序 · 更新于{" "}
            {popular.generated_at
              ? new Date(popular.generated_at).toLocaleString("zh-CN")
              : "未知"}
          </p>
          {popular.items.map((item) => (
            <button
              disabled={busy}
              key={item.id}
              onClick={() => onPopularSubmit(item)}
              type="button"
            >
              {item.question}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
