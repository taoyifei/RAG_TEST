import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { compile } from '@vue/compiler-dom'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import { renderToString } from 'vue/server-renderer'

const source = readFileSync(new URL('./index.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
assert(descriptor.template)
const compiled = compile(descriptor.template.content, { mode: 'function', prefixIdentifiers: true })
const render = new Function('Vue', compiled.code)(vue)

async function settingsHosts(gatewayMode: boolean, path: string): Promise<string> {
  // 渲染实际布局模板；设置面板本身用只读占位组件，避免测试触发后台配置接口。
  const app = vue.createSSRApp(vue.defineComponent({
    setup: () => ({
      WANSHITONG_GATEWAY_AUTH: gatewayMode,
      route: { path },
      uiStore: { sidebarDisplayWidth: 238 },
      isRouterAlive: true,
      ismask: false,
      dropzone: null,
    }),
    render,
  }))
  const empty = vue.defineComponent({ render: () => null })
  for (const name of ['WanshitongAdminSidebar', 'Menu', 'RouterView', 'AssistantPanel',
    'AssistantLauncher', 'AssistantPageBridge', 'UploadMask', 'GlobalCommandPalette',
    'UploadTasksPanel', 'NewUserGuide']) app.component(name, empty)
  app.component('Settings', vue.defineComponent({
    render: () => vue.h('section', { 'data-settings-host': 'mounted' },
      path === '/platform/settings' ? '模型配置' : ''),
  }))
  return renderToString(app)
}

test('湾小度模型与解析路由必须挂载唯一设置面板，不能只剩空路由出口', async () => {
  const html = await settingsHosts(true, '/platform/settings')
  assert.equal((html.match(/data-settings-host=/g) || []).length, 1)
  assert(html.includes('模型配置'))
})

test('普通原生模式和湾小度其他页面仍复用唯一全局设置面板', async () => {
  for (const [gatewayMode, path] of [
    [false, '/platform/settings'],
    [false, '/platform/knowledge-bases'],
    [true, '/overview'],
  ] as const) {
    const html = await settingsHosts(gatewayMode, path)
    assert.equal((html.match(/data-settings-host=/g) || []).length, 1,
      `${gatewayMode ? '湾小度' : '原生'} ${path}应只有一个设置宿主`)
  }
})
