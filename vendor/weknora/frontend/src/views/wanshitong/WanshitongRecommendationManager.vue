<template>
  <section class="wst-recommendations">
    <div class="wst-recommendation-list">
      <div class="wst-heading">
        <div><h2>推荐问题审核目录</h2><p>公开题面由人工核对资料后发布；题目展示不改变检索和模型输出。</p></div>
        <button type="button" @click="newDraft()">新建题目</button>
      </div>
      <button v-for="item in items" :key="item.recommendation_id" type="button"
        :class="{ active: selected?.recommendation_id === item.recommendation_id }"
        :aria-pressed="selected?.recommendation_id === item.recommendation_id" @click="select(item)">
        <span>{{ item.question }}</span><small>{{ stateLabel(item.state) }} · v{{ item.version }}</small>
      </button>
      <p v-if="items.length === 0">尚无推荐题。</p>
    </div>
    <form class="wst-recommendation-form" @submit.prevent>
      <h3>{{ selected ? '编辑推荐题' : '新建推荐题' }}</h3>
      <label>可公开题面<textarea v-model="question" maxlength="500" @input="confirmed = false" /></label>
      <label>主题<input v-model="topic" maxlength="100" /></label>
      <label>已核对的知识库文件 ID（每行一个）<textarea v-model="sourceIds" @input="confirmed = false" /></label>
      <label>审核记录<textarea v-model="reviewNote" maxlength="1000" @input="confirmed = false" /></label>
      <label class="wst-check"><input v-model="confirmed" type="checkbox" />我已核对题面和对应资料，确认可以公开</label>
      <label>下架原因<input v-model="disabledReason" maxlength="500" /></label>
      <div class="wst-actions">
        <button type="button" :disabled="busy || !ready" @click="save('DRAFT')">保存草稿</button>
        <button type="button" :disabled="busy || !ready || !hasSources || !reviewNote.trim() || !confirmed" @click="save('APPROVED')">审核发布</button>
        <button type="button" :disabled="busy || !selected || !disabledReason.trim()" @click="save('DISABLED')">立即下架</button>
      </div>
      <p v-if="error" role="alert" class="wst-error">{{ error }}</p>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getOpsRecommendations, saveOpsRecommendation, type Recommendation } from '@/api/wanshitongOps'
import { useWanshitongAdminStore } from '@/stores/wanshitongAdmin'

const adminStore = useWanshitongAdminStore()
const items = ref<Recommendation[]>([])
const selected = ref<Recommendation | null>(null)
const question = ref('')
const topic = ref('')
const sourceIds = ref('')
const reviewNote = ref('')
const confirmed = ref(false)
const disabledReason = ref('')
const busy = ref(false)
const error = ref('')
const ready = computed(() => Boolean(question.value.trim() && topic.value.trim()))
const hasSources = computed(() => sourceIds.value.split(/[,\n]/u).some(id => id.trim()))

function stateLabel(state: Recommendation['state']): string {
  return state === 'APPROVED' ? '已发布' : state === 'DISABLED' ? '已下架' : '草稿'
}
function newDraft(seed = '') {
  selected.value = null
  question.value = seed
  topic.value = ''
  sourceIds.value = ''
  reviewNote.value = ''
  confirmed.value = false
  disabledReason.value = ''
}
function select(item: Recommendation) {
  selected.value = item
  question.value = item.question
  topic.value = item.topic_key
  sourceIds.value = item.source_knowledge_ids.join('\n')
  reviewNote.value = item.review_note
  disabledReason.value = item.disabled_reason ?? ''
  confirmed.value = false
}
async function load() {
  try {
    items.value = (await getOpsRecommendations()).items
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法读取推荐问题'
  }
}
async function save(state: Recommendation['state']) {
  busy.value = true
  error.value = ''
  try {
    const result = await saveOpsRecommendation({
      recommendation_id: selected.value?.recommendation_id,
      expected_version: selected.value?.version ?? null,
      question: question.value.trim(),
      topic_key: topic.value.trim(),
      source_knowledge_ids: sourceIds.value.split(/[,\n]/u).map(id => id.trim()).filter(Boolean),
      review_note: reviewNote.value.trim(),
      state,
      review_confirmed: state === 'APPROVED' && confirmed.value,
      disabled_reason: state === 'DISABLED' ? disabledReason.value.trim() : null,
    })
    await load()
    select(result)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '推荐问题保存失败'
  } finally {
    busy.value = false
  }
}
watch(() => adminStore.seedQuestion, (seed) => {
  if (seed) newDraft(adminStore.consumeRecommendationDraft())
}, { immediate: true })
onMounted(() => { void load() })
</script>

<style scoped>
.wst-recommendations { display: grid; grid-template-columns: minmax(240px, 1fr) minmax(340px, 1.4fr); gap: 18px; }
.wst-recommendation-list, .wst-recommendation-form { padding: 22px; border: 1px solid #e0e8f0; border-radius: var(--app-radius-md); background: white; }
.wst-heading { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
h2, h3 { margin: 0 0 9px; color: #274059; }
p { color: #72869a; line-height: 1.5; }
.wst-recommendation-list > button:not(.wst-heading button) { display: grid; gap: 5px; width: 100%; margin-top: 8px; padding: 11px; border: 1px solid #e1eaf2; border-radius: var(--app-radius-md); background: #f9fbfd; text-align: left; color: #284a64; cursor: pointer; }
.wst-recommendation-list > button.active { border-color: #83bbda; background: #eff8fd; }
.wst-recommendation-list small { color: #718699; }
.wst-recommendation-form { display: grid; align-content: start; gap: 14px; }
.wst-recommendation-form label:not(.wst-check) { display: grid; gap: 6px; color: #445e72; font-size: var(--app-text-md); }
.wst-recommendation-form textarea, .wst-recommendation-form input:not([type=checkbox]) { width: 100%; min-height: 37px; padding: 8px 10px; border: 1px solid #ccd9e5; border-radius: var(--app-radius-md); font: inherit; }
.wst-recommendation-form textarea { min-height: 70px; resize: vertical; }
.wst-check { display: flex; align-items: center; gap: 8px; color: #445e72; }
.wst-actions { display: flex; flex-wrap: wrap; gap: 8px; }
button { padding: 8px 12px; border: 1px solid #cadbe7; border-radius: var(--app-radius-md); background: white; color: #1b5f86; font: inherit; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
button:focus-visible, input:focus-visible, textarea:focus-visible { outline: 2px solid #1677aa; outline-offset: 2px; }
.wst-error { color: #b42318; }
@media (max-width: 1100px) { .wst-recommendations { grid-template-columns: 1fr; } }
</style>
