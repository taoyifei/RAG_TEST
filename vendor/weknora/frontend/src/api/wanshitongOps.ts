import { gatewayCsrfHeaders } from '@/config/wanshitongGateway'

const OPS_API = '/kb/api/admin/ops'

export interface OpsSummary {
  turns: number
  users: number
  completed: number
  failed: number
  negative_feedback: number
  feedback_count: number
  helpful_feedback: number
  pending_feedback: number
}

export interface TraceSummary {
  trace_id: string
  created_at: string
  status: string
  asker_id: string
  asker_name: string | null
  question: string
  feedback_useful: number | null
  review_status: string | null
}

export interface TraceDetail {
  bridge_trace_id: string
  native_session_id: string
  native_message_id: string | null
  native_request_id: string | null
  status: string
  asker_id: string
  asker_name: string | null
  question: string
  answer: string | null
  events: { sequence: number; event_type: string; created_at: string }[]
  references: { reference_id: string; native_knowledge_id?: string | null; native_chunk_id?: string | null }[]
  feedback: { useful: boolean; reason_detail: string | null; comment?: string | null } | null
  review: {
    status: string
    note: string
    root_cause: string
    fix_reference: string
    verification_references: string[]
    evaluation_candidate: boolean
  } | null
}

export interface QuestionStat {
  question: string
  count: number
  user_count: number
  negative_feedback: number
  last_asked_at: string
}

export interface Recommendation {
  recommendation_id: string
  question: string
  topic_key: string
  source_knowledge_ids: string[]
  review_note: string
  state: 'DRAFT' | 'APPROVED' | 'DISABLED'
  disabled_reason: string | null
  version: number
  updated_at: string
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const response = await fetch(`${OPS_API}${path}`, {
    ...options,
    credentials: 'include',
    cache: 'no-store',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...gatewayCsrfHeaders(method),
      ...options.headers,
    },
  })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json() as { detail?: string }
      if (body.detail) message += `：${body.detail}`
    } catch { /* 保留 HTTP 状态 */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

async function download(path: string, name: string, body: object): Promise<void> {
  const response = await fetch(`${OPS_API}${path}`, {
    method: 'POST',
    credentials: 'include',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...gatewayCsrfHeaders('POST') },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`导出失败（${response.status}）`)
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const getOpsSummary = () => request<OpsSummary>('/summary')
export const getOpsMigration = () => request<{ status: string; items: unknown[] }>('/migration')
export const getOpsQuestions = () => request<{ items: QuestionStat[] }>('/questions')
export const getOpsRecommendations = () => request<{ items: Recommendation[] }>('/recommendations')
export const getOpsTrace = (id: string) => request<TraceDetail>(`/traces/${encodeURIComponent(id)}`)
export const listOpsTraces = (params: { limit: number; offset: number; query: string; feedback_only: boolean }) =>
  request<{ items: TraceSummary[]; total: number }>(`/traces?${new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
    query: params.query,
    feedback_only: String(params.feedback_only),
  })}`)

export function saveOpsReview(id: string, body: object): Promise<{ review: TraceDetail['review'] }> {
  return request(`/traces/${encodeURIComponent(id)}/review`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function saveOpsRecommendation(item: Partial<Recommendation> & {
  expected_version: number | null
  review_confirmed: boolean
}): Promise<Recommendation> {
  const path = item.recommendation_id
    ? `/recommendations/${encodeURIComponent(item.recommendation_id)}`
    : '/recommendations'
  return request(path, {
    method: item.recommendation_id ? 'PUT' : 'POST',
    body: JSON.stringify({
      expected_version: item.expected_version,
      question: item.question,
      topic_key: item.topic_key,
      source_knowledge_ids: item.source_knowledge_ids,
      review_note: item.review_note,
      state: item.state,
      review_confirmed: item.review_confirmed,
      disabled_reason: item.disabled_reason,
    }),
  })
}

export const exportOpsTrace = (id: string, includeContent: boolean) =>
  download(`/traces/${encodeURIComponent(id)}/export`, `${id}.json`, {
    include_content: includeContent,
    confirm_id: includeContent ? id : null,
  })
export const exportOpsTraces = () =>
  download('/traces/export', 'wanshitong-traces.jsonl', { include_content: false })
