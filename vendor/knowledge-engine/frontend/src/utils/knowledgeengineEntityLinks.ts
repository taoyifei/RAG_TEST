export type KnowledgeEngineEntityKind = 'kb' | 'doc' | 'chunk'

export interface KnowledgeEngineEntityRef {
  kind: KnowledgeEngineEntityKind
  id: string
}

export type KnowledgeEngineEntityLinkLabels = Record<KnowledgeEngineEntityKind, string>

export const KNOWLEDGE_ENGINE_ENTITY_LINK_CLASS = 'knowledgeengine-entity-link'

const URI_BODY = '(kb|doc|chunk)/([0-9A-Za-z][0-9A-Za-z_-]*)'
const URI_EXACT_RE = new RegExp(`^knowledgeengine://${URI_BODY}$`, 'i')
const URI_IN_TEXT_RE = new RegExp(`knowledgeengine://${URI_BODY}`, 'gi')
const HREF_RE = /\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')/i
const SKIPPED_TAGS = new Set(['code', 'pre', 'a', 'button', 'svg', 'math'])

/** Parses a `knowledgeengine://kb|doc|chunk/<id>` durable URI emitted by the model-context codec. */
export function parseKnowledgeEngineEntityURI(value: string | null | undefined): KnowledgeEngineEntityRef | null {
  const match = URI_EXACT_RE.exec((value || '').trim())
  if (!match) return null
  return { kind: match[1].toLowerCase() as KnowledgeEngineEntityKind, id: match[2] }
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function entityOpenTag(ref: KnowledgeEngineEntityRef, labels: KnowledgeEngineEntityLinkLabels): string {
  return `<span class="${KNOWLEDGE_ENGINE_ENTITY_LINK_CLASS}" role="link" tabindex="0" ` +
    `title="${escapeHtml(labels[ref.kind])}" data-entity-kind="${ref.kind}" data-entity-id="${ref.id}">`
}

/**
 * Turns `knowledgeengine://` URIs in rendered chat HTML into clickable entity chips.
 * Markdown links keep their text; bare URIs in prose show the real ID, since
 * the model writes them where an ID is meant. The kind label is the tooltip.
 * Text inside code, pre and ordinary links is left alone. Must run before
 * DOMPurify, whose URI allowlist would otherwise strip the `knowledgeengine:` scheme.
 */
export function renderKnowledgeEngineEntityLinks(html: string, labels: KnowledgeEngineEntityLinkLabels): string {
  if (!html || !/knowledgeengine:\/\//i.test(html)) return html
  const parts = html.split(/(<[^>]*>)/)
  const skipDepth = new Map<string, number>()
  let skipping = 0
  let entityAnchorOpen = false

  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]
    if (!part) continue
    if (part.startsWith('<')) {
      const tag = /^<\s*(\/?)\s*([a-zA-Z][\w-]*)/.exec(part)
      if (!tag) continue
      const closing = tag[1] === '/'
      const name = tag[2].toLowerCase()
      if (name === 'a' && closing && entityAnchorOpen) {
        parts[i] = '</span>'
        entityAnchorOpen = false
        continue
      }
      if (name === 'a' && !closing && skipping === 0) {
        const hrefMatch = HREF_RE.exec(part)
        const ref = parseKnowledgeEngineEntityURI(hrefMatch?.[1] ?? hrefMatch?.[2])
        if (ref) {
          parts[i] = entityOpenTag(ref, labels)
          entityAnchorOpen = true
          continue
        }
      }
      if (!SKIPPED_TAGS.has(name) || part.endsWith('/>')) continue
      const depth = skipDepth.get(name) || 0
      if (closing) {
        if (depth > 0) {
          skipDepth.set(name, depth - 1)
          skipping--
        }
      } else {
        skipDepth.set(name, depth + 1)
        skipping++
      }
      continue
    }
    if (skipping > 0 || entityAnchorOpen) continue
    parts[i] = part.replace(URI_IN_TEXT_RE, (_m, kind: string, id: string) => {
      const ref = { kind: kind.toLowerCase() as KnowledgeEngineEntityKind, id }
      return `${entityOpenTag(ref, labels)}${id}</span>`
    })
  }
  return parts.join('')
}
