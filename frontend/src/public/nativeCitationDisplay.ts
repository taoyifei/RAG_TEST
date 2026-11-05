import type { PublicCitation } from "./publicSse";

const KB_TAG = /<kb\b([^>]*?)\s*\/?>/gi;
const ATTRIBUTE = /([\w-]+)\s*=\s*"([^"]*)"/g;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const INLINE_CODE = /(`+)([\s\S]*?)\1/g;
const RESOURCE_URL = /(?:resource|https?):\/\/[^\s<>"'\\)\]]+/g;
const SOURCE_IMAGE =
  /!\[[^\]\n]*\]\(\s*<?((?:resource|https?):\/\/[^\s<>)]*)>?(?:\s+["'][^"']*["'])?\s*\)/g;

function matchesChunk(citation: PublicCitation, chunk: string): boolean {
  const children = citation.native_reference?.sub_chunk_id;
  return (
    (citation.native_chunk_id != null &&
      String(citation.native_chunk_id) === chunk) ||
    (Array.isArray(children) && children.includes(chunk))
  );
}

/** 实际引用必须指向本轮已返回的片段；代码中的引用示例不计为依据。 */
export function referencedCitations(
  content: string,
  citations: PublicCitation[],
): PublicCitation[] {
  const used = new Set<PublicCitation>();
  let fence = "";
  for (const line of content.split("\n")) {
    const marker = FENCE.exec(line)?.[1];
    if (marker) {
      if (!fence) fence = marker;
      else if (marker[0] === fence[0] && marker.length >= fence.length)
        fence = "";
      continue;
    }
    if (fence) continue;
    const text = line.replace(INLINE_CODE, "");
    const images = new Set(
      [...text.matchAll(SOURCE_IMAGE)].map((image) => image[1]),
    );
    if (images.size > 0) {
      for (const citation of citations) {
        const sources =
          (citation.quote ?? "") +
          "\n" +
          JSON.stringify(citation.native_reference?.image_info ?? null);
        if (
          [...sources.matchAll(RESOURCE_URL)].some((match) =>
            images.has(match[0]),
          )
        )
          used.add(citation);
      }
    }
    for (const tag of text.matchAll(KB_TAG)) {
      const attributes = Object.fromEntries(
        [...tag[1].matchAll(ATTRIBUTE)].map((attribute) => [
          attribute[1],
          attribute[2],
        ]),
      );
      const chunk = attributes.chunk_id ?? attributes.chunkId;
      const citation = citations.find(
        (item) =>
          chunk &&
          matchesChunk(item, chunk) &&
          (!attributes.kb_id ||
            attributes.kb_id === item.native_reference?.knowledge_base_id),
      );
      if (citation) used.add(citation);
    }
  }
  return citations.filter((citation) => used.has(citation));
}

function displayMarker(tag: string, citations: PublicCitation[]): string {
  const attributes: Record<string, string> = {};
  ATTRIBUTE.lastIndex = 0;
  for (const match of tag.matchAll(ATTRIBUTE)) attributes[match[1]] = match[2];
  const chunkId = attributes.chunk_id ?? attributes.chunkId;
  const documentName = attributes.doc ?? "";
  const citationIndex = citations.findIndex(
    (citation) =>
      (chunkId && matchesChunk(citation, chunkId)) ||
      (!chunkId && citation.document_name === documentName),
  );
  return citationIndex >= 0
    ? `〔${citationIndex + 1}〕`
    : `〔引用：${documentName || "来源"}〕`;
}

function replaceOutsideInlineCode(
  line: string,
  citations: PublicCitation[],
): string {
  let result = "";
  let start = 0;
  INLINE_CODE.lastIndex = 0;
  for (const code of line.matchAll(INLINE_CODE)) {
    const index = code.index ?? 0;
    result += line
      .slice(start, index)
      .replace(KB_TAG, (tag) => displayMarker(tag, citations));
    result += code[0];
    start = index + code[0].length;
  }
  return (
    result +
    line.slice(start).replace(KB_TAG, (tag) => displayMarker(tag, citations))
  );
}

/** 仅变换可见 Markdown；原生回答正文和来源对象仍按原样保存。 */
export function nativeCitationDisplay(
  content: string,
  citations: PublicCitation[],
): string {
  let fenceCharacter = "";
  let fenceLength = 0;
  return content
    .split("\n")
    .map((line) => {
      const marker = FENCE.exec(line)?.[1];
      if (marker) {
        if (!fenceCharacter) {
          fenceCharacter = marker[0];
          fenceLength = marker.length;
        } else if (
          marker[0] === fenceCharacter &&
          marker.length >= fenceLength
        ) {
          fenceCharacter = "";
          fenceLength = 0;
        }
        return line;
      }
      return fenceCharacter ? line : replaceOutsideInlineCode(line, citations);
    })
    .join("\n");
}
