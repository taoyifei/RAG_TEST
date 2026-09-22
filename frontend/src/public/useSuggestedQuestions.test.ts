import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  SUGGESTED_QUESTIONS,
  SUGGESTED_QUESTIONS_REVISION,
  type SuggestedQuestion,
  type SuggestedQuestionStyle,
} from "./suggestedQuestions";
import {
  pickSuggestedQuestions,
  useSuggestedQuestions,
} from "./useSuggestedQuestions";

function catalogItem(
  id: string,
  style: SuggestedQuestionStyle,
  overrides: Partial<SuggestedQuestion> = {},
): SuggestedQuestion {
  return {
    id,
    question: `${id}的问题？`,
    documentId: `DOC-${id}`,
    style,
    topicKey: `topic-${id}`,
    enabled: true,
    ...overrides,
  };
}

function styleCounts(items: readonly SuggestedQuestion[]) {
  return Object.fromEntries(
    (["SHORT", "STANDARD", "COMPOUND"] as const).map((style) => [
      style,
      items.filter((item) => item.style === style).length,
    ]),
  );
}

describe("湾事通推荐问题", () => {
  it("首批题库使用稳定身份和明确的三类配额", () => {
    const enabled = SUGGESTED_QUESTIONS.filter((item) => item.enabled);
    expect(SUGGESTED_QUESTIONS_REVISION).toBe("2026-09-22-f01-r1");
    expect(enabled.length).toBeGreaterThanOrEqual(12);
    expect(enabled.length).toBeLessThanOrEqual(18);
    expect(new Set(enabled.map((item) => item.id)).size).toBe(enabled.length);
    expect(new Set(enabled.map((item) => item.question)).size).toBe(
      enabled.length,
    );
    expect(new Set(enabled.map((item) => item.documentId)).size).toBe(
      enabled.length,
    );
    expect(new Set(enabled.map((item) => item.topicKey)).size).toBe(
      enabled.length,
    );
    expect(styleCounts(enabled)).toEqual({
      SHORT: 9,
      STANDARD: 3,
      COMPOUND: 3,
    });
    expect(
      enabled.every(
        (item) =>
          item.id.startsWith("sq-") &&
          !item.id.includes(item.documentId.toLocaleLowerCase()),
      ),
    ).toBe(true);
    expect(enabled.map((item) => item.question)).toEqual(
      expect.arrayContaining([
        "上线安全评估要提前多久申请？",
        "固定资产从什么时候开始折旧？",
        "研发工时怎么填、怎么留痕？",
        "业务团队收到设计文档后怎么确认？",
        "公司公章怎么申请？",
        "研发外协费用需要先有预算吗？",
      ]),
    );
    expect(
      enabled.some(
        (item) =>
          item.question.includes("必须") && item.question.includes("不能"),
      ),
    ).toBe(true);
    expect(enabled.some((item) => item.question.includes("工作日"))).toBe(true);
  });

  it("每组优先未展示题，并按三短一标准一复合选择", () => {
    const random = () => 0.25;
    const first = pickSuggestedQuestions([], [], new Set(), random);
    const seen = new Set(first.map((item) => item.id));
    const asked = `  ${first[0].question.replace(/？$/u, "?")}  `;
    const second = pickSuggestedQuestions([asked], first, seen, random);

    expect(first).toHaveLength(5);
    expect(styleCounts(first)).toEqual({
      SHORT: 3,
      STANDARD: 1,
      COMPOUND: 1,
    });
    expect(new Set(first.map((item) => item.documentId)).size).toBe(5);
    expect(new Set(first.map((item) => item.topicKey)).size).toBe(5);
    expect(second).toHaveLength(5);
    expect(second.map((item) => item.id)).not.toContain(first[0].id);
    expect(second.every((item) => !seen.has(item.id))).toBe(true);
  });

  it("连续换一换耗尽新题后才回用，且不回用上一批", () => {
    const random = () => 0.37;
    const seen = new Set<string>();
    let previous: SuggestedQuestion[] = [];
    const batches: SuggestedQuestion[][] = [];

    for (let index = 0; index < 4; index += 1) {
      const next = pickSuggestedQuestions([], previous, seen, random);
      expect(next).toHaveLength(5);
      expect(
        next.every((item) => !previous.some((old) => old.id === item.id)),
      ).toBe(true);
      batches.push(next);
      next.forEach((item) => seen.add(item.id));
      previous = next;
    }

    expect(
      new Set(
        batches
          .slice(0, 3)
          .flat()
          .map((item) => item.id),
      ).size,
    ).toBe(15);
    expect(batches[3].some((item) => batches[0].includes(item))).toBe(true);
  });

  it("依次排除停用题、模板题、已问题和上一批", () => {
    const active = catalogItem("active", "SHORT");
    const disabled = catalogItem("disabled", "SHORT", { enabled: false });
    const template = catalogItem("template", "SHORT", {
      documentId: "DOCX-028",
    });
    const asked = catalogItem("asked", "SHORT");
    const previous = catalogItem("previous", "STANDARD");
    const remaining = catalogItem("remaining", "COMPOUND");
    const selected = pickSuggestedQuestions(
      [asked.question],
      [previous],
      new Set(),
      () => 0,
      [active, disabled, template, asked, previous, remaining],
    );

    expect(selected.map((item) => item.id).sort()).toEqual([
      "active",
      "remaining",
    ]);
  });

  it("配额不足时不重复凑数，并按ID和规范化题面双重去重", () => {
    const first = catalogItem("first", "SHORT", { question: "同一道题？" });
    const duplicateId = catalogItem("first", "STANDARD");
    const duplicateQuestion = catalogItem("duplicate-question", "COMPOUND", {
      question: " 同 一 道 题? ",
    });
    const standard = catalogItem("standard", "STANDARD");
    const selected = pickSuggestedQuestions([], [], new Set(), () => 0, [
      first,
      duplicateId,
      duplicateQuestion,
      standard,
    ]);

    expect(selected.map((item) => item.id).sort()).toEqual([
      "first",
      "standard",
    ]);
  });

  it("账号改变时清空已见记录，同账号刷新不清空", () => {
    const random = () => 0.25;
    const { result, rerender } = renderHook(
      ({ identityKey }) => useSuggestedQuestions([], identityKey, random),
      { initialProps: { identityKey: "candidate:user-a" } },
    );
    const initialIds = result.current.questions.map((item) => item.id);

    act(() => result.current.refresh());
    const refreshedIds = result.current.questions.map((item) => item.id);
    expect(refreshedIds.every((id) => !initialIds.includes(id))).toBe(true);

    rerender({ identityKey: "candidate:user-a" });
    expect(result.current.questions.map((item) => item.id)).toEqual(
      refreshedIds,
    );

    rerender({ identityKey: "candidate:user-b" });
    expect(result.current.questions.map((item) => item.id)).toEqual(initialIds);
  });
});
