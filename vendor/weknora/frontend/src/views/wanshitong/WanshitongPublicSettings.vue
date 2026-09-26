<template>
  <main class="wst-public-settings">
    <header class="wst-page-head">
      <div>
        <p class="wst-eyebrow">湾事通 · 管理后台</p>
        <h1>用户端设置</h1>
        <p>这里保存的公开资料、回答配置和页面设置用于普通用户的新请求。</p>
      </div>
      <button type="button" :disabled="loading || saving" @click="reload">重新读取</button>
    </header>
    <p v-if="error" class="wst-error" role="alert">{{ error }}</p>
    <p v-if="success" class="wst-success" role="status">{{ success }}</p>
    <div class="wst-status" v-if="config">
      <span><strong>公共应用</strong> {{ config.application_id }}</span>
      <span><strong>状态</strong> {{ config.status === 'ACTIVE' ? '已生效' : config.status === 'DRIFTED' ? '配置异常' : config.status === 'UNAVAILABLE' ? '原生服务暂不可用' : '待迁移核实' }}</span>
      <span><strong>版本</strong> {{ config.revision }}</span>
      <span><strong>当前模型</strong> {{ activeModelName }}</span>
      <span><strong>资料范围</strong> {{ config.knowledge_base_ids.length }} 个知识库</span>
    </div>
    <div v-if="isMigration" class="wst-migration" role="note">
      <strong>首次建立公共应用前，请核实当前实际运行配置。</strong>
      <p>当前没有已生效的公共应用绑定。请逐项核对模型、重排、多轮、改写、引用、输出额度与资料范围，再保存并生效。页面不会自动采用表单初始值。</p>
      <p v-if="config?.legacy_kb_ids?.length">旧部署资料 ID（仅作迁移参考）：{{ config.legacy_kb_ids.join('、') }}</p>
    </div>
    <div v-if="isDrifted" class="wst-migration wst-recovery" role="alert">
      <strong>公共应用配置异常，新问答已停止。</strong>
      <p>原生应用与当前绑定不一致。请重新选择资料范围，并从实际运行配置重新填写全部回答参数。确认后会创建新的原生应用并切换公共绑定；旧版本在保存成功前继续保留。</p>
    </div>
    <p v-if="isUnavailable" class="wst-error" role="alert">原生服务暂不可用。请先恢复服务并重新读取配置；此状态不能重建公共应用。</p>
    <nav class="wst-tabs" aria-label="用户端设置栏目">
      <button v-for="item in tabs" :key="item.key" type="button" :class="{ active: tab === item.key }"
        :aria-current="tab === item.key ? 'page' : undefined" @click="switchTab(item.key)">{{ item.label }}</button>
    </nav>
    <template v-if="tab === 'recommendations'">
      <WanshitongRecommendationManager />
    </template>
    <template v-else>
      <section v-if="tab === 'scope'" class="wst-panel">
        <h2>公开资料范围</h2>
        <p class="wst-muted">只有选中的知识库会进入新提问的检索范围。新知识库默认不公开；空范围不能保存。</p>
        <p v-if="resourceError" class="wst-error" role="alert">{{ resourceError }}</p>
        <div class="wst-kb-list">
          <label v-for="kb in knowledgeBases" :key="kb.id" class="wst-kb-row">
            <input type="checkbox" :checked="selectedIds.includes(kb.id)" @change="toggleKb(kb.id, $event)" />
            <span><strong>{{ kb.name }}</strong><small>{{ kb.type || '资料库' }} · {{ kb.status || '状态未提供' }} · {{ kb.id }}</small></span>
          </label>
          <p v-if="!knowledgeBases.length" class="wst-muted">尚未从原生服务读取到知识库。请先检查知识资料或代理状态。</p>
        </div>
      </section>

      <section v-else-if="tab === 'answer'" class="wst-panel">
        <h2>回答设置</h2>
        <p class="wst-muted">模型和参数由原生公共应用保存。修改后只影响后续新提问；不会改写已有回答。提示词正文必须填写已核实的实际模板内容，空值可能继承全局模板并改变行为。</p>
        <div v-if="requiresVerification" class="wst-template-loader">
          <button type="button" :disabled="loadingTemplates" @click="loadCurrentNativeTemplates">{{ loadingTemplates ? '正在读取原生模板…' : '载入当前原生模板并核实' }}</button>
          <span>读取 default_kb、default_context、default_rewrite 的实际正文；载入后仍需逐项核实。</span>
        </div>
        <p v-if="templateInfo" class="wst-success" role="status">{{ templateInfo }}</p>
        <div class="wst-field-grid">
          <label>回答模型
            <select v-model="answer.model_id" required><option value="">请选择</option>
              <option v-for="model in chatModels" :key="model.id" :value="model.id">{{ model.label }} · {{ model.id }} · {{ model.type }} · {{ model.status || '状态未提供' }}</option>
            </select>
          </label>
          <label>重排模型
            <select v-model="answer.rerank_model_id"><option value="">不使用</option>
              <option v-for="model in rerankModels" :key="model.id" :value="model.id">{{ model.label }} · {{ model.id }} · {{ model.type }} · {{ model.status || '状态未提供' }}</option>
            </select>
          </label>
          <label>温度（0–1）<input v-model.number="answer.temperature" type="number" min="0" max="1" step="0.01" /></label>
          <label>最大输出 token（0 表示沿用原生限制）<input v-model.number="answer.max_completion_tokens" type="number" min="0" step="1" /></label>
        </div>
        <div class="wst-switches">
          <label><input v-model="answer.citation_enabled" type="checkbox" @change="verifiedFlags.citation_enabled = false" />显示引用</label>
          <label><input v-model="answer.multi_turn_enabled" type="checkbox" @change="verifiedFlags.multi_turn_enabled = false" />启用多轮</label>
          <label><input v-model="answer.thinking" type="checkbox" @change="verifiedFlags.thinking = false" />模型思考</label>
        </div>
        <div class="wst-field-grid">
          <label>历史轮数<input v-model.number="answer.history_turns" type="number" min="1" step="1" /></label>
          <label>系统提示词模板 ID<input v-model="answer.system_prompt_id" maxlength="128" /></label>
          <label>上下文模板 ID<input v-model="answer.context_template_id" maxlength="128" /></label>
        </div>
        <label class="wst-field">系统提示词<textarea v-model="answer.system_prompt" rows="5" /></label>
        <label class="wst-field">上下文模板<textarea v-model="answer.context_template" rows="5" /></label>
        <details class="wst-advanced">
          <summary>检索与改写参数</summary>
          <p class="wst-muted">这些是原生检索参数。首次迁移或异常恢复时，应按已核实的运行值逐项填写。启用问题改写时，两段改写提示词都必须填写实际内容。</p>
          <div class="wst-field-grid">
            <label>向量召回 top-k<input v-model.number="answer.embedding_top_k" type="number" min="1" step="1" /></label>
            <label>关键词阈值<input v-model.number="answer.keyword_threshold" type="number" min="0.000001" max="1" step="0.01" /></label>
            <label>向量阈值<input v-model.number="answer.vector_threshold" type="number" min="0.000001" max="1" step="0.01" /></label>
            <label>重排 top-k<input v-model.number="answer.rerank_top_k" type="number" min="1" step="1" /></label>
            <label>重排阈值<input v-model.number="answer.rerank_threshold" type="number" min="0" max="1" step="0.01" /></label>
          </div>
          <div class="wst-switches">
            <label><input v-model="answer.enable_rewrite" type="checkbox" @change="verifiedFlags.enable_rewrite = false" />问题改写</label>
            <label><input v-model="answer.enable_query_expansion" type="checkbox" @change="verifiedFlags.enable_query_expansion = false" />查询扩展</label>
          </div>
          <label class="wst-field">改写系统提示词<textarea v-model="answer.rewrite_prompt_system" rows="3" /></label>
          <label class="wst-field">改写用户提示词<textarea v-model="answer.rewrite_prompt_user" rows="3" /></label>
        </details>
        <details class="wst-advanced">
          <summary>兜底回答</summary>
          <div class="wst-field-grid">
            <label>兜底方式<select v-model="answer.fallback_strategy"><option value="fixed">固定回复</option><option value="model">模型生成</option></select></label>
          </div>
          <label class="wst-field">固定回复<textarea v-model="answer.fallback_response" rows="3" /></label>
          <label class="wst-field">模型兜底提示词<textarea v-model="answer.fallback_prompt" rows="3" /></label>
        </details>
        <div v-if="requiresVerification" class="wst-verify-list">
          <h3>逐项核实回答开关</h3>
          <p class="wst-muted">初次表单中的关闭状态不代表当前运行值。请核对实际配置后，分别确认以下五项。</p>
          <label v-for="item in answerBooleanChecks" :key="item.key">
            <input v-model="verifiedFlags[item.key]" type="checkbox" />
            {{ item.label }}：{{ answer[item.key] ? '开启' : '关闭' }}，已与实际运行配置核对
          </label>
        </div>
      </section>

      <section v-else class="wst-panel">
        <h2>页面设置</h2>
        <p class="wst-muted">这些字段只控制湾事通用户端展示和允许的操作；不开放任意 HTML 或样式。</p>
        <label class="wst-field">欢迎文案<textarea v-model="page.welcome_text" rows="3" maxlength="500" /></label>
        <label class="wst-field">输入框提示<input v-model="page.input_placeholder" maxlength="200" /></label>
        <div class="wst-switches wst-page-switches">
          <label><input v-model="page.show_recommendations" type="checkbox" />显示推荐问题</label>
          <label><input v-model="page.show_history" type="checkbox" />显示历史入口</label>
          <label><input v-model="page.allow_feedback" type="checkbox" />允许反馈</label>
          <label><input v-model="page.allow_source_download" type="checkbox" />允许原件下载</label>
        </div>
      </section>
      <div class="wst-save-bar">
        <label v-if="requiresVerification" class="wst-verify"><input v-model="migrationVerified" type="checkbox" />我已核对当前实际运行配置，确认{{ isDrifted ? '重建并切换公共绑定' : '首次绑定' }}可以生效</label>
        <span>保存后回读原生配置，显示实际生效版本。</span>
        <button class="wst-primary" type="button" :disabled="saving || loading || !config || isUnavailable" @click="save">
          {{ saving ? '正在校验并保存…' : '保存并生效' }}
        </button>
      </div>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listKnowledgeBases } from '@/api/knowledge-base'
