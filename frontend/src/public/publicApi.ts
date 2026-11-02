import { withAppBase } from "../app/basePath";
import { PublicSseParseError } from "./publicSse";
import type { PublicCitation } from "./publicSse";

export interface PublicSessionUser {
  userId: string;
  displayName: string;
  username?: string;
  role?: "admin" | "hr" | "user";
  isSuperAdmin?: boolean;
  deptId?: string | null;
}

export interface PublicSession {
  sessionId: string;
  csrfToken: string;
  expiresIn: number;
  deploymentId?: string;
  user?: PublicSessionUser;
}

export interface PublicShortcut {
  shortcut_id: string;
  label: string;
  description: string;
  revision: number;
}

export type PublicSearchScope =
  | "public"
  | "hr_public"
  | "personal"
  | "hr_internal";

export interface PublicSearchScopeOption {
  id: PublicSearchScope;
  label: string;
}

export interface PublicCapabilities {
  mode: "wanshitong";
  stream: true;
  stream_protocol: "wanshitong-public-sse-v1" | "wanshitong-weknora-sse-v1";
  natural_stream_protocol?:
    | "wanshitong-natural-sse-v1"
    | "wanshitong-weknora-sse-v1";
  document_visibility: "all_internal";
  feedback: boolean;
  feedback_details?: boolean;
  request_usage_context?: boolean;
  shortcuts: PublicShortcut[];
  page_settings?: Partial<PublicPageSettings>;
  search_scopes?: PublicSearchScopeOption[];
  default_search_scopes?: PublicSearchScope[];
  workbench_enabled?: boolean;
}

export interface PublicWorkbench {
  workspace_id: string;
  search_scopes: PublicSearchScope[];
  scope_label: string;
}

export interface PublicPageSettings {
  welcome_text: string;
  input_placeholder: string;
  show_recommendations: boolean;
  show_history: boolean;
  allow_feedback: boolean;
  allow_source_download: boolean;
}

export interface PublicUsageContext {
  entrypoint: "manual" | "suggestion" | "popular" | "retry";
  recommendation_id?: string;
  retry_of_trace_id?: string;
}

export interface PublicPopularQuestion {
  id: string;
  question: string;
  topic_key: string;
}

export interface PublicPopularQuestions {
  mode: "POPULAR" | "EMPTY";
  generated_at: string | null;
  window_days: 7;
  items: PublicPopularQuestion[];
}

export type PublicFeedbackReasonDetail =
  | "INCORRECT"
  | "INCOMPLETE"
  | "WRONG_SOURCE"
  | "FALSE_REFUSAL"
  | "UNSAFE_ANSWER"
  | "TOO_SLOW"
  | "OTHER";

export interface PublicFeedbackSubmission {
  useful: boolean;
  reasonDetail?: PublicFeedbackReasonDetail;
  comment?: string;
}

interface PublicErrorBody {
  error?: {
    code?: unknown;
    message?: unknown;
  };
  detail?: unknown;
}

export class PublicApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryAfterSeconds?: number;

  constructor(
    message: string,
    options: {
      status: number;
      code?: string;
      retryAfterSeconds?: number;
    },
  ) {
    super(message);
    this.name = "PublicApiError";
    this.status = options.status;
    this.code = options.code;
    this.retryAfterSeconds = options.retryAfterSeconds;
  }
}

const PUBLIC_SESSION_PATH = "/api/public/session";
const PUBLIC_CAPABILITIES_PATH = "/api/public/capabilities";
const PUBLIC_CHAT_PATH = "/api/public/chat";
const PUBLIC_FEEDBACK_PATH = "/api/public/feedback";
const PUBLIC_POPULAR_QUESTIONS_PATH = "/api/public/popular-questions";

