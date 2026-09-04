import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type KnowledgeBase = components["schemas"]["KnowledgeBase"];
export type Document = components["schemas"]["Document"];
export type DocumentVersion = components["schemas"]["DocumentVersion"];
export type Job = components["schemas"]["Job"];
export type JobPage = components["schemas"]["JobPage"];
export type RevisionInspection = components["schemas"]["RevisionInspection"];
export type ChunkPage = components["schemas"]["ChunkPage"];
export type QueryResponse = components["schemas"]["QueryResponse"];
export type RetrievalDiagnostics =
  components["schemas"]["RetrievalDiagnostics"];
export type SystemStatus = components["schemas"]["SystemStatus"];
export type Evidence = components["schemas"]["EvidenceItem"];

export interface Page<T> {
  items: T[];
  total: number;
  offset: number;
  page_size: number;
  next_offset?: number | null;
}

export interface Tokens {
  admin: string;
  query: string;
}

export interface ProviderProbeResult {
  request_budget: number;
  last_explicit_probe_at: string;
  results: Record<string, unknown>[];
}

interface ErrorPayload {
  error?: {
    code?: string;
    message?: string;
    stage?: string;
    retryable?: boolean;
    trace_id?: string;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly stage?: string;
  readonly retryable: boolean;
  readonly traceId?: string;

  constructor(status: number, payload: ErrorPayload) {
    const detail = payload.error;
    super(detail?.message ?? `请求失败（HTTP ${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.code = detail?.code ?? "HTTP_ERROR";
    this.stage = detail?.stage;
    this.retryable = detail?.retryable ?? false;
    this.traceId = detail?.trace_id;
  }
}

async function request<T>(
  path: string,
  token: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw new ApiError(response.status, payload);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function readSseResponse(
  response: Response,
): Promise<QueryResponse> {
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw new ApiError(response.status, payload);
  }
  const body = await response.text();
  for (const frame of body.split("\n\n")) {
    const lines = frame.split("\n");
    const event = lines.find((line) => line.startsWith("event: "))?.slice(7);
    const data = lines.find((line) => line.startsWith("data: "))?.slice(6);
    if (!event || !data) continue;
    const payload = JSON.parse(data) as QueryResponse | ErrorPayload;
    if (event === "error") throw new ApiError(500, payload as ErrorPayload);
    if (event === "final") return payload as QueryResponse;
  }
  throw new Error("SSE 响应缺少 final 事件");
}

function jsonInit(
  method: "POST" | "PATCH",
  body: object,
  idempotencyKey?: string,
): RequestInit {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return { method, headers, body: JSON.stringify(body) };
}

export const api = {
  listProjects: (token: string) =>
    request<Page<Project>>("/api/v1/projects", token),
  createProject: (token: string, name: string, key: string) =>
    request<Project>(
      "/api/v1/projects",
      token,
      jsonInit("POST", { name }, key),
    ),
  listKnowledgeBases: (token: string, projectId: string) =>
    request<Page<KnowledgeBase>>(
      `/api/v1/projects/${projectId}/knowledge-bases`,
      token,
    ),
  createKnowledgeBase: (
    token: string,
    projectId: string,
    name: string,
    key: string,
  ) =>
    request<KnowledgeBase>(
      `/api/v1/projects/${projectId}/knowledge-bases`,
      token,
      jsonInit("POST", { name, description: "" }, key),
    ),
  listDocuments: (token: string, projectId: string, kbId: string) =>
    request<Page<Document>>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents`,
      token,
    ),
  getDocument: (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
  ) =>
    request<Document>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}`,
      token,
    ),
  listVersions: (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
  ) =>
    request<Page<DocumentVersion>>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}/versions`,
      token,
    ),
  renameDocument: (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
    displayName: string,
  ) =>
    request<Document>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}`,
      token,
      jsonInit("PATCH", { display_name: displayName }),
    ),
  deleteDocument: (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
  ) =>
    request<Document>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}`,
      token,
      { method: "DELETE" },
    ),
  uploadDocument: (
    token: string,
    projectId: string,
    kbId: string,
    file: File,
    idempotencyKey: string,
  ) => {
    const params = new URLSearchParams({ display_name: file.name });
    return request<Job>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents?${params}`,
      token,
      {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "Idempotency-Key": idempotencyKey,
        },
        body: file,
      },
    );
  },
  uploadVersion: (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
    file: File,
    idempotencyKey: string,
  ) =>
    request<Job>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}/versions`,
      token,
      {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "Idempotency-Key": idempotencyKey,
        },
        body: file,
      },
    ),
  listJobs: (token: string, projectId?: string, kbId?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (kbId) params.set("knowledge_base_id", kbId);
    return request<JobPage>(`/api/v1/jobs?${params}`, token);
  },
  getJob: (token: string, jobId: string) =>
    request<Job>(`/api/v1/jobs/${jobId}`, token),
  inspectRevision: (
    token: string,
    projectId: string,
    kbId: string,
    revisionId: string,
  ) =>
    request<RevisionInspection>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/revisions/${revisionId}`,
      token,
    ),
  listChunks: (
    token: string,
    projectId: string,
    kbId: string,
    revisionId: string,
  ) =>
    request<ChunkPage>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/revisions/${revisionId}/chunks`,
      token,
    ),
  revisionReports: (
    token: string,
    projectId: string,
    kbId: string,
    revisionId: string,
  ) =>
    request<{ items: Record<string, unknown>[] }>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/revisions/${revisionId}/reports`,
      token,
    ),
  search: (
    token: string,
    projectId: string,
    kbId: string,
    query: string,
    signal?: AbortSignal,
  ) =>
    request<QueryResponse>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:search`,
      token,
      { ...jsonInit("POST", { query, limit: 10, stream: false }), signal },
    ),
  answer: (token: string, projectId: string, kbId: string, query: string) =>
    request<QueryResponse>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:answer`,
      token,
      jsonInit("POST", { query, limit: 10, stream: false }),
    ),
  answerStream: async (
    token: string,
    projectId: string,
    kbId: string,
    query: string,
    signal?: AbortSignal,
  ) => {
    const init = jsonInit("POST", { query, limit: 10, stream: true });
    const headers = new Headers(init.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:answer`,
      { ...init, headers, signal },
    );
    return readSseResponse(response);
  },
  diagnostics: (token: string, traceId: string) =>
    request<RetrievalDiagnostics>(
      `/api/v1/admin/retrieval-diagnostics/${traceId}`,
      token,
    ),
  system: (token: string) =>
    request<SystemStatus>("/api/v1/system/components", token),
  probe: (token: string, requestBudget = 1) =>
    request<ProviderProbeResult>("/api/v1/system/providers:probe", token, {
      method: "POST",
      headers: {
        "X-Allow-Network": "true",
        "X-Request-Budget": String(requestBudget),
      },
    }),
};

export function createIdempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}
