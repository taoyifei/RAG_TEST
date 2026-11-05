import type { PublicCitation } from "./publicSse";

const KB_TAG = /<kb\b([^>]*?)\s*\/?>/gi;
const ATTRIBUTE = /([\w-]+)\s*=\s*"([^"]*)"/g;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const INLINE_CODE = /(`+)([\s\S]*?)\1/g;

export const MODEL_GENERATED_NOTICE =
  "未引用知识库资料，以下内容由大模型生成，仅供参考。";
const MODEL_GENERATED_SOURCE = "来源：模型生成";
const KNOWLEDGE_SOURCE = "来源：知识库";

function sourceFooter(content: string): string {
  const answer = content.trimEnd();
  return answer.slice(answer.lastIndexOf("\n") + 1).trim();
}

export function hasModelNotice(content: string): boolean {
  return (
    content.trimStart().startsWith(MODEL_GENERATED_NOTICE) ||
    sourceFooter(content) === MODEL_GENERATED_SOURCE
  );
}

function displayMarker(tag: string, citations: PublicCitation[]): string {
  const attributes: Record<string, string> = {};
  ATTRIBUTE.lastIndex = 0;
  for (const match of tag.matchAll(ATTRIBUTE)) attributes[match[1]] = match[2];
  const chunkId = attributes.chunk_id ?? attributes.chunkId;
  const documentName = attributes.doc ?? "";
  const citationIndex = citations.findIndex(
    (citation) =>
      (chunkId && String(citation.native_chunk_id) === chunkId) ||
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
  if (content.trimStart().startsWith(MODEL_GENERATED_NOTICE)) {
    content = content
      .trimStart()
      .slice(MODEL_GENERATED_NOTICE.length)
      .trimStart();
  }
  const footer = sourceFooter(content);
  if (footer === MODEL_GENERATED_SOURCE || footer === KNOWLEDGE_SOURCE) {
    const answer = content.trimEnd();
    content = answer.slice(0, answer.lastIndexOf("\n") + 1).trimEnd();
  }
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
