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
  generation_reason_code?: string | null;
  degraded_reason_codes?: string[];
  fallback_answer_available?: boolean;
  requested_answer_type?: string;
  query_semantic_source?: string;
  reason_code?: string;
  error_stage?: string;
  active_index_revision_id?: string;
  data_plane?: components["schemas"]["QueryDataPlane"];
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
    requested_answer_type?: string;
    query_semantic_source?: string;
    data_plane?: components["schemas"]["QueryDataPlane"] | null;
  } | null;
}

export interface HistoryPageResult extends Page<HistoryEntry> {
  total: number;
  total_is_exact?: boolean;
  body_enabled: boolean;
  retention_days: number;
  storage: string;
  search_complete?: boolean;
  scanned_count?: number;
  next_cursor?: string | null;
  truncation_reason?: "CANDIDATE_LIMIT" | "TIME_LIMIT";
  candidate_scan_limit?: number;
  time_scan_limit_ms?: number;
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

export type TraceMode = "SAFE" | "DIAGNOSTIC" | "FULL";

export interface OperationalTraceRoot {
  trace_id: string;
  schema_version: string;
  mode: TraceMode;
  kind: string;
  status: string;
  created_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  project_id?: string | null;
  knowledge_base_id?: string | null;
  request_id?: string | null;
  job_id?: string | null;
  document_id?: string | null;
  revision_id?: string | null;
  profile_id?: string | null;
  index_fingerprint?: string | null;
  serving_fingerprint: string;
  source_revision?: string | null;
  capture_complete: boolean;
  capture_incomplete_reason?: string | null;
  dropped_span_count: number;
  dropped_decision_count: number;
  writer_queue_high_water: number;
  feedback_useful?: boolean | null;
}

export interface OperationalTraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id?: string | null;
  sequence: number;
  name: string;
  kind: string;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  status: string;
  reason_code: string;
  attributes: Record<string, unknown>;
  input_artifact_id?: string | null;
  output_artifact_id?: string | null;
}

export interface OperationalTraceDecision {
  trace_id: string;
  sequence: number;
  stage: string;
  chunk_id: string;
  selected: boolean;
  reason_code: string;
  details: Record<string, unknown>;
  candidate_id?: string | null;
  evidence_id?: string | null;
  channel?: string | null;
  rank?: number | null;
  score_type?: string | null;
  score?: number | null;
  contribution?: number | null;
}

export interface OperationalTraceArtifact {
  artifact_id: string;
  trace_id: string;
  kind: string;
  media_type: string;
  sha256: string;
  original_bytes: number;
  compressed_bytes: number;
  created_at: string;
}

export interface OperationalTraceDetail {
  trace: OperationalTraceRoot;
  spans: OperationalTraceSpan[];
  candidate_decisions: OperationalTraceDecision[];
  artifacts: OperationalTraceArtifact[];
  legacy_flat_events: HistoryEntry["events"];
}

export interface OperationalTracePage {
  items: OperationalTraceRoot[];
  page: number;
  page_size: number;
  total: number;
}

export interface OperationalTraceFilters {
  page?: number;
  page_size?: number;
  trace_id?: string;
  created_from?: string;
  created_to?: string;
  kind?: string;
  status?: string;
  project_id?: string;
  knowledge_base_id?: string;
  request_id?: string;
  job_id?: string;
  document_id?: string;
  revision_id?: string;
  refusal_code?: string;
  error_code?: string;
  capture_mode?: TraceMode | "";
  capture_complete?: boolean;
  feedback_useful?: boolean;
}

export interface OperationalTraceArtifactContent {
  mediaType: string;
  sha256: string;
  body: string;
}

export interface DownloadFile {
  blob: Blob;
  filename: string;
}

export interface ProductFeedback {
  trace_id: string;
  project_id: string;
  knowledge_base_id: string;
  useful: boolean;
  reason_code?: string | null;
  projection_state: "PENDING" | "APPLIED" | "NOT_APPLICABLE";
  updated_at: string;
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
  corpus_authorization?: CorpusAuthorizationStatus;
  retrieval_data_plane?: RetrievalDataPlaneStatus;
}

export type CorpusAuthorizationStatus =
  components["schemas"]["CorpusAuthorizationStatus"];
export type CorpusAuthorizationApproval =
  components["schemas"]["CorpusAuthorizationApproval"];

