<template>
  <main class="workbench-shell">
    <div v-if="!ready" class="workbench-state">
      <template v-if="error">
        <h1>暂时无法打开工作助手</h1>
        <p role="alert">{{ error }}</p>
        <div class="workbench-state__actions">
          <t-button @click="connect">重新连接</t-button>
          <a href="/kb/" target="_top">返回知识问答</a>
        </div>
      </template>
      <t-loading v-else text="正在连接你的工作区…" />
    </div>
    <template v-else>
      <nav class="workbench-tabs" aria-label="工作助手功能">
        <router-link to="/" :aria-current="isAssistant ? 'page' : undefined">资料助手</router-link>
        <router-link to="/platform/automations" :class="{ 'router-link-active': !isAssistant }">自动化任务</router-link>
        <span class="workbench-tabs__note">只读当前资料范围</span>
        <WorkbenchNotifications />
      </nav>
      <div v-show="isAssistant" class="workbench-surface">
        <AssistantPanel />
      </div>
      <section v-if="!isAssistant" class="workbench-surface workbench-page">
        <router-view />
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import WorkbenchNotifications from './WorkbenchNotifications.vue'
import { useAuthStore } from '@/stores/auth'
import { useAssistantStore } from '@/stores/assistant'
import { useTheme } from '@/composables/useTheme'
import { resumeWanshitongWorkbenchSession, WORKBENCH_SESSION_EXPIRED } from '@/config/wanshitongGateway'

const auth = useAuthStore()
const assistant = useAssistantStore()
const theme = useTheme()
const route = useRoute()
const isAssistant = computed(() => route.name === 'workbenchAssistant')
const ready = ref(false)
const error = ref('')
let generation = 0

async function connect() {
  const current = ++generation
  ready.value = false
  error.value = ''
  try {
    await resumeWanshitongWorkbenchSession()
    if (current !== generation) return
    if (!await auth.refreshFromAuthMe()) throw new Error('工作区会话不可用，请返回湾小度重新打开。')
    if (current !== generation) return
    assistant.setWorkspaceConfig({ display_name: '湾小度工作助手', max_access_level: 'read_only' })
    assistant.setAccessLevel('read_only')
    assistant.open()
    ready.value = true
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : '暂时无法连接工作区，请重试。'
  }
}

function onSessionExpired() {
  ++generation
  ready.value = false
  error.value = '登录已过期，请返回湾小度重新登录后再打开工作助手。'
}
function onThemeMessage(event: MessageEvent) {
  if (event.origin !== window.location.origin || event.source !== window.parent) return
  if (event.data?.type === 'wanshitong-workbench-theme' && ['dark', 'light'].includes(event.data.theme)) {
    theme.setTheme(event.data.theme)
  }
}
onMounted(() => {
  window.addEventListener(WORKBENCH_SESSION_EXPIRED, onSessionExpired)
  window.addEventListener('message', onThemeMessage)
  void connect()
})
onBeforeUnmount(() => {
  ++generation
  window.removeEventListener(WORKBENCH_SESSION_EXPIRED, onSessionExpired)
  window.removeEventListener('message', onThemeMessage)
})
</script>

<style lang="less">
html.wanshitong-workbench, .wanshitong-workbench body, .wanshitong-workbench #app {
  margin: 0;
  width: 100%;
  height: 100%;
  min-height: 0 !important;
  overflow: hidden;
}
.workbench-shell {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  color: var(--td-text-color-primary);
  background: var(--td-bg-color-container);
}
.workbench-tabs {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  padding: 0 16px;
  border-bottom: 1px solid var(--td-component-stroke);
  a {
    padding: 8px 12px;
    border-radius: 8px;
    color: var(--td-text-color-secondary);
    text-decoration: none;
    font-size: 14px;
  }
  a.router-link-exact-active, a.router-link-active:not([href$='#/']) {
    color: var(--td-brand-color);
    background: var(--td-brand-color-light);
  }
  a:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
}
.workbench-tabs__note { margin-left: auto; color: var(--td-text-color-placeholder); font-size: 12px; }
.workbench-surface { display: flex; flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
.workbench-page > * { flex: 1; min-width: 0; min-height: 0; }
.workbench-state {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 24px;
  text-align: center;
  h1 { font-size: 20px; margin: 0; }
  p { max-width: 480px; margin: 0; color: var(--td-text-color-secondary); line-height: 1.7; }
}
.workbench-state__actions { display: flex; align-items: center; gap: 20px; }
.workbench-state__actions a { color: var(--td-brand-color); }
@media (max-width: 480px) {
  .workbench-tabs { padding: 0 8px; gap: 2px; }
  .workbench-tabs__note { max-width: 88px; text-align: right; }
  .workbench-shell .automation-page { padding: 16px; }
}
</style>
