import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

function gateway(path: string) {
  const id = '11111111-1111-4111-8111-111111111111'
  const calls: { url: string; options: RequestInit }[] = []
  const window = Object.assign(new EventTarget(), { location: { pathname: path, replace: assert.fail } })
  const exports: any = {}
  const source = readFileSync(new URL('./wanshitongGateway.ts', import.meta.url), 'utf8')
    .replace("import.meta.env?.VITE_WANSHITONG_GATEWAY_AUTH === 'true'", 'true')
  runInNewContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, window, Event, URL,
    fetch: async (url: string, options: RequestInit) => {
      calls.push({ url, options })
      return Response.json(url.includes('/public/') ? { csrf_token: 'employee-csrf' } : { authenticated: true, csrf_token: 'admin-csrf' })
    },
  })
  return { config: exports, calls, id, window }
}

test('独立助手使用员工Cookie和内存CSRF，所有原生资源固定走本人workspace代理', async () => {
  const f = gateway('/kb/workbench/11111111-1111-4111-8111-111111111111/')
  assert.equal(f.config.WANSHITONG_WORKBENCH, true)
  const base = `/kb/api/workbench/${f.id}/native`
  assert.equal(f.config.WANSHITONG_ENGINE_API, base)
  assert.equal(f.config.gatewayCsrfHeaders('POST')['X-CSRF-Token'], undefined)
  await f.config.resumeWanshitongWorkbenchSession()
  assert.deepEqual(f.calls.map(call => call.url), ['/kb/api/public/session'])
  assert.equal(f.calls[0].options.credentials, 'include')
  assert.equal(f.config.gatewayCsrfHeaders('POST')['X-CSRF-Token'], 'employee-csrf')
  assert.deepEqual(Object.keys(f.config.gatewayCsrfHeaders('GET')), [])
  assert.equal(f.config.engineResourceUrl('/api/v1/sessions/s/messages/m/files?file_path=resource%3A%2F%2Fx'), `${base}/api/v1/sessions/s/messages/m/files?file_path=resource%3A%2F%2Fx`)
  let expired = false
  f.window.addEventListener(f.config.WORKBENCH_SESSION_EXPIRED, () => { expired = true })
  f.config.redirectToWanshitongAdminLogin()
  assert.equal(expired, true)
  assert.equal(f.config.gatewayCsrfHeaders('POST')['X-CSRF-Token'], undefined)
})

test('原管理台仍走Console Session，工作区初始化不能借用它', async () => {
  const f = gateway('/kb/admin/overview')
  assert.equal(f.config.WANSHITONG_WORKBENCH, false)
  assert.equal(f.config.WANSHITONG_ENGINE_API, '/kb/api/engine-admin')
  await assert.rejects(f.config.resumeWanshitongWorkbenchSession(), /工作区地址无效/)
  assert.equal(f.calls.length, 0)
  assert.equal(await f.config.resumeWanshitongAdminSession(), true)
  assert.equal(f.calls[0].url, '/kb/api/v1/console/session')
  assert.equal(f.config.gatewayCsrfHeaders('POST')['X-CSRF-Token'], 'admin-csrf')
})

test('工作区图片代理能识别本人的新前缀，文件请求不继承原生JWT或租户选择', () => {
  const f = gateway('/kb/workbench/11111111-1111-4111-8111-111111111111/')
  const exports: any = {}
  runInNewContext(ts.transpileModule(readFileSync(new URL('../utils/protectedFileAccess.ts', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, window: {}, URLSearchParams,
    localStorage: { getItem: () => 'stale-native-identity' },
    require: (name: string) => {
      assert.equal(name, '../config/wanshitongGateway')
      return f.config
    },
  })
  const resource = 'resource://AbCdEfGhIjKlMnOpQrStUv'
  const result = exports.buildProtectedFileRequest(resource, { mode:'message', sessionId:'session-a', messageId:'message-a' })
  assert.equal(result.url, `${f.config.WANSHITONG_ENGINE_API}/api/v1/sessions/session-a/messages/message-a/files?file_path=${encodeURIComponent(resource)}`)
  assert.equal(exports.isProtectedFileProxyPath(result.url.split('?')[0]), true)
  assert.equal(exports.isProtectedFileProxyPath('/kb/api/workbench/other/native/api/v1/sessions/s/messages/m/files'), false)
  assert.equal(Object.keys(result.headers).length, 0)
})
