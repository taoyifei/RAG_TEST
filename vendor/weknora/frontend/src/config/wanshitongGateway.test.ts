import assert from 'node:assert/strict'
import test from 'node:test'
import { safeWanshitongReturnTo } from './wanshitongGateway'

test('管理员登录仅返回已知管理页面和知识库详情', () => {
  assert.equal(safeWanshitongReturnTo('/overview'), '/overview')
  assert.equal(safeWanshitongReturnTo('/public-settings?tab=answer'), '/public-settings?tab=answer')
  assert.equal(safeWanshitongReturnTo('/platform/knowledge-bases/kb-123'), '/platform/knowledge-bases/kb-123')
  assert.equal(safeWanshitongReturnTo('/platform/settings?section=models'), '/platform/settings?section=models')
})

test('管理员登录不会回跳到外站、原生聊天或未注册管理页面', () => {
  for (const target of [
    'https://example.com', '//example.com', '/login', '/platform/creatChat',
    '/platform/chat/123', '/platform/knowledge-bases/kb-123/creatChat',
    '/kb/admin/ops/', null,
  ]) {
    assert.equal(safeWanshitongReturnTo(target), '/overview')
  }
})
