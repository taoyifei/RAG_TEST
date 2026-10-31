import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as pinia from 'pinia'
import * as vue from 'vue'
import ts from 'typescript'

const source = readFileSync(new URL('./assistant.ts', import.meta.url), 'utf8')

function evaluate(script: string, dependencies: Record<string, unknown>, globals = {}) {
  const exports: any = {}
  runInNewContext(ts.transpileModule(script, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, ...globals,
    require(name: string) {
      assert.ok(name in dependencies, `Unexpected dependency: ${name}`)
      return dependencies[name]
    },
  })
  return exports
}

function fixture(gatewayMode: boolean) {
  const auth = vue.reactive({ user: { id: 'user-a' }, effectiveTenantId: 1 })
  const values = new Map([
    ['weknora:assistant-session:user-a:1', 'native-assistant-session'],
    ['weknora:assistant-visible:user-a:1', '1'],
  ])
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
  const reads: string[] = []
  const stores = evaluate(source, {
    vue, pinia,
    './auth': { useAuthStore: () => auth },
    '@/config/wanshitongGateway': { WANSHITONG_GATEWAY_AUTH: gatewayMode },
    '@/api/assistant': {
      DEFAULT_ASSISTANT_ACCESS_LEVEL: 'confirm',
      capAssistantAccessLevel: (level: string) => level,
      isAssistantAccessLevel: (value: unknown) => ['read_only', 'confirm', 'auto_write'].includes(String(value)),
      getTenantAssistantConfig: async () => {
        reads.push('config')
        return { data: { display_name: 'Assistant', max_access_level: 'confirm' } }
      },
    },
  }, { localStorage: storage, sessionStorage: storage })
  const root = pinia.createPinia()
  pinia.setActivePinia(root)
  return { auth, reads, assistant: stores.useAssistantStore(), close: () => pinia.disposePinia(root) }
}

test('湾小度不恢复原生助手，不读取配置，也不能通过快捷操作开启', async () => {
  const f = fixture(true)
  try {
    f.assistant.setPageAccess(true)
    f.assistant.open({ query: '读取当前资料' })
    f.assistant.toggle()
    f.assistant.ask('分析人力文件')
    f.auth.effectiveTenantId = 2
    await vue.nextTick()
    assert.equal(f.assistant.visible, false)
    assert.equal(f.assistant.pageAccess, false)
    assert.equal(f.assistant.sessionId, '')
    assert.equal(f.assistant.pendingRequest, null)
    assert.equal(f.assistant.queuedMessage, null)
    assert.deepEqual(f.reads, [])
  } finally {
    f.close()
  }
})

test('非网关模式保留官方助手恢复和开启行为', () => {
  const f = fixture(false)
  try {
    assert.equal(f.assistant.visible, true)
    assert.equal(f.assistant.pageAccess, true)
    assert.equal(f.assistant.sessionId, 'native-assistant-session')
    assert.deepEqual(f.reads, ['config'])
    f.assistant.close()
    f.assistant.open({ query: '整理当前页面' })
    assert.equal(f.assistant.visible, true)
    assert.equal(f.assistant.pendingRequest.query, '整理当前页面')
  } finally {
    f.close()
  }
})

test('知识资料页共用的助手按钮在网关模式不渲染', () => {
  const { descriptor } = parse(readFileSync(new URL('../components/assistant/AssistantButton.vue', import.meta.url), 'utf8'))
  const script = compileScript(descriptor, { id: 'wanshitong-assistant-button', inlineTemplate: true }).content
  for (const gatewayMode of [true, false]) {
    const component = evaluate(script, {
      vue,
      '@/components/icons/ResourceIcon.vue': { render: () => null },
      '@/config/wanshitongGateway': { WANSHITONG_GATEWAY_AUTH: gatewayMode },
    }).default
    const render = component.setup({ label: 'Assistant' }, { expose() {} })
    assert.equal(render({}, []).type, gatewayMode ? vue.Comment : 'button')
  }
})
