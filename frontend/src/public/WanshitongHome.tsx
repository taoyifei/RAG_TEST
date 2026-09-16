import { ArrowUpRight, ChevronLeft, ChevronRight } from "lucide-react";
import { useRef } from "react";

import { PublicChatComposer } from "./PublicChatComposer";
import { SUGGESTED_QUESTIONS } from "./suggestedQuestions";

export function WanshitongHome({
  busy,
  onStop,
  onSubmit,
}: {
  busy: boolean;
  onStop: () => void;
  onSubmit: (question: string) => void;
}) {
  const suggestionsRef = useRef<HTMLDivElement>(null);

  const scrollSuggestions = (direction: -1 | 1) => {
    const element = suggestionsRef.current;
    if (!element) return;
    element.scrollBy({ left: direction * element.clientWidth * 0.75 });
  };

  return (
    <main className="wst-home" id="main-content">
      <div className="wst-home-content">
        <div className="wst-hero-copy">
          <span className="wst-wordmark">湾事通</span>
          <h1>你的内部知识助手</h1>
        </div>
        <section className="wst-suggestions" aria-label="你可能想问">
          <div className="wst-suggestions-heading">
            <span>你可能想问</span>
            <div className="wst-suggestions-controls">
              <button
                aria-label="向左浏览问题"
                onClick={() => scrollSuggestions(-1)}
                type="button"
              >
                <ChevronLeft aria-hidden="true" size={17} />
              </button>
              <button
                aria-label="向右浏览问题"
                onClick={() => scrollSuggestions(1)}
                type="button"
              >
                <ChevronRight aria-hidden="true" size={17} />
              </button>
            </div>
          </div>
          <div className="wst-suggestions-track" ref={suggestionsRef}>
            {SUGGESTED_QUESTIONS.map((question) => (
              <button
                className="wst-suggestion"
                disabled={busy}
                key={question}
                onClick={() => onSubmit(question)}
                type="button"
              >
                <span>{question}</span>
                <ArrowUpRight aria-hidden="true" size={15} />
              </button>
            ))}
          </div>
        </section>
        <PublicChatComposer busy={busy} onStop={onStop} onSubmit={onSubmit} />
      </div>
    </main>
  );
}