export interface PublicNaturalHistoryTurn {
  turn_id: string;
  trace_id: string;
  question: string;
  status: string;
  answer: string | null;
  citations: PublicCitation[];
  created_at: string;
  source_status?: "available" | "missing" | "not_created";
  native_is_completed?: boolean | null;
  partial?: boolean;
  truncated?: boolean;
  finish_reason?: string | null;
  native_message_id?: string | null;
  native_request_id?: string | null;
  search_scopes?: PublicSearchScope[];
}

export interface PublicNaturalSession {
  conversation_id: string;
  title: string;
  updated_at: string;
  archived?: boolean;
}

export interface PublicNaturalImageContext {
  conversationId: string;
  traceId: string;
}

/** 回答与引用配图共用本人会话的资源接口，服务端再次核对资料权限。 */
export function publicNaturalImageUrl(
  context: PublicNaturalImageContext,
  resource: string,
): string {
  const path =
    `/api/public/conversations/${encodeURIComponent(context.conversationId)}` +
    `/turns/${encodeURIComponent(context.traceId)}/resources` +
    `?file_path=${encodeURIComponent(resource)}`;
  return typeof window === "undefined" ? path : withAppBase(path);
}

async function readError(response: Response): Promise<PublicApiError> {
  let body: PublicErrorBody = {};
  try {
    body = (await response.json()) as PublicErrorBody;
  } catch {
    // 非 JSON 错误页不会直接暴露给公共用户。
  }
  const code =
    typeof body.error?.code === "string" ? body.error.code : undefined;
  const serverMessage =
    typeof body.error?.message === "string"
      ? body.error.message
      : typeof body.detail === "string"
        ? body.detail
        : undefined;
  const retryAfter = Number.parseInt(
    response.headers.get("Retry-After") ?? "",
    10,
  );
  return new PublicApiError(serverMessage ?? "公共服务请求失败。", {
    status: response.status,
    code,
    retryAfterSeconds:
      Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : undefined,
  });
}

async function requireJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

/** 工作助手只接受当前员工有权选择的范围，最终权限由服务端校验。 */
export async function bootstrapPublicWorkbench(
  searchScopes: readonly PublicSearchScope[],
  csrfToken: string,
  signal?: AbortSignal,
): Promise<PublicWorkbench> {
  const response = await fetch(withAppBase("/api/public/workbench/bootstrap"), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ search_scopes: searchScopes }),
    signal,
  });
  const workspace = await requireJson<PublicWorkbench>(response);
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      workspace.workspace_id,
    ) ||
    !Array.isArray(workspace.search_scopes) ||
    workspace.search_scopes.length !== searchScopes.length ||
    new Set(workspace.search_scopes).size !== searchScopes.length ||
    workspace.search_scopes.some((scope) => !searchScopes.includes(scope)) ||
    typeof workspace.scope_label !== "string"
  ) {
    throw new Error("工作区响应不完整，请重试。");
  }
  return workspace;
}

