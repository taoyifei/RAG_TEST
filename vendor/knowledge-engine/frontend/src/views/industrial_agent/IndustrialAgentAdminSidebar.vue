<template>
  <aside class="ia-admin-sidebar" aria-label="工业智能体管理导航">
    <RouterLink class="ia-admin-brand" to="/overview" aria-label="工业智能体管理后台首页">
      <span class="ia-admin-brand-mark">工</span>
      <span><strong>工业智能体</strong><small>管理后台</small></span>
    </RouterLink>
    <p class="ia-admin-nav-label">工作台</p>
    <nav>
      <a class="ia-admin-return-chat" href="/kb/" target="_top" aria-label="返回对话" title="返回对话">
        <t-icon name="chat" size="19px" />
        <span>返回对话</span>
      </a>
      <RouterLink
        v-for="item in items"
        :key="item.path"
        :to="item.path"
        :class="{ active: isActive(item.path) }"
        :aria-label="item.label"
        :aria-current="isActive(item.path) ? 'page' : undefined"
      >
        <t-icon :name="item.icon" size="19px" />
        <span>{{ item.label }}</span>
      </RouterLink>
      <a href="/kb/?view=users"><t-icon name="usergroup" size="19px" /><span>用户与角色</span></a>
    </nav>
    <div class="ia-admin-sidebar-foot">
      <p>管理员设置会影响用户端的新请求。问答记录仅供复核。</p>
      <button type="button" :disabled="leaving" :aria-label="leaving ? '正在退出管理' : '退出管理'" @click="logout">
        <t-icon name="logout" size="18px" />
        <span>{{ leaving ? '正在退出…' : '退出管理' }}</span>
      </button>
      <p v-if="error" role="alert" class="ia-admin-sidebar-error">{{ error }}</p>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { logoutIndustrialAgentAdmin } from '@/config/industrialAgentGateway'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const leaving = ref(false)
const error = ref('')
const items = [
  { label: '运行概览', icon: 'dashboard', path: '/overview' },
  { label: '知识资料', icon: 'book-open', path: '/platform/knowledge-bases' },
  { label: '用户端设置', icon: 'setting-1', path: '/public-settings' },
  { label: '模型与解析', icon: 'control-platform', path: '/platform/settings?section=models' },
  { label: '使用记录', icon: 'history', path: '/records' },
  { label: '系统与诊断', icon: 'system-log', path: '/diagnostics' },
]

function isActive(path: string): boolean {
  if (path.startsWith('/platform/settings')) return route.path === '/platform/settings'
  if (path === '/platform/knowledge-bases') return route.path.startsWith(path)
  return route.path === path
}

async function logout() {
  leaving.value = true
  error.value = ''
  try {
    await logoutIndustrialAgentAdmin()
    authStore.logout()
    await router.replace('/login')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '退出失败'
  } finally {
    leaving.value = false
  }
}
</script>

<style scoped>
.ia-admin-sidebar {
  box-sizing: border-box;
  width: 238px;
  min-width: 238px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 20px 12px 14px;
  background: #f5f8fc;
  border-right: 1px solid #e1e9f2;
  color: #213650;
}
.ia-admin-brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 4px 12px 24px;
  text-decoration: none;
  color: inherit;
}
.ia-admin-brand-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: var(--app-radius-md);
  color: white;
  background: #145a8d;
  font-weight: 700;
}
.ia-admin-brand strong, .ia-admin-brand small { display: block; }
.ia-admin-brand strong { font-size: var(--app-text-2xl); letter-spacing: .04em; }
.ia-admin-brand small { margin-top: 1px; color: #647b94; font-size: var(--app-text-xs); }
.ia-admin-nav-label { margin: 0 12px 9px; color: #748aa2; font-size: var(--app-text-xs); letter-spacing: .1em; }
nav { display: grid; gap: 3px; }
nav a {
  display: flex;
  align-items: center;
  gap: 11px;
  min-height: 42px;
  padding: 0 12px;
  border-radius: var(--app-radius-md);
  color: #38536d;
  text-decoration: none;
  font-size: var(--app-text-base);
  font-weight: 500;
}
nav a.ia-admin-return-chat {
  margin-bottom: 6px;
  border: 1px solid #c7ddec;
  color: #145a8d;
  background: white;
  font-weight: 650;
}
nav a:hover { background: #eaf1f9; }
nav a.active { color: #125682; background: #dfeef9; font-weight: 650; }
nav a:focus-visible, button:focus-visible, .ia-admin-brand:focus-visible {
  outline: 2px solid #1677aa;
  outline-offset: 2px;
}
.ia-admin-sidebar-foot { margin-top: auto; padding: 16px 10px 2px; }
.ia-admin-sidebar-foot p { margin: 0 0 14px; color: #6b8197; font-size: var(--app-text-sm); line-height: 1.5; }
.ia-admin-sidebar-foot button {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d7e3ef;
  border-radius: var(--app-radius-md);
  background: white;
  color: #38536d;
  cursor: pointer;
}
.ia-admin-sidebar-foot .ia-admin-sidebar-error { margin-top: 10px; color: #b42318; }
@media (max-width: 900px) {
  .ia-admin-sidebar { width: 64px; min-width: 64px; padding-inline: 7px; }
  .ia-admin-brand { justify-content: center; padding-inline: 0; }
  .ia-admin-brand > span:last-child, .ia-admin-nav-label, nav a span, .ia-admin-sidebar-foot p, .ia-admin-sidebar-foot button span { display: none; }
  nav a { justify-content: center; padding: 0; }
  .ia-admin-sidebar-foot { padding-inline: 0; }
  .ia-admin-sidebar-foot button { justify-content: center; padding-inline: 0; }
  .ia-admin-sidebar-foot .ia-admin-sidebar-error {
    position: fixed;
    bottom: 16px;
    left: 72px;
    z-index: 20;
    display: block;
    max-width: min(320px, calc(100vw - 88px));
    margin: 0;
    padding: 10px 12px;
    border-radius: var(--app-radius-md);
    background: #fff0ef;
    box-shadow: 0 4px 18px #253c591c;
  }
}
</style>
