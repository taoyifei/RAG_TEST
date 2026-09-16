import {
  consoleRequest,
  consoleRawRequest,
  type HistoryEntry,
  type HistoryFilters,
  type HistoryPageResult,
  type Job,
  type OperationalTraceDetail,
  type OperationalTraceFilters,
  type OperationalTracePage,
  type OperationalTraceArtifactContent,
} from "../../api/client";

const BASE_PATH = "/api/v1/admin/wanshitong";
export const DOCX_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export interface WanshitongScopeStatus {
  mode: "wanshitong";
  ready: boolean;
  system_key?: string | null;
  project_id: string | null;
  project_name: string | null;
  knowledge_base_id: string | null;
  knowledge_base_name: string | null;
  created_at?: string | null;
  blocker_code?: string | null;
  blocker_message?: string | null;
}

export interface WanshitongDocument {
  document_id: string;
  display_name: string;
  relative_path: string | null;
  department_key?: string | null;
  department_name?: string | null;
  department?: string | null;
  category_path: string[];
  document_title?: string | null;
  source_relative_path?: string | null;
  topic_keys: string[];
  visibility_scope: "all_internal";
  allowed_roles: string[];
  allowed_groups: string[];
  metadata_revision?: string | null;
  status: string;
  current_version_id?: string | null;
  current_version_status?: string | null;
  active_index_revision_id?: string | null;
  latest_job?: Job | null;
  retrievable: boolean;
  created_at: string;
  updated_at: string;
}

export interface WanshitongUploadMetadata {
  source_relative_path: string;
  department_name?: string | null;
  category_path?: string[] | null;
  document_title?: string | null;
  topic_keys?: string[];
}

export interface CursorPage<T> {
  items: T[];
  total?: number;
  page_size?: number;
  offset?: number;
  next_offset?: number | null;
  next_cursor?: string | null;
}

export interface WanshitongUploadResult {
  document: WanshitongDocument;
  job: Job;
}

export interface ProviderReadiness {
  configured: boolean;
  status?: string | null;
  connection_id?: string | null;
  connection_name?: string | null;
  provider_type?: string | null;
  model?: string | null;
  dimension?: number | null;
  protocol?: string | null;
  path?: string | null;
  host?: string | null;
  validation_status?: string | null;
  validated_at?: string | null;
  enabled?: boolean;
}

export interface WanshitongOverview {
  scope: WanshitongScopeStatus;
  documents: { total: number; retrievable: number };
  jobs: {
    queued: number;
    running: number;
    failed_retryable: number;
    failed_terminal: number;
  };
  active_index_revision_id?: string | null;
  providers: {
    embedding: ProviderReadiness;
    reranker: ProviderReadiness;
    llm: ProviderReadiness;
  };
  public_query_ready: boolean;
  supported_formats: {
    docx: "enabled" | "disabled";
    pdf: "enabled" | "disabled";
    doc: "enabled" | "disabled";
    excel: "enabled" | "disabled";
    zip: "enabled" | "disabled";
  };
  history_retention_days: number;
  recent_history: HistoryEntry[];
  recent_errors: Array<{
    code: string;
    message: string;
    occurred_at: string;
    job_id?: string | null;
    document_id?: string | null;
  }>;
}

export interface WanshitongHistoryEntry extends HistoryEntry {
  owner_masked_id?: string | null;
}

export interface WanshitongModels {
  embedding: ProviderReadiness;
  reranker: ProviderReadiness;
  llm: ProviderReadiness;
  retrieval_profile: {
    profile_revision_id: string;
    status: string;
    index_semantic_fingerprint: string;
    serving_fingerprint: string;
  } | null;
  kb_model_settings: {
    generation_connection_id: string | null;
    generation_model: string | null;
    rewrite_enabled: boolean;
    ocr_connection_id: string | null;
    ocr_model: string | null;
    ocr_enabled: boolean;
    pdf_parser_connection_id: string | null;
    pdf_parser_model: string | null;
    pdf_parser_enabled: boolean;
  };
  image_ocr: ProviderReadiness;
  pdf_parser: ProviderReadiness;
}

export interface WanshitongSystem {
  mode: "wanshitong";
  app: { status: string };
  live: boolean;
  ready: boolean;
  qdrant: {
    mode: "memory" | "url";
    status: "ready" | "configured_not_probed";
  };
  sqlite: { ready: boolean; integrity_status: string };
  scope: {
    ready: boolean;
    project_id: string;
    knowledge_base_id: string;
  };
  active_index_revision_id?: string | null;
  query_executor: {
    max_workers: number;
    max_queue: number;
    in_flight: number;
    retry_after_seconds: number;
  };
  history: { ready: boolean; retention_days: number };
  operational_trace: {
    ready: boolean;
    metrics: Record<string, number>;
  };
  public_session: { ready: boolean };
  frontend_build_id: string;
}

