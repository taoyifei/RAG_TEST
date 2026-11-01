import { describe, expect, it } from "vitest";

import {
  availableSearchScopes,
  orderedSearchScopes,
  toggleSearchScope,
} from "./searchScopes";
import type { PublicSessionUser } from "./publicApi";

describe("问答资料范围权限", () => {
  it.each(["user", "admin"] as const)("%s 可选择公共、人力公开和本人资料", (role) => {
    const user: PublicSessionUser = { userId: "1", displayName: "测试", role };
    expect(availableSearchScopes(user).map((item) => item.id)).toEqual([
      "public",
      "hr_public",
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
      "hr_public",
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
  it("公共范围覆盖公开人力制度，窄范围和本人资料组合不扩大权限", () => {
    const available = availableSearchScopes();
    expect(
      orderedSearchScopes(["personal", "hr_public", "public"], available),
    ).toEqual(["public", "personal"]);
    expect(
      toggleSearchScope(["public", "personal"], "hr_public", available),
    ).toEqual(["hr_public", "personal"]);
    expect(
      toggleSearchScope(["hr_public", "personal"], "public", available),
    ).toEqual(["public", "personal"]);
    expect(
      orderedSearchScopes(["hr_public", "hr_internal"], available),
    ).toEqual(["hr_public"]);
  });
});
