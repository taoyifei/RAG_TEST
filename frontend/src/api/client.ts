import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type KnowledgeBase = components["schemas"]["KnowledgeBase"];
export type Document = components["schemas"]["Document"];
export type DocumentVersion = components["schemas"]["DocumentVersion"];
export type Job = components["schemas"]["Job"] & {
  error_code?: string | null;
  trace_id?: string | null;
};
export type JobPage = components["schemas"]["JobPage"];
export type RevisionInspection = components["schemas"]["RevisionInspection"];
export type ChunkPage = components["schemas"]["ChunkPage"];
export type QueryResponse = components["schemas"]["QueryResponse"];
export type RetrievalDiagnostics =
  components["schemas"]["RetrievalDiagnostics"];
export type SystemStatus = components["schemas"]["SystemStatus"];
export type Evidence = components["schemas"]["EvidenceItem"];
export type RelatedContent = components["schemas"]["RelatedContent"];
export type SourceChunk = components["schemas"]["Chunk"];

export interface Page<T> {
  items: T[];
  total?: number;
  offset: number;
  page_size: number;
  next_offset?: number | null;
}

export interface HistoryEntry {
  trace_id: string;
  project_id: string;
  knowledge_base_id: string;
  created_at: string;
  finished_at?: string | null;
  status: string;
  duration_ms?: number | null;
  question?: string | null;
  answer?: string | null;
  answer_summary: string;
  body_saved: boolean;
  body_available: boolean;
  body_message: string;
  cache_hit?: boolean;
  generation_mode?: string;
  reason_code?: string;
  error_stage?: string;
  active_index_revision_id?: string;
  models?: string[];
  provider_usage?: {
    operation: string;
    call_count: number;
    reason_code?: string | null;
    usage?: number | string | null;
  }[];
  events?: { event_name: string; occurred_at: string; attributes: unknown }[];
  diagnostics?: RetrievalDiagnostics;
  result?: {
    evidence?: Evidence[];
    related_contents?: RelatedContent[];
    generation_mode?: string;
  } | null;
}

export interface HistoryPageResult extends Page<HistoryEntry> {
  total: number;
  body_enabled: boolean;
  retention_days: number;
  storage: string;
}

export interface HistoryFilters {
  project_id?: string;
  knowledge_base_id?: string;
  status?: string;
  created_from?: string;
  created_to?: string;
  keyword?: string;
  page_size?: number;
  offset?: number;
}

export interface Tokens {
  admin: string;
  query: string;
}

export interface KnowledgeBaseModelSettings {
  generation_connection_id: string | null;
  generation_model: string | null;
  rewrite_enabled: boolean;
  ocr_connection_id: string | null;
  ocr_model: string | null;
  ocr_enabled: boolean;
  budget_campaign_id: string | null;
  generation_configured?: boolean;
  ocr_configured?: boolean;
}

export interface DocumentOcrScan {
  media: {
    media_sha256: string;
    artifact_id: string;
    part_uri: string;
    media_type: string;
    size_bytes: number;
    width?: number | null;
    height?: number | null;
    supported: boolean;
    approved: boolean;
    cached: boolean;
    reason_code?: string | null;
  }[];
  media_count: number;
  recognized_count: number;
  pending_count: number;
}

export interface ConsoleSession {
  authenticated: boolean;
  session_id: string;
  csrf_token: string;
  expires_in: number;
}

export interface CredentialSummary {
  credential_id: string;
  provider_type: "jina" | "aliyun-model-studio";
  configured: boolean;
  source: "environment_managed" | "database_encrypted";
  masked_hint: string;
  key_version: number;
  status: string;
}

export interface ProviderConnection {
  connection_id: string;
  display_name: string;
  provider_type: "jina" | "aliyun-model-studio";
  credential_id: string;
  status: string;
  workspace_id?: string | null;
  region?: string | null;
  configuration_version: number;
  endpoint_mode?: "workspace_host" | "beijing_dashscope" | "";
  api_host?: string | null;
  request_budget?: number;
  token_budget?: number;
  enabled?: boolean;
}

export interface CatalogProvider {
  provider_type: "jina" | "aliyun-model-studio";
  display_name: string;
  operations: string[];
  models: string[];
  regions: string[];
  endpoint_profiles: string[];
  operation_models: Record<string, string[]>;
}

export interface ProviderCatalog {
  catalog_version: string;
  providers: CatalogProvider[];
}