export interface RetrievalDataPlaneStatus {
  retrieval_data_plane: "active_remote_profile" | "default_local_fallback";
  profile_state: string;
  active_retrieval_profile_revision_id?: string | null;
  pending_profile_revision_id?: string | null;
  activation_job_id?: string | null;
  active_index_revision_id?: string | null;
  index_fingerprint?: string | null;
  serving_fingerprint?: string | null;
  embedding_provider_id?: string | null;
  embedding_model?: string | null;
  selected_vector_space?: string | null;
  reranker_provider_id?: string | null;
  reranker_model?: string | null;
  dense_calibration_state: string;
  vector_coverage_complete: boolean;
  fallback_reason_codes: string[];
  remediation_path: string;
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
    indexed?: boolean;
    reason_code?: string | null;
  }[];
  media_count: number;
  recognized_count: number;
  pending_count: number;
  indexed_count?: number;
  rebuild_count?: number;
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

export interface StreamedAnswerClaim {
  claim_index: number;
  text: string;
  supports: { support_id: string; quote: string }[];
  active_index_revision_id: string;
}

export interface AnswerStreamHandlers {
  onMeta?: (traceId: string) => void;
  onStage?: (stage: string) => void;
  onClaim?: (claim: StreamedAnswerClaim) => void;
}

export interface AnswerStreamScope {
  projectId: string;
  knowledgeBaseId: string;
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

async function rawRequest(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
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
  return response;
}

async function downloadResponse(
  response: Response,
  fallbackFilename: string,
): Promise<DownloadFile> {
  return {
    blob: await response.blob(),
    filename: contentDispositionFilename(
      response.headers.get("Content-Disposition"),
      fallbackFilename,
    ),
  };
}

export function contentDispositionFilename(
  header: string | null,
  fallbackFilename: string,
): string {
  const fallback = safeFilename(fallbackFilename) ?? "download.bin";
  if (!header) return fallback;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
  const quoted = /filename="([^"]+)"/i.exec(header)?.[1];
  const plain = /filename=([^;\s]+)/i.exec(header)?.[1];
  let candidate = encoded ?? quoted ?? plain;
  if (!candidate) return fallback;
  if (encoded) {
    try {
      candidate = decodeURIComponent(candidate);
    } catch {
      return fallback;
    }
  }
  return safeFilename(candidate) ?? fallback;
}

function safeFilename(value: string): string | undefined {
  if (
    value !== value.trim() ||
    value === "." ||
    value === ".." ||
    value.length > 180 ||
    /[\\/]/.test(value) ||
    [...value].some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code === 127;
    }) ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)
  ) {
    return undefined;
  }
  return value;
}

