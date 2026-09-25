import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WanshitongApp } from "./WanshitongApp";
import * as authNavigation from "./authNavigation";
import type { PublicPopularQuestions } from "./publicApi";

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
  feedback_details: true,
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

function installFetch(
  chatResponse: Response | (() => Response),
  popular: PublicPopularQuestions = {
    mode: "EMPTY",
    generated_at: null,
    window_days: 7,
    items: [],
  },
  capabilities: typeof capabilitiesBody & {
    request_usage_context?: boolean;
  } = capabilitiesBody,
) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input: RequestInfo | URL) => {
      const path = pathOf(input);
      if (path === "/api/public/session") {
        return Promise.resolve(Response.json(sessionBody));
      }
      if (path === "/api/public/capabilities") {
        return Promise.resolve(Response.json(capabilities));
      }
      if (path === "/api/public/popular-questions") {
        return Promise.resolve(Response.json(popular));
      }
      if (path === "/api/public/chat") {
        return Promise.resolve(
          typeof chatResponse === "function" ? chatResponse() : chatResponse,
        );
      }
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
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("湾事通公共应用", () => {
  it("外观切换始终位于公共页面右上角并跨首问保持", async () => {
    installFetch(
      streamResponse([
        event("final", 0, { answer: "已完成回答。", citations: [] }),
      ]),
    );
    const user = userEvent.setup();
    await openHome();
    const root = document.querySelector(".wst-root");
    expect(root).toHaveAttribute("data-theme", "dark");
    await user.click(screen.getByRole("button", { name: "切换到浅色模式" }));
    expect(root).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem("wanshitong-theme")).toBe("light");

    await ask();
    await screen.findByText("已完成回答。");
    expect(root).toHaveAttribute("data-theme", "light");
    await user.click(screen.getByRole("button", { name: "切换到深色模式" }));
    expect(root).toHaveAttribute("data-theme", "dark");
  });

  it("重新打开页面后沿用保存的浅色外观", async () => {
    window.localStorage.setItem("wanshitong-theme", "light");
    installFetch(streamResponse([]));
    await openHome();
    expect(document.querySelector(".wst-root")).toHaveAttribute(
      "data-theme",
      "light",
    );
    expect(
      screen.getByRole("button", { name: "切换到深色模式" }),
    ).toBeVisible();
  });

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
        "今天想了解什么？",
      ),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([input]) => pathOf(input))).toEqual([
        "/api/public/session",
        "/api/public/capabilities",
        "/api/public/popular-questions",
      ]),
    );
    expect(screen.queryByText("管理员登录")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "大家常问" })).toBeDisabled();
    expect(screen.getByText(/当前展示示例问题/)).toBeVisible();
  });

  it("大家常问只提交审核后的可见题面和 popular 入口", async () => {
    const question = "办理材料如何提交？";
    const recommendationId = `pq_${"a".repeat(32)}`;
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "已回答公开问题。", citations: [] }),
      ]),
      {
        mode: "POPULAR",
        generated_at: "2026-09-23T00:00:00Z",
        window_days: 7,
        items: [{ id: recommendationId, question, topic_key: "办理" }],
      },
      { ...capabilitiesBody, request_usage_context: true },
    );
    const user = userEvent.setup();
    await openHome();
    const tab = await screen.findByRole("tab", { name: "大家常问" });
    await waitFor(() => expect(tab).toBeEnabled());
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => pathOf(input) === "/api/public/chat",
      ),
    ).toHaveLength(0);
    await user.click(tab);
    await user.click(screen.getByRole("button", { name: question }));
    await screen.findByText("已回答公开问题。");
    const chatCalls = fetchMock.mock.calls.filter(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    expect(chatCalls).toHaveLength(1);
    const body = chatCalls[0][1]?.body;
    expect(JSON.parse(typeof body === "string" ? body : "{}")).toMatchObject({
      query: question,
      client_context: {
        entrypoint: "popular",
        recommendation_id: recommendationId,
      },
    });
  });

  it("聊天页大家常问保留已有问答并沿用会话", async () => {
    const question = "办理材料如何提交？";
    const recommendationId = `pq_${"a".repeat(32)}`;
    const fetchMock = installFetch(
      () =>
        streamResponse([
          event("final", 0, { answer: "已回答公开问题。", citations: [] }),
        ]),
      {
        mode: "POPULAR",
        generated_at: "2026-09-23T00:00:00Z",
        window_days: 7,
        items: [{ id: recommendationId, question, topic_key: "办理" }],
      },
      { ...capabilitiesBody, request_usage_context: true },
    );
    const user = userEvent.setup();
    await openHome();
    await ask("此前提问");
    await screen.findByText("已回答公开问题。");
    const tab = await screen.findByRole("tab", { name: "大家常问" });
    await user.click(tab);
    await user.click(screen.getByRole("button", { name: question }));
    await waitFor(() =>
      expect(screen.getAllByText("已回答公开问题。")).toHaveLength(2),
    );

    expect(screen.getByText("此前提问")).toBeInTheDocument();
    expect(screen.getByText(question)).toBeInTheDocument();
    const bodies = fetchMock.mock.calls
      .filter(([input]) => pathOf(input) === "/api/public/chat")
      .map(
        ([, init]) =>
          JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
            conversation_id: string;
            client_context: { entrypoint: string; recommendation_id?: string };
          },
      );
    expect(bodies).toHaveLength(2);
    expect(bodies[1].conversation_id).toBe(bodies[0].conversation_id);
    expect(bodies[1].client_context).toEqual({
      entrypoint: "popular",
      recommendation_id: recommendationId,
    });
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
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("未登录 bootstrap 只触发整页 SSO，不显示匿名降级", async () => {
    const redirect = vi
      .spyOn(authNavigation, "redirectToSso")
      .mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "public login required" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<WanshitongApp />);

    await waitFor(() => expect(redirect).toHaveBeenCalledOnce());
    expect(
      screen.queryByRole("heading", { name: "你的内部知识助手" }),
    ).not.toBeInTheDocument();
  });

  it("SSO 用户本地退出后清空工作台并提供重新登录", async () => {
    const ssoSession = {
      ...sessionBody,
      deployment_id: "candidate_8289",
      user: { user_id: "1001", display_name: "测试用户" },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(ssoSession));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/sso/logout") {
          return Promise.resolve(
            Response.json({ status: "logged_out", scope: "kb_local" }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      });
    await openHome();

    expect(screen.getByText("测试用户")).toBeVisible();
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "退出湾事通" }));

    expect(
      await screen.findByRole("heading", { name: "已退出" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "重新登录" })).toBeVisible();
    expect(
      fetchMock.mock.calls.filter(([input]) => pathOf(input) === "/sso/logout"),
    ).toHaveLength(1);
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

  it("已验证问题可横向浏览，点击后只提交原问题", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "实时生成的回答", citations: [] }),
      ]),
    );
    await openHome();
    const suggestions = screen.getByRole("region", { name: "你可能想问" });
    const cards = within(suggestions)
      .getAllByRole("button")
      .filter((button) => button.classList.contains("wst-suggestion"));
    expect(cards).toHaveLength(5);
    const question = cards[0].textContent ?? "";
    await userEvent.setup().click(cards[0]);

    expect(await screen.findByText("实时生成的回答")).toBeInTheDocument();
    expect(screen.getByText(question)).toBeInTheDocument();
    const chatCalls = fetchMock.mock.calls.filter(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    expect(chatCalls).toHaveLength(1);
    const chatCall = chatCalls[0];
    const chatBody = chatCall?.[1]?.body;
    expect(typeof chatBody).toBe("string");
    const body = JSON.parse(
      typeof chatBody === "string" ? chatBody : "{}",
    ) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["conversation_id", "query"]);
    expect(typeof body.conversation_id).toBe("string");
    expect(body.query).toBe(question);
    expect(body).not.toHaveProperty("shortcut_id");
    await waitFor(() => {
      const next = screen.getByRole("region", { name: "换个话题" });
      expect(within(next).queryByRole("button", { name: question })).toBeNull();
    });
  });

  it("卡片和手工输入同题沿用同一会话与请求合同", async () => {
    let chatCount = 0;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          chatCount += 1;
          return Promise.resolve(
            streamResponse([
              event("final", 0, {
                answer: `第 ${chatCount} 次回答`,
                citations: [],
              }),
            ]),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      });
    const user = userEvent.setup();
    await openHome();
    const suggestions = screen.getByRole("region", { name: "你可能想问" });
    const card = within(suggestions)
      .getAllByRole("button")
      .find((button) => button.classList.contains("wst-suggestion"));
    expect(card).toBeDefined();
    const question = card?.textContent ?? "";

    await user.click(card!);
    await screen.findByText("第 1 次回答");
    const textbox = screen.getByRole("textbox", { name: "向湾事通提问" });
    await user.type(textbox, question);
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText("第 2 次回答");

    const bodies = fetchMock.mock.calls
      .filter(([input]) => pathOf(input) === "/api/public/chat")
      .map(
        ([, init]) =>
          JSON.parse(
            typeof init?.body === "string" ? init.body : "{}",
          ) as Record<string, unknown>,
      );
    expect(bodies).toHaveLength(2);
    expect(bodies[0].conversation_id).toBe(bodies[1].conversation_id);
    expect(bodies.map((body) => body.query)).toEqual([question, question]);
    expect(
      bodies.every(
        (body) =>
          JSON.stringify(Object.keys(body).sort()) ===
          JSON.stringify(["conversation_id", "query"]),
      ),
    ).toBe(true);
  });

  it("推荐卡片快速双击只发起一次问答", async () => {
    const pending = pendingStream(event("meta", 0));
    const fetchMock = installFetch(pending.response);
    const user = userEvent.setup();
    await openHome();
    const suggestions = screen.getByRole("region", { name: "你可能想问" });
    const card = within(suggestions)
      .getAllByRole("button")
      .find((button) => button.classList.contains("wst-suggestion"));
    expect(card).toBeDefined();

    await user.dblClick(card!);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input]) => pathOf(input) === "/api/public/chat",
        ),
      ).toHaveLength(1);
    });
    await user.click(screen.getByRole("button", { name: "停止" }));
    await waitFor(() => expect(pending.wasCancelled()).toBe(true));
  });

  it("否定限定从推荐卡片原样进入气泡与query", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "已按限定回答。", citations: [] }),
      ]),
    );
    const user = userEvent.setup();
    const question = "哪些认证不适用外部认证管理办法？";
    await openHome();

    let card = screen.queryByRole("button", { name: question });
    for (let index = 0; !card && index < 2; index += 1) {
      await user.click(screen.getByRole("button", { name: "换一换推荐问题" }));
      card = screen.queryByRole("button", { name: question });
    }
    expect(card).not.toBeNull();
    await user.click(card!);

    expect(await screen.findByText("已按限定回答。")).toBeInTheDocument();
    expect(screen.getByText(question)).toBeInTheDocument();
    const chatCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    const body = JSON.parse(
      typeof chatCall?.[1]?.body === "string" ? chatCall[1].body : "{}",
    ) as Record<string, unknown>;
    expect(body.query).toBe(question);
  });

  it("停用后的必须和工作日限定题仍可手工原样提问", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "已按限定回答。", citations: [] }),
      ]),
    );
    const question = "标前公示截止日必须是工作日吗，至少要公示几天？";
    await openHome();

    expect(screen.queryByRole("button", { name: question })).toBeNull();
    await ask(question);
    expect(await screen.findByText("已按限定回答。")).toBeInTheDocument();
    expect(screen.getByText(question)).toBeInTheDocument();
    const chatCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    const body = JSON.parse(
      typeof chatCall?.[1]?.body === "string" ? chatCall[1].body : "{}",
    ) as Record<string, unknown>;
    expect(body.query).toBe(question);
  });

  it("停用或模板题不主动展示，但用户仍可手工提问", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "已查找相关资料。", citations: [] }),
      ]),
    );
    const question = "是否有测试报告模板可供参考？";
    await openHome();

    expect(screen.queryByRole("button", { name: question })).toBeNull();
    await ask(question);
    expect(await screen.findByText("已查找相关资料。")).toBeInTheDocument();
    const chatCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/chat",
    );
    const body = JSON.parse(
      typeof chatCall?.[1]?.body === "string" ? chatCall[1].body : "{}",
    ) as Record<string, unknown>;
    expect(body.query).toBe(question);
  });

  it("每轮结束及手动换一换都换新组，且不推荐任何已问问题", async () => {
    let chatCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          chatCalls += 1;
          return Promise.resolve(
            streamResponse([
              event("final", 0, {
                answer: `第 ${chatCalls} 轮回答`,
                citations: [],
              }),
            ]),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      },
    );
    const user = userEvent.setup();
    await openHome();
    const visibleQuestions = () =>
      within(
        screen.queryByRole("region", { name: "换个话题" }) ??
          screen.getByRole("region", { name: "你可能想问" }),
      )
        .getAllByRole("button")
        .filter((button) => button.classList.contains("wst-suggestion"))
        .map((button) => button.textContent ?? "");
    const initial = visibleQuestions();
    await user.click(screen.getByRole("button", { name: "换一换推荐问题" }));
    const refreshed = visibleQuestions();
    expect(refreshed).toHaveLength(5);
    expect(refreshed.every((question) => !initial.includes(question))).toBe(
      true,
    );

    await user.click(
      within(screen.getByRole("region", { name: "你可能想问" })).getByRole(
        "button",
        { name: refreshed[0] },
      ),
    );
    await screen.findByText("第 1 轮回答");
    await waitFor(() => {
      const next = visibleQuestions();
      expect(next).toHaveLength(5);
      expect(next.every((question) => !refreshed.includes(question))).toBe(
        true,
      );
    });
    const afterFirst = visibleQuestions();
    await user.click(
      within(screen.getByRole("region", { name: "换个话题" })).getByRole(
        "button",
        { name: afterFirst[0] },
      ),
    );
    await screen.findByText("第 2 轮回答");
    await waitFor(() => {
      const next = visibleQuestions();
      expect(next).toHaveLength(5);
      expect(next).not.toContain(refreshed[0]);
      expect(next).not.toContain(afterFirst[0]);
      expect(next.every((question) => !afterFirst.includes(question))).toBe(
        true,
      );
    });
    expect(chatCalls).toBe(2);
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
    expect(screen.getByRole("button", { name: "新问题" })).toBeEnabled();
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
      event("meta", 0) +
        event("stage", 1, { stage: "accepted" }) +
        event("stage", 2, { stage: "retrieval" }),
    );
    installFetch(pending.response);
    await openHome();
    const user = await ask();

    expect(
      await within(screen.getByLabelText("湾事通回答")).findByText(
        "正在检索内部资料",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "处理进度" })).getByText(
        "已接收",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/^\d+ 秒$/)).toBeInTheDocument();
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

  it("校验未完成时展示服务端重试提示而非资料不足文案", async () => {
    installFetch(
      streamResponse([
        event("final", 0, {
          status: "INSUFFICIENT_EVIDENCE",
          answer: null,
          user_message:
            "已找到相关资料，但本次答案生成或核验未完成。你可以稍后重试。",
          citations: [],
        }),
      ]),
    );
    await openHome();
    await ask();

    expect(
      await screen.findByText(
        "已找到相关资料，但本次答案生成或核验未完成。你可以稍后重试。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("暂未在内部资料中找到足以回答这个问题的内容。"),
    ).not.toBeInTheDocument();
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

  it("已显示的对话保留且提供新问题入口", async () => {
    installFetch(
      streamResponse([
        event("final", 0, { answer: "本轮答案", citations: [] }),
      ]),
    );
    await openHome();
    await ask("第一轮问题");
    await screen.findByText("本轮答案");

    expect(screen.getByText("第一轮问题")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新问题" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toBeEnabled();
  });

  it("新问题键盘操作回到空输入，登录恢复草稿不重现且不额外请求 Session", async () => {
    authNavigation.saveLoginDraft("登录前草稿");
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "推荐题已回答", citations: [] }),
      ]),
    );
    const user = userEvent.setup();
    await openHome();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toHaveValue(
      "登录前草稿",
    );
    const card = within(screen.getByRole("region", { name: "你可能想问" }))
      .getAllByRole("button")
      .find((button) => button.classList.contains("wst-suggestion"));
    expect(card).toBeDefined();
    const suggestedQuestion = card?.textContent ?? "";
    await user.click(card!);
    await screen.findByText("推荐题已回答");
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toHaveValue(
      "",
    );

    screen.getByRole("button", { name: "新问题" }).focus();
    await user.keyboard("{Enter}");
    expect(
      await screen.findByRole("heading", { name: "你的内部知识助手" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向湾事通提问" })).toHaveValue(
      "",
    );
    const paths = fetchMock.mock.calls.map(([input]) => pathOf(input));
    expect(paths).toEqual([
      "/api/public/session",
      "/api/public/capabilities",
      "/api/public/popular-questions",
      "/api/public/chat",
    ]);
    const chatBody = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/chat",
    )?.[1]?.body;
    expect(
      JSON.parse(typeof chatBody === "string" ? chatBody : "{}"),
    ).toMatchObject({ query: suggestedQuestion });
  });

  it("聊天页全局推荐保留历史和会话并只发可见题面一次", async () => {
    let chatCount = 0;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(
            Response.json({ ...capabilitiesBody, request_usage_context: true }),
          );
        }
        if (path === "/api/public/chat") {
          chatCount += 1;
          return Promise.resolve(
            streamResponse([
              event("final", 0, {
                answer: `第 ${chatCount} 次回答`,
                citations: [],
              }),
            ]),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      });
    const user = userEvent.setup();
    await openHome();
    await ask("不同主题的问题 A");
    await screen.findByText("第 1 次回答");
    const suggestions = screen.getByRole("region", { name: "换个话题" });
    const card = within(suggestions)
      .getAllByRole("button")
      .find((button) => button.classList.contains("wst-suggestion"));
    expect(card).toBeDefined();
    const suggestedQuestion = card?.textContent ?? "";
    await user.click(card!);
    await screen.findByText("第 2 次回答");

    expect(screen.getByText("不同主题的问题 A")).toBeInTheDocument();
    expect(screen.getByText("第 1 次回答")).toBeInTheDocument();
    expect(screen.getByText(suggestedQuestion)).toBeInTheDocument();
    const bodies = fetchMock.mock.calls
      .filter(([input]) => pathOf(input) === "/api/public/chat")
      .map(
        ([, init]) =>
          JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
            conversation_id: string;
            query: string;
            client_context: {
              entrypoint: string;
              recommendation_id?: string;
            };
          },
      );
    expect(bodies).toHaveLength(2);
    expect(bodies[0].conversation_id).toBe(bodies[1].conversation_id);
    expect(bodies.map((body) => body.query)).toEqual([
      "不同主题的问题 A",
      suggestedQuestion,
    ]);
    expect(bodies[0].client_context).toEqual({ entrypoint: "manual" });
    expect(bodies[1].client_context.entrypoint).toBe("suggestion");
    expect(bodies[1].client_context.recommendation_id).toMatch(/^sq-/);
  });

  it("忙碌时禁用新问题，停止后才允许换话题", async () => {
    const pending = pendingStream(event("meta", 0));
    const fetchMock = installFetch(pending.response);
    await openHome();
    const user = await ask("正在回答的问题");
    const newTopic = screen.getByRole("button", { name: "新问题" });
    expect(newTopic).toBeDisabled();
    await user.click(newTopic);
    expect(screen.getByText("正在回答的问题")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => pathOf(input) === "/api/public/chat",
      ),
    ).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "停止" }));
    expect(newTopic).toBeEnabled();
    await user.click(newTopic);
    expect(
      await screen.findByRole("heading", { name: "你的内部知识助手" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(pending.wasCancelled()).toBe(true));
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
    const summary = await screen.findByText("引用依据（1段）");
    await user.click(summary);
    const groupSummary = screen.getByText("展开全部片段").closest("summary");
    expect(groupSummary).not.toBeNull();
    if (groupSummary) await user.click(groupSummary);

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

  it("负反馈可选原因和说明并提交兼容字段", async () => {
    const fetchMock = installFetch(
      streamResponse([
        event("final", 0, { answer: "来源待核对答案", citations: [] }),
      ]),
    );
    await openHome();
    const user = await ask();
    await screen.findByText("来源待核对答案");

    await user.click(screen.getByRole("button", { name: "没帮助" }));
    await user.selectOptions(
      screen.getByLabelText("主要原因（可选）"),
      "WRONG_SOURCE",
    );
    await user.type(
      screen.getByLabelText("补充说明（可选）"),
      "应该引用最新办事指南。",
    );
    await user.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(await screen.findByText("感谢你的反馈")).toBeInTheDocument();
    const feedbackCall = fetchMock.mock.calls.find(
      ([input]) => pathOf(input) === "/api/public/feedback",
    );
    expect(
      JSON.parse(
        typeof feedbackCall?.[1]?.body === "string"
          ? feedbackCall[1].body
          : "{}",
      ),
    ).toEqual({
      trace_id: "trace_11111111111111111111111111111111",
      useful: false,
      reason_code: "WRONG_SOURCE",
      reason_detail: "WRONG_SOURCE",
      comment: "应该引用最新办事指南。",
    });
  });

  it("反馈失败保留选项与输入并允许原样重试", async () => {
    let feedbackCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          return Promise.resolve(
            streamResponse([
              event("final", 0, { answer: "可重试反馈答案", citations: [] }),
            ]),
          );
        }
        if (path === "/api/public/feedback") {
          feedbackCalls += 1;
          return Promise.resolve(
            feedbackCalls === 1
              ? Response.json(
                  { error: { code: "FEEDBACK_STORE_UNAVAILABLE" } },
                  { status: 503 },
                )
              : Response.json({ useful: false }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      },
    );
    await openHome();
    const user = await ask();
    await screen.findByText("可重试反馈答案");
    await user.click(screen.getByRole("button", { name: "没帮助" }));
    const reason = screen.getByLabelText("主要原因（可选）");
    const comment = screen.getByLabelText("补充说明（可选）");
    await user.selectOptions(reason, "INCOMPLETE");
    await user.type(comment, "缺少办理时限。");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "已保留你的选择和说明",
    );
    expect(reason).toHaveValue("INCOMPLETE");
    expect(comment).toHaveValue("缺少办理时限。");
    await user.click(screen.getByRole("button", { name: "重新提交反馈" }));

    expect(await screen.findByText("感谢你的反馈")).toBeInTheDocument();
    expect(feedbackCalls).toBe(2);
  });

  it("反馈时会话过期不丢失说明并提供重新登录", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          return Promise.resolve(
            streamResponse([
              event("final", 0, { answer: "会话过期反馈答案", citations: [] }),
            ]),
          );
        }
        if (path === "/api/public/feedback") {
          return Promise.resolve(new Response(null, { status: 401 }));
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      },
    );
    await openHome();
    const user = await ask();
    await screen.findByText("会话过期反馈答案");
    await user.click(screen.getByRole("button", { name: "没帮助" }));
    const comment = screen.getByLabelText("补充说明（可选）");
    await user.type(comment, "需要重新登录后提交。");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "登录会话已过期",
    );
    expect(comment).toHaveValue("需要重新登录后提交。");
    expect(screen.getByRole("button", { name: "重新登录" })).toBeVisible();
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

  it("Session 过期时整页登录且不自动重发原问题", async () => {
    let sessionCalls = 0;
    let chatCalls = 0;
    const redirect = vi
      .spyOn(authNavigation, "redirectToSso")
      .mockImplementation(() => undefined);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const path = pathOf(input);
        if (path === "/api/public/session") {
          sessionCalls += 1;
          return Promise.resolve(Response.json(sessionBody));
        }
        if (path === "/api/public/capabilities") {
          return Promise.resolve(Response.json(capabilitiesBody));
        }
        if (path === "/api/public/chat") {
          chatCalls += 1;
          return Promise.resolve(new Response(null, { status: 401 }));
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      });
    await openHome();
    await ask("会话过期测试");

    await waitFor(() => {
      expect(redirect).toHaveBeenCalledWith("会话过期测试");
    });
    expect(sessionCalls).toBe(1);
    expect(chatCalls).toBe(1);
    expect(fetchMock.mock.calls).toHaveLength(4);
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
