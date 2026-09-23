import {
  consoleRequest,
  consoleRawRequest,
  downloadResponse,
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

export type FeedbackReviewStatus =
  | "NEW"
  | "REVIEWED"
  | "FIX_PLANNED"
  | "RESOLVED"
  | "EXPECTED_BEHAVIOR";

export interface FeedbackListItem {
  trace_id: string;
  created_at: string;
  updated_at: string;
  question_summary: string | null;
  question_sha256: string | null;
  final_status: string | null;
  useful: boolean;
  reason_code: string | null;
  reason_detail: string | null;
  comment_present: boolean;
  feedback_revision: number;
  answer_path: string | null;
  duration_ms: number | null;
  review_status: FeedbackReviewStatus;
  root_cause: string | null;
  review_version: number;
  new_feedback_pending: boolean;
}

export interface FeedbackStatistics {
  evaluated_count: number;
  helpful_count: number;
  helpful_rate: number | null;
  pending_count: number;
  confirmed_wrong_source_count: number;
  confirmed_false_refusal_count: number;
  latency_ms: {
    actual_sso_users: { count: number; p50: number | null; p95: number | null };
    non_sso_or_replay: {
      count: number;
      p50: number | null;
      p95: number | null;
    };
  };
}

export interface FeedbackCitation {
  document_id: string | null;
  document_version_id: string | null;
  display_name: string | null;
  source_label: string | null;
  selected_source_eligible: boolean;
}

export interface FeedbackDetail {
  trace_id: string;
  feedback: {
    useful: boolean;
    reason_code: string | null;
    reason_detail: string | null;
    comment: string | null;
    comment_available: boolean;
    comment_unavailable_reason: string | null;
    feedback_revision: number;
    projection_state: string;
    created_at: string;
    updated_at: string;
  };
  review: {
    review_status: FeedbackReviewStatus;
    root_cause: string | null;
    note: string | null;
    note_available: boolean;
    note_unavailable_reason: string | null;
    selected_source_document_id: string | null;
    selected_source_version_id: string | null;
    reviewed_feedback_revision: number;
    evaluation_candidate: boolean;
    fix_reference: string | null;
    verification_references: string[];
    review_version: number;
    reviewed_by_admin: string | null;
    updated_at: string | null;
    new_feedback_pending: boolean;
  };
  history: {
    available: boolean;
    unavailable_reason: string | null;
    question: string | null;
    answer: string | null;
    final_status: string | null;
    duration_ms: number | null;
  };
  citations: FeedbackCitation[];
  trace_projection: Record<string, Record<string, unknown>>;
  operational_trace: Record<string, unknown> | null;
  operational_trace_unavailable_reason: string | null;
}

export interface FeedbackReviewInput {
  expected_version: number;
  review_status: FeedbackReviewStatus;
  root_cause: string | null;
  note: string | null;
  selected_source_document_id: string | null;
  selected_source_version_id: string | null;
  evaluation_candidate: boolean;
  fix_reference: string | null;
  verification_references: string[];
}

export type QuestionAnalyticsBoard = "frequent" | "unresolved";
export type QuestionAnalyticsRunState =
  | "BUILDING"
  | "COMPLETE"
  | "FAILED"
  | "LIMITED";

export interface QuestionAnalyticsRun {
  run_id: string;
  state: QuestionAnalyticsRunState;
  deployment_id: string;
  window_start: string;
  window_end: string;
  observed_at: string;
  source_total: number;
  eligible_count: number;
  unknown_count: number;
  excluded_count: number;
  unknown_identity_count: number;
  body_missing_count: number;
  unreadable_count: number;
  pending_count: number;
  created_at: string;
  finished_at: string | null;
  expires_at: string | null;
  failure_code: string | null;
  coverage_start: string | null;
}

export interface QuestionAnalyticsItem {
  group_key: string;
  group_kind: "EXACT" | "APPROVED_ALIAS" | "CONTEXT";
  representative_question: string | null;
  request_count: number;
  distinct_users: number;
  user_day_heat: number;
  manual_request_count: number;
  manual_distinct_users: number;
  manual_user_day_heat: number;
  suggestion_count: number;
  popular_count: number;
  retry_count: number;
  unknown_entry_count: number;
  answered_count: number;
  refused_count: number;
  failed_count: number;
  feedback_count: number;
  helpful_count: number;
  negative_feedback_count: number;
  false_refusal_count: number;
  confirmed_open_issue_count: number;
  last_seen_at: string;
  sample_trace_ids: string[];
}

export interface QuestionAnalyticsPage {
  run: QuestionAnalyticsRun | null;
  latest_run: QuestionAnalyticsRun | null;
  items: QuestionAnalyticsItem[];
  total: number;
  next_offset: number | null;
}

export interface QuestionAnalyticsSample {
  trace_id: string;
  question: string | null;
  created_at: string;
  status: string;
  has_feedback: boolean;
}

export interface QuestionAnalyticsSamples {
  items: QuestionAnalyticsSample[];
}

export type RecommendationState =
  | "DRAFT"
  | "APPROVED"
  | "DISABLED"
  | "NEEDS_REVIEW";

export interface RecommendationSource {
  document_id: string;
  version_id: string;
}

export interface Recommendation {
  recommendation_id: string;
  question_text: string;
  question_style: "SHORT" | "STANDARD" | "COMPOUND";
  topic_key: string;
  state: RecommendationState;
  version: number;
  alias_keys: string[];
  validated_sources: RecommendationSource[];
  source_current: boolean;
  approved_at: string | null;
  disabled_reason: string | null;
}

export interface RecommendationUpdate {
  expected_version: number;
  question_text: string;
  question_style: Recommendation["question_style"];
  topic_key: string;
  state: RecommendationState;
  alias_keys: string[];
  validated_sources: RecommendationSource[];
  review_confirmed: boolean;
  disabled_reason: string | null;
}

function queryPath<T extends object>(path: string, values: T): string {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") {
      parameters.set(key, String(value));
    }
  }
  const query = parameters.toString();
  return query ? `${path}?${query}` : path;
}