import { listModels } from '@/api/model'
import { getPromptTemplates } from '@/api/system'
import {
  getPublicAppConfig, savePublicAppConfig,
  type PublicAnswerSettings, type PublicAppConfig, type PublicPageSettings,
} from '@/api/wanshitongPublicApp'
import WanshitongRecommendationManager from './WanshitongRecommendationManager.vue'

type Tab = 'scope' | 'answer' | 'page' | 'recommendations'
type AnswerBooleanKey = 'citation_enabled' | 'multi_turn_enabled' | 'thinking' | 'enable_rewrite' | 'enable_query_expansion'
const answerBooleanChecks: { key: AnswerBooleanKey; label: string }[] = [
  { key: 'citation_enabled', label: '显示引用' },
  { key: 'multi_turn_enabled', label: '启用多轮' },
  { key: 'thinking', label: '模型思考' },
  { key: 'enable_rewrite', label: '问题改写' },
  { key: 'enable_query_expansion', label: '查询扩展' },
]
const tabs: { key: Tab; label: string }[] = [
  { key: 'scope', label: '资料范围' },
  { key: 'answer', label: '回答设置' },
  { key: 'page', label: '页面设置' },
  { key: 'recommendations', label: '推荐问题' },
]
const route = useRoute()
const router = useRouter()
const tab = computed<Tab>(() => tabs.some(item => item.key === route.query.tab)
  ? route.query.tab as Tab : 'scope')