export async function createPublicSession(
  signal?: AbortSignal,
): Promise<PublicSession> {
  const response = await fetch(withAppBase(PUBLIC_SESSION_PATH), {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  const body = await requireJson<{
    session_id: string;
    csrf_token: string;
    expires_in: number;
    deployment_id?: string;
    user?: {
      user_id: string;
      display_name: string;
      username?: string;
      role?: "admin" | "hr" | "user";
      is_super_admin?: boolean;
      dept_id?: string | null;
    };
  }>(response);
  return {
    sessionId: body.session_id,
    csrfToken: body.csrf_token,
    expiresIn: body.expires_in,
    deploymentId: body.deployment_id,
    user: body.user
      ? {
          userId: body.user.user_id,
          displayName: body.user.display_name,
          username: body.user.username,
          role: body.user.role ?? "user",
          isSuperAdmin: body.user.is_super_admin ?? false,
          deptId: body.user.dept_id,
        }
      : undefined,
  };
}

export async function getPublicCapabilities(
  signal?: AbortSignal,
): Promise<PublicCapabilities> {
  const response = await fetch(withAppBase(PUBLIC_CAPABILITIES_PATH), {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  return requireJson<PublicCapabilities>(response);
}

export async function getPopularQuestions(
  signal?: AbortSignal,
): Promise<PublicPopularQuestions> {
  const response = await fetch(withAppBase(PUBLIC_POPULAR_QUESTIONS_PATH), {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  return requireJson<PublicPopularQuestions>(response);
}

export async function openPublicChat(options: {
  conversationId: string;
  csrfToken: string;
  question: string;
  signal: AbortSignal;
  clientContext?: PublicUsageContext;
  naturalProtocol?: "wanshitong-natural-sse-v1" | "wanshitong-weknora-sse-v1";
  searchScopes?: PublicSearchScope[];
}): Promise<Response> {
  const response = await fetch(withAppBase(PUBLIC_CHAT_PATH), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
      ...(options.naturalProtocol
        ? { "X-Wanshitong-Stream-Protocol": options.naturalProtocol }
        : {}),
    },
    body: JSON.stringify({
      conversation_id: options.conversationId,
      query: options.question,
      search_scopes: options.searchScopes ?? ["public"],
      ...(options.clientContext
        ? { client_context: options.clientContext }
        : {}),
    }),
    signal: options.signal,
  });
  if (!response.ok) throw await readError(response);
  if (!response.body) {
    throw new PublicApiError("回答流不可用。", { status: response.status });
  }
  return response;
}

export async function continuePublicNaturalChat(options: {
  conversationId: string;
  csrfToken: string;
  traceId: string;
  signal: AbortSignal;
}): Promise<Response> {
  const path = `/api/public/chat/${encodeURIComponent(options.traceId)}/continue`;
  const response = await fetch(withAppBase(path), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({ conversation_id: options.conversationId }),
    signal: options.signal,
  });
  if (!response.ok) throw await readError(response);
  if (!response.body) {
    throw new PublicApiError("原回答无法恢复。", { status: response.status });
  }
  return response;
}

export async function stopPublicNaturalChat(options: {
  conversationId: string;
  csrfToken: string;
  traceId: string;
}): Promise<boolean> {
  const path = `/api/public/chat/${encodeURIComponent(options.traceId)}/stop`;
  const response = await fetch(withAppBase(path), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({ conversation_id: options.conversationId }),
  });
  const result = await requireJson<{ cancelled: boolean }>(response);
  return result.cancelled;
}

export async function getPublicNaturalHistory(options: {
  conversationId: string;
  csrfToken: string;
  signal?: AbortSignal;
}): Promise<PublicNaturalHistoryTurn[]> {
  const path = `/api/public/conversations/${encodeURIComponent(options.conversationId)}`;
  const response = await fetch(withAppBase(path), {
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    signal: options.signal,
  });
  const body = await requireJson<{ turns: PublicNaturalHistoryTurn[] }>(
    response,
  );
  return body.turns;
}

export async function getPublicNaturalSessions(options: {
  csrfToken: string;
  signal?: AbortSignal;
  archived?: boolean;
  query?: string;
  cursor?: string;
  limit?: number;
}): Promise<{
  items: PublicNaturalSession[];
  has_more: boolean;
  total: number;
  next_cursor: string | null;
}> {
  const parameters = new URLSearchParams();
  if (options.archived) parameters.set("archived", "true");
  if (options.query?.trim()) parameters.set("query", options.query.trim());
  if (options.cursor) parameters.set("cursor", options.cursor);
  if (options.limit !== undefined)
    parameters.set("limit", String(options.limit));
  const suffix = parameters.size ? `?${parameters}` : "";
  const path = `/api/public/conversations${suffix}`;
  const response = await fetch(withAppBase(path), {
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    signal: options.signal,
  });
  const body = await requireJson<{
    items: PublicNaturalSession[];
    has_more?: boolean;
    total?: number;
    next_cursor?: string | null;
  }>(response);
  return {
    items: body.items,
    has_more: body.has_more === true,
    total: body.total ?? body.items.length,
    next_cursor: body.next_cursor ?? null,
  };
}

export async function archivePublicNaturalConversation(options: {
  conversationId: string;
  archived: boolean;
  csrfToken: string;
}): Promise<void> {
  const path = `/api/public/conversations/${encodeURIComponent(options.conversationId)}/archive`;
  const response = await fetch(withAppBase(path), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({ archived: options.archived }),
  });
  await requireJson<{ conversation_id: string; archived: boolean }>(response);
}

export async function deletePublicNaturalConversation(options: {
  conversationId: string;
  csrfToken: string;
}): Promise<void> {
  const path = `/api/public/conversations/${encodeURIComponent(options.conversationId)}`;
  const response = await fetch(withAppBase(path), {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": options.csrfToken },
  });
  await requireJson<{ deleted: boolean }>(response);
}

export async function getPublicNaturalSource(options: {
  conversationId: string;
  traceId: string;
  referenceId: string;
  csrfToken: string;
  original?: boolean;
}): Promise<Blob> {
  const path =
    `/api/public/conversations/${encodeURIComponent(options.conversationId)}` +
    `/turns/${encodeURIComponent(options.traceId)}` +
    `/references/${encodeURIComponent(options.referenceId)}/${options.original ? "original" : "source"}`;
  const response = await fetch(withAppBase(path), {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": options.csrfToken },
  });
  if (!response.ok) throw await readError(response);
  return response.blob();
}

export async function sendPublicFeedback(options: {
  csrfToken: string;
  traceId: string;
  feedback: PublicFeedbackSubmission;
  signal?: AbortSignal;
}): Promise<void> {
  const reasonCodes: Record<PublicFeedbackReasonDetail, string> = {
    INCORRECT: "INCORRECT",
    INCOMPLETE: "INCOMPLETE",
    WRONG_SOURCE: "WRONG_SOURCE",
    FALSE_REFUSAL: "INCORRECT",
    UNSAFE_ANSWER: "INCORRECT",
    TOO_SLOW: "TOO_SLOW",
    OTHER: "OTHER",
  };
  const comment = options.feedback.comment?.trim() || undefined;
  const reasonDetail = options.feedback.reasonDetail;
  const response = await fetch(withAppBase(PUBLIC_FEEDBACK_PATH), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({
      trace_id: options.traceId,
      useful: options.feedback.useful,
      ...(reasonDetail
        ? {
            reason_code: reasonCodes[reasonDetail],
            reason_detail: reasonDetail,
          }
        : {}),
      ...(comment ? { comment } : {}),
    }),
    signal: options.signal,
  });
  if (!response.ok) throw await readError(response);
}

