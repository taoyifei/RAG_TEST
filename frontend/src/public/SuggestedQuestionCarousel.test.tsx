import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import type { SuggestedQuestion } from "./suggestedQuestions";

const reviewedQuestions: SuggestedQuestion[] = [
  {
    id: "pq-1",
    question: "审核通过的问题一？",
    documentId: "kb-1",
    style: "SHORT",
    topicKey: "制度",
    enabled: true,
  },
  {
    id: "pq-2",
    question: "审核通过的问题二？",
    documentId: "kb-2",
    style: "SHORT",
    topicKey: "流程",
    enabled: true,
  },
];

describe("推荐问题轮播", () => {
  it("逐项向上层传递完整推荐对象且不改写题面", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <SuggestedQuestionCarousel
        busy={false}
        onRefresh={vi.fn()}
        onSubmit={onSubmit}
        questions={[reviewedQuestions[0]]}
      />,
    );

    for (const item of reviewedQuestions) {
      rerender(
        <SuggestedQuestionCarousel
          busy={false}
          onRefresh={vi.fn()}
          onSubmit={onSubmit}
          questions={[item]}
        />,
      );
      await user.click(screen.getByRole("button", { name: item.question }));
      expect(onSubmit).toHaveBeenLastCalledWith(item);
    }
    expect(onSubmit).toHaveBeenCalledTimes(reviewedQuestions.length);
  });

  it("忙碌时同时禁用换一换和推荐提交", () => {
    render(
      <SuggestedQuestionCarousel
        busy
        onRefresh={vi.fn()}
        onSubmit={vi.fn()}
        questions={[reviewedQuestions[0]]}
      />,
    );

    expect(
      screen.getByRole("button", { name: "换一换推荐问题" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: reviewedQuestions[0].question }),
    ).toBeDisabled();
  });
});
