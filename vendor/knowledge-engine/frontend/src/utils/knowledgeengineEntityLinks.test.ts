import assert from 'node:assert/strict'
import test from 'node:test'

import { marked } from 'marked'

import { renderChatMarkdown } from './chatMarkdownRenderer.ts'
import { parseKnowledgeEngineEntityURI, renderKnowledgeEngineEntityLinks } from './knowledgeengineEntityLinks.ts'

const LABELS = { kb: '知识库', doc: '文档', chunk: '片段' }
const KB_ID = 'kb-0001'
const DOC_ID = '00000001-0000-4000-8000-000000000001'

test('parseKnowledgeEngineEntityURI accepts only whole durable URIs', () => {
  assert.deepEqual(parseKnowledgeEngineEntityURI(`knowledgeengine://kb/${KB_ID}`), { kind: 'kb', id: KB_ID })
  assert.deepEqual(parseKnowledgeEngineEntityURI(`KnowledgeEngine://DOC/${DOC_ID}`), { kind: 'doc', id: DOC_ID })
  assert.equal(parseKnowledgeEngineEntityURI('knowledgeengine://kb/'), null)
  assert.equal(parseKnowledgeEngineEntityURI('knowledgeengine://user/1'), null)
  assert.equal(parseKnowledgeEngineEntityURI(`see knowledgeengine://kb/${KB_ID}`), null)
  assert.equal(parseKnowledgeEngineEntityURI('javascript:alert(1)'), null)
})

test('markdown links to knowledgeengine URIs keep their text and become entity links', () => {
  const html = renderKnowledgeEngineEntityLinks(
    `<p>存入 <a href="knowledgeengine://kb/${KB_ID}">每日热榜</a> 即可</p>`,
    LABELS,
  )
  assert.equal(
    html,
    '<p>存入 <span class="knowledgeengine-entity-link" role="link" tabindex="0" title="知识库" ' +
      `data-entity-kind="kb" data-entity-id="${KB_ID}">每日热榜</span> 即可</p>`,
  )
})

test('bare knowledgeengine URIs in prose show the real ID with the kind as tooltip', () => {
  const html = renderKnowledgeEngineEntityLinks(`<p>参考 knowledgeengine://doc/${DOC_ID}。</p>`, LABELS)
  assert.match(html, new RegExp(`title="文档" data-entity-kind="doc" data-entity-id="${DOC_ID}">${DOC_ID}</span>。`))
  assert.doesNotMatch(html, /knowledgeengine:\/\//)
})

test('knowledgeengine URIs inside code, pre and ordinary links are left alone', () => {
  const input =
    `<p><code>knowledgeengine://kb/${KB_ID}</code></p>` +
    `<pre><code>knowledgeengine://kb/${KB_ID}</code></pre>` +
    `<p><a href="https://example.com">knowledgeengine://kb/${KB_ID}</a></p>`
  assert.equal(renderKnowledgeEngineEntityLinks(input, LABELS), input)
})

test('renderChatMarkdown renders knowledgeengine links only when labels are supplied', () => {
  const markdown = `把结果写入 [热榜知识库](knowledgeengine://kb/${KB_ID})，并参考 knowledgeengine://doc/${DOC_ID}`
  const options = {
    renderer: new marked.Renderer(),
    escapeMarkdown: (text: string) => text,
    sanitizeHtml: (html: string) => html,
  }
  const plain = renderChatMarkdown(markdown, options)
  assert.doesNotMatch(plain, /knowledgeengine-entity-link/)

  const linked = renderChatMarkdown(markdown, { ...options, entityLinkLabels: LABELS })
  assert.match(linked, new RegExp(`data-entity-kind="kb" data-entity-id="${KB_ID}">热榜知识库</span>`))
  assert.match(linked, new RegExp(`data-entity-kind="doc" data-entity-id="${DOC_ID}">${DOC_ID}</span>`))
  assert.doesNotMatch(linked, /href="knowledgeengine:/)
})
