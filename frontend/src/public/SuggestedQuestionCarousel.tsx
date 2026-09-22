import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { SuggestedQuestion } from "./suggestedQuestions";

export function SuggestedQuestionCarousel({
  busy,
  onRefresh,
  onSubmit,
  questions,
}: {
  busy: boolean;
  onRefresh: () => void;
  onSubmit: (question: string) => void;
  questions: readonly SuggestedQuestion[];
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  useEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    track.scrollLeft = 0;
    const update = () => {
      setCanScrollLeft(track.scrollLeft > 1);
      setCanScrollRight(
        track.scrollLeft + track.clientWidth < track.scrollWidth - 1,
      );
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [questions]);

  const scroll = (direction: -1 | 1) => {
    const track = trackRef.current;
    if (!track) return;
    track.scrollBy({ left: direction * track.clientWidth * 0.75 });
  };

  if (questions.length === 0) return null;

  return (
    <section className="wst-suggestions" aria-label="你可能想问">
      <div className="wst-suggestions-heading">
        <span>你可能想问</span>
        <div className="wst-suggestions-controls">
          <button
            aria-label="换一换推荐问题"
            className="wst-suggestions-refresh"
            disabled={busy}
            onClick={onRefresh}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={14} />
            换一换
          </button>
          <button
            aria-label="向左浏览问题"
            className="wst-suggestions-arrow"
            disabled={!canScrollLeft}
            onClick={() => scroll(-1)}
            type="button"
          >
            <ChevronLeft aria-hidden="true" size={17} />
          </button>
          <button
            aria-label="向右浏览问题"
            className="wst-suggestions-arrow"
            disabled={!canScrollRight}
            onClick={() => scroll(1)}
            type="button"
          >
            <ChevronRight aria-hidden="true" size={17} />
          </button>
        </div>
      </div>
      <div
        className="wst-suggestions-track"
        onScroll={() => {
          const track = trackRef.current;
          if (!track) return;
          setCanScrollLeft(track.scrollLeft > 1);
          setCanScrollRight(
            track.scrollLeft + track.clientWidth < track.scrollWidth - 1,
          );
        }}
        ref={trackRef}
      >
        {questions.map((item) => (
          <button
            className="wst-suggestion"
            disabled={busy}
            key={item.question}
            onClick={() => onSubmit(item.question)}
            type="button"
          >
            <span>{item.question}</span>
            <ArrowUpRight aria-hidden="true" size={15} />
          </button>
        ))}
      </div>
    </section>
  );
}
