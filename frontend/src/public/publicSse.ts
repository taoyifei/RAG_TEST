export interface PublicCitation {
  document_name: string;
  department?: string;
  department_name?: string;
  category_path?: string | string[];
  document_title?: string;
  source_relative_path?: string;
  locator?: string;
  quote?: string;
}

interface PublicEventBase {
  protocol?: string;
  type: string;
  trace_id?: string;
  sequence: number;
}

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

export type PublicStreamEvent =
  | PublicMetaEvent
  | PublicStageEvent
  | PublicClaimEvent
  | PublicFinalEvent
  | PublicErrorEvent
  | PublicCancelledEvent;

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
  "final",
  "error",
  "cancelled",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCitation(value: unknown): value is PublicCitation {
  if (!isRecord(value) || typeof value.document_name !== "string") return false;
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
    type === "final" &&
    ((value.answer !== null && typeof value.answer !== "string") ||
      !Array.isArray(value.citations) ||
      !value.citations.every(isCitation) ||
      (value.user_message !== undefined &&
        typeof value.user_message !== "string"))
  ) {
    throw new PublicSseParseError();
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