function queryPath<T extends object>(
  path: string,
  values: T,
): string {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") {
      parameters.set(key, String(value));
    }
  }
  const query = parameters.toString();
  return query ? `${path}?${query}` : path;
}

function jsonInit(method: "POST", body?: object): RequestInit {
  return {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };
}

export const wanshitongAdminApi = {
  scope: (signal?: AbortSignal) =>
    consoleRequest<WanshitongScopeStatus>(`${BASE_PATH}/scope`, { signal }),
  overview: (signal?: AbortSignal) =>
    consoleRequest<WanshitongOverview>(`${BASE_PATH}/overview`, { signal }),
  listDocuments: (cursor?: string, signal?: AbortSignal) =>
    consoleRequest<CursorPage<WanshitongDocument>>(
      queryPath(`${BASE_PATH}/documents`, { cursor }),
      { signal },
    ),
  document: (documentId: string, signal?: AbortSignal) =>
    consoleRequest<WanshitongDocument>(
      `${BASE_PATH}/documents/${encodeURIComponent(documentId)}`,
      { signal },
    ),
  uploadDocument: (
    file: File,
    metadata: WanshitongUploadMetadata,
    idempotencyKey: string,
  ) =>
    consoleRequest<WanshitongUploadResult>(
      queryPath(`${BASE_PATH}/documents`, {
        metadata: JSON.stringify(metadata),
      }),
      {
        method: "POST",
        headers: {
          "Content-Type": DOCX_MEDIA_TYPE,
          "Idempotency-Key": idempotencyKey,
        },
        body: file,
      },
    ),
  uploadVersion: (
    documentId: string,
    file: File,
    metadata: WanshitongUploadMetadata,
    idempotencyKey: string,
  ) =>
    consoleRequest<WanshitongUploadResult>(
      queryPath(
        `${BASE_PATH}/documents/${encodeURIComponent(documentId)}/versions`,
        { metadata: JSON.stringify(metadata) },
      ),
      {
        method: "POST",
        headers: {
          "Content-Type": DOCX_MEDIA_TYPE,
          "Idempotency-Key": idempotencyKey,
        },
        body: file,
      },
    ),
  deleteDocument: (documentId: string) =>
    consoleRequest<void>(
      `${BASE_PATH}/documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    ),
  listJobs: (cursor?: string, signal?: AbortSignal) =>
    consoleRequest<CursorPage<Job>>(
      queryPath(`${BASE_PATH}/jobs`, { cursor }),
      { signal },
    ),
  job: (jobId: string, signal?: AbortSignal) =>
    consoleRequest<Job>(`${BASE_PATH}/jobs/${encodeURIComponent(jobId)}`, {
      signal,
    }),
  retryJob: (jobId: string) =>
    consoleRequest<Job>(
      `${BASE_PATH}/jobs/${encodeURIComponent(jobId)}:retry`,
      jsonInit("POST"),
    ),
  cancelJob: (jobId: string) =>
    consoleRequest<Job>(
      `${BASE_PATH}/jobs/${encodeURIComponent(jobId)}:cancel`,
      jsonInit("POST"),
    ),
  listHistory: (
    filters: HistoryFilters = {},
    signal?: AbortSignal,
  ) =>
    consoleRequest<HistoryPageResult>(
      queryPath(`${BASE_PATH}/history`, filters),
      { signal },
    ),
  history: (traceId: string, signal?: AbortSignal) =>
    consoleRequest<WanshitongHistoryEntry>(
      `${BASE_PATH}/history/${encodeURIComponent(traceId)}`,
      { signal },
    ),
  clearHistory: () =>
    consoleRequest<void>(`${BASE_PATH}/history`, { method: "DELETE" }),
  listOperationalTraces: (
    filters: OperationalTraceFilters = {},
    signal?: AbortSignal,
  ) =>
    consoleRequest<OperationalTracePage>(
      queryPath(`${BASE_PATH}/operational-traces`, filters),
      { signal },
    ),
  operationalTrace: (traceId: string, signal?: AbortSignal) =>
    consoleRequest<OperationalTraceDetail>(
      `${BASE_PATH}/operational-traces/${encodeURIComponent(traceId)}`,
      { signal },
    ),
  operationalTraceArtifact: async (
    traceId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<OperationalTraceArtifactContent> => {
    const response = await consoleRawRequest(
      `${BASE_PATH}/operational-traces/${encodeURIComponent(traceId)}/artifacts/${encodeURIComponent(artifactId)}`,
      { signal },
    );
    return {
      mediaType:
        response.headers.get("Content-Type") ?? "application/octet-stream",
      sha256: response.headers.get("X-Artifact-SHA256") ?? "",
      body: await response.text(),
    };
  },
  models: (signal?: AbortSignal) =>
    consoleRequest<WanshitongModels>(`${BASE_PATH}/models`, { signal }),
  system: (signal?: AbortSignal) =>
    consoleRequest<WanshitongSystem>(`${BASE_PATH}/system`, { signal }),
};
