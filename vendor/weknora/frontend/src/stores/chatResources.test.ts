import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test, { type TestContext } from 'node:test'
import * as vue from 'vue'
import * as pinia from 'pinia'
import * as resources from './resourceCache.ts'
import * as localized from './localizedResourceCache.ts'
import { change, fixture, flush } from '../test-utils/vueFixture.ts'

function setup(t: TestContext, workbench = false) {
  const f = fixture(t)
  const api = {
    listKnowledgeBases: t.mock.fn(async () => ({ data: [{ id: 'kb' }] })),
    getKnowledgeBaseById: t.mock.fn(async () => ({ data: { id: 'kb' } })),
    listAgents: t.mock.fn(async () => ({ data: [{ id: 'agent' }] })),
    getAgentById: t.mock.fn(async () => ({ data: { id: 'builtin-weknora-assistant' } })),
    listModels: t.mock.fn(async () => [{ id: 'model' }]),
  }
  const { useChatResourcesStore } = f.evaluate(readFileSync(new URL('./chatResources.ts', import.meta.url), 'utf8'), {
    vue, pinia, '@/utils/assistantDataChange': f.dataChange,
    '@/api/knowledge-base': api, '@/api/agent': api,
    '@/api/model': api, '@/api/web-search-provider': {}, '@/api/system': {},
    '@/config/wanshitongGateway': { WANSHITONG_WORKBENCH: workbench },
    '@/utils/request': { getCurrentLanguage: () => 'en-US' },
    '@/stores/organization': { useOrganizationStore: () => ({
      async fetchSharedKnowledgeBases() {}, async fetchSharedAgents() {},
    }) },
    './resourceCache': resources, './localizedResourceCache': localized,
  })
  const root = pinia.createPinia()
  pinia.setActivePinia(root)
  t.after(() => pinia.disposePinia(root))
  return { ...f, api, store: useChatResourcesStore() }
}

test('refreshes agent selectors and invalidates per-agent KB access snapshots', async t => {
  const { store, api, emit } = setup(t)
  await store.ensureAgentKnowledgeBases('agent')
  await store.ensureAgentKnowledgeBases('agent')
  assert.equal(api.listKnowledgeBases.mock.callCount(), 1)
  emit(change('agent.update', { agent_id: ['agent'] }))
  t.mock.timers.tick(300)
  await flush()
  assert.equal(api.listAgents.mock.callCount(), 1)
  assert.equal(store.agents[0].id, 'agent')
  await store.ensureAgentKnowledgeBases('agent')
  assert.equal(api.listKnowledgeBases.mock.callCount(), 2)
  store.$dispose()
  emit(change('agent.delete'))
  t.mock.timers.tick(300)
  await flush()
  assert.equal(api.listAgents.mock.callCount(), 1)
})

test('工作区预取仅加载所选KB、内置助手和模型，不碰组织或外部工具目录', async t => {
  const { store, api } = setup(t, true)
  await store.prefetchChatInput()
  await store.ensureSandboxConfigs()
  assert.equal(api.getAgentById.mock.callCount(), 1)
  assert.equal(api.listAgents.mock.callCount(), 0)
  assert.equal(api.listKnowledgeBases.mock.callCount(), 1)
  assert.equal(api.listModels.mock.callCount(), 1)
  assert.equal(store.agents[0].id, 'builtin-weknora-assistant')
  assert.deepEqual(Array.from(store.webSearchProviders), [])
  assert.deepEqual(Array.from(store.sandboxConfigs), [])
})

for (const operation of ['knowledge_base.generate_profile', 'knowledge.delete', 'faq.update_entry', 'wiki.revert']) {
  test(`refreshes KB selectors and invalidates details on ${operation}`, async t => {
    const { store, api, emit } = setup(t)
    await store.fetchKnowledgeBaseById('kb')
    await store.fetchKnowledgeBaseById('kb')
    assert.equal(api.getKnowledgeBaseById.mock.callCount(), 1)
    emit(change(operation, { knowledge_base_id: ['kb'] }))
    t.mock.timers.tick(300)
    await flush()
    assert.equal(api.listKnowledgeBases.mock.callCount(), 1)
    await store.fetchKnowledgeBaseById('kb')
    assert.equal(api.getKnowledgeBaseById.mock.callCount(), 2)
  })
}

test('refreshes the model selectors on a model change', async t => {
  const { store, api, emit } = setup(t)
  emit(change('model.update'))
  t.mock.timers.tick(300)
  await flush()
  assert.equal(api.listModels.mock.callCount(), 1)
  assert.equal(store.allModels[0].id, 'model')
  assert.equal(api.listAgents.mock.callCount(), 0)
})

test('ignores unknown areas', async t => {
  const { api, emit } = setup(t)
  emit(change('unknown.update'))
  t.mock.timers.tick(300)
  await flush()
  assert.equal(api.listAgents.mock.callCount(), 0)
  assert.equal(api.listKnowledgeBases.mock.callCount(), 0)
  assert.equal(api.listModels.mock.callCount(), 0)
})
