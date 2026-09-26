<template>
  <main class="wst-login">
    <section class="wst-login-card">
      <div class="wst-login-mark">湾</div>
      <p class="wst-login-eyebrow">湾事通 · 管理后台</p>
      <h1>管理员登录</h1>
      <p class="wst-login-hint">使用湾事通管理员口令进入知识资料和用户端配置。</p>
      <form @submit.prevent="login">
        <label for="wst-admin-token">管理员口令</label>
        <input id="wst-admin-token" v-model="token" type="password" autocomplete="off" required :disabled="busy" />
        <button type="submit" :disabled="busy || !token.trim()">{{ busy ? '正在登录…' : '进入管理后台' }}</button>
      </form>
      <p v-if="error" role="alert" class="wst-login-error">{{ error }}</p>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { loginWanshitongAdmin, safeWanshitongReturnTo } from '@/config/wanshitongGateway'

const token = ref('')
const busy = ref(false)
const error = ref('')
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

async function login() {
  if (!token.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    await loginWanshitongAdmin(token.value)
    token.value = ''
    if (!await authStore.refreshFromAuthMe() || !authStore.hasValidTenant) {
      throw new Error('管理员会话已建立，但知识库管理身份不可用')
    }
    await router.replace(safeWanshitongReturnTo(route.query.returnTo))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.wst-login { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f1f6fb; }
.wst-login-card { width: min(100%, 420px); padding: 34px; border: 1px solid #dce8f3; border-radius: var(--app-radius-md); background: white; box-shadow: 0 20px 60px #27486414; }
.wst-login-mark { display: grid; place-items: center; width: 46px; height: 46px; border-radius: var(--app-radius-md); background: #145a8d; color: white; font-size: var(--app-text-4xl); font-weight: 700; }
.wst-login-eyebrow { margin: 23px 0 5px; color: #547996; font-size: var(--app-text-md); }
h1 { margin: 0; color: #203a53; font-size: var(--app-text-4xl); }
.wst-login-hint { margin: 11px 0 27px; color: #667e93; line-height: 1.6; }
form { display: grid; gap: 10px; }
label { color: #314a63; font-weight: 600; }
input { width: 100%; height: 43px; padding: 0 12px; border: 1px solid #c9d7e4; border-radius: var(--app-radius-md); font: inherit; }
button { height: 43px; margin-top: 8px; border: 0; border-radius: var(--app-radius-md); background: #145a8d; color: white; font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { opacity: .6; cursor: not-allowed; }
input:focus-visible, button:focus-visible { outline: 2px solid #1677aa; outline-offset: 2px; }
.wst-login-error { color: #b42318; }
</style>