export async function readSseResponse(
  response: Response,
  handlers: AnswerStreamHandlers = {},
  expectedScope?: AnswerStreamScope,
): Promise<QueryResponse> {
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorPayload;
    throw new ApiError(response.status, payload);
  }
  if (!response.body) throw new Error("SSE 响应缺少正文");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const maxFrameChars = 256 * 1024;
  const maxResponseChars = 4 * 1024 * 1024;
  let buffer = "";
  let responseChars = 0;
  let eventName = "message";
  let dataLines: string[] = [];
  let frameChars = 0;
  let lastSequence = -1;
  let lastClaimIndex = -1;
  let traceId = response.headers.get("X-Trace-Id") ?? "";
  let finalResult: QueryResponse | undefined;
  let streamCompleted = false;

  const dispatch = () => {
    if (!dataLines.length) {
      eventName = "message";
      frameChars = 0;
      return;
    }
    const data = dataLines.join("\n");
    dataLines = [];
    const payload = JSON.parse(data) as Record<string, unknown>;
    const versioned = payload.protocol === "rag-answer-sse-v1";
    if (finalResult) {
      throw new Error("SSE Final 后包含额外协议事件");
    }
    if (expectedScope && !versioned) {
      throw new Error("SSE 事件缺少协商的协议版本");
    }
    if (versioned) {
      if (payload.type !== eventName) {
        throw new Error("SSE 事件名称与类型不匹配");
      }
      const sequence = payload.sequence;
      if (
        typeof sequence !== "number" ||
        !Number.isInteger(sequence) ||
        sequence !== lastSequence + 1
      ) {
        throw new Error("SSE 事件序号不连续");
      }
      if (
        expectedScope &&
        (payload.project_id !== expectedScope.projectId ||
          payload.knowledge_base_id !== expectedScope.knowledgeBaseId)
      ) {
        throw new Error("SSE 事件知识库范围不匹配");
      }
      if (typeof payload.trace_id !== "string") {
        throw new Error("SSE 事件缺少 trace_id");
      }
      if (traceId && payload.trace_id !== traceId) {
        throw new Error("SSE 事件 trace_id 不匹配");
      }
      traceId = payload.trace_id;
      lastSequence = sequence;
    }
    if (eventName === "meta") {
      if (versioned && payload.delivery !== "incremental_or_final_only") {
        throw new Error("SSE meta 结构无效");
      }
      const value = payload.trace_id;
      if (typeof value === "string") handlers.onMeta?.(value);
    } else if (eventName === "stage") {
      if (typeof payload.stage === "string") handlers.onStage?.(payload.stage);
    } else if (eventName === "claim") {
      const claim = payload.claim as Record<string, unknown> | undefined;
      const supports = claim?.supports;
      if (
        !versioned ||
        typeof payload.claim_index !== "number" ||
        !Number.isInteger(payload.claim_index) ||
        payload.claim_index < 0 ||
        payload.claim_index !== lastClaimIndex + 1 ||
        typeof payload.active_index_revision_id !== "string" ||
        !/^irev_[0-9a-f]{32}$/.test(payload.active_index_revision_id) ||
        payload.provisional !== true ||
        typeof claim?.text !== "string" ||
        claim.text.length === 0 ||
        !Array.isArray(supports) ||
        supports.length === 0 ||
        !supports.every(
          (item) =>
            typeof item === "object" &&
            item !== null &&
            typeof (item as Record<string, unknown>).support_id === "string" &&
            typeof (item as Record<string, unknown>).quote === "string",
        )
      ) {
        throw new Error("SSE claim 结构无效");
      }
      lastClaimIndex = payload.claim_index;
      handlers.onClaim?.({
        claim_index: payload.claim_index,
        text: claim.text,
        supports: supports as { support_id: string; quote: string }[],
        active_index_revision_id: payload.active_index_revision_id,
      });
    } else if (eventName === "error") {
      if (
        versioned &&
        (typeof payload.code !== "string" ||
          typeof payload.message !== "string" ||
          typeof payload.stage !== "string" ||
          typeof payload.retryable !== "boolean" ||
          typeof payload.partial !== "boolean")
      ) {
        throw new Error("SSE error 结构无效");
      }
      const nested = payload.error as ErrorPayload["error"] | undefined;
      const detail = nested ?? {
        code: typeof payload.code === "string" ? payload.code : undefined,
        message:
          typeof payload.message === "string" ? payload.message : undefined,
        stage: typeof payload.stage === "string" ? payload.stage : undefined,
        retryable:
          typeof payload.retryable === "boolean"
            ? payload.retryable
            : undefined,
        trace_id:
          typeof payload.trace_id === "string" ? payload.trace_id : undefined,
      };
      throw new ApiError(500, { error: detail });
    } else if (eventName === "cancelled") {
      throw new Error("流式查询已取消");
    } else if (eventName === "final") {
      if (finalResult) throw new Error("SSE 响应包含重复 final 事件");
      if (
        versioned &&
        (typeof payload.status !== "string" ||
          typeof payload.reason_code !== "string" ||
          !(typeof payload.answer === "string" || payload.answer === null) ||
          !Array.isArray(payload.evidence) ||
          typeof payload.active_index_revision_id !== "string")
      ) {
        throw new Error("SSE final 结构无效");
      }
      finalResult = payload as unknown as QueryResponse;
    }
    eventName = "message";
    frameChars = 0;
  };

  const consumeLine = (rawLine: string) => {
    frameChars += rawLine.length + 1;
    if (frameChars > maxFrameChars) {
      throw new Error("SSE 单事件超过客户端上限");
    }
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (!line) {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    if (field === "data") dataLines.push(value);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      const decoded = decoder.decode(value, { stream: !done });
      responseChars += decoded.length;
      if (responseChars > maxResponseChars) {
        throw new Error("SSE 响应超过客户端上限");
      }
      buffer += decoded;
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        consumeLine(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
      }
      if (!done) continue;
      if (buffer) consumeLine(buffer);
      dispatch();
      break;
    }
    if (!finalResult) throw new Error("SSE 响应缺少合法终态");
    streamCompleted = true;
    return finalResult;
  } finally {
    if (!streamCompleted) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
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
  corpusAuthorization: (kbId: string) =>
    request<CorpusAuthorizationStatus>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/corpus-authorization`,
      "",
    ),
  approveCorpusAuthorization: (
    kbId: string,
    approval: CorpusAuthorizationApproval,
  ) =>
    request<CorpusAuthorizationStatus>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/corpus-authorization:approve`,
      "",
      jsonInit("POST", approval),
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
  exportHistoryTrace: async (traceId: string, includeHistoryBody: boolean) => {
    const params = new URLSearchParams({
      include_history_body: String(includeHistoryBody),
    });
    const response = await rawRequest(
      `/api/v1/admin/history-traces/${encodeURIComponent(traceId)}/export?${params}`,
    );
    return downloadResponse(response, `${traceId}-support.zip`);
  },
  exportHistoryTraces: async (
    traceIds: string[],
    includeHistoryBody: boolean,
  ) => {
    const response = await rawRequest(
      "/api/v1/admin/history-traces:export",
      jsonInit("POST", {
        trace_ids: traceIds,
        include_history_body: includeHistoryBody,
      }),
    );
    return downloadResponse(response, "history-traces-support.zip");
  },
  listOperationalTraces: (
    filters: OperationalTraceFilters = {},
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    return request<OperationalTracePage>(
      `/api/v1/admin/operational-traces?${params}`,
      "",
      { signal },
    );
  },
  operationalTraceDetail: (traceId: string, signal?: AbortSignal) =>
    request<OperationalTraceDetail>(
      `/api/v1/admin/operational-traces/${encodeURIComponent(traceId)}`,
      "",
      { signal },
    ),
  operationalTraceArtifact: async (
    traceId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<OperationalTraceArtifactContent> => {
    const response = await rawRequest(
      `/api/v1/admin/operational-traces/${encodeURIComponent(traceId)}/artifacts/${encodeURIComponent(artifactId)}`,
      { signal },
    );
    return {
      mediaType:
        response.headers.get("Content-Type") ?? "application/octet-stream",
      sha256: response.headers.get("X-Artifact-SHA256") ?? "",
      body: await response.text(),
    };
  },
  exportOperationalTrace: async (traceId: string) => {
    const response = await rawRequest(
      `/api/v1/admin/operational-traces/${encodeURIComponent(traceId)}/export`,
    );
    return downloadResponse(response, `${traceId}.json`);
  },
  exportOperationalTraces: async (traceIds: string[]) => {
    const response = await rawRequest(
      "/api/v1/admin/operational-traces:export",
      jsonInit("POST", { trace_ids: traceIds }),
    );
    return downloadResponse(response, "operational-traces.zip");
  },
  pruneOperationalTraces: () =>
    request<{ pruned: number }>("/api/v1/admin/operational-traces:prune", "", {
      method: "POST",
    }),
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
    conversationId?: string,
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
          ...(conversationId ? { conversation_id: conversationId } : {}),
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
    conversationId?: string,
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
          ...(conversationId ? { conversation_id: conversationId } : {}),
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
    historyMode?: "full" | "metadata_only",
    handlers: AnswerStreamHandlers = {},
    conversationId?: string,
  ) => {
    void token;
    const init = jsonInit("POST", {
      query,
      limit: 10,
      stream: true,
      stream_protocol: "rag-answer-sse-v1",
      ...(includeRelatedContent ? { include_related_content: true } : {}),
      ...(historyMode ? { history_mode: historyMode } : {}),
      ...(conversationId ? { conversation_id: conversationId } : {}),
    });
    const headers = new Headers(init.headers);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}:answer`,
      { ...init, headers, signal, credentials: "same-origin" },
    );
    return readSseResponse(response, handlers, {
      projectId,
      knowledgeBaseId: kbId,
    });
  },
  diagnostics: (token: string, traceId: string) =>
    request<RetrievalDiagnostics>(
      `/api/v1/admin/retrieval-diagnostics/${traceId}`,
      token,
    ),
  getFeedback: (
    projectId: string,
    kbId: string,
    traceId: string,
    signal?: AbortSignal,
  ) =>
    request<{ feedback: ProductFeedback | null }>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/queries/${encodeURIComponent(traceId)}/feedback`,
      "",
      { signal },
    ),
  putFeedback: (
    projectId: string,
    kbId: string,
    traceId: string,
    useful: boolean,
    reasonCode?: string,
  ) =>
    request<ProductFeedback>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/queries/${encodeURIComponent(traceId)}/feedback`,
      "",
      jsonInit("PUT", {
        useful,
        ...(reasonCode ? { reason_code: reasonCode } : {}),
      }),
    ),
  clearConversation: (
    projectId: string,
    kbId: string,
    conversationId: string,
  ) =>
    request<{
      conversation_id: string;
      owner_id: string;
      deleted: boolean;
      deleted_turns: number;
    }>(
      `/api/v1/projects/${projectId}/knowledge-bases/${kbId}/conversations/${encodeURIComponent(conversationId)}`,
      "",
      { method: "DELETE" },
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