export async function logoutPublicSession(
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(withAppBase("/sso/logout"), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "X-CSRF-Token": csrfToken,
    },
    signal,
  });
  if (!response.ok) throw await readError(response);
}

export function publicErrorMessage(error: unknown): string {
  if (error instanceof PublicApiError) {
    if (error.status === 429 || error.code === "RATE_LIMITED") {
      return "当前使用人数较多，请稍后再试。";
    }
    if (error.status === 401) {
      return "会话已过期，请重新尝试。";
    }
    if (error.status === 403) {
      return error.message === "公共服务请求失败。"
        ? "当前角色没有访问所选资料的权限，请切换资料范围或新建对话。"
        : error.message;
    }
    if (error.status === 409) {
      return error.message === "公共服务请求失败。"
        ? "资料范围已变化，请新建对话后再试。"
        : error.message;
    }
    if (error.status === 503 || error.code === "QUEUE_LIMIT_EXCEEDED") {
      return "当前服务较忙或尚未就绪，请稍后再试。";
    }
    if (error.status >= 500) return "服务暂时不可用，请稍后再试。";
  }
  if (error instanceof PublicSseParseError) return error.message;
  if (error instanceof TypeError) {
    return "网络连接中断，请检查网络后重新尝试。";
  }
  return "暂时无法完成回答，请稍后再试。";
}
