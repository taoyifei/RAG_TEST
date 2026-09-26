<template>
  <main class="wst-ops-view">
    <div class="wst-page-head">
      <div>
        <p class="wst-eyebrow">湾事通 · 管理后台</p>
        <h1>{{ mode === 'overview' ? '运行概览' : mode === 'records' ? '使用记录' : '系统与诊断' }}</h1>
        <p class="wst-description">{{ mode === 'records' ? '查看真实 RDMS 用户问答与反馈，记录处理结果。' : mode === 'overview' ? '掌握用户端使用情况和当前资料状态。' : '查看迁移状态与诊断入口，发布门禁单独验收。' }}</p>
      </div>
      <button class="wst-button" type="button" :disabled="loading" @click="refresh">刷新数据</button>
    </div>
    <p v-if="error" class="wst-error" role="alert">{{ error }}</p>

    <template v-if="mode === 'overview'">
      <section class="wst-panel wst-public-config" aria-label="当前用户端配置">
        <div class="wst-panel-heading"><h2>当前用户端配置</h2><RouterLink to="/public-settings">查看用户端设置</RouterLink></div>
        <p v-if="publicConfigError" class="wst-error" role="alert">{{ publicConfigError }}</p>
        <dl v-else-if="publicConfig" class="wst-detail">
          <dt>生效状态</dt><dd>{{ publicStatusLabel }}</dd>
          <dt>回答模型</dt><dd>{{ publicConfig.model_id || '未绑定' }}</dd>
          <dt>发布资料范围</dt><dd>{{ publicConfig.knowledge_base_ids.length }} 个知识库<span v-if="publicConfig.knowledge_base_ids.length" class="wst-config-ids">{{ publicConfig.knowledge_base_ids.join('、') }}</span></dd>
          <dt>配置版本</dt><dd>{{ publicConfig.revision }}</dd>
        </dl>
        <p v-else class="wst-muted">正在读取公共应用状态…</p>
      </section>
      <section class="wst-metrics" aria-label="运行统计">
        <div v-for="metric in metrics" :key="metric.label" class="wst-metric">
          <span>{{ metric.label }}</span><strong>{{ metric.value ?? '—' }}</strong>
        </div>
      </section>
      <div class="wst-grid">
        <section class="wst-panel">
          <div class="wst-panel-heading"><h2>最近问答</h2><RouterLink to="/records">查看全部</RouterLink></div>
          <p v-if="traces.length === 0" class="wst-empty">暂无新引擎问答记录。</p>
          <ul v-else class="wst-recent">
            <li v-for="item in traces.slice(0, 5)" :key="item.trace_id">
              <button type="button" @click="selectTrace(item.trace_id)">{{ item.question }}</button>
              <small>{{ actorName(item) }} · {{ displayTime(item.created_at) }}</small>
            </li>
          </ul>
        </section>
        <section class="wst-panel">
          <h2>管理入口</h2>
          <div class="wst-link-list">
            <RouterLink to="/platform/knowledge-bases">知识库、文件与分块 <t-icon name="chevron-right" /></RouterLink>
            <RouterLink to="/public-settings">用户端资料范围与回答设置 <t-icon name="chevron-right" /></RouterLink>
            <RouterLink to="/platform/settings?section=models">模型与解析资源 <t-icon name="chevron-right" /></RouterLink>
            <RouterLink to="/records?tab=feedback">待处理反馈 <t-icon name="chevron-right" /></RouterLink>
          </div>
        </section>
      </div>
    </template>

    <template v-else-if="mode === 'records'">
      <div class="wst-tabs" role="tablist" aria-label="使用记录栏目">
        <button v-for="item in recordTabs" :key="item.key" type="button" role="tab"
          :aria-selected="tab === item.key" :class="{ active: tab === item.key }"
          @click="changeTab(item.key)">{{ item.label }}</button>
      </div>
      <section v-if="tab === 'questions'" class="wst-panel">
        <h2>问题运营榜</h2>
        <p class="wst-muted">近 7 天按完全相同的问题文本统计；次数不代表答案正确。</p>
        <div class="wst-inline-actions">
          <button class="wst-button" :class="{ active: board === 'frequent' }" type="button" @click="board = 'frequent'">高频问题</button>
          <button class="wst-button" :class="{ active: board === 'unresolved' }" type="button" @click="board = 'unresolved'">高频但答不好</button>
        </div>
        <div class="wst-table-wrap"><table>
          <thead><tr><th>问题</th><th>次数</th><th>人数</th><th>待改进反馈</th><th>最近提问</th><th>运营</th></tr></thead>
          <tbody><tr v-for="item in filteredQuestions" :key="item.question">
            <td>{{ item.question }}</td><td>{{ item.count }}</td><td>{{ item.user_count }}</td>
            <td>{{ item.negative_feedback }}</td><td>{{ displayTime(item.last_asked_at) }}</td>
            <td><button class="wst-link-button" type="button" @click="startRecommendation(item.question)">建推荐草稿</button></td>
          </tr></tbody>
        </table></div>
        <p v-if="filteredQuestions.length === 0" class="wst-empty">当前没有符合条件的重复问题。</p>
      </section>
      <template v-else>
        <section class="wst-panel">
          <div class="wst-panel-heading">
            <div>
              <h2>{{ tab === 'feedback' ? '反馈与复核' : tab === 'trace' ? '技术 Trace' : '用户问答历史' }}</h2>
              <p class="wst-muted">提问者来自 RDMS 身份；历史只读，不能从此处续聊。</p>
            </div>
            <button class="wst-button" type="button" @click="exportAll">导出全部脱敏 Trace</button>
          </div>
          <div v-if="tab === 'feedback'" class="wst-metrics wst-metrics-small">
            <div class="wst-metric"><span>用户评价</span><strong>{{ summary?.feedback_count ?? '—' }}</strong></div>
            <div class="wst-metric"><span>有帮助</span><strong>{{ summary?.helpful_feedback ?? '—' }}</strong></div>
            <div class="wst-metric"><span>待处理</span><strong>{{ summary?.pending_feedback ?? '—' }}</strong></div>
          </div>
          <form class="wst-search" @submit.prevent="searchTraces">
            <label for="wst-trace-search">搜索问题、姓名或 RDMS 用户 ID</label>
            <input id="wst-trace-search" v-model="search" />
            <button class="wst-button" type="submit">搜索</button>
          </form>
          <div class="wst-table-wrap"><table>
            <thead><tr><th>时间</th><th>提问者</th><th>问题</th><th>状态</th><th>反馈 / 复核</th><th>Trace</th></tr></thead>
            <tbody><tr v-for="item in traces" :key="item.trace_id">
              <td>{{ displayTime(item.created_at) }}</td><td>{{ actorName(item) }}</td><td>{{ item.question }}</td>
              <td>{{ item.status }}</td><td>{{ feedbackLabel(item) }}{{ item.review_status ? ` · ${item.review_status}` : '' }}</td>
              <td><button class="wst-link-button" type="button" @click="selectTrace(item.trace_id)">查看</button></td>
            </tr></tbody>
          </table></div>
          <p v-if="traces.length === 0" class="wst-empty">当前条件下没有记录。</p>
          <div class="wst-pagination">
            <span>共 {{ total }} 条 · 第 {{ Math.floor(offset / PAGE_SIZE) + 1 }} 页</span>
            <button class="wst-button" type="button" :disabled="offset === 0" @click="offset = Math.max(0, offset - PAGE_SIZE)">上一页</button>
            <button class="wst-button" type="button" :disabled="offset + PAGE_SIZE >= total" @click="offset += PAGE_SIZE">下一页</button>
          </div>
        </section>
        <section v-if="tab === 'trace' && selected" class="wst-panel">
          <h2>Trace 详情</h2>
          <dl class="wst-detail">
            <dt>提问者</dt><dd>{{ actorName(selected) }}</dd>
            <dt>原问题</dt><dd>{{ selected.question }}</dd>
            <dt>原生回答</dt><dd class="wst-answer">{{ selected.answer ?? '尚无完整回答' }}</dd>
            <dt>状态</dt><dd>{{ selected.status }}</dd>
            <dt>桥接 Trace</dt><dd>{{ selected.bridge_trace_id }}</dd>
            <dt>原生会话</dt><dd>{{ selected.native_session_id }}</dd>
            <dt>原生消息</dt><dd>{{ selected.native_message_id ?? '未返回' }}</dd>
            <dt>原生请求</dt><dd>{{ selected.native_request_id ?? '未返回' }}</dd>
          </dl>
          <p class="wst-muted">实际流事件 {{ selected.events.length }} 条 · 引用 {{ selected.references.length }} 段</p>
          <details v-if="selected.references.length"><summary>查看引用 ID</summary>
            <ul><li v-for="reference in selected.references" :key="reference.reference_id">文件 {{ reference.native_knowledge_id ?? '未提供' }} · 分块 {{ reference.native_chunk_id ?? '未提供' }}</li></ul>
          </details>
          <details><summary>查看事件时序</summary>
            <ol><li v-for="event in selected.events" :key="event.sequence">{{ event.sequence }} · {{ event.event_type }} · {{ displayTime(event.created_at) }}</li></ol>
          </details>
          <div class="wst-inline-actions">
            <button class="wst-button" type="button" @click="exportOne(false)">下载脱敏 Trace</button>
            <button class="wst-button" type="button" @click="exportOne(true)">确认后下载完整 Trace</button>
          </div>
          <div v-if="selected.feedback" class="wst-review">
            <h3>反馈复核</h3>
            <p>用户反馈：{{ selected.feedback.useful ? '有帮助' : '待改进' }}{{ selected.feedback.reason_detail ? ` · ${selected.feedback.reason_detail}` : '' }}{{ selected.feedback.comment ? ` · ${selected.feedback.comment}` : '' }}</p>
            <label>处理状态<select v-model="reviewStatus"><option value="open">待处理</option><option value="in_review">复核中</option><option value="resolved">已处理</option></select></label>
            <label>复核备注<textarea v-model="reviewNote" maxlength="2000" /></label>
            <label>根因<input v-model="rootCause" maxlength="100" /></label>
            <label>修复引用 / 批次<input v-model="fixReference" maxlength="500" /></label>
            <label>验证记录（每行一条）<textarea v-model="verificationReferences" /></label>
            <label class="wst-check"><input v-model="evaluationCandidate" type="checkbox" />标记为 Evaluation Candidate（只记录候选）</label>
            <button class="wst-button wst-button-primary" type="button" :disabled="savingReview" @click="saveReview">保存复核</button>
          </div>
        </section>
      </template>
    </template>

    <template v-else>
      <div class="wst-grid">
        <section class="wst-panel">
          <h2>迁移与服务状态</h2>
          <dl class="wst-detail">
            <dt>迁移清单</dt><dd>{{ migrationStatus || '读取中' }}</dd>
            <dt>已映射原件</dt><dd>{{ migrationCount ?? '—' }}</dd>
            <dt>问答次数</dt><dd>{{ summary?.turns ?? '—' }}</dd>
            <dt>失败回答</dt><dd>{{ summary?.failed ?? '—' }}</dd>
          </dl>
          <p class="wst-muted">这些是当前候选环境的运营数据；正式发布仍需独立验收上下文预算与内容质量。</p>
        </section>
        <section class="wst-panel">
          <h2>定位入口</h2>
          <div class="wst-link-list">
            <RouterLink to="/records?tab=trace">网关 Trace</RouterLink>
            <RouterLink to="/platform/knowledge-bases">知识资料与解析状态</RouterLink>
            <RouterLink to="/platform/settings?section=parser">解析资源设置</RouterLink>
            <RouterLink to="/platform/settings?section=storage">存储高级设置</RouterLink>
          </div>
          <p class="wst-muted">未配置的原生诊断能力不显示为已连接。</p>
        </section>
      </div>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  exportOpsTrace, exportOpsTraces, getOpsMigration, getOpsQuestions, getOpsSummary,
  getOpsTrace, listOpsTraces, saveOpsReview,
  type OpsSummary, type QuestionStat, type TraceDetail, type TraceSummary,
} from '@/api/wanshitongOps'
import { getPublicAppConfig, type PublicAppConfig } from '@/api/wanshitongPublicApp'
import { useWanshitongAdminStore } from '@/stores/wanshitongAdmin'

