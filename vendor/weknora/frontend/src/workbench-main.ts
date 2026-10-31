import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/dist/tdesign.css'
import '@/assets/theme/theme.css'
import '@/assets/theme/tdesign-overrides.less'
import '@/assets/dropdown-menu.less'
import '@/components/css/chat-hljs-dark.less'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'
import WorkbenchApp from '@/workbench/WorkbenchApp.vue'
import i18n from '@/i18n'
import { initTheme } from '@/composables/useTheme'
import { initFont } from '@/composables/useFont'
import { installTDesignIconOfflineGuard } from '@/utils/tdesign-icon-offline'
import { installAutofillGuard } from '@/utils/disable-autofill'
import { WANSHITONG_WORKBENCH_ID } from '@/config/wanshitongGateway'

// 独立小路由不加载原生管理台，也没有组织、模型、智能体配置入口。
const router = createRouter({
  history: createWebHashHistory(`/kb/workbench/${WANSHITONG_WORKBENCH_ID}/`),
  routes: [
    { path: '/', name: 'workbenchAssistant', component: { render: () => null } },
    { path: '/platform/automations/:automationId?', name: 'automationList', component: () => import('@/views/automation/AutomationList.vue') },
    { path: '/platform/chat/:session_id', name: 'chat', component: () => import('@/views/chat/index.vue'), props: true },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

installTDesignIconOfflineGuard()
initTheme()
initFont()
document.documentElement.classList.add('wanshitong-workbench')
document.title = '湾小度工作助手'
const app = createApp(WorkbenchApp)
app.use(createPinia())
app.use(TDesign)
app.use(i18n)
app.use(router)
async function mountWorkbench() {
  await router.isReady()
  app.mount('#app')
  installAutofillGuard()
}
void mountWorkbench()
