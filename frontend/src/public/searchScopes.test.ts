import { describe, expect, it } from "vitest";

import { availableSearchScopes, orderedSearchScopes } from "./searchScopes";
import type { PublicSessionUser } from "./publicApi";

describe("问答资料范围权限", () => {
  it.each(["user", "admin"] as const)("%s 仅提供公共和本人资料", (role) => {
    const user: PublicSessionUser = { userId: "1", displayName: "测试", role };
    expect(availableSearchScopes(user).map((item) => item.id)).toEqual([
      "public",
      "personal",
    ]);
    expect(
      availableSearchScopes(user, [{ id: "hr_internal", label: "内部" }]),
    ).toEqual([]);
  });
  it.each([
    { userId: "2", displayName: "人力", role: "hr" as const },
    {
      userId: "325",
      displayName: "超管",
      role: "admin" as const,
      isSuperAdmin: true,
    },
  ])("人力或超管能选择内部资料，仍遵循服务端公开的选项", (user) => {
    expect(availableSearchScopes(user).map((item) => item.id)).toEqual([
      "public",
      "personal",
      "hr_internal",
    ]);
    expect(
      availableSearchScopes(user, [{ id: "public", label: "公开" }]).map(
        (item) => item.id,
      ),
    ).toEqual(["public"]);
  });
  it("选择顺序固定且去重，非法范围不会进入请求", () => {
    expect(
      orderedSearchScopes(
        ["personal", "public", "personal", "hr_internal"],
        availableSearchScopes(),
      ),
    ).toEqual(["public", "personal"]);
  });
});