const PAGE_SIZE = 20
type RecordTab = 'history' | 'feedback' | 'questions' | 'trace'
const recordTabs: { key: RecordTab; label: string }[] = [
  { key: 'history', label: '问答历史' },
  { key: 'feedback', label: '反馈待办' },
  { key: 'questions', label: '问题运营榜' },
  { key: 'trace', label: '技术 Trace' },
]
const route = useRoute()
const router = useRouter()
const adminStore = useWanshitongAdminStore()
const mode = computed(() => route.path === '/records' ? 'records' : route.path === '/diagnostics' ? 'diagnostics' : 'overview')
const tab = computed<RecordTab>(() => recordTabs.some(item => item.key === route.query.tab)
  ? route.query.tab as RecordTab : 'history')
const loading = ref(false)
const error = ref('')
const summary = ref<OpsSummary | null>(null)
const publicConfig = ref<PublicAppConfig | null>(null)
const publicConfigError = ref('')
const migrationCount = ref<number | null>(null)
const migrationStatus = ref('')
const questions = ref<QuestionStat[]>([])
const traces = ref<TraceSummary[]>([])
const total = ref(0)
const offset = ref(0)
const search = ref('')
const query = ref('')
const board = ref<'frequent' | 'unresolved'>('frequent')
const selected = ref<TraceDetail | null>(null)
const reviewStatus = ref('in_review')
const reviewNote = ref('')
const rootCause = ref('')
const fixReference = ref('')
const verificationReferences = ref('')
const evaluationCandidate = ref(false)
const savingReview = ref(false)
const metrics = computed(() => [
  { label: '问答次数', value: summary.value?.turns },
  { label: '提问人数', value: summary.value?.users },
  { label: '完成回答', value: summary.value?.completed },
  { label: '失败回答', value: summary.value?.failed },
  { label: '待改进反馈', value: summary.value?.negative_feedback },
  { label: '已映射原件', value: migrationCount.value },
])
const publicStatusLabel = computed(() => {
  switch (publicConfig.value?.status) {
    case 'ACTIVE': return '已生效'
    case 'MIGRATION_REQUIRED': return '待迁移核实'
    case 'DRIFTED': return '配置异常，新问答已停止'
    case 'UNAVAILABLE': return '原生服务暂不可用'
    default: return '未读取'
  }
})
const filteredQuestions = computed(() => questions.value.filter(item =>
  item.count >= 2 && (board.value === 'frequent' || item.negative_feedback > 0)))

