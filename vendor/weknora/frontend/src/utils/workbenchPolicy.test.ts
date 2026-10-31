import assert from 'node:assert/strict'
import test from 'node:test'
import { workbenchAutomationInput, workbenchNotificationLocation } from './workbenchPolicy.ts'

test('手工自动化保留用户任务与计划，但强制只读、每次独立且不继承结果', () => {
  const original = { name: '每周制度整理', instruction: '整理当前范围的新制度并注明来源',
    trigger: { type: 'cron' as const, expr: '0 9 * * 1', timezone: 'Asia/Shanghai' },
    model_id: 'other-model', access_level: 'auto_write' as const, session_mode: 'persistent' as const,
    carry_last_result: true, notify_on_success: true }
  const result = workbenchAutomationInput(original)
  assert.equal(result.name, original.name)
  assert.equal(result.instruction, original.instruction)
  assert.deepEqual(result.trigger, original.trigger)
  assert.equal(result.notify_on_success, true)
  assert.equal(result.access_level, 'read_only')
  assert.equal(result.session_mode, 'per_run')
  assert.equal(result.carry_last_result, false)
  assert.equal(result.model_id, '')
  assert.equal(original.access_level, 'auto_write')
})

test('工作区通知仅跳转到已有任务或本人的会话记录', () => {
  assert.equal(workbenchNotificationLocation('/platform/automations/task-a?run=run-a'), '/platform/automations/task-a?run=run-a')
  assert.equal(workbenchNotificationLocation('/platform/chat/session-a'), '/platform/chat/session-a')
  for (const link of ['/platform/settings', '//example.com', '/kb/admin/', '/platform/organizations/org-a']) {
    assert.equal(workbenchNotificationLocation(link), '')
  }
})
