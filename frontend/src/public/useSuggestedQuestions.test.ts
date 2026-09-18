import { describe, expect, it } from "vitest";

import { SUGGESTED_QUESTIONS } from "./suggestedQuestions";
import { pickSuggestedQuestions } from "./useSuggestedQuestions";

describe("湾事通推荐问题", () => {
  it("题池有百题以上且只保留唯一题干", () => {
    expect(SUGGESTED_QUESTIONS.length).toBeGreaterThan(100);
    expect(new Set(SUGGESTED_QUESTIONS.map((item) => item.question)).size).toBe(
      SUGGESTED_QUESTIONS.length,
    );
  });

  it("轮换时优先未展示题，并排除已问及上一组", () => {
    const first = pickSuggestedQuestions([], [], new Set(), () => 0.25);
    const seen = new Set(first.map((item) => item.question));
    const asked = `  ${first[0].question.replace(/？$/u, "?")}  `;
    const second = pickSuggestedQuestions([asked], first, seen, () => 0.25);

    expect(first).toHaveLength(5);
    expect(second).toHaveLength(5);
    expect(second.map((item) => item.question)).not.toContain(
      first[0].question,
    );
    expect(second.every((item) => !seen.has(item.question))).toBe(true);
    expect(new Set(second.map((item) => item.documentId)).size).toBe(5);
  });

  it("暂不把模板相关问题放进任何一组推荐", () => {
    const templates = SUGGESTED_QUESTIONS.filter((item) =>
      item.question.includes("模板"),
    );
    expect(templates.length).toBeGreaterThan(0);
    const seen = new Set<string>();
    let previous: typeof templates = [];
    for (let index = 0; index < 20; index += 1) {
      const next = pickSuggestedQuestions([], previous, seen, () => 0.37);
      expect(next).toHaveLength(5);
      expect(next.every((item) => !templates.includes(item))).toBe(true);
      next.forEach((item) => seen.add(item.question));
      previous = next;
    }
  });
});
