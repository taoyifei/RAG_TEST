import type {
  PublicSearchScope,
  PublicSearchScopeOption,
  PublicSessionUser,
} from "./publicApi";

const OPTIONS: PublicSearchScopeOption[] = [
  { id: "public", label: "公共知识库" },
  { id: "hr_public", label: "公开人力制度" },
  { id: "personal", label: "我的资料" },
  { id: "hr_internal", label: "人力内部资料" },
];

export function availableSearchScopes(
  user?: PublicSessionUser,
  supplied?: PublicSearchScopeOption[],
): PublicSearchScopeOption[] {
  const canReadHr = user?.role === "hr" || user?.isSuperAdmin === true;
  return OPTIONS.filter(
    (option) =>
      (option.id !== "hr_internal" || canReadHr) &&
      (!supplied || supplied.some((value) => value.id === option.id)),
  );
}

export function orderedSearchScopes(
  selected: readonly PublicSearchScope[],
  available: readonly PublicSearchScopeOption[],
): PublicSearchScope[] {
  return available
    .filter(
      (option) =>
        selected.includes(option.id) &&
        !(option.id === "hr_public" && selected.includes("public")),
    )
    .map((option) => option.id);
}

export function toggleSearchScope(
  selected: readonly PublicSearchScope[],
  scope: PublicSearchScope,
  available: readonly PublicSearchScopeOption[],
): PublicSearchScope[] {
  if (selected.includes(scope)) {
    return orderedSearchScopes(
      selected.filter((value) => value !== scope),
      available,
    );
  }
  // 公共知识库已经包含公开人力制度，直接选制度时切换到较小范围。
  const replaced =
    scope === "public" ? "hr_public" : scope === "hr_public" ? "public" : undefined;
  return orderedSearchScopes(
    [...selected.filter((value) => value !== replaced), scope],
    available,
  );
}