export interface ProviderValidation {
  validation_id: string;
  connection_id: string;
  operation: string;
  provider_model: string;
  status: string;
  http_category: string;
  safe_error_code?: string | null;
  dimension?: number | null;
  finished_at: string;
  stage?: string;
  request_dispatched?: boolean | null;
  http_status?: number | null;
  provider_code?: string | null;
  provider_request_id?: string | null;
  configuration_version: number;
  credential_key_version: number;
  catalog_version: string;
  validation_mode: string;
  request_policy_identity: string;
  is_current?: boolean;
}

export interface ProviderUsageDaily {
  usage_date: string;
  connection_id: string;
  operation: string;
  request_count: number;
  successful_requests: number;
  failed_requests: number;
  estimated_tokens: number;
  observed_tokens: number;
  retry_count: number;
  rate_limit_count: number;
  failover_count: number;
  cache_hit_count: number;
  average_latency_ms: number;
}

export interface ImpactPreview {
  impact: "NO_REINDEX" | "SERVING_RELOAD" | "NEW_INDEX_REVISION_REQUIRED";
  proposed_profile_revision_id: string;
  current_profile_revision_id?: string | null;
  index_fingerprint_changed: boolean;
  serving_fingerprint_changed: boolean;
}

export interface RetrievalProfile {
  profile_revision_id: string;
  knowledge_base_id: string;
  status: string;
  primary_connection_id: string;
  primary_embedding_model: string;
  standby_connection_id?: string | null;
  standby_embedding_model?: string | null;
  reranker_connection_id?: string | null;
  reranker_model?: string | null;
  index_semantic_fingerprint: string;
  serving_fingerprint: string;
  primary_dimension: number;
  standby_dimension?: number | null;
  primary_document_policy: Record<string, unknown>;
  primary_query_policy: Record<string, unknown>;
  standby_document_policy: Record<string, unknown>;
  standby_query_policy: Record<string, unknown> & { query_instruct?: string };
  retrieval_policy: Record<string, unknown>;
  evidence_policy: Record<string, unknown>;
  standby_budget: { requests?: number; tokens?: number };
  failover_enabled: boolean;
  activation_job_id?: string | null;
  effective_serving_fingerprint?: string;
}

export interface AccessTokenSummary {
  token_id: string;
  name: string;
  scopes: string[];
  project_id?: string | null;
  knowledge_base_id?: string | null;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
  token?: string;
}

let csrfToken = "";