function message(cause: unknown, fallback: string): string {
  return cause instanceof Error ? cause.message : fallback
}
function displayTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}
function actorName(item: { asker_id: string; asker_name: string | null }): string {
  return item.asker_name ? `${item.asker_name}（${item.asker_id}）` : `RDMS ${item.asker_id}`
}
function feedbackLabel(item: TraceSummary): string {
  return item.feedback_useful === null ? '未反馈' : item.feedback_useful ? '有帮助' : '待改进'
}
async function loadBase() {
  try {
    const [counts, migration, boardData] = await Promise.all([
      getOpsSummary(), getOpsMigration(), getOpsQuestions(),
    ])
    summary.value = counts
    migrationCount.value = migration.items.length
    migrationStatus.value = migration.status
    questions.value = boardData.items
  } catch (cause) {
    error.value = message(cause, '读取运营数据失败')
  }
}
async function loadPublicConfig() {
  publicConfig.value = null
  publicConfigError.value = ''
  try {
    publicConfig.value = await getPublicAppConfig()
  } catch (cause) {
    publicConfigError.value = `公共应用状态读取失败：${message(cause, '请检查原生服务')}`
  }
}
async function loadTraces() {
  if (mode.value === 'diagnostics' || (mode.value === 'records' && tab.value === 'questions')) return
  try {
    const data = await listOpsTraces({
      limit: PAGE_SIZE, offset: offset.value, query: query.value,
      feedback_only: mode.value === 'records' && tab.value === 'feedback',
    })
    traces.value = data.items
    total.value = data.total
  } catch (cause) {
    error.value = message(cause, '读取问答记录失败')
  }
}
async function refresh() {
  loading.value = true
  error.value = ''
  await Promise.all([loadBase(), loadTraces(), loadPublicConfig()])
  loading.value = false
}
function changeTab(next: RecordTab) {
  offset.value = 0
  search.value = ''
  query.value = ''
  void router.push({ path: '/records', query: { tab: next } })
}
function searchTraces() {
  offset.value = 0
  query.value = search.value.trim()
}
async function selectTrace(id: string) {
  error.value = ''
  try {
    const detail = await getOpsTrace(id)
    selected.value = detail
    reviewStatus.value = detail.review?.status ?? 'in_review'
    reviewNote.value = detail.review?.note ?? ''
    rootCause.value = detail.review?.root_cause ?? ''
    fixReference.value = detail.review?.fix_reference ?? ''
    verificationReferences.value = detail.review?.verification_references.join('\n') ?? ''
    evaluationCandidate.value = detail.review?.evaluation_candidate ?? false
    await router.push({ path: '/records', query: { tab: 'trace' } })
  } catch (cause) {
    error.value = message(cause, '读取 Trace 失败')
  }
}
async function saveReview() {
  if (!selected.value) return
  savingReview.value = true
  error.value = ''
  try {
    await saveOpsReview(selected.value.bridge_trace_id, {
      status: reviewStatus.value,
      note: reviewNote.value,
      root_cause: rootCause.value,
      fix_reference: fixReference.value,
      verification_references: verificationReferences.value.split('\n').map(item => item.trim()).filter(Boolean),
      evaluation_candidate: evaluationCandidate.value,
    })
    await selectTrace(selected.value.bridge_trace_id)
    await loadTraces()
  } catch (cause) {
    error.value = message(cause, '保存复核失败')
  } finally {
    savingReview.value = false
  }
}
async function exportOne(includeContent: boolean) {
  if (!selected.value) return
  if (includeContent && !window.confirm('此文件包含原始问题、回答和引用正文。确认下载？')) return
  try {
    await exportOpsTrace(selected.value.bridge_trace_id, includeContent)
  } catch (cause) {
    error.value = message(cause, '导出失败')
  }
}
async function exportAll() {
  try {
    await exportOpsTraces()
  } catch (cause) {
    error.value = message(cause, '批量导出失败')
  }
}
function startRecommendation(question: string) {
  adminStore.startRecommendationDraft(question)
  void router.push({ path: '/public-settings', query: { tab: 'recommendations' } })
}
watch([mode, tab, offset, query], () => { void loadTraces() }, { immediate: true })
watch(mode, value => { if (value === 'overview') void loadPublicConfig() }, { immediate: true })
void loadBase()
</script>

