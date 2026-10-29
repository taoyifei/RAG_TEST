import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { baseParse, NodeTypes, type ElementNode, type TemplateChildNode } from '@vue/compiler-dom'
import { parse } from '@vue/compiler-sfc'

function returnLink(): ElementNode {
  const source = readFileSync(new URL('./WanshitongAdminSidebar.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  assert(descriptor.template)
  const nodes: TemplateChildNode[] = [...baseParse(descriptor.template.content).children]
  while (nodes.length) {
    const node = nodes.shift()!
    if (node.type !== NodeTypes.ELEMENT) continue
    if (node.tag === 'a' && attribute(node, 'aria-label') === '返回对话') return node
    nodes.push(...node.children)
  }
  throw new Error('管理导航缺少返回对话入口')
}

function attribute(node: ElementNode, name: string): string | undefined {
  const property = node.props.find(item => item.type === NodeTypes.ATTRIBUTE && item.name === name)
  return property?.type === NodeTypes.ATTRIBUTE ? property.value?.content : undefined
}

test('管理侧栏返回当前同源对话，嵌入时导航到顶层页面', () => {
  const link = returnLink()
  assert.equal(attribute(link, 'href'), '/kb/')
  assert.equal(attribute(link, 'target'), '_top')
  assert(link.children.some(child =>
    child.type === NodeTypes.ELEMENT && child.tag === 'span' &&
    child.children.some(text => text.type === NodeTypes.TEXT && text.content.trim() === '返回对话'),
  ))
})
