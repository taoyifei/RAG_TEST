import assert from 'node:assert/strict'
import test from 'node:test'
import * as vue from 'vue'
import { fixture, flush } from '../test-utils/vueFixture.ts'
import { workbenchNotificationLocation } from '../utils/workbenchPolicy.ts'

test('通知按需读取，点击标记本人通知并回到工作区原生任务页', async t => {
  const calls: unknown[] = []
  const row = { id: 'notice-a', title: '资料整理完成', body: '可查看结果', link: '/platform/automations/task-a?run=run-a', read_at: null }
  const { vm } = fixture(t).component(new URL('./WorkbenchNotifications.vue', import.meta.url), {
    'vue-router': { useRouter: () => ({ push: async (path: string) => { calls.push(path) } }) },
    '@/api/automation': {
      listNotifications: async (limit: number) => { calls.push(limit); return { data: {items:[row], unread:1} } },
      markNotificationsRead: async (ids: string[]) => { calls.push([...ids]) },
    },
    '@/utils/workbenchPolicy': { workbenchNotificationLocation },
  })
  assert.equal(calls.length, 0)
  vm.visible = true
  await vue.nextTick()
  await flush()
  assert.equal(vm.items[0].title, row.title)
  assert.equal(vm.unread, 1)
  await vm.openItem(vm.items[0])
  assert.deepEqual(calls, [30, ['notice-a'], row.link])
  assert.equal(vm.visible, false)
})

test('读取失败保留重试，管理页面通知不能越过工作区路由', async t => {
  let failed = true
  const { vm } = fixture(t).component(new URL('./WorkbenchNotifications.vue', import.meta.url), {
    'vue-router': { useRouter: () => ({ push: assert.fail }) },
    '@/api/automation': {
      listNotifications: async () => { if (failed) throw new Error('unavailable'); return {data:{items:[],unread:0}} },
      markNotificationsRead: assert.fail,
    },
    '@/utils/workbenchPolicy': { workbenchNotificationLocation },
  })
  await vm.load()
  assert.match(vm.error, /刷新重试/)
  failed = false
  await vm.load()
  assert.equal(vm.error, '')
  await vm.openItem({ id:'other', link:'/platform/settings' })
  assert.match(vm.error, /没有可打开的工作区记录/)
})
