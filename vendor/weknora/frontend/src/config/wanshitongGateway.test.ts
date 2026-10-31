import assert from 'node:assert/strict'
import test from 'node:test'
import { safeWanshitongReturnTo, workbenchIdFromPath } from './wanshitongGateway'

test('管理员登录仅返回已知管理页面和知识库详情', () => {
  assert.equal(safeWanshitongReturnTo('/overview'), '/overview')
  assert.equal(safeWanshitongReturnTo('/public-settings?tab=answer'), '/public-settings?tab=answer')
  assert.equal(safeWanshitongReturnTo('/platform/knowledge-bases/kb-123'), '/platform/knowledge-bases/kb-123')
  assert.equal(safeWanshitongReturnTo('/platform/settings?section=models'), '/platform/settings?section=models')
})

test('工作区标识只接受独立入口内完整UUID，不从任意查询或管理路径复用', () => {
  const id = '11111111-1111-4111-8111-111111111111'
  assert.equal(workbenchIdFromPath(`/kb/workbench/${id}/`), id)
  for (const path of ['/kb/admin/', `/kb/admin/${id}`, `/kb/workbench/${id}/extra`, '/kb/workbench/../../engine-admin', '/kb/workbench/not-a-uuid/']) {
    assert.equal(workbenchIdFromPath(path), '')
  }
})

test('管理员登录不会回跳到外站、原生聊天或未注册管理页面', () => {
  for (const target of [
    'https://example.com', '//example.com', '/login', '/platform/creatChat',
    '/platform/chat/123', '/platform/knowledge-bases/kb-123/creatChat',
    '/platform/automations', '/platform/automations/task-123',
    '/kb/admin/ops/', null,
  ]) {
    assert.equal(safeWanshitongReturnTo(target), '/overview')
  }
})
