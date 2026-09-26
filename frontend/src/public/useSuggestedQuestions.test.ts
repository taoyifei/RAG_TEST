import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SuggestedQuestion } from "./suggestedQuestions";
import {
  pickSuggestedQuestions,
  useSuggestedQuestions,
} from "./useSuggestedQuestions";

const reviewed: SuggestedQuestion[] = Array.from(
  { length: 12 },
  (_, index) => ({
    id: `pq-${index}`,
    question: `审核通过的问题 ${index}？`,
    documentId: `kb-${index}`,
    style: index < 9 ? "SHORT" : index < 11 ? "STANDARD" : "COMPOUND",
    topicKey: `topic-${index}`,
    enabled: true,
  }),
);

describe("公共推荐仅使用审核题库", () => {
  it("没有审核题目时不显示任何前端内置题", () => {
    const { result } = renderHook(() => useSuggestedQuestions([], "user-a"));
    expect(result.current.questions).toEqual([]);
    expect(pickSuggestedQuestions([], [], new Set(), () => 0)).toEqual([]);
  });

  it("按已审核目录展示并换组，不推荐已问题", () => {
    const random = () => 0.25;
    const first = pickSuggestedQuestions([], [], new Set(), random, reviewed);
    const second = pickSuggestedQuestions(
      [first[0].question],
      first,
      new Set(first.map((item) => item.id)),
      random,
      reviewed,
    );
    expect(first).toHaveLength(5);
    expect(first.filter((item) => item.style === "SHORT")).toHaveLength(3);
    expect(first.filter((item) => item.style === "STANDARD")).toHaveLength(1);
    expect(first.filter((item) => item.style === "COMPOUND")).toHaveLength(1);
    expect(second).toHaveLength(5);
    expect(second.map((item) => item.question)).not.toContain(
      first[0].question,
    );
    expect(second.every((item) => reviewed.includes(item))).toBe(true);
  });

  it("目录只有少量审核题时换一换也不会凭空补题或清空", () => {
    const smallCatalog = reviewed.slice(0, 2);
    const first = pickSuggestedQuestions(
      [],
      [],
      new Set(),
      () => 0,
      smallCatalog,
    );
    const second = pickSuggestedQuestions(
      [],
      first,
      new Set(first.map((item) => item.id)),
      () => 0,
      smallCatalog,
    );
    expect(second.map((item) => item.id).sort()).toEqual(
      first.map((item) => item.id).sort(),
    );
  });

  it("只排除停用、已问和重复题面，不拦截管理员审核的模板题", () => {
    const catalog: SuggestedQuestion[] = [
      reviewed[0],
      { ...reviewed[1], enabled: false },
      { ...reviewed[2], documentId: "DOCX-028" },
      { ...reviewed[3], question: reviewed[0].question },
      reviewed[4],
    ];
    expect(
      pickSuggestedQuestions([], [], new Set(), () => 0, catalog)
        .map((item) => item.id)
        .sort(),
    ).toEqual([reviewed[0].id, reviewed[2].id, reviewed[4].id].sort());
    expect(
      pickSuggestedQuestions(
        [reviewed[0].question],
        [],
        new Set(),
        () => 0,
        catalog,
      ).map((item) => item.id).sort(),
    ).toEqual([reviewed[2].id, reviewed[4].id]);
  });

  it("题库加载和身份切换会重置轮播范围", async () => {
    const random = () => 0.25;
    const { result, rerender } = renderHook(
      ({ identityKey, catalog }) =>
        useSuggestedQuestions([], identityKey, catalog, random),
      {
        initialProps: {
          identityKey: "candidate:user-a",
          catalog: [] as SuggestedQuestion[],
        },
      },
    );
    expect(result.current.questions).toEqual([]);

    rerender({ identityKey: "candidate:user-a", catalog: reviewed });
    await waitFor(() => expect(result.current.questions).toHaveLength(5));
    const firstIds = result.current.questions.map((item) => item.id);
    expect(firstIds).toHaveLength(5);
    act(() => result.current.refresh());
    const secondIds = result.current.questions.map((item) => item.id);
    expect(secondIds).not.toEqual(firstIds);

    rerender({ identityKey: "candidate:user-b", catalog: reviewed });
    expect(result.current.questions.map((item) => item.id)).toEqual(firstIds);
  });
});
