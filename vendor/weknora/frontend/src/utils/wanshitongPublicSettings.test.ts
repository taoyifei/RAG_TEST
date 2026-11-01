import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isProxy, ref } from 'vue'
import { clonePublicSettings } from './wanshitongPublicSettings'

test('响应式回答、页面和资料范围可保存，嵌套提示词完整且快照不随表单变化', () => {
  const answer = ref({
    fallback_strategy: 'model', fallback_prompt: '这题我先不抢答。{{query}}',
    temperature: 0, thinking: false, intent_prompts: { clarify: '请补充具体业务名称。' },
  })
  const page = ref({ welcome_text: '湾小度', show_history: false })
  const selectedIds = ref(['kb-public', 'kb-hr-public'])
  assert.throws(() => structuredClone(answer.value), { name: 'DataCloneError' })
  const payload = {
    knowledge_base_ids: clonePublicSettings(selectedIds.value),
    answer_settings: clonePublicSettings(answer.value),
    page_settings: clonePublicSettings(page.value),
  }
  assert.equal(isProxy(payload.answer_settings), false)
  assert.equal(isProxy(payload.answer_settings.intent_prompts), false)
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), {
    knowledge_base_ids: ['kb-public', 'kb-hr-public'],
    answer_settings: {
      fallback_strategy: 'model', fallback_prompt: '这题我先不抢答。{{query}}',
      temperature: 0, thinking: false, intent_prompts: { clarify: '请补充具体业务名称。' },
    },
    page_settings: { welcome_text: '湾小度', show_history: false },
  })
  answer.value.intent_prompts.clarify = '后续编辑'
  page.value.welcome_text = '后续编辑'
  selectedIds.value.push('another-kb')
  assert.equal(payload.answer_settings.intent_prompts.clarify, '请补充具体业务名称。')
  assert.equal(payload.page_settings.welcome_text, '湾小度')
  assert.deepEqual(payload.knowledge_base_ids, ['kb-public', 'kb-hr-public'])
})

test('保存后回读暂缺回答设置时，可复制当前响应式表单', () => {
  const answer = ref({ fallback_prompt: '当前提示词', max_completion_tokens: 0 })
  const readback: { answer_settings: typeof answer.value | null } = { answer_settings: null }
  const restored = clonePublicSettings(readback.answer_settings ?? answer.value)
  assert.deepEqual(restored, { fallback_prompt: '当前提示词', max_completion_tokens: 0 })
  assert.notEqual(restored, answer.value)
})
