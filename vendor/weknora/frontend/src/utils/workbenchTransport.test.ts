import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import * as vue from 'vue'
import { renderToString } from 'vue/server-renderer'
import ts from 'typescript'

const base = '/kb/api/workbench/11111111-1111-4111-8111-111111111111/native'
const config = {
  WANSHITONG_GATEWAY_AUTH: true, WANSHITONG_WORKBENCH: true,
  gatewayCsrfHeaders: (method: string) => method.toUpperCase() === 'POST' ? { 'X-CSRF-Token':'employee-csrf' } : {},
  redirectToWanshitongAdminLogin: assert.fail,
}
const i18n = { __esModule: true, default: { global: { t: (key: string) => key, locale:{ value:'zh-CN' } } } }
function evaluate(file: string, dependencies: Record<string, unknown>) {
  const exports: any = {}
  runInNewContext(ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { module:ts.ModuleKind.CommonJS, target:ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, console, performance, AbortController, setTimeout, clearTimeout, URLSearchParams,
    localStorage:{ getItem: () => 'stale-native-identity' },
    require: (name: string) => {
      assert.ok(name in dependencies, `Unexpected import: ${name}`)
      return dependencies[name]
    },
  })
  return exports
}

test('工作区HTTP请求使用本人代理和Cookie，清理旧JWT及原生租户请求头', async () => {
  let intercept!: (request: any) => Promise<any> | any
  let baseURL = ''
  evaluate('./request.ts', {
    axios:{ __esModule:true, default:{ create: (options: {baseURL:string}) => {
      baseURL = options.baseURL
      return { interceptors:{ request:{ use(fn: typeof intercept) { intercept = fn } }, response:{use() {}} } }
    } } },
    './index': { generateRandomString: () => 'request-a' }, '@/i18n':i18n,
    './api-base': { getApiBaseUrl: () => base }, '@/config/wanshitongGateway':config,
    './uploadLimit':{}, './requestTimeouts':{}, './authRefresh':{},
  })
  const request = await intercept({ method:'POST', url:'/api/v1/sessions', headers:{Authorization:'Bearer stale',authorization:'Bearer old','X-Tenant-ID':'99'} })
  assert.equal(baseURL, base)
  assert.equal(request.headers.Authorization, undefined)
  assert.equal(request.headers.authorization, undefined)
  assert.equal(request.headers['X-Tenant-ID'], undefined)
  assert.equal(request.headers['X-CSRF-Token'], 'employee-csrf')
})

test('工作助手发问和续流都走本人workspace，不发送旧JWT或原生租户', async () => {
  const calls: {url:string; options:any}[] = []
  const streamModule = evaluate('../api/chat/streame.ts', {
    vue, '@microsoft/fetch-event-source':{fetchEventSource:async (url:string, options:any) => {
      calls.push({url,options})
      await options.onopen(new Response(null, {status:200}))
      options.onmessage({data:JSON.stringify({response_type:'complete',done:true})})
    } },
    '@/utils/index':{generateRandomString: () => 'request-a'}, '@/i18n':i18n,
    '@/utils/api-base':{ getApiBaseUrl: () => base }, '@/config/wanshitongGateway':config,
    '@/utils/chatRequestDebug':{ sanitizeStreamRequestBody:(body:unknown) => body },
    '@/utils/authRefresh':{},
  })
  let stream: any
  await renderToString(vue.createSSRApp({setup() {stream=streamModule.useStream(); return () => null}}))
  try {
    await stream.startStream({session_id:'session-a',query:'整理当前资料',method:'POST',url:'/api/v1/agent-chat',agent_id:'builtin-weknora-assistant',assistant_access:'read_only',web_search_enabled:false})
    assert.equal(stream.error.value, null)
    assert.equal(calls[0].url, `${base}/api/v1/agent-chat/session-a`)
    assert.equal(calls[0].options.headers['X-CSRF-Token'], 'employee-csrf')
    const body = JSON.parse(calls[0].options.body)
    assert.equal(body.assistant_access, 'read_only')
    assert.equal(body.web_search_enabled, false)
    await stream.startStream({session_id:'session-a',query:'message-a',method:'GET',url:'/api/v1/sessions/continue-stream'})
    assert.equal(calls[1].url, `${base}/api/v1/sessions/continue-stream/session-a?message_id=message-a`)
    for (const call of calls) {
      assert.equal(call.options.credentials, 'include')
      assert.equal(call.options.headers.Authorization, undefined)
      assert.equal(call.options.headers['X-Tenant-ID'], undefined)
    }
  } finally { stream.stopStream() }
})
