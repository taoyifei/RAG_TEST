import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePublicChat } from "./usePublicChat";

const originalId = "history-current";
const storageKey = "wst-natural-conversation:history_8289:1001";
const turn = {
  turn_id: "turn-history",
  trace_id: "trace-history",
  question: "原问题",
  answer: "原回答",
  status: "completed",
  citations: [],
  created_at: "2026-11-01T04:00:00Z",
  search_scopes: ["public"],
};

function installHistory(
  options: {
    archiveStatus?: number;
    pendingArchive?: Promise<Response>;
    pendingList?: Promise<Response>;
    historyStatus?: number;
    list?: (url: URL) => Response | Promise<Response>;
  } = {},
) {
  const active = [
    {
      conversation_id: originalId,
      title: "原问题",
      updated_at: turn.created_at,
      archived: false,
    },
  ];
  let archived: typeof active = [];
  if (options.historyStatus) active.splice(0);
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const url = new URL(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/public/session")
        return Response.json({
          session_id: "wstsid_11111111111111111111111111111111",
          csrf_token: "a".repeat(64),
          deployment_id: "history_8289",
          user: { user_id: "1001", display_name: "测试用户", role: "user" },
        });
      if (url.pathname === "/api/public/capabilities")
        return Response.json({
          stream_protocol: "wanshitong-weknora-sse-v1",
          natural_stream_protocol: "wanshitong-weknora-sse-v1",
          feedback: false,
          shortcuts: [],
        });
      if (url.pathname === "/api/public/conversations") {
        if (options.list) return options.list(url);
        if (url.searchParams.get("archived") === "true" && options.pendingList)
          return options.pendingList;
        const items =
          url.searchParams.get("archived") === "true" ? archived : active;
        return Response.json({
          items: [...items],
          has_more: false,
          total: items.length,
        });
      }
      if (url.pathname.endsWith("/archive")) {
        if (options.pendingArchive) return options.pendingArchive;
        if (options.archiveStatus)
          return Response.json(
            { detail: "生成中" },
            { status: options.archiveStatus },
          );
        const payload = JSON.parse(
          typeof init?.body === "string" ? init.body : "{}",
        ) as { archived: boolean };
        const selected = payload.archived;
        if (selected)
          archived = active
            .splice(0)
            .map((item) => ({ ...item, archived: true }));
        else
          active.push(
            ...archived.splice(0).map((item) => ({ ...item, archived: false })),
          );
        return Response.json({
          conversation_id: originalId,
          archived: selected,
        });
      }
      if (init?.method === "DELETE") {
        active.splice(0);
        archived = [];
        return Response.json({ deleted: true });
      }
      if (url.pathname.endsWith(originalId) && options.historyStatus)
        return Response.json(
          { detail: "会话已删除" },
          { status: options.historyStatus },
        );
      if (url.pathname.startsWith("/api/public/conversations/"))
        return Response.json({
          turns: url.pathname.endsWith(originalId) ? [turn] : [],
        });
      return new Response(null, { status: 404 });
    });
}

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("个人历史会话操作", () => {
  it("历史分页追加且去重，标题搜索可以返回尚未加载的旧会话", async () => {
    let resolvePage!: (response: Response) => void;
    const pendingPage = new Promise<Response>((done) => {
      resolvePage = done;
    });
    const recent = {
      conversation_id: originalId,
      title: "最近一次提问",
      updated_at: turn.created_at,
      archived: false,
    };
    const older = {
      ...recent,
      conversation_id: "older-history",
      title: "半年以前的委托单",
    };
    const lists: URL[] = [];
    installHistory({
      list: (url) => {
        lists.push(url);
        if (url.searchParams.has("cursor")) return pendingPage;
        const searching = url.searchParams.get("query") === "委托单";
        return Response.json({
          items: searching ? [older] : [recent],
          has_more: !searching,
          next_cursor: searching ? null : "next-history-page",
          total: searching ? 1 : 2,
        });
      },
    });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.historyHasMore).toBe(true));
    let nextPage!: Promise<void>;
    act(() => {
      nextPage = result.current.loadMoreHistory();
    });
    expect(result.current.historyLoadingMore).toBe(true);
    expect(result.current.historyLoading).toBe(false);
    expect(result.current.historySessions).toEqual([recent]);
    await act(async () => {
      resolvePage(
        Response.json({
          items: [recent, older],
          has_more: false,
          next_cursor: null,
          total: 2,
        }),
      );
      await nextPage;
    });
    expect(result.current.historySessions).toEqual([recent, older]);
    expect(result.current.historyHasMore).toBe(false);
    expect(lists[1].searchParams.get("cursor")).toBe("next-history-page");
    await act(async () => result.current.searchHistory(" 委托单 "));
    expect(result.current.historySessions).toEqual([older]);
    expect(lists.at(-1)?.searchParams.get("query")).toBe("委托单");
    expect(lists.at(-1)?.searchParams.has("cursor")).toBe(false);
  });

  it("新关键词覆盖迟到的搜索结果，失败可重试且归档不沿用搜索条件", async () => {
    let resolveOlder!: (response: Response) => void;
    const pendingOlder = new Promise<Response>((done) => {
      resolveOlder = done;
    });
    let attempts = 0;
    const lists: URL[] = [];
    installHistory({
      list: (url) => {
        lists.push(url);
        const query = url.searchParams.get("query");
        if (query === "旧关键词") return pendingOlder;
        if (query === "新关键词" && attempts++ === 0)
          return Response.json({}, { status: 502 });
        return Response.json({
          items: query
            ? [
                {
                  conversation_id: "new-match",
                  title: "新关键词匹配",
                  updated_at: turn.created_at,
                },
              ]
            : [],
          total: query ? 1 : 0,
          has_more: false,
          next_cursor: null,
        });
      },
    });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    let oldRequest!: Promise<void>;
    act(() => {
      oldRequest = result.current.searchHistory("旧关键词");
    });
    await act(async () => {
      await expect(
        result.current.searchHistory("新关键词"),
      ).rejects.toMatchObject({ status: 502 });
    });
    expect(result.current.historyError).toBeTruthy();
    await act(async () => result.current.retryHistory());
    expect(result.current.historySessions[0].title).toBe("新关键词匹配");
    await act(async () => {
      resolveOlder(Response.json({ items: [], total: 0, has_more: false }));
      await oldRequest;
    });
    expect(result.current.historySessions[0].title).toBe("新关键词匹配");
    expect(result.current.historyError).toBeUndefined();
    await act(async () => result.current.changeHistoryView(true));
    expect(lists.at(-1)?.searchParams.get("archived")).toBe("true");
    expect(lists.at(-1)?.searchParams.has("query")).toBe(false);
  });

  it("复制和导出前重新检查当前会话访问权限", async () => {
    window.sessionStorage.setItem(storageKey, originalId);
    const fetchMock = installHistory();
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.turns).toHaveLength(1));
    const displayed = result.current.turns[0];
    await act(async () => result.current.ensureTurnAccess(displayed));
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(Response.json({}, { status: 403 })),
    );
    await expect(
      result.current.ensureTurnAccess(displayed),
    ).rejects.toMatchObject({ status: 403 });
    expect(result.current.turns[0].answer).toBe("原回答");
  });

  it("分页失败重试保留已加载内容，过期分页错误不干扰新搜索", async () => {
    let rejectPage!: (error: Error) => void;
    const oldPage = new Promise<Response>((_, reject) => {
      rejectPage = reject;
    });
    let pageAttempts = 0;
    const recent = {
      conversation_id: originalId,
      title: "最近提问",
      updated_at: turn.created_at,
    };
    const older = { ...recent, conversation_id: "older", title: "较早提问" };
    installHistory({
      list: (url) => {
        if (url.searchParams.has("query"))
          return Response.json({
            items: [older],
            total: 1,
            has_more: false,
            next_cursor: null,
          });
        if (url.searchParams.has("cursor")) {
          pageAttempts += 1;
          if (pageAttempts === 1) return Response.json({}, { status: 502 });
          if (pageAttempts === 2)
            return Response.json({
              items: [older],
              total: 3,
              has_more: true,
              next_cursor: "third-page",
            });
          return oldPage;
        }
        return Response.json({
          items: [recent],
          total: 3,
          has_more: true,
          next_cursor: "second-page",
        });
      },
    });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.historyHasMore).toBe(true));
    await act(async () => {
      await expect(result.current.loadMoreHistory()).rejects.toMatchObject({
        status: 502,
      });
    });
    expect(result.current.historySessions).toEqual([recent]);
    await act(async () => result.current.retryHistory());
    expect(result.current.historySessions).toEqual([recent, older]);
    let stale!: Promise<void>;
    act(() => {
      stale = result.current.loadMoreHistory();
    });
    await act(async () => result.current.searchHistory("较早"));
    await act(async () => {
      rejectPage(new Error("迟到的分页错误"));
      await expect(stale).resolves.toBeUndefined();
    });
    expect(result.current.historySessions).toEqual([older]);
    expect(result.current.historyError).toBeUndefined();
    expect(result.current.historyLoadingMore).toBe(false);
  });

  it("刷新时原会话已经被删除，使用新ID并正常加载历史列表", async () => {
    window.sessionStorage.setItem(storageKey, originalId);
    installHistory({ historyStatus: 404 });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    expect(result.current.conversationId).not.toBe(originalId);
    expect(window.sessionStorage.getItem(storageKey)).toBe(
      result.current.conversationId,
    );
    expect(result.current.turns).toEqual([]);
    expect(result.current.historySessions).toEqual([]);
  });
  it("归档保留当前问答和刷新会话标识，归档列表支持打开和恢复", async () => {
    window.sessionStorage.setItem(storageKey, originalId);
    const fetchMock = installHistory();
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    await act(async () => result.current.archiveHistory(originalId, true));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    expect(result.current.historySessions).toEqual([]);
    expect(result.current.conversationId).toBe(originalId);
    expect(result.current.turns[0].answer).toBe("原回答");
    expect(window.sessionStorage.getItem(storageKey)).toBe(originalId);
    await act(async () => result.current.changeHistoryView(true));
    expect(result.current.historyArchived).toBe(true);
    expect(result.current.historySessions[0].archived).toBe(true);
    await act(async () => result.current.openHistory(originalId));
    await act(async () => result.current.archiveHistory(originalId, false));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    expect(result.current.historySessions).toEqual([]);
    await act(async () => result.current.changeHistoryView(false));
    expect(result.current.historySessions[0].conversation_id).toBe(originalId);
    const mutations = fetchMock.mock.calls.filter(
      ([, init]) =>
        init?.method === "POST" &&
        typeof init.body === "string" &&
        init.body.includes("archived"),
    );
    expect(mutations).toHaveLength(2);
    expect(mutations[0][1]?.headers).toMatchObject({
      "X-CSRF-Token": "a".repeat(64),
    });
  });

  it("删除当前会话后清空正文并保存新会话标识", async () => {
    window.sessionStorage.setItem(storageKey, originalId);
    const fetchMock = installHistory();
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    await act(async () => result.current.deleteHistory(originalId));
    await waitFor(() => expect(result.current.historyLoading).toBe(false));
    expect(result.current.conversationId).not.toBe(originalId);
    expect(window.sessionStorage.getItem(storageKey)).toBe(
      result.current.conversationId,
    );
    expect(result.current.turns).toEqual([]);
    expect(result.current.searchScopes).toEqual(["public"]);
    expect(result.current.historySessions).toEqual([]);
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE"),
    ).toBe(true);
  });

  it("归档失败保留当前会话和列表，等待修改时不能提交新问题", async () => {
    let resolve!: (response: Response) => void;
    const pendingArchive = new Promise<Response>((done) => {
      resolve = done;
    });
    window.sessionStorage.setItem(storageKey, originalId);
    const fetchMock = installHistory({ pendingArchive });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    let action!: Promise<void>;
    act(() => {
      action = result.current.archiveHistory(originalId, true);
    });
    expect(result.current.historyActionBusy).toBe(true);
    act(() => result.current.submit("不应提交"));
    expect(
      fetchMock.mock.calls.some(([input]) =>
        (typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url
        ).endsWith("/chat"),
      ),
    ).toBe(false);
    await act(async () => {
      resolve(Response.json({ detail: "生成中" }, { status: 409 }));
      await expect(action).rejects.toMatchObject({ status: 409 });
    });
    expect(result.current.historyActionBusy).toBe(false);
    expect(result.current.conversationId).toBe(originalId);
    expect(result.current.historySessions[0].conversation_id).toBe(originalId);
    expect(result.current.turns[0].answer).toBe("原回答");
  });

  it("快速切换历史分类，迟到的归档响应不会覆盖普通列表", async () => {
    let resolve!: (response: Response) => void;
    const pendingList = new Promise<Response>((done) => {
      resolve = done;
    });
    installHistory({ pendingList });
    const { result } = renderHook(() => usePublicChat());
    await waitFor(() => expect(result.current.sessionReady).toBe(true));
    let archivedRequest!: Promise<void>;
    act(() => {
      archivedRequest = result.current.changeHistoryView(true);
    });
    await act(async () => result.current.changeHistoryView(false));
    await act(async () => {
      resolve(
        Response.json({
          items: [
            {
              conversation_id: "late",
              title: "迟到归档",
              updated_at: turn.created_at,
              archived: true,
            },
          ],
        }),
      );
      await archivedRequest;
    });
    expect(result.current.historyArchived).toBe(false);
    expect(
      result.current.historySessions.map((item) => item.conversation_id),
    ).toEqual([originalId]);
  });
});
