import { describe, expect, it, vi } from "vitest";

import {
  consumePublicSse,
  PublicSseParseError,
  PublicSseParser,
  publicStageLabel,
} from "./publicSse";

const encoder = new TextEncoder();

function body(...chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

describe("湾事通 SSE parser", () => {
  it("跨网络 chunk 组装一个完整事件", () => {
    const parser = new PublicSseParser();
    expect(parser.push('event: stage\ndata: {"type":"sta')).toEqual([]);
    expect(parser.push('ge","sequence":1,"stage":"retrieval"}\n\n')).toEqual([
      {
        event: "stage",
        data: '{"type":"stage","sequence":1,"stage":"retrieval"}',
      },
    ]);
  });

  it("从同一 chunk 解析多个事件，并兼容 CRLF、心跳和空 data", () => {
    const parser = new PublicSseParser();
    const frames = parser.push(
      ': heartbeat\r\n\r\nevent: meta\r\ndata: {"type":"meta","sequence":0}\r\n\r\n' +
        "event: stage\ndata:\n\n" +
        'event: stage\ndata: {"type":"stage","sequence":1,"stage":"snapshot"}\n\n',
    );
    expect(frames).toEqual([
      { event: "meta", data: '{"type":"meta","sequence":0}' },
      { event: "stage", data: "" },
      {
        event: "stage",
        data: '{"type":"stage","sequence":1,"stage":"snapshot"}',
      },
    ]);
  });

  it("拒绝损坏的 JSON，而不是静默无限等待", async () => {
    const controller = new AbortController();
    await expect(
      consumePublicSse(
        body("event: meta\ndata: {not-json}\n\n"),
        vi.fn(),
        controller.signal,
      ),
    ).rejects.toBeInstanceOf(PublicSseParseError);
  });

  it("终态后停止消费同一网络块中的晚到事件", async () => {
    const controller = new AbortController();
    const seen: string[] = [];
    await expect(
      consumePublicSse(
        body(
          'event: final\ndata: {"type":"final","sequence":1,"answer":"完成","citations":[]}\n\n' +
            "event: error\ndata: {not-json}\n\n",
        ),
        (event) => {
          seen.push(event.type);
          return event.type !== "final";
        },
        controller.signal,
      ),
    ).resolves.toBeUndefined();
    expect(seen).toEqual(["final"]);
  });

  it("收到真实流数据或心跳时通知界面连接仍有活动", async () => {
    const onActivity = vi.fn();
    const controller = new AbortController();
    await consumePublicSse(
      body(
        ": heartbeat 2000\n\n",
        'event: stage\ndata: {"type":"stage","sequence":0,"stage":"generation"}\n\n',
      ),
      vi.fn(),
      controller.signal,
      onActivity,
    );
    expect(onActivity).toHaveBeenCalledTimes(2);
  });

  it("只向用户展示中文阶段文案", () => {
    expect(publicStageLabel("accepted")).toBe("已收到问题");
    expect(publicStageLabel("snapshot")).toBe("正在确认资料版本");
    expect(publicStageLabel("retrieval")).toBe("正在检索内部资料");
    expect(publicStageLabel("generation")).toBe("正在组织回答");
    expect(publicStageLabel("validation")).toBe("正在核对回答与来源");
    expect(publicStageLabel("internal.future.stage")).toBe("正在处理问题");
  });

  it("自然协议在最终答案前逐段交付，未知协议与旧协议增量均拒绝", async () => {
    const controller = new AbortController();
    const seen: string[] = [];
    await consumePublicSse(
      body(
        'event: answer_delta\ndata: {"protocol":"wanshitong-natural-sse-v1","type":"answer_delta","sequence":1,"text":"第","provisional":true}\n\n' +
          'event: answer_delta\ndata: {"protocol":"wanshitong-natural-sse-v1","type":"answer_delta","sequence":2,"text":"一段","provisional":true}\n\n' +
          'event: final\ndata: {"protocol":"wanshitong-natural-sse-v1","type":"final","sequence":3,"status":"ANSWERED","published":true,"answer":"第一段","citation_status":"valid","citations":[{"document_name":"依据.docx"}]}\n\n',
      ),
      (event) => {
        seen.push(event.type === "answer_delta" ? event.text : event.type);
      },
      controller.signal,
    );
    expect(seen).toEqual(["第", "一段", "final"]);
    for (const protocol of ["wanshitong-public-sse-v1", "future-v2"]) {
      await expect(
        consumePublicSse(
          body(
            `event: answer_delta\ndata: {"protocol":"${protocol}","type":"answer_delta","sequence":1,"text":"假增量","provisional":true}\n\n`,
          ),
          vi.fn(),
          controller.signal,
        ),
      ).rejects.toBeInstanceOf(PublicSseParseError);
    }
  });

  it("WeKnora 流保留原生正文与引用顺序，并接受诊断事件", async () => {
    const controller = new AbortController();
    const seen: unknown[] = [];
    const protocol = "wanshitong-weknora-sse-v1";
    const citation = {
      document_name: "政策.md",
      reference_id: `ref_${"a".repeat(32)}`,
      native_chunk_id: "chunk-1",
      native_knowledge_id: "knowledge-1",
      quote: "第一段",
    };
    const frame = (type: string, sequence: number, fields: object) =>
      `event: ${type}\ndata: ${JSON.stringify({ protocol, type, sequence, ...fields })}\n\n`;
    await consumePublicSse(
      body(
        frame("meta", 0, { native_session_id: "session-1" }) +
          frame("answer_delta", 1, {
            text: "# 标题\n\n|甲|乙|\n|--|--|\n|1|2|",
            native_request_id: "request-1",
          }) +
          frame("references", 2, { items: [citation] }) +
          frame("native_event", 3, {
            response_type: "thinking",
            done: false,
            native: { response_type: "thinking", content: "原生过程" },
          }) +
          frame("final", 4, {
            answer: "# 标题\n\n|甲|乙|\n|--|--|\n|1|2|",
            citations: [citation],
            truncated: false,
            finish_reason: "stop",
            native_message_id: "message-1",
          }),
      ),
      (event) => {
        seen.push(event);
      },
      controller.signal,
    );
    expect(seen).toHaveLength(5);
    expect(seen[1]).toMatchObject({
      type: "answer_delta",
      native_request_id: "request-1",
    });
    expect(seen[2]).toMatchObject({ type: "references", items: [citation] });
    expect(seen[3]).toMatchObject({
      type: "native_event",
      response_type: "thinking",
    });
    expect(seen[4]).toMatchObject({
      type: "final",
      citations: [citation],
      finish_reason: "stop",
    });
  });

  it("WeKnora 流缺少原生终态字段时拒绝伪造 final", async () => {
    const controller = new AbortController();
    await expect(
      consumePublicSse(
        body(
          'event: final\ndata: {"protocol":"wanshitong-weknora-sse-v1","type":"final","sequence":1,"answer":"正文","citations":[],"published":true}\n\n',
        ),
        vi.fn(),
        controller.signal,
      ),
    ).rejects.toBeInstanceOf(PublicSseParseError);
  });
});
