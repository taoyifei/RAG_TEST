import {
  currentReturnTo,
  WANSHITONG_BASE_PATH,
  withAppBase,
} from "../app/basePath";

const MAX_DRAFT_CHARS = 4_000;

function draftKey(): string {
  return `wanshitong-sso-draft:${withAppBase("/")}`;
}

/** 构造只回到当前 KB 页面、不会自动重发问题的 SSO entry 地址。 */
export function ssoEntryLocation(): string {
  const parameters = new URLSearchParams({ return_to: currentReturnTo() });
  return `${WANSHITONG_BASE_PATH}/sso/entry?${parameters.toString()}`;
}

/** 在整页登录前暂存待发送草稿；callback 后只恢复到输入框。 */
export function saveLoginDraft(question?: string): void {
  const value = question?.trim();
  if (!value || value.length > MAX_DRAFT_CHARS) return;
  try {
    window.sessionStorage.setItem(draftKey(), value);
  } catch {
    // 浏览器存储被禁用时仍允许继续登录。
  }
}

/** 消费一次登录前草稿，避免刷新后反复恢复旧问题。 */
export function consumeLoginDraft(): string {
  try {
    const key = draftKey();
    const value = window.sessionStorage.getItem(key) ?? "";
    window.sessionStorage.removeItem(key);
    return value;
  } catch {
    return "";
  }
}

/** 401 只触发浏览器整页登录，不在 fetch/SSE 内跟随 HTML 302。 */
export function redirectToSso(question?: string): void {
  saveLoginDraft(question);
  window.location.assign(ssoEntryLocation());
}
