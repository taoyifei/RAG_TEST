import { PublicSseParseError } from "./publicSse";

export interface PublicSession {
  sessionId: string;
  csrfToken: string;
  expiresIn: number;
}

export interface PublicShortcut {
  id: string;
  label: string;
  prompt: string;
  visible: boolean;
}

export interface PublicCapabilities {
  mode: "wanshitong";
  stream: true;
  stream_protocol: "wanshitong-public-sse-v1";
  document_visibility: "all_internal";
  feedback: boolean;
  shortcuts?: PublicShortcut[];
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
    typeof body.error?.message === "string" ? body.error.message : undefined;
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

export async function createPublicSession(
  signal?: AbortSignal,
): Promise<PublicSession> {
  const response = await fetch(PUBLIC_SESSION_PATH, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  const body = await requireJson<{
    session_id: string;
    csrf_token: string;
    expires_in: number;
  }>(response);
  return {
    sessionId: body.session_id,
    csrfToken: body.csrf_token,
    expiresIn: body.expires_in,
  };
}

export async function getPublicCapabilities(
  signal?: AbortSignal,
): Promise<PublicCapabilities> {
  const response = await fetch(PUBLIC_CAPABILITIES_PATH, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  return requireJson<PublicCapabilities>(response);
}

export async function openPublicChat(options: {
  conversationId: string;
  csrfToken: string;
  question: string;
  signal: AbortSignal;
}): Promise<Response> {
  const response = await fetch(PUBLIC_CHAT_PATH, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({
      conversation_id: options.conversationId,
      query: options.question,
    }),
    signal: options.signal,
  });
  if (!response.ok) throw await readError(response);
  if (!response.body) {
    throw new PublicApiError("回答流不可用。", { status: response.status });
  }
  return response;
}

export async function sendPublicFeedback(options: {
  csrfToken: string;
  traceId: string;
  useful: boolean;
  signal?: AbortSignal;
}): Promise<void> {
  const response = await fetch(PUBLIC_FEEDBACK_PATH, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": options.csrfToken,
    },
    body: JSON.stringify({
      trace_id: options.traceId,
      useful: options.useful,
    }),
    signal: options.signal,
  });
  if (!response.ok) throw await readError(response);
}

export function publicErrorMessage(error: unknown): string {
  if (error instanceof PublicApiError) {
    if (error.status === 429 || error.code === "RATE_LIMITED") {
      return "当前使用人数较多，请稍后再试。";
    }
    if (error.status === 401 || error.status === 403) {
      return "会话已过期，请重新尝试。";
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