function jsonInit(method: "PATCH" | "POST", body?: object): RequestInit {
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
  listHistory: (filters: HistoryFilters = {}, signal?: AbortSignal) =>
    consoleRequest<HistoryPageResult>(
      queryPath(`${BASE_PATH}/history`, filters),
      { signal },
    ),
  history: (traceId: string, signal?: AbortSignal) =>
    consoleRequest<WanshitongHistoryEntry>(
      `${BASE_PATH}/history/${encodeURIComponent(traceId)}`,
      { signal },
    ),
  exportHistoryTraces: async (traceIds: string[]) => {
    const response = await consoleRawRequest(
      `${BASE_PATH}/history-traces:export`,
      jsonInit("POST", { trace_ids: traceIds }),
    );
    return downloadResponse(response, "wanshitong-history-traces.zip");
  },
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
  exportOperationalTrace: async (traceId: string) => {
    const response = await consoleRawRequest(
      `${BASE_PATH}/operational-traces/${encodeURIComponent(traceId)}/export`,
    );
    return downloadResponse(response, `${traceId}.json`);
  },
  exportOperationalTraces: async (traceIds: string[]) => {
    const response = await consoleRawRequest(
      `${BASE_PATH}/operational-traces:export`,
      jsonInit("POST", { trace_ids: traceIds }),
    );
    return downloadResponse(response, "operational-traces.zip");
  },
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
  listFeedback: (
    filters: {
      reason?: string;
      review_status?: FeedbackReviewStatus;
      created_from?: string;
      created_to?: string;
      page_size?: number;
      offset?: number;
    } = {},
    signal?: AbortSignal,
  ) =>
    consoleRequest<CursorPage<FeedbackListItem>>(
      queryPath(`${BASE_PATH}/feedback`, filters),
      { signal },
    ),
  feedbackStatistics: (signal?: AbortSignal) =>
    consoleRequest<FeedbackStatistics>(`${BASE_PATH}/feedback/statistics`, {
      signal,
    }),
  feedbackDetail: (traceId: string, signal?: AbortSignal) =>
    consoleRequest<FeedbackDetail>(
      `${BASE_PATH}/feedback/${encodeURIComponent(traceId)}`,
      { signal },
    ),
  reviewFeedback: (traceId: string, body: FeedbackReviewInput) =>
    consoleRequest<FeedbackDetail>(
      `${BASE_PATH}/feedback/${encodeURIComponent(traceId)}/review`,
      jsonInit("PATCH", body),
    ),
  exportFeedback: async (traceIds: string[] = []) => {
    const response = await consoleRawRequest(
      `${BASE_PATH}/feedback/export`,
      jsonInit("POST", { trace_ids: traceIds }),
    );
    return downloadResponse(response, "wanshitong-feedback.json");
  },
  questionAnalytics: (
    board: QuestionAnalyticsBoard,
    offset = 0,
    signal?: AbortSignal,
  ) =>
    consoleRequest<QuestionAnalyticsPage>(
      queryPath(`${BASE_PATH}/question-analytics`, {
        board,
        page_size: 20,
        offset,
      }),
      { signal },
    ),
  refreshQuestionAnalytics: () =>
    consoleRequest<QuestionAnalyticsRun>(
      `${BASE_PATH}/question-analytics:refresh`,
      jsonInit("POST"),
    ),
  questionAnalyticsRun: (runId: string, signal?: AbortSignal) =>
    consoleRequest<QuestionAnalyticsRun>(
      `${BASE_PATH}/question-analytics/runs/${encodeURIComponent(runId)}`,
      { signal },
    ),
  questionAnalyticsSamples: (
    runId: string,
    groupKey: string,
    signal?: AbortSignal,
  ) =>
    consoleRequest<QuestionAnalyticsSamples>(
      queryPath(`${BASE_PATH}/question-analytics/samples`, {
        run_id: runId,
        group_key: groupKey,
      }),
      { signal },
    ),
  recommendations: (signal?: AbortSignal) =>
    consoleRequest<{ items: Recommendation[]; alias_revision: string }>(
      `${BASE_PATH}/recommendations`,
      { signal },
    ),
  createRecommendation: (
    groupKey: string,
    question: string,
    topicKey: string,
  ) =>
    consoleRequest<Recommendation>(
      `${BASE_PATH}/recommendations`,
      jsonInit("POST", {
        source_group_key: groupKey,
        question_text: question,
        topic_key: topicKey,
        question_style: "SHORT",
      }),
    ),
  updateRecommendation: (id: string, body: RecommendationUpdate) =>
    consoleRequest<Recommendation>(
      `${BASE_PATH}/recommendations/${encodeURIComponent(id)}`,
      jsonInit("PATCH", body),
    ),
  models: (signal?: AbortSignal) =>
    consoleRequest<WanshitongModels>(`${BASE_PATH}/models`, { signal }),
  system: (signal?: AbortSignal) =>
    consoleRequest<WanshitongSystem>(`${BASE_PATH}/system`, { signal }),
};
