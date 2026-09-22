import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SuggestedQuestionCarousel } from "./SuggestedQuestionCarousel";
import { SUGGESTED_QUESTIONS } from "./suggestedQuestions";

describe("推荐问题轮播", () => {
  it("逐项向上层传递完整推荐对象且不改写题面", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <SuggestedQuestionCarousel
        busy={false}
        onRefresh={vi.fn()}
        onSubmit={onSubmit}
        questions={[SUGGESTED_QUESTIONS[0]]}
      />,
    );

    for (const item of SUGGESTED_QUESTIONS) {
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
    expect(onSubmit).toHaveBeenCalledTimes(SUGGESTED_QUESTIONS.length);
  });

  it("忙碌时同时禁用换一换和推荐提交", () => {
    render(
      <SuggestedQuestionCarousel
        busy
        onRefresh={vi.fn()}
        onSubmit={vi.fn()}
        questions={[SUGGESTED_QUESTIONS[0]]}
      />,
    );

    expect(
      screen.getByRole("button", { name: "换一换推荐问题" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: SUGGESTED_QUESTIONS[0].question }),
    ).toBeDisabled();
  });
});