const config = ref<PublicAppConfig | null>(null)
const selectedIds = ref<string[]>([])
const knowledgeBases = ref<{ id: string; name: string; type: string; status: string }[]>([])
const models = ref<{ id: string; label: string; type: string; status: string }[]>([])
const answer = ref<PublicAnswerSettings>(blankAnswer())
const page = ref<PublicPageSettings>(blankPage())
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const resourceError = ref('')
const success = ref('')
const templateInfo = ref('')
const loadingTemplates = ref(false)
const migrationVerified = ref(false)
const verifiedFlags = ref<Record<AnswerBooleanKey, boolean>>({
  citation_enabled: false, multi_turn_enabled: false, thinking: false,
  enable_rewrite: false, enable_query_expansion: false,
})
const isMigration = computed(() => config.value?.status === 'MIGRATION_REQUIRED')
const isDrifted = computed(() => config.value?.status === 'DRIFTED')
const isUnavailable = computed(() => config.value?.status === 'UNAVAILABLE')
const requiresVerification = computed(() => isMigration.value || isDrifted.value)
const chatModels = computed(() => models.value.filter(item => item.type === 'KnowledgeQA'))
const rerankModels = computed(() => models.value.filter(item => item.type === 'Rerank'))
const activeModelName = computed(() => {
  const model = models.value.find(item => item.id === config.value?.model_id)
  return model ? `${model.label}（${model.id}）` : config.value?.model_id ?? '未绑定'
})

