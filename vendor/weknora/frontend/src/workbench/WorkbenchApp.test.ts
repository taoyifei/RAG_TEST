import assert from 'node:assert/strict'
import test from 'node:test'
import { fixture, flush } from '../test-utils/vueFixture.ts'

test('工作助手先恢复员工会话再取本人原生身份，只读初始化后才显示内容', async t => {
  const calls: string[] = []
  const f = fixture(t)
  let release!: () => void
  const session = new Promise<void>(resolve => { release = resolve })
  const assistant = {
    setWorkspaceConfig(value: unknown) { calls.push(JSON.stringify(value)) },
    setAccessLevel(level: string) { calls.push(level) },
    open() { calls.push('open') },
  }
  const { vm } = f.component(new URL('./WorkbenchApp.vue', import.meta.url), {
    'vue-router': { useRoute: () => ({ name: 'workbenchAssistant' }) },
    '@/stores/auth': { useAuthStore: () => ({ refreshFromAuthMe: async () => { calls.push('native-me'); return true } }) },
    '@/stores/assistant': { useAssistantStore: () => assistant },
    '@/composables/useTheme': { useTheme: () => ({ setTheme() {} }) },
    '@/config/wanshitongGateway': {
      resumeWanshitongWorkbenchSession: async () => { calls.push('public-session'); await session },
      WORKBENCH_SESSION_EXPIRED: 'workbench-expired',
    },
  })
  assert.equal(vm.ready, false)
  assert.deepEqual(calls, ['public-session'])
  release()
  await flush()
  assert.equal(vm.ready, true)
  assert.deepEqual(calls, ['public-session', 'native-me', JSON.stringify({ display_name: '湾小度工作助手', max_access_level: 'read_only' }), 'read_only', 'open'])
  f.window.dispatchEvent(new Event('workbench-expired'))
  await flush()
  assert.equal(vm.ready, false)
  assert.match(vm.error, /登录已过期/)
})

test('原生身份拒绝时显示重试，不挂载助手，也不会开启管理台', async t => {
  const f = fixture(t)
  let allowed = false
  let opens = 0
  const { vm } = f.component(new URL('./WorkbenchApp.vue', import.meta.url), {
    'vue-router': { useRoute: () => ({ name: 'workbenchAssistant' }) },
    '@/stores/auth': { useAuthStore: () => ({ refreshFromAuthMe: async () => allowed }) },
    '@/stores/assistant': { useAssistantStore: () => ({ setWorkspaceConfig() {}, setAccessLevel() {}, open() { opens++ } }) },
    '@/composables/useTheme': { useTheme: () => ({ setTheme() {} }) },
    '@/config/wanshitongGateway': { resumeWanshitongWorkbenchSession: async () => {}, WORKBENCH_SESSION_EXPIRED: 'workbench-expired' },
  })
  await flush()
  assert.equal(vm.ready, false)
  assert.match(vm.error, /工作区会话不可用/)
  assert.equal(opens, 0)
  allowed = true
  await vm.connect()
  assert.equal(vm.error, '')
  assert.equal(vm.ready, true)
  assert.equal(opens, 1)
})
