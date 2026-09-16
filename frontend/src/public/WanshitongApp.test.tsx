import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WanshitongApp } from "./WanshitongApp";

const encoder = new TextEncoder();

function event(
  type: string,
  sequence: number,
  fields: Record<string, unknown> = {},
): string {
  return `event: ${type}\ndata: ${JSON.stringify({
    protocol: "wanshitong-public-sse-v1",
    type,
    trace_id: "trace_11111111111111111111111111111111",
    sequence,
    ...fields,
  })}\n\n`;
}

function streamResponse(chunks: string[]): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function pendingStream(initial: string) {
  let cancelled = false;
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(initial));
      },
      cancel() {
        cancelled = true;
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
  return { response, wasCancelled: () => cancelled };
}

const sessionBody = {
  session_id: "wstsid_11111111111111111111111111111111",
  csrf_token: "a".repeat(64),
  expires_in: 3600,
};

const capabilitiesBody = {
  mode: "wanshitong",
  stream: true,
  stream_protocol: "wanshitong-public-sse-v1",
  document_visibility: "all_internal",
  feedback: true,
  shortcuts: [],
};

function pathOf(input: RequestInfo | URL): string {
  const value =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
  return new URL(value, "http://localhost").pathname;
}

function installFetch(chatResponse: Response) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input: RequestInfo | URL) => {
      const path = pathOf(input);
      if (path === "/api/public/session") {
        return Promise.resolve(Response.json(sessionBody));
      }
      if (path === "/api/public/capabilities") {
        return Promise.resolve(Response.json(capabilitiesBody));
      }
      if (path === "/api/public/chat") return Promise.resolve(chatResponse);
      if (path === "/api/public/feedback") {
        return Promise.resolve(Response.json({ useful: true }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
}

async function openHome() {
  render(<WanshitongApp />);
  await screen.findByRole("heading", { name: "你的内部知识助手" });
}

async function ask(question = "材料多久完成核验？") {
  const user = userEvent.setup();
  const textbox = screen.getByRole("textbox", { name: "向湾事通提问" });
  await user.type(textbox, question);
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  return user;
}

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

describe("湾事通公共应用", () => {
  it("初始化匿名 Session 后渲染无登录首屏", async () => {
    const fetchMock = installFetch(
      streamResponse([event("error", 0, { message: "未就绪" })]),
    );
    await openHome();

    expect(screen.getByText("湾事通")).toBeInTheDocument();
    expect(
      screen.queryByText("从已核验的内部资料中寻找答案，并把来源交代清楚。"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(
        "今天想了解什么？我会从内部资料中查找并核对来源",
      ),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => pathOf(input))).toEqual([
      "/api/public/session",
      "/api/public/capabilities",
    ]);
    expect(screen.queryByText("管理员登录")).not.toBeInTheDocument();
  });

  it("Session 初始化失败时提供可重试页面", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(Response.json(sessionBody))
      .mockResolvedValueOnce(Response.json(capabilitiesBody));
    const user = userEvent.setup();
    render(<WanshitongApp />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "暂时无法连接湾事通",
    );
    await user.click(screen.getByRole("button", { name: "重新连接" }));
    expect(
      await screen.findByRole("heading", { name: "你的内部知识助手" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("不显示部门筛选、管理能力或不可见快捷入口", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(
            Response.json({
              ...capabilitiesBody,
              shortcuts: [
                {
                  id: "hidden",
                  label: "制度政策",
                  prompt: "隐藏问题",
                  visible: false,
                },
              ],
            }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      },
    );
    await openHome();

    for (const text of [
      "制度政策",
      "部门",
      "工作空间",
      "文件上传",
      "Provider",
      "Trace",
    ]) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });

  it("首问后切换聊天模式，且请求只含 WB-03 允许字段", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("meta", 0),
        event("final", 1, { answer: "五个工作日内完成。", citations: [] }),
      ]),
    );
    await openHome();
    await ask();

    expect(await screen.findByText("五个工作日内完成。")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "新建会话" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "你的内部知识助手" }),
    ).not.toBeInTheDocument();
    const chatCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    const chatBody = chatCall?.[1]?.body;
    expect(typeof chatBody).toBe("string");
    const requestBody = JSON.parse(
      typeof chatBody === "string" ? chatBody : "{}",
    ) as Record<string, unknown>;
    expect(Object.keys(requestBody).sort()).toEqual([
      "conversation_id",
      "query",
    ]);
    expect(requestBody.query).toBe("材料多久完成核验？");
    expect(requestBody).not.toHaveProperty("departments");
    expect(requestBody).not.toHaveProperty("shortcut_id");
  });

  it("将内部 stage 映射为简洁中文状态", async () => {
    const pending = pendingStream(
      event("meta", 0) + event("stage", 1, { stage: "retrieval" }),
    );
    installFetch(pending.response);
    await openHome();
    const user = await ask();

    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "正在检索内部资料",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("retrieval")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "停止" }));
  });

  it("按 sequence 忽略乱序事件、按 claim_index 去重并以 final 收束", async () => {
    installFetch(
      streamResponse([
        event("meta", 0),
        event("claim", 2, {
          claim_index: 0,
          claim: { text: "临时已验证内容" },
          provisional: true,
        }),
        event("stage", 1, { stage: "snapshot" }),
        event("claim", 3, {
          claim_index: 0,
          claim: { text: "重复内容" },
          provisional: true,
        }),
        event("final", 4, { answer: "唯一权威答案", citations: [] }),
        event("error", 5, { message: "terminal 后错误" }),
      ]),
    );
    await openHome();
    await ask();

    expect(await screen.findByText("唯一权威答案")).toBeInTheDocument();
    expect(screen.queryByText("临时已验证内容")).not.toBeInTheDocument();
    expect(screen.queryByText("重复内容")).not.toBeInTheDocument();
    expect(screen.queryByText("terminal 后错误")).not.toBeInTheDocument();
  });

  it("无答案 final 优先展示服务端提示且不伪造来源", async () => {
    installFetch(
      streamResponse([
        event("final", 0, {
          status: "AMBIGUOUS_NEEDS_CLARIFICATION",
          answer: null,
          user_message: "请补充要查询的具体事项。",
          citations: [],
        }),
      ]),
    );
    await openHome();
    await ask();

    expect(
      await screen.findByText("请补充要查询的具体事项。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已核验来源（/)).not.toBeInTheDocument();
  });

  it("兼容 WB-03 未携带 user_message 的空答案 final", async () => {
    installFetch(
      streamResponse([
        event("final", 0, {
          status: "INDEX_NOT_READY",
          answer: null,
          citations: [],
        }),
      ]),
    );
    await openHome();
    await ask();

    expect(
      await screen.findByText("内部资料仍在准备中，请稍后再试。"),
    ).toBeInTheDocument();
  });

  it("已有 claim 后失败时保留并明确标记临时内容", async () => {
    installFetch(
      streamResponse([
        event("claim", 0, {
          claim_index: 0,
          claim: { text: "仅供临时查看的已验证片段" },
        }),
        event("error", 1, { message: "内部失败", partial: true }),
      ]),
    );
    await openHome();
    await ask();

    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "回答未完成，以下内容不能作为最终结论",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("仅供临时查看的已验证片段")).toBeInTheDocument();
    expect(screen.queryByText("有帮助")).not.toBeInTheDocument();
  });

  it("停止会取消流并恢复输入", async () => {
    const pending = pendingStream(event("stage", 0, { stage: "generation" }));
    installFetch(pending.response);
    await openHome();
    const user = await ask();
    await within(screen.getByLabelText("湾事通回答")).findByText(
      "正在组织回答",
    );

    await user.click(screen.getByRole("button", { name: "停止" }));
    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "已停止回答",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toBeEnabled();
    await waitFor(() => expect(pending.wasCancelled()).toBe(true));
  });

  it("已显示的对话保留且没有新建会话入口", async () => {
    installFetch(
      streamResponse([
        event("final", 0, { answer: "本轮答案", citations: [] }),
      ]),
    );
    await openHome();
    await ask("第一轮问题");
    await screen.findByText("本轮答案");

    expect(screen.getByText("第一轮问题")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建会话" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toBeEnabled();
  });

  it("来源卡片只展示允许的公共字段", async () => {
    installFetch(
      streamResponse([
        event("final", 0, {
          answer: "核验完成。",
          citations: [
            {
              document_name: "湾事通办事指南.docx",
              department: "政务服务部",
              category_path: ["办事服务", "材料办理"],
              locator: "申请指南 > 材料核验",
              quote: "五个工作日内完成核验。",
            },
          ],
        }),
      ]),
    );
    await openHome();
    const user = await ask();
    const summary = await screen.findByText("已核验来源（1）");
    await user.click(summary);

    expect(screen.getByText("湾事通办事指南.docx")).toBeInTheDocument();
    expect(
      screen.getByText("政务服务部 · 办事服务 / 材料办理"),
    ).toBeInTheDocument();
    expect(screen.getByText("申请指南 > 材料核验")).toBeInTheDocument();
    expect(
      screen.queryByText(/chunk_id|document_id|trace_id/),
    ).not.toBeInTheDocument();
  });

  it("final 后只允许用内部 trace_id 提交一次反馈", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "可反馈答案", citations: [] }),
      ]),
    );
    await openHome();
    const user = await ask();
    await screen.findByText("可反馈答案");

    await user.click(screen.getByRole("button", { name: "有帮助" }));
    expect(await screen.findByText("感谢你的反馈")).toBeInTheDocument();
    const feedbackCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/feedback",
    );
    const feedbackBody = feedbackCall?.[1]?.body;
    expect(typeof feedbackBody).toBe("string");
    expect(
      JSON.parse(typeof feedbackBody === "string" ? feedbackBody : "{}"),
    ).toEqual({
      trace_id: "trace_11111111111111111111111111111111",
      useful: true,
    });
    expect(
      screen.queryByRole("button", { name: "没帮助" }),
    ).not.toBeInTheDocument();
  });

  it("429 读取 Retry-After 后显示限流文案且不自动重试", async () => {
    const fetchMock = installFetch(
      new Response(
        JSON.stringify({
          error: { code: "RATE_LIMITED", message: "raw throttled" },
        }),
        { status: 429, headers: { "Retry-After": "60" } },
      ),
    );
    await openHome();
    await ask();

    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "当前使用人数较多，请稍后再试。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("raw throttled")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => pathOf(input) === "/api/public/chat",
      ),
    ).toHaveLength(1);
  });

  it("网络在 terminal 前断开时结束 loading 并允许重试", async () => {
    installFetch(streamResponse([]));
    await openHome();
    await ask();

    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "网络连接中断，请检查网络后重新尝试。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新尝试" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toBeEnabled();
  });

  it("Session 过期时只重建一次并重试原问题", async () => {
    let sessionCalls = 0;
    let chatCalls = 0;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          sessionCalls += 1;
          return Promise.resolve(
            Response.json({
              ...sessionBody,
              csrf_token: (sessionCalls === 1 ? "a" : "b").repeat(64),
            }),
          );
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          chatCalls += 1;
          if (chatCalls === 1) {
            return Promise.resolve(new Response(null, { status: 401 }));
          }
          return Promise.resolve(
            streamResponse([
              event("final", 0, { answer: "重试成功", citations: [] }),
            ]),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      });
    await openHome();
    await ask("会话过期测试");

    expect(await screen.findByText("重试成功")).toBeInTheDocument();
    expect(sessionCalls).toBe(2);
    expect(chatCalls).toBe(2);
    const chatHeaders = fetchMock.mock.calls
      .filter(([input]) => pathOf(input) === "/api/public/chat")
      .map(
        ([, init]) => (init?.headers as Record<string, string>)["X-CSRF-Token"],
      );
    expect(chatHeaders).toEqual(["a".repeat(64), "b".repeat(64)]);
  });

  it("pagehide 取消仍在进行的请求", async () => {
    const pending = pendingStream(event("stage", 0, { stage: "snapshot" }));
    installFetch(pending.response);
    await openHome();
    await ask();
    await within(screen.getByLabelText("湾事通回答")).findByText(
      "正在确认资料版本",
    );

    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await waitFor(() => expect(pending.wasCancelled()).toBe(true));
  });
});
