export interface PublicCitation {
  document_name: string;
  reference_id?: string;
  source_available?: boolean;
  original_available?: boolean;
  native_chunk_id?: string | number | null;
  native_knowledge_id?: string | number | null;
  alias?: string;
  source_kind?: string;
  department?: string;
  department_name?: string;
  category_path?: string | string[];
  document_title?: string;
  source_relative_path?: string;
  locator?: string;
  quote?: string;
  native_reference?: Record<string, unknown>;
}

interface PublicEventBase {
  protocol?: string;
  type: string;
  trace_id?: string;
  sequence: number;
  native_request_id?: string | null;
  native_message_id?: string | null;
  native_session_id?: string | null;
}

export const WEKNORA_STREAM_PROTOCOL = "wanshitong-weknora-sse-v1" as const;

export interface PublicMetaEvent extends PublicEventBase {
  type: "meta";
}

export interface PublicStageEvent extends PublicEventBase {
  type: "stage";
  stage: string;
}

export interface PublicClaimEvent extends PublicEventBase {
  type: "claim";
  claim_index: number;
  claim: { text: string };
  provisional?: boolean;
}

export interface PublicFinalEvent extends PublicEventBase {
  type: "final";
  status?: string;
  reason_code?: string | null;
  answer: string | null;
  user_message?: string;
  citations: PublicCitation[];
  published?: boolean;
  citation_status?: "valid" | "missing" | "invalid";
  validation_level?: string;
  truncated?: boolean;
  finish_reason?: string | null;
}

export interface PublicAnswerDeltaEvent extends PublicEventBase {
  type: "answer_delta";
  text: string;
  provisional?: true;
  truncated?: boolean;
}

export interface PublicReferencesEvent extends PublicEventBase {
  type: "references";
  items: PublicCitation[];
}

export interface PublicErrorEvent extends PublicEventBase {
  type: "error";
  code?: string;
  message?: string;
  user_message?: string;
  partial?: boolean;
}

export interface PublicCancelledEvent extends PublicEventBase {
  type: "cancelled";
}

export interface PublicNativeEvent extends PublicEventBase {
  type: "native_event";
  response_type: string;
  done?: unknown;
  native: Record<string, unknown>;
}

export type PublicStreamEvent =
  | PublicMetaEvent
  | PublicStageEvent
  | PublicClaimEvent
  | PublicAnswerDeltaEvent
  | PublicReferencesEvent
  | PublicFinalEvent
  | PublicErrorEvent
  | PublicCancelledEvent
  | PublicNativeEvent;

export interface SseFrame {
  event: string;
  data: string;
}

export class PublicSseParseError extends Error {
  constructor(message = "接收回答时出现异常，请重新尝试。") {
    super(message);
    this.name = "PublicSseParseError";
  }
}

function fieldValue(line: string): string {
  const separator = line.indexOf(":");
  if (separator < 0) return "";
  const value = line.slice(separator + 1);
  return value.startsWith(" ") ? value.slice(1) : value;
}

function decodeFrame(frame: string): SseFrame | undefined {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    if (line === "event" || line.startsWith("event:")) {
      event = fieldValue(line);
    } else if (line === "data" || line.startsWith("data:")) {
      data.push(fieldValue(line));
    }
  }
  if (data.length === 0) return undefined;
  return { event, data: data.join("\n") };
}

export class PublicSseParser {
  private buffer = "";

  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    while (true) {
      const boundary = /\r?\n\r?\n/.exec(this.buffer);
      if (!boundary || boundary.index === undefined) break;
      const raw = this.buffer.slice(0, boundary.index);
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
      const frame = decodeFrame(raw);
      if (frame) frames.push(frame);
    }
    return frames;
  }

  finish(): SseFrame[] {
    const raw = this.buffer;
    this.buffer = "";
    if (!raw) return [];
    const frame = decodeFrame(raw);
    return frame ? [frame] : [];
  }
}