<style scoped>
.wst-ops-view { width: 100%; min-height: 0; overflow-y: auto; padding: 32px clamp(20px, 4vw, 52px) 56px; color: #274059; }
.wst-page-head, .wst-panel-heading { display: flex; justify-content: space-between; align-items: start; gap: 16px; }
.wst-page-head { margin-bottom: 24px; }
.wst-eyebrow { margin: 0 0 5px; color: #477795; font-size: var(--app-text-sm); font-weight: 650; letter-spacing: .06em; }
h1 { margin: 0; font-size: var(--app-text-4xl); line-height: 1.3; } h2 { margin: 0 0 12px; font-size: var(--app-text-2xl); } h3 { margin: 0 0 12px; font-size: var(--app-text-xl); }
.wst-description, .wst-muted { color: #718497; line-height: 1.6; }
.wst-description { margin: 8px 0 0; }
.wst-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 20px; }
.wst-metric, .wst-panel { border: 1px solid #e0e8f0; border-radius: var(--app-radius-md); background: #fff; }
.wst-metric { padding: 17px 19px; }
.wst-metric span { display: block; color: #6b8095; font-size: var(--app-text-md); }
.wst-metric strong { display: block; margin-top: 8px; color: #173e5e; font-size: var(--app-text-4xl); }
.wst-metrics-small { margin-top: 18px; }
.wst-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(260px, 1fr); gap: 18px; }
.wst-panel { padding: 23px; margin-bottom: 18px; }
.wst-panel-heading a, .wst-link-button { color: #12658f; }
.wst-link-list { display: grid; gap: 8px; }
.wst-link-list a { display: flex; justify-content: space-between; padding: 11px 13px; border-radius: var(--app-radius-md); background: #f2f7fb; color: #285976; text-decoration: none; }
.wst-link-list a:hover { background: #e4f0f8; }
.wst-recent { list-style: none; margin: 0; padding: 0; }
.wst-recent li { display: grid; gap: 4px; padding: 10px 0; border-bottom: 1px solid #edf1f5; }
.wst-recent button, .wst-link-button { padding: 0; border: 0; background: transparent; text-align: left; font: inherit; cursor: pointer; }
.wst-recent button { color: #274059; }
.wst-recent small { color: #8293a4; }
.wst-tabs { display: flex; gap: 4px; margin-bottom: 18px; border-bottom: 1px solid #dce7ef; }
.wst-tabs button { padding: 11px 17px; border: 0; border-bottom: 3px solid transparent; background: transparent; color: #687f93; cursor: pointer; }
.wst-tabs button.active { border-bottom-color: #176c9a; color: #155b83; font-weight: 650; }
.wst-button { padding: 8px 13px; border: 1px solid #ccdce8; border-radius: var(--app-radius-md); background: white; color: #2b5f7f; cursor: pointer; font: inherit; }
.wst-button.active, .wst-button-primary { border-color: #146694; background: #146694; color: #fff; }
.wst-button:disabled { opacity: .5; cursor: not-allowed; }
.wst-inline-actions, .wst-pagination { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin: 18px 0; }
.wst-pagination span { margin-right: auto; color: #718497; }
.wst-search { display: flex; align-items: end; gap: 10px; margin: 18px 0; }
.wst-search label { display: grid; gap: 6px; flex: 1; font-size: var(--app-text-md); color: #536b80; }
.wst-search input, .wst-review input, .wst-review textarea, .wst-review select { min-height: 36px; padding: 7px 10px; border: 1px solid #ccd8e3; border-radius: var(--app-radius-md); font: inherit; }
.wst-search input { flex: 1; }
.wst-table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--app-text-md); }
th, td { padding: 12px 10px; border-bottom: 1px solid #e9eef3; text-align: left; vertical-align: top; }
th { color: #61798d; font-weight: 650; background: #f8fafc; white-space: nowrap; }
td { max-width: 360px; overflow-wrap: anywhere; }
.wst-empty { padding: 22px 0; color: #8192a3; }
.wst-detail { display: grid; grid-template-columns: 115px minmax(0, 1fr); gap: 12px 16px; }
.wst-detail dt { color: #6b8095; } .wst-detail dd { margin: 0; overflow-wrap: anywhere; }
.wst-config-ids { display: block; margin-top: 3px; color: #718497; font-size: var(--app-text-sm); }
.wst-answer { white-space: pre-wrap; }
.wst-review { display: grid; gap: 13px; margin-top: 24px; padding-top: 20px; border-top: 1px solid #e3ebf2; }
.wst-review label:not(.wst-check) { display: grid; gap: 6px; }
.wst-review textarea { min-height: 75px; }
.wst-check { display: flex; align-items: center; gap: 8px; }
.wst-error { padding: 12px; border-radius: var(--app-radius-md); background: #fff0ef; color: #b42318; }
button:focus-visible, a:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible { outline: 2px solid #1677aa; outline-offset: 2px; }
@media (max-width: 1000px) { .wst-grid { grid-template-columns: 1fr; } .wst-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