export function setBrowserCsrfToken(value: string): void {
  csrfToken = value;
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
  receivedStatus?: (status: number) => void,
): Promise<T> {
  void token;
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (!new Set(["GET", "HEAD", "OPTIONS"]).has(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw new ApiError(response.status, payload);
  }
  receivedStatus?.(response.status);
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
  method: "POST" | "PATCH" | "PUT",
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
  login: async (bootstrapToken: string) => {
    const response = await request<ConsoleSession>(
      "/api/v1/console/session",
      "",
      jsonInit("POST", { bootstrap_token: bootstrapToken }),
    );
    setBrowserCsrfToken(response.csrf_token);
    return response;
  },
  resumeSession: async () => {
    const response = await request<ConsoleSession>(
      "/api/v1/console/session",
      "",
    );
    setBrowserCsrfToken(response.csrf_token);
    return response;
  },
  rotateSession: async () => {
    const response = await request<ConsoleSession>(
      "/api/v1/console/session:rotate",
      "",
      { method: "POST" },
    );
    setBrowserCsrfToken(response.csrf_token);
    return response;
  },
  logout: async () => {
    await request<void>("/api/v1/console/session", "", { method: "DELETE" });
    setBrowserCsrfToken("");
  },
  providerCatalog: () =>
    request<ProviderCatalog>("/api/v1/provider-catalog", ""),
  modelSettings: (kbId: string) =>
    request<KnowledgeBaseModelSettings>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/model-settings`,
      "",
    ),
  scanDocumentImages: (kbId: string, documentId: string) =>
    request<DocumentOcrScan>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(documentId)}/ocr`,
      "",
    ),
  recognizeDocumentImages: (
    kbId: string,
    documentId: string,
    hashes: string[],
  ) =>
    request<Job>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(documentId)}/ocr`,
      "",
      jsonInit("POST", { confirmed_media_hashes: hashes }),
    ),
  documentImageUrl: (
    projectId: string,
    kbId: string,
    documentId: string,
    artifactId: string,
  ) =>
    `/api/v1/projects/${encodeURIComponent(projectId)}/knowledge-bases/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(documentId)}/images/${encodeURIComponent(artifactId)}`,
  saveModelSettings: (kbId: string, settings: KnowledgeBaseModelSettings) => {
    const {
      generation_connection_id,
      generation_model,
      rewrite_enabled,
      ocr_connection_id,
      ocr_model,
      ocr_enabled,
      budget_campaign_id,
    } = settings;
    return request<KnowledgeBaseModelSettings>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/model-settings`,
      "",
      jsonInit("PUT", {
        generation_connection_id,
        generation_model,
        rewrite_enabled,
        ocr_connection_id,
        ocr_model,
        ocr_enabled,
        budget_campaign_id,
      }),
    );
  },
  listProjects: (token: string, offset = 0) =>
    request<Page<Project>>(`/api/v1/projects?offset=${offset}`, token),
  createProject: (token: string, name: string, key: string) =>
    request<Project>(
      "/api/v1/projects",
      token,
      jsonInit("POST", { name }, key),
    ),
  listKnowledgeBases: (token: string, projectId: string, offset = 0) =>
    request<Page<KnowledgeBase>>(
      `/api/v1/projects/${projectId}/knowledge-bases?offset=${offset}`,
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
  listDocuments: (token: string, projectId: string, kbId: string, offset = 0) =>
    request<Page<Document>>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents?offset=${offset}`,
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
  deleteDocument: async (
    token: string,
    projectId: string,
    kbId: string,
    documentId: string,
  ) => {
    let statusCode = 200;
    const document = await request<Document | { status?: string } | void>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/documents/${documentId}`,
      token,
      { method: "DELETE" },
      (status) => {
        statusCode = status;
      },
    );
    return { statusCode, document };
  },
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
  jobTrace: (jobId: string) =>
    request<{
      trace_id: string;
      job: Job;
      events: NonNullable<HistoryEntry["events"]>;
    }>(`/api/v1/admin/traces?${new URLSearchParams({ job_id: jobId })}`, ""),
  retryJob: (token: string, jobId: string) =>
    request<Job>(`/api/v1/jobs/${jobId}:retry`, token, { method: "POST" }),
  cancelJob: (token: string, jobId: string) =>
    request<Job>(`/api/v1/jobs/${jobId}:cancel`, token, { method: "POST" }),
  listHistory: (filters: HistoryFilters = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    return request<HistoryPageResult>(`/api/v1/history?${params}`, "", {
      signal,
    });
  },
  historyDetail: (traceId: string, signal?: AbortSignal) =>
    request<HistoryEntry>(
      `/api/v1/history/${encodeURIComponent(traceId)}`,
      "",
      { signal },
    ),
  clearHistory: () =>
    request<void>("/api/v1/history", "", { method: "DELETE" }),
  readEvidenceSource: async (
    token: string,
    projectId: string,
    kbId: string,
    revisionId: string,
    evidence: Evidence,
    signal?: AbortSignal,
  ): Promise<SourceChunk> => {
    if (!evidence.document_id || !evidence.document_version_id) {
      throw new Error("引用缺少来源身份，无法读取原文。");
    }
    const params = new URLSearchParams({
      chunk_id: evidence.chunk_id,
      document_id: evidence.document_id,
      page_size: "1",
    });
    const page = await request<ChunkPage>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/revisions/${revisionId}/chunks?${params}`,
      token,
      { signal },
    );
    const chunk = page.items[0];
    if (
      !chunk ||
      chunk.chunk_id !== evidence.chunk_id ||
      chunk.project_id !== projectId ||
      chunk.knowledge_base_id !== kbId ||
      chunk.index_revision_id !== revisionId ||
      chunk.version.document_id !== evidence.document_id ||
      chunk.version.document_version_id !== evidence.document_version_id
    ) {
      throw new Error("原文当前不可读取，请重新查询。");
    }
    return chunk;
  },
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
  readRelatedSource: async (
    token: string,
    projectId: string,
    kbId: string,
    related: RelatedContent,
    signal?: AbortSignal,
  ): Promise<SourceChunk> => {
    const params = new URLSearchParams({
      chunk_id: related.chunk_id,
      document_id: related.document_id,
      page_size: "1",
    });
    const page = await request<ChunkPage>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/revisions/${related.index_revision_id}/chunks?${params}`,
      token,
      { signal },
    );
    const chunk = page.items[0];
    if (
      !chunk ||
      chunk.chunk_id !== related.chunk_id ||
      chunk.project_id !== projectId ||
      chunk.knowledge_base_id !== kbId ||
      chunk.index_revision_id !== related.index_revision_id ||
      chunk.version.document_id !== related.document_id ||
      chunk.version.document_version_id !== related.document_version_id
    ) {
      throw new Error("原文当前不可读取，请重新查询。");
    }
    return chunk;
  },
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
    includeRelatedContent = false,
    historyMode?: "full" | "metadata_only",
  ) =>
    request<QueryResponse>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:search`,
      token,
      {
        ...jsonInit("POST", {
          query,
          limit: 10,
          stream: false,
          ...(includeRelatedContent ? { include_related_content: true } : {}),
          ...(historyMode ? { history_mode: historyMode } : {}),
        }),
        signal,
      },
    ),
  answer: (
    token: string,
    projectId: string,
    kbId: string,
    query: string,
    signal?: AbortSignal,
    includeRelatedContent = false,
    historyMode?: "full" | "metadata_only",
  ) =>
    request<QueryResponse>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:answer`,
      token,
      {
        ...jsonInit("POST", {
          query,
          limit: 10,
          stream: false,
          ...(includeRelatedContent ? { include_related_content: true } : {}),
          ...(historyMode ? { history_mode: historyMode } : {}),
        }),
        signal,
      },
    ),
  answerStream: async (
    token: string,
    projectId: string,
    kbId: string,
    query: string,
    signal?: AbortSignal,
    includeRelatedContent = false,
  ) => {
    void token;
    const init = jsonInit("POST", {
      query,
      limit: 10,
      stream: true,
      ...(includeRelatedContent ? { include_related_content: true } : {}),
    });
    const headers = new Headers(init.headers);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:answer`,
      { ...init, headers, signal, credentials: "same-origin" },
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
  listCredentials: () =>
    request<{ items: CredentialSummary[] }>("/api/v1/provider-credentials", ""),
  createCredential: (body: Record<string, unknown>) =>
    request<CredentialSummary>(
      "/api/v1/provider-credentials",
      "",
      jsonInit("POST", body),
    ),
  rotateCredential: (credentialId: string, value: string) =>
    request<CredentialSummary>(
      `/api/v1/provider-credentials/${credentialId}:rotate`,
      "",
      jsonInit("POST", { secret_value: value }),
    ),
  listConnections: () =>
    request<{ items: ProviderConnection[] }>(
      "/api/v1/provider-connections",
      "",
    ),
  createConnection: (body: Record<string, unknown>) =>
    request<ProviderConnection>(
      "/api/v1/provider-connections",
      "",
      jsonInit("POST", body),
    ),
  updateConnection: (connectionId: string, body: Record<string, unknown>) =>
    request<ProviderConnection>(
      `/api/v1/provider-connections/${connectionId}`,
      "",
      jsonInit("PATCH", body),
    ),
  validateConnection: (connectionId: string, body: Record<string, unknown>) =>
    request<ProviderValidation>(
      `/api/v1/provider-connections/${connectionId}:validate`,
      "",
      jsonInit("POST", body),
    ),
  listValidations: (connectionId: string) =>
    request<{ items: ProviderValidation[] }>(
      `/api/v1/provider-connections/${connectionId}/validations`,
      "",
    ),
  listDailyProviderUsage: () =>
    request<{ items: ProviderUsageDaily[] }>(
      "/api/v1/provider-usage/daily",
      "",
    ),
  listRetrievalProfiles: (knowledgeBaseId: string) =>
    request<{ items: RetrievalProfile[] }>(
      `/api/v1/knowledge-bases/${knowledgeBaseId}/retrieval-profiles`,
      "",
    ),
  createRetrievalProfile: (
    knowledgeBaseId: string,
    body: Record<string, unknown>,
  ) =>
    request<RetrievalProfile>(
      `/api/v1/knowledge-bases/${knowledgeBaseId}/retrieval-profiles`,
      "",
      jsonInit("POST", body),
    ),
  previewRetrievalProfile: (profileRevisionId: string) =>
    request<ImpactPreview>(
      `/api/v1/retrieval-profiles/${profileRevisionId}:preview`,
      "",
    ),
  activateRetrievalProfile: (
    profileRevisionId: string,
    impact: ImpactPreview["impact"],
  ) =>
    request<RetrievalProfile>(
      `/api/v1/retrieval-profiles/${profileRevisionId}:activate`,
      "",
      jsonInit("POST", { confirmed_impact: impact }),
    ),
  listAccessTokens: () =>
    request<{ items: AccessTokenSummary[] }>("/api/v1/access-tokens", ""),
  createAccessToken: (body: Record<string, unknown>) =>
    request<AccessTokenSummary>(
      "/api/v1/access-tokens",
      "",
      jsonInit("POST", body),
    ),
  revokeAccessToken: (tokenId: string) =>
    request<AccessTokenSummary>(`/api/v1/access-tokens/${tokenId}:revoke`, "", {
      method: "POST",
    }),
};

export function createIdempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}
