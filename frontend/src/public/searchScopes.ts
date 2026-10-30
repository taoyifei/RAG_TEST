import type {
  PublicSearchScope,
  PublicSearchScopeOption,
  PublicSessionUser,
} from "./publicApi";

const OPTIONS: PublicSearchScopeOption[] = [
  { id: "public", label: "公共知识库" },
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
    .filter((option) => selected.includes(option.id))
    .map((option) => option.id);
}
