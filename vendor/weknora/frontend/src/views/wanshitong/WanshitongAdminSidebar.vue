<template>
  <aside class="wst-admin-sidebar" aria-label="湾事通管理导航">
    <RouterLink class="wst-admin-brand" to="/overview" aria-label="湾事通管理后台首页">
      <span class="wst-admin-brand-mark">湾</span>
      <span><strong>湾事通</strong><small>管理后台</small></span>
    </RouterLink>
    <p class="wst-admin-nav-label">工作台</p>
    <nav>
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
    </nav>
    <div class="wst-admin-sidebar-foot">
      <p>管理员设置会影响用户端的新请求。问答记录仅供复核。</p>
      <button type="button" :disabled="leaving" :aria-label="leaving ? '正在退出管理' : '退出管理'" @click="logout">
        <t-icon name="logout" size="18px" />
        <span>{{ leaving ? '正在退出…' : '退出管理' }}</span>
      </button>
      <p v-if="error" role="alert" class="wst-admin-sidebar-error">{{ error }}</p>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { logoutWanshitongAdmin } from '@/config/wanshitongGateway'

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
    await logoutWanshitongAdmin()
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
.wst-admin-sidebar {
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
.wst-admin-brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 4px 12px 24px;
  text-decoration: none;
  color: inherit;
}
.wst-admin-brand-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: var(--app-radius-md);
  color: white;
  background: #145a8d;
  font-weight: 700;
}
.wst-admin-brand strong, .wst-admin-brand small { display: block; }
.wst-admin-brand strong { font-size: var(--app-text-2xl); letter-spacing: .04em; }
.wst-admin-brand small { margin-top: 1px; color: #647b94; font-size: var(--app-text-xs); }
.wst-admin-nav-label { margin: 0 12px 9px; color: #748aa2; font-size: var(--app-text-xs); letter-spacing: .1em; }
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
nav a:hover { background: #eaf1f9; }
nav a.active { color: #125682; background: #dfeef9; font-weight: 650; }
nav a:focus-visible, button:focus-visible, .wst-admin-brand:focus-visible {
  outline: 2px solid #1677aa;
  outline-offset: 2px;
}
.wst-admin-sidebar-foot { margin-top: auto; padding: 16px 10px 2px; }
.wst-admin-sidebar-foot p { margin: 0 0 14px; color: #6b8197; font-size: var(--app-text-sm); line-height: 1.5; }
.wst-admin-sidebar-foot button {
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
.wst-admin-sidebar-foot .wst-admin-sidebar-error { margin-top: 10px; color: #b42318; }
@media (max-width: 900px) {
  .wst-admin-sidebar { width: 64px; min-width: 64px; padding-inline: 7px; }
  .wst-admin-brand { justify-content: center; padding-inline: 0; }
  .wst-admin-brand > span:last-child, .wst-admin-nav-label, nav a span, .wst-admin-sidebar-foot p, .wst-admin-sidebar-foot button span { display: none; }
  nav a { justify-content: center; padding: 0; }
  .wst-admin-sidebar-foot { padding-inline: 0; }
  .wst-admin-sidebar-foot button { justify-content: center; padding-inline: 0; }
  .wst-admin-sidebar-foot .wst-admin-sidebar-error {
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
