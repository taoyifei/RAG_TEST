import { gatewayCsrfHeaders } from '@/config/wanshitongGateway'

export interface PublicAnswerSettings {
  model_id: string
  rerank_model_id: string
  system_prompt_id: string
  context_template_id: string
  system_prompt: string
  context_template: string
  temperature: number
  max_completion_tokens: number
  thinking: boolean
  citation_enabled: boolean
  multi_turn_enabled: boolean
  history_turns: number
  embedding_top_k: number
  keyword_threshold: number
  vector_threshold: number
  rerank_top_k: number
  rerank_threshold: number
  enable_rewrite: boolean
  enable_query_expansion: boolean
  rewrite_prompt_system: string
  rewrite_prompt_user: string
  fallback_strategy: 'fixed' | 'model'
  fallback_response: string
  fallback_prompt: string
}

export interface PublicPageSettings {
  welcome_text: string
  input_placeholder: string
  show_recommendations: boolean
  show_history: boolean
  allow_feedback: boolean
  allow_source_download: boolean
}

export interface PublicAppConfig {
  application_id: 'wanshitong-public'
  status: 'MIGRATION_REQUIRED' | 'ACTIVE' | 'DRIFTED' | 'UNAVAILABLE'
  revision: number
  native_agent_id: string | null
  model_id?: string | null
  knowledge_base_ids: string[]
  legacy_kb_ids?: string[]
  answer_settings: PublicAnswerSettings | null
  page_settings: PublicPageSettings
}

export interface SavePublicAppConfig {
  expected_revision: number
  migration_confirmed?: boolean
  knowledge_base_ids: string[]
  answer_settings: PublicAnswerSettings
  page_settings: PublicPageSettings
}

async function publicAppRequest<T>(method: 'GET' | 'PUT', body?: object): Promise<T> {
  const response = await fetch('/kb/api/admin/public-app', {
    method,
    credentials: 'include',
    cache: 'no-store',
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...gatewayCsrfHeaders(method),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json() as { detail?: string | { message?: string } }
      detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.message ?? ''
    } catch { /* 保留 HTTP 状态 */ }
    const reason = response.status === 409 ? '版本冲突，请刷新配置后重试'
      : response.status === 422 ? '配置字段或资料范围无效'
      : response.status === 502 ? '原生配置校验或保存失败，旧版仍有效'
      : '请求失败'
    throw new Error(`${reason}（${response.status}）${detail ? `：${detail}` : ''}`)
  }
  return response.json() as Promise<T>
}

export const getPublicAppConfig = () => publicAppRequest<PublicAppConfig>('GET')
export const savePublicAppConfig = (body: SavePublicAppConfig) =>
  publicAppRequest<PublicAppConfig>('PUT', body)
