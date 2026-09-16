import { describe, expect, it } from "vitest";

import { SUGGESTED_QUESTIONS } from "./suggestedQuestions";
import { pickSuggestedQuestions } from "./useSuggestedQuestions";

describe("湾事通推荐问题", () => {
  it("题池有百题以上且只保留唯一题干与来源 ID", () => {
    expect(SUGGESTED_QUESTIONS.length).toBeGreaterThan(100);
    expect(new Set(SUGGESTED_QUESTIONS.map((item) => item.caseId)).size).toBe(
      SUGGESTED_QUESTIONS.length,
    );
    expect(new Set(SUGGESTED_QUESTIONS.map((item) => item.question)).size).toBe(
      SUGGESTED_QUESTIONS.length,
    );
  });

  it("轮换时优先未展示题，并排除已问及上一组", () => {
    const first = pickSuggestedQuestions([], [], new Set(), () => 0.25);
    const seen = new Set(first.map((item) => item.caseId));
    const asked = `  ${first[0].question.replace(/？$/u, "?")}  `;
    const second = pickSuggestedQuestions([asked], first, seen, () => 0.25);

    expect(first).toHaveLength(5);
    expect(second).toHaveLength(5);
    expect(second.map((item) => item.caseId)).not.toContain(first[0].caseId);
    expect(second.every((item) => !seen.has(item.caseId))).toBe(true);
    expect(new Set(second.map((item) => item.documentId)).size).toBe(5);
  });
});
