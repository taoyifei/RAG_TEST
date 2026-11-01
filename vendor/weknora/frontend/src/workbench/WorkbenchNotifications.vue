<template>
  <t-popup v-model:visible="visible" trigger="click" placement="bottom-right" :overlay-inner-style="{ padding: 0 }">
    <button class="workbench-notifications__trigger" type="button" :aria-expanded="visible" aria-label="工作区通知">
      <t-icon name="notification" size="16px" /><span>通知</span>
      <span v-if="unread" class="workbench-notifications__count">{{ unread > 99 ? '99+' : unread }}</span>
    </button>
    <template #content>
      <section class="workbench-notifications" aria-label="工作区通知">
        <header><strong>工作区通知</strong><button type="button" :disabled="loading" @click="load">刷新</button></header>
        <p v-if="error" role="alert">{{ error }}</p>
        <p v-else-if="loading && !items.length" role="status">正在读取通知…</p>
        <p v-else-if="!items.length">还没有通知。任务结果也可以在自动化运行记录中查看。</p>
        <ul v-else>
          <li v-for="item in items" :key="item.id">
            <button type="button" :class="{ 'is-unread': !item.read_at }" @click="openItem(item)">
              <strong>{{ item.title }}</strong>
              <span>{{ item.body }}</span>
            </button>
          </li>
        </ul>
      </section>
    </template>
  </t-popup>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { listNotifications, markNotificationsRead, type AppNotification } from '@/api/automation'
import { workbenchNotificationLocation } from '@/utils/workbenchPolicy'

const router = useRouter()
const visible = ref(false)
const loading = ref(false)
const error = ref('')
const items = ref<AppNotification[]>([])
const unread = ref(0)
let generation = 0
async function load() {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const response = await listNotifications(30)
    if (current !== generation) return
    items.value = response.data.items || []
    unread.value = response.data.unread || 0
  } catch {
    if (current === generation) error.value = '通知暂时无法读取，请刷新重试。'
  } finally {
    if (current === generation) loading.value = false
  }
}
async function openItem(item: AppNotification) {
  const target = workbenchNotificationLocation(item.link)
  if (!target) { error.value = '这条通知没有可打开的工作区记录。'; return }
  try {
    if (!item.read_at) await markNotificationsRead([item.id])
    visible.value = false
    await router.push(target)
  } catch { error.value = '暂时无法打开通知，请重试。' }
}
watch(visible, open => { if (open) void load() })
onBeforeUnmount(() => { ++generation })
</script>

<style scoped lang="less">
.workbench-notifications__trigger {
  display: flex; align-items: center; gap: 6px; min-height: 34px; padding: 6px 10px;
  border: 1px solid var(--td-component-stroke); border-radius: 8px;
  background: transparent; color: var(--td-text-color-secondary); cursor: pointer;
  &:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
}
.workbench-notifications__count { color: var(--td-brand-color); }
.workbench-notifications {
  width: min(360px, calc(100vw - 32px)); max-height: min(480px, calc(100vh - 100px));
  overflow-y: auto; padding: 14px; box-sizing: border-box; color: var(--td-text-color-primary);
  header { display: flex; justify-content: space-between; align-items: center; }
  header button { border: 0; background: transparent; color: var(--td-brand-color); cursor: pointer; }
  p { margin: 14px 0; color: var(--td-text-color-secondary); line-height: 1.7; }
  ul { padding: 0; margin: 12px 0 0; list-style: none; }
  li + li { border-top: 1px solid var(--td-component-stroke); }
  li button { display: flex; width: 100%; flex-direction: column; gap: 5px; padding: 10px 4px;
    border: 0; border-radius: 4px; background: transparent; color: var(--td-text-color-secondary); text-align: left; cursor: pointer; }
  li button:hover { background: var(--td-bg-color-container-hover); }
  li button.is-unread strong { color: var(--td-text-color-primary); }
  li span { line-height: 1.6; overflow-wrap: anywhere; }
}
</style>