const EVENT_TYPES = new Set([
  "meta",
  "stage",
  "claim",
  "answer_delta",
  "references",
  "final",
  "error",
  "cancelled",
  "native_event",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCitation(value: unknown): value is PublicCitation {
  if (!isRecord(value) || typeof value.document_name !== "string") return false;
  if (
    value.reference_id !== undefined &&
    (typeof value.reference_id !== "string" ||
      !/^ref_[0-9a-f]{32}$/.test(value.reference_id))
  )
    return false;
  for (const key of ["native_chunk_id", "native_knowledge_id"]) {
    const id = value[key];
    if (
      id !== undefined &&
      id !== null &&
      typeof id !== "string" &&
      typeof id !== "number"
    ) {
      return false;
    }
  }
  if (
    value.source_available !== undefined &&
    typeof value.source_available !== "boolean"
  ) {
    return false;
  }
  if (value.department !== undefined && typeof value.department !== "string") {
    return false;
  }
  if (
    value.department_name !== undefined &&
    typeof value.department_name !== "string"
  ) {
    return false;
  }
  if (
    value.document_title !== undefined &&
    typeof value.document_title !== "string"
  ) {
    return false;
  }
  if (
    value.source_relative_path !== undefined &&
    typeof value.source_relative_path !== "string"
  ) {
    return false;
  }
  if (value.locator !== undefined && typeof value.locator !== "string") {
    return false;
  }
  if (value.quote !== undefined && typeof value.quote !== "string")
    return false;
  const category = value.category_path;
  return (
    category === undefined ||
    typeof category === "string" ||
    (Array.isArray(category) &&
      category.every((item) => typeof item === "string"))
  );
}

function decodePublicEvent(frame: SseFrame): PublicStreamEvent | undefined {
  if (frame.data.trim() === "") return undefined;
  let value: unknown;
  try {
    value = JSON.parse(frame.data);
  } catch {
    throw new PublicSseParseError();
  }
  if (!EVENT_TYPES.has(frame.event) || !isRecord(value)) {
    throw new PublicSseParseError();
  }
  const type = typeof value.type === "string" ? value.type : frame.event;
  const natural = value.protocol === "wanshitong-natural-sse-v1";
  const weknora = value.protocol === WEKNORA_STREAM_PROTOCOL;
  if (
    value.protocol !== undefined &&
    value.protocol !== "wanshitong-public-sse-v1" &&
    !natural &&
    !weknora
  ) {
    throw new PublicSseParseError();
  }
  if ((natural || weknora) && type === "claim") throw new PublicSseParseError();
  if (weknora && type === "stage") throw new PublicSseParseError();
  if (
    (type === "answer_delta" || type === "references") &&
    !natural &&
    !weknora
  ) {
    throw new PublicSseParseError();
  }
  if (type === "native_event" && !weknora) throw new PublicSseParseError();
  const sequence = value.sequence;
  if (
    type !== frame.event ||
    !Number.isSafeInteger(sequence) ||
    (sequence as number) < 0
  ) {
    throw new PublicSseParseError();
  }
  if (type === "stage" && typeof value.stage !== "string") {
    throw new PublicSseParseError();
  }
  if (
    type === "claim" &&
    (!Number.isSafeInteger(value.claim_index) ||
      !isRecord(value.claim) ||
      typeof value.claim.text !== "string")
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "answer_delta" &&
    (typeof value.text !== "string" ||
      (!weknora && value.provisional !== true) ||
      (weknora &&
        value.truncated !== undefined &&
        typeof value.truncated !== "boolean"))
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "native_event" &&
    (typeof value.response_type !== "string" || !isRecord(value.native))
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "references" &&
    (!Array.isArray(value.items) || !value.items.every(isCitation))
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "final" &&
    ((value.answer !== null && typeof value.answer !== "string") ||
      !Array.isArray(value.citations) ||
      !value.citations.every(isCitation) ||
      (value.user_message !== undefined &&
        typeof value.user_message !== "string"))
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "final" &&
    natural &&
    (typeof value.published !== "boolean" ||
      !["valid", "missing", "invalid"].includes(String(value.citation_status)))
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "final" &&
    natural &&
    (value.published
      ? value.citation_status !== "valid" ||
        typeof value.answer !== "string" ||
        (value.citations as unknown[]).length === 0
      : value.answer !== null || (value.citations as unknown[]).length !== 0)
  ) {
    throw new PublicSseParseError();
  }
  if (
    type === "final" &&
    weknora &&
    (typeof value.answer !== "string" ||
      typeof value.truncated !== "boolean" ||
      (value.finish_reason !== undefined &&
        value.finish_reason !== null &&
        typeof value.finish_reason !== "string"))
  ) {
    throw new PublicSseParseError();
  }
  if (weknora) {
    for (const key of [
      "native_request_id",
      "native_message_id",
      "native_session_id",
    ]) {
      const id = value[key];
      if (id !== undefined && id !== null && typeof id !== "string") {
        throw new PublicSseParseError();
      }
    }
  }
  return value as unknown as PublicStreamEvent;
}

export async function consumePublicSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: PublicStreamEvent) => boolean | void,
  signal: AbortSignal,
  onActivity?: () => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new PublicSseParser();
  const deliver = (frames: SseFrame[]): boolean => {
    for (const frame of frames) {
      const event = decodePublicEvent(frame);
      if (event && onEvent(event) === false) return false;
    }
    return true;
  };
  const abort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal.addEventListener("abort", abort, { once: true });
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      if (value.byteLength > 0) onActivity?.();
      if (!deliver(parser.push(decoder.decode(value, { stream: true })))) {
        await reader.cancel().catch(() => undefined);
        return;
      }
    }
    if (!deliver(parser.push(decoder.decode()))) return;
    deliver(parser.finish());
  } finally {
    signal.removeEventListener("abort", abort);
    reader.releaseLock();
  }
}

export const PUBLIC_STAGE_LABELS: Readonly<Record<string, string>> = {
  accepted: "已收到问题",
  snapshot: "正在确认资料版本",
  retrieval: "正在检索内部资料",
  generation: "正在组织回答",
  validation: "正在核对回答与来源",
};

export function publicStageLabel(stage: string): string {
  return PUBLIC_STAGE_LABELS[stage] ?? "正在处理问题";
}
