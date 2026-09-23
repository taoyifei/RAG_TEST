import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as authNavigation from "./authNavigation";
import { usePublicChat } from "./usePublicChat";

const encoder = new TextEncoder();

function event(
  type: string,
  sequence: number,
  fields: Record<string, unknown> = {},
) {
  return `event: ${type}\ndata: ${JSON.stringify({
    protocol: "wanshitong-public-sse-v1",
    type,
    trace_id: `trace_${String(sequence + 1).padStart(32, "1")}`,
    sequence,
    ...fields,
  })}\n\n`;
}

function streamResponse(body: string): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(body));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

function pendingResponse() {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(event("meta", 0)));
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function pathOf(input: RequestInfo | URL): string {
  const value =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
  return new URL(value, "http://localhost").pathname;
}

function installFetch(
  chat: (index: number) => Promise<Response>,
  usageContext = false,
) {
  let chatCount = 0;
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const path = pathOf(input);
    if (path === "/api/public/session") {
      return Promise.resolve(
        Response.json({
          session_id: "wstsid_11111111111111111111111111111111",
          csrf_token: "a".repeat(64),
          expires_in: 3600,
          deployment_id: "candidate_8289",
          user: { user_id: "1001", display_name: "测试用户" },
        }),
      );
    }
    if (path === "/api/public/capabilities") {
      return Promise.resolve(
        Response.json({
          mode: "wanshitong",
          stream: true,
          stream_protocol: "wanshitong-public-sse-v1",
          document_visibility: "all_internal",
          feedback: true,
          shortcuts: [],
          ...(usageContext ? { request_usage_context: true } : {}),
        }),
      );
    }
    if (path === "/api/public/chat") return chat(++chatCount);
    return Promise.resolve(new Response(null, { status: 404 }));
  });
}

function chatBodies(fetchMock: ReturnType<typeof installFetch>) {
  return fetchMock.mock.calls
    .filter(([input]) => pathOf(input) === "/api/public/chat")
    .map(
      ([, init]) =>
        JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
          conversation_id: string;
          query: string;
          client_context?: {
            entrypoint: string;
            recommendation_id?: string;
          };
        },
    );
}

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("公共问答新话题生命周期", () => {
  it("新话题仅清本页上下文，追问沿新会话且不删除 History 或重建登录", async () => {
    const fetchMock = installFetch(() =>
      Promise.resolve(
        streamResponse(event("final", 0, { answer: "已回答", citations: [] })),
      ),
    );
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    const user = result.current.user;
    const deploymentId = result.current.deploymentId;

    act(() => result.current.submit("问题 A"));
    await waitFor(() =>
      expect(result.current.turns[0]?.status).toBe("completed"),
    );
    const oldConversationId = result.current.turns[0].conversationId;
    act(() => result.current.startNewTopic());
    expect(result.current.turns).toEqual([]);
    expect(result.current.phase).toBe("ready");
    expect(result.current.sessionReady).toBe(true);
    expect(result.current.user).toEqual(user);
    expect(result.current.deploymentId).toBe(deploymentId);

    act(() => result.current.submit("问题 B"));
    await waitFor(() =>
      expect(result.current.turns[0]?.status).toBe("completed"),
    );
    const newConversationId = result.current.turns[0].conversationId;
    expect(newConversationId).not.toBe(oldConversationId);
    act(() => result.current.submit("那需要多久"));
    await waitFor(() => expect(result.current.turns).toHaveLength(2));
    expect(result.current.turns[1].conversationId).toBe(newConversationId);
    expect(chatBodies(fetchMock).map((body) => body.query)).toEqual([
      "问题 A",
      "问题 B",
      "那需要多久",
    ]);
    expect(chatBodies(fetchMock).map((body) => body.conversation_id)).toEqual([
      oldConversationId,
      newConversationId,
      newConversationId,
    ]);
    expect(fetchMock.mock.calls.map(([input]) => pathOf(input))).toEqual([
      "/api/public/session",
      "/api/public/capabilities",
      "/api/public/chat",
      "/api/public/chat",
      "/api/public/chat",
    ]);
  });

  it("推荐沿用会话并保留历史，只发送可见题面且失败重试冻结原问", async () => {
    const fetchMock = installFetch(
      (index) =>
        Promise.resolve(
          streamResponse(
            index === 2
              ? event("error", 0, { message: "暂时失败" })
              : event("final", 0, { answer: "已回答", citations: [] }),
          ),
        ),
      true,
    );
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    act(() => result.current.submit("问题 A"));
    await waitFor(() =>
      expect(result.current.turns[0]?.status).toBe("completed"),
    );
    const oldConversationId = result.current.turns[0].conversationId;

    act(() => result.current.submitRecommendation("推荐题面 B", "sq-test-01"));
    await waitFor(() => expect(result.current.turns[1]?.status).toBe("failed"));
    expect(result.current.turns[0].question).toBe("问题 A");
    const failedTurn = result.current.turns[1];
    expect(failedTurn.conversationId).toBe(oldConversationId);
    act(() =>
      result.current.retry(
        failedTurn.id,
        failedTurn.question,
        failedTurn.conversationId,
      ),
    );
    await waitFor(() =>
      expect(result.current.turns[1]?.status).toBe("completed"),
    );
    expect(result.current.turns).toHaveLength(2);
    expect(chatBodies(fetchMock).map((body) => body.query)).toEqual([
      "问题 A",
      "推荐题面 B",
      "推荐题面 B",
    ]);
    expect(chatBodies(fetchMock).map((body) => body.client_context)).toEqual([
      { entrypoint: "manual" },
      { entrypoint: "suggestion", recommendation_id: "sq-test-01" },
      { entrypoint: "retry" },
    ]);
    expect(chatBodies(fetchMock).map((body) => body.conversation_id)).toEqual([
      oldConversationId,
      oldConversationId,
      oldConversationId,
    ]);
  });

  it.each([
    [
      "迟到的 final",
      () =>
        streamResponse(event("final", 0, { answer: "旧答案", citations: [] })),
    ],
    ["迟到的 401", () => new Response(null, { status: 401 })],
  ])("停止后旧请求%s不污染新话题或新 busy", async (_label, oldResponse) => {
    const old = deferredResponse();
    const redirect = vi
      .spyOn(authNavigation, "redirectToSso")
      .mockImplementation(() => undefined);
    const fetchMock = installFetch((index) =>
      index === 1 ? old.promise : Promise.resolve(pendingResponse()),
    );
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    act(() => result.current.submit("旧问题"));
    expect(result.current.busy).toBe(true);
    act(() => result.current.stop());
    act(() => result.current.startNewTopic());
    act(() => result.current.submit("新问题"));
    await waitFor(() =>
      expect(result.current.turns[0]?.status).toBe("streaming"),
    );

    await act(async () => {
      old.resolve(oldResponse());
      await old.promise;
    });
    expect(result.current.turns).toHaveLength(1);
    expect(result.current.turns[0].question).toBe("新问题");
    expect(result.current.turns[0].status).toBe("streaming");
    expect(result.current.busy).toBe(true);
    expect(redirect).not.toHaveBeenCalled();
    expect(chatBodies(fetchMock)).toHaveLength(2);
    act(() => result.current.stop());
  });
});