function blankAnswer(): PublicAnswerSettings {
  // 空表单不代表部署默认值；首次迁移必须人工核实后才能提交。
  return {
    model_id: '', rerank_model_id: '', system_prompt_id: '', context_template_id: '',
    system_prompt: '', context_template: '',
    temperature: Number.NaN, max_completion_tokens: Number.NaN, thinking: false, citation_enabled: false,
    multi_turn_enabled: false, history_turns: Number.NaN, embedding_top_k: Number.NaN,
    keyword_threshold: Number.NaN, vector_threshold: Number.NaN,
    rerank_top_k: Number.NaN, rerank_threshold: Number.NaN,
    enable_rewrite: false, enable_query_expansion: false,
    rewrite_prompt_system: '', rewrite_prompt_user: '',
    fallback_strategy: 'fixed', fallback_response: '', fallback_prompt: '',
  }
}
function blankPage(): PublicPageSettings {
  return {
    welcome_text: '', input_placeholder: '', show_recommendations: true,
    show_history: true, allow_feedback: true, allow_source_download: true,
  }
}
async function reload() {
  loading.value = true
  error.value = ''
  success.value = ''
  templateInfo.value = ''
  resourceError.value = ''
  try {
    const current = await getPublicAppConfig()
    config.value = current
    selectedIds.value = current.status === 'DRIFTED' ? [] : [...current.knowledge_base_ids]
    answer.value = current.answer_settings ? structuredClone(current.answer_settings) : blankAnswer()
    page.value = structuredClone(current.page_settings)
    migrationVerified.value = false
    for (const item of answerBooleanChecks) verifiedFlags.value[item.key] = false
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取公共应用失败'
  }
  try {
    const [kbResponse, modelList] = await Promise.all([listKnowledgeBases(), listModels()])
    const raw = (kbResponse as { data?: unknown }).data
    if (!Array.isArray(raw)) throw new Error('原生知识库列表格式无效')
    knowledgeBases.value = raw.map((kb: { id: string; name: string; type?: string; status?: string }) => ({
      id: String(kb.id), name: kb.name, type: kb.type ?? '', status: kb.status ?? '',
    }))
    models.value = modelList.filter(model => model.id).map(model => ({
      id: String(model.id), label: model.display_name || model.name, type: model.type,
      status: model.status ?? '',
    }))
  } catch (cause) {
    resourceError.value = cause instanceof Error ? cause.message : '读取知识库或模型资源失败'
  } finally {
    loading.value = false
  }
}
async function loadCurrentNativeTemplates() {
  loadingTemplates.value = true
  error.value = ''
  templateInfo.value = ''
  try {
    const response = await getPromptTemplates()
    const system = response.data.system_prompt.find(item => item.id === 'default_kb')
    const context = response.data.context_template.find(item => item.id === 'default_context')
    const rewrite = response.data.rewrite.find(item => item.id === 'default_rewrite')
    if (!system?.content?.trim() || !context?.content?.trim()
      || !rewrite?.content?.trim() || !rewrite.user?.trim()) {
      throw new Error('原生默认模板正文不完整，无法自动载入；请先检查原生模板设置')
    }
    answer.value.system_prompt_id = system.id
    answer.value.context_template_id = context.id
    answer.value.system_prompt = system.content
    answer.value.context_template = context.content
    answer.value.rewrite_prompt_system = rewrite.content
    answer.value.rewrite_prompt_user = rewrite.user
    templateInfo.value = '已载入原生默认模板正文。请核实内容及其余运行参数后再确认保存。'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取原生模板失败'
  } finally {
    loadingTemplates.value = false
  }
}
function switchTab(next: Tab) {
  void router.push({ path: '/public-settings', query: { tab: next } })
}
function toggleKb(id: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = checked
    ? [...new Set([...selectedIds.value, id])]
    : selectedIds.value.filter(item => item !== id)
}
function validate(): string | null {
  if (isUnavailable.value) return '原生服务暂不可用，请恢复后重新读取'
  if (!selectedIds.value.length) return '至少选择一个公开知识库；空范围不能生效'
  if (!answer.value.model_id) return '请选择回答模型'
  if (!answer.value.system_prompt_id.trim() || !answer.value.context_template_id.trim()) return '请填写并核实提示词与上下文模板 ID'
  if (!answer.value.system_prompt.trim() || !answer.value.context_template.trim()) return '请填写并核实系统提示词与上下文模板正文'
  if (answer.value.system_prompt_id.length > 128 || answer.value.context_template_id.length > 128) return '模板 ID 不能超过 128 字符'
  if (!models.value.some(model => model.id === answer.value.model_id && model.type === 'KnowledgeQA')) return '回答模型不在当前原生模型列表中'
  if (answer.value.rerank_model_id && !rerankModels.value.some(model => model.id === answer.value.rerank_model_id)) return '重排模型不在当前原生模型列表中'
  if (![answer.value.temperature, answer.value.max_completion_tokens, answer.value.history_turns,
    answer.value.embedding_top_k, answer.value.keyword_threshold, answer.value.vector_threshold,
    answer.value.rerank_top_k, answer.value.rerank_threshold].every(Number.isFinite)) return '请逐项填写并核实全部数值参数'
  if (![answer.value.max_completion_tokens, answer.value.history_turns,
    answer.value.embedding_top_k, answer.value.rerank_top_k].every(Number.isInteger)) return '输出额度、历史轮数和检索 top-k 必须是整数'
  if (answer.value.history_turns < 1 || answer.value.embedding_top_k < 1 || answer.value.rerank_top_k < 1) return '历史轮数及检索 top-k 必须大于零'
  if (answer.value.keyword_threshold <= 0 || answer.value.keyword_threshold > 1
    || answer.value.vector_threshold <= 0 || answer.value.vector_threshold > 1
    || answer.value.rerank_threshold < 0 || answer.value.rerank_threshold > 1
    || answer.value.temperature < 0 || answer.value.temperature > 1
    || answer.value.max_completion_tokens < 0) return '请检查温度、阈值和输出额度'
  if (answer.value.enable_rewrite && (!answer.value.rewrite_prompt_system.trim() || !answer.value.rewrite_prompt_user.trim())) return '启用问题改写时，请填写两段已核实的实际提示词'
  if (requiresVerification.value && answerBooleanChecks.some(item => !verifiedFlags.value[item.key])) return '请逐项核实回答开关的实际运行状态'
  if (requiresVerification.value && !migrationVerified.value) return '首次绑定或异常恢复前，请核实当前实际运行配置'
  return null
}
async function save() {
  if (!config.value) return
  error.value = ''
  success.value = ''
  const issue = validate()
  if (issue) { error.value = issue; return }
  saving.value = true
  try {
    await savePublicAppConfig({
      expected_revision: config.value.revision,
      migration_confirmed: requiresVerification.value ? true : undefined,
      knowledge_base_ids: selectedIds.value,
      answer_settings: structuredClone(answer.value),
      page_settings: structuredClone(page.value),
    })
    const readback = await getPublicAppConfig()
    if (readback.status !== 'ACTIVE') throw new Error('保存后回读未显示已生效状态，请重新检查')
    config.value = readback
    selectedIds.value = [...readback.knowledge_base_ids]
    answer.value = structuredClone(readback.answer_settings ?? answer.value)
    page.value = structuredClone(readback.page_settings)
    migrationVerified.value = false
    success.value = `配置已回读：版本 ${readback.revision}，模型 ${activeModelName.value}，资料范围 ${readback.knowledge_base_ids.length} 个知识库。后续新提问生效。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存失败'
  } finally {
    saving.value = false
  }
}
watch(() => route.query.tab, () => { success.value = ''; error.value = '' })
void reload()
</script>

<style scoped>
.wst-public-settings { width: 100%; min-height: 0; overflow-y: auto; padding: 32px clamp(20px, 4vw, 52px) 56px; color: #274059; }
.wst-page-head { display: flex; align-items: start; justify-content: space-between; gap: 16px; }
.wst-eyebrow { margin: 0 0 5px; color: #477795; font-size: var(--app-text-sm); font-weight: 650; letter-spacing: .06em; }
h1 { margin: 0; font-size: var(--app-text-4xl); } h2 { margin: 0 0 10px; font-size: var(--app-text-2xl); }
.wst-page-head p:last-child, .wst-muted { color: #718497; line-height: 1.6; }
.wst-status { display: flex; gap: 16px; flex-wrap: wrap; padding: 14px 18px; margin: 22px 0 16px; border: 1px solid #dce8f0; border-radius: var(--app-radius-md); background: #f5fafc; font-size: var(--app-text-md); }
.wst-status strong { margin-right: 5px; color: #536f86; }
.wst-migration { padding: 15px 18px; margin-bottom: 16px; border: 1px solid #ebd6a0; border-radius: var(--app-radius-md); background: #fffaf0; }
.wst-migration p { margin: 8px 0 0; color: #806639; line-height: 1.5; }
.wst-recovery { border-color: #efb3a9; background: #fff4f2; }
.wst-recovery p { color: #9a3e32; }
.wst-tabs { display: flex; gap: 3px; border-bottom: 1px solid #dce7ef; margin-bottom: 18px; }
.wst-tabs button { padding: 11px 17px; border: 0; border-bottom: 3px solid transparent; background: transparent; color: #687f93; cursor: pointer; }
.wst-tabs button.active { border-bottom-color: #176c9a; color: #155b83; font-weight: 650; }
.wst-panel { padding: 24px; border: 1px solid #e0e8f0; border-radius: var(--app-radius-md); background: white; }
.wst-kb-list { display: grid; gap: 8px; margin-top: 17px; }
.wst-kb-row { display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid #e2eaf1; border-radius: var(--app-radius-md); cursor: pointer; }
.wst-kb-row:hover { background: #f7fbfd; }
.wst-kb-row small { display: block; margin-top: 3px; color: #8294a3; }
.wst-field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 18px 0; }
.wst-field-grid label, .wst-field { display: grid; gap: 6px; color: #526b80; font-size: var(--app-text-md); }
.wst-field { margin: 15px 0; }
input:not([type=checkbox]), select, textarea { width: 100%; min-height: 38px; padding: 8px 10px; border: 1px solid #cad9e5; border-radius: var(--app-radius-md); font: inherit; color: #274059; }
textarea { resize: vertical; }
.wst-switches { display: flex; flex-wrap: wrap; gap: 18px; margin: 15px 0; }
.wst-switches label, .wst-verify { display: flex; align-items: center; gap: 8px; color: #465f75; }
.wst-page-switches { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.wst-advanced { margin-top: 17px; padding: 15px; border: 1px solid #e1e9ef; border-radius: var(--app-radius-md); }
.wst-verify-list { display: grid; gap: 9px; margin-top: 18px; padding: 18px; border: 1px solid #e6cf9e; border-radius: var(--app-radius-md); background: #fffaf0; }
.wst-verify-list h3, .wst-verify-list p { margin: 0; }
.wst-verify-list label { display: flex; gap: 8px; align-items: center; color: #526b80; }
.wst-template-loader { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
.wst-template-loader span { color: #718497; font-size: var(--app-text-sm); }
.wst-advanced summary { cursor: pointer; font-weight: 600; }
.wst-save-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; margin-top: 18px; padding: 16px; border: 1px solid #dce7ef; border-radius: var(--app-radius-md); background: #f7fafc; }
.wst-save-bar span { color: #718497; font-size: var(--app-text-md); }
.wst-verify { margin-right: auto; }
button { padding: 8px 13px; border: 1px solid #cadbe7; border-radius: var(--app-radius-md); background: white; color: #1b5f86; font: inherit; cursor: pointer; }
.wst-primary { border-color: #146694; background: #146694; color: white; }
button:disabled { opacity: .5; cursor: not-allowed; }
.wst-error, .wst-success { padding: 11px 13px; border-radius: var(--app-radius-md); }
.wst-error { background: #fff0ef; color: #b42318; }
.wst-success { background: #e7f6ec; color: #176a3b; }
button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid #1677aa; outline-offset: 2px; }
@media (max-width: 900px) { .wst-field-grid { grid-template-columns: 1fr; } .wst-page-switches { grid-template-columns: 1fr; } }
</style>
