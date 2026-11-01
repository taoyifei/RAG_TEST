import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublicConversationLayout } from "./PublicConversationLayout";
import { PublicApiError } from "./publicApi";

function dayAgo(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  date.setHours(10, 0, 0, 0);
  return date.toISOString();
}

const sessions = [
  {
    conversation_id: "history-1",
    title: "差旅费结算单怎么填？",
    updated_at: dayAgo(0),
  },
  {
    conversation_id: "history-2",
    title: "AI Office 下载入口",
    updated_at: dayAgo(1),
  },
  {
    conversation_id: "history-3",
    title: "线上委托单审批顺序",
    updated_at: dayAgo(4),
  },
  {
    conversation_id: "history-4",
    title: "自然语言搜索个人资料",
    updated_at: dayAgo(12),
  },
];

function props() {
  return {
    busy: false,
    conversationId: "history-2",
    historyEnabled: true,
    sessions,
    moreCount: 0,
    onNewTopic: vi.fn(),
    onSelect: vi
      .fn<(id: string) => Promise<void>>()
      .mockResolvedValue(undefined),
    toolbar: <button type="button">我的资料</button>,
    viewLabel: "知识问答",
    children: (
      <main>
        <div className="wst-composer">
          <textarea aria-label="向湾小度提问" />
        </div>
      </main>
    ),
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("历史会话侧栏", () => {
  it("会话菜单不打开会话，支持归档、恢复及点外部收起", async () => {
    const user = userEvent.setup();
    const parameters = props();
    const onArchive = vi.fn().mockResolvedValue(undefined);
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const onHistoryViewChange = vi.fn().mockResolvedValue(undefined);
    const view = render(
      <PublicConversationLayout
        {...parameters}
        onArchive={onArchive}
        onDelete={onDelete}
        onHistoryViewChange={onHistoryViewChange}
      />,
    );
    const trigger = screen.getByRole("button", {
      name: `会话操作：${sessions[0].title}`,
    });
    await user.click(trigger);
    expect(parameters.onSelect).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("button", { name: "归档会话" }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "归档会话" }));
    expect(onArchive).toHaveBeenCalledExactlyOnceWith("history-1", true);
    expect(
      await screen.findByText("会话已归档，可在“已归档”中恢复。"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看已归档会话" }));
    expect(onHistoryViewChange).toHaveBeenCalledExactlyOnceWith(true);
    view.rerender(
      <PublicConversationLayout
        {...parameters}
        historyArchived
        onArchive={onArchive}
        onDelete={onDelete}
        onHistoryViewChange={onHistoryViewChange}
      />,
    );
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "恢复会话" }));
    expect(onArchive).toHaveBeenLastCalledWith("history-1", false);
    await user.click(trigger);
    await user.click(screen.getByRole("heading", { name: "已归档" }));
    expect(
      screen.queryByRole("button", { name: "恢复会话" }),
    ).not.toBeInTheDocument();
  });

  it("删除先确认，取消保留会话，活动会话错误可见", async () => {
    const user = userEvent.setup();
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const onDelete = vi
      .fn()
      .mockRejectedValue(new PublicApiError("正在生成", { status: 409 }));
    render(
      <PublicConversationLayout
        {...props()}
        onArchive={vi.fn()}
        onDelete={onDelete}
      />,
    );
    const trigger = screen.getByRole("button", {
      name: `会话操作：${sessions[1].title}`,
    });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "删除会话" }));
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("删除后无法恢复"),
    );
    expect(onDelete).not.toHaveBeenCalled();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "删除会话" }));
    expect(onDelete).toHaveBeenCalledExactlyOnceWith("history-2");
    expect(await screen.findByRole("alert")).toHaveTextContent("会话正在回答");
    expect(
      screen.getByRole("button", { name: sessions[1].title }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("会话操作中不允许换会话，主区域暂时不可操作但内容保留", () => {
    render(
      <PublicConversationLayout
        {...props()}
        historyActionBusy
        onArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "新对话" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: `会话操作：${sessions[0].title}` }),
    ).toBeDisabled();
    expect(
      screen
        .getByRole("textbox", { name: "向湾小度提问" })
        .closest(".wst-main-pane"),
    ).toHaveAttribute("inert");
    expect(screen.getByText("正在更新会话…")).toBeVisible();
  });
  it("桌面默认常驻，按日期分组并高亮当前会话，点选后保留侧栏", async () => {
    const user = userEvent.setup();
    const parameters = props();
    render(<PublicConversationLayout {...parameters} />);
    expect(
      screen.getByRole("complementary", { name: "会话侧栏" }),
    ).toBeVisible();
    for (const title of ["今天", "昨天", "近 7 天", "更早"]) {
      expect(screen.getByRole("heading", { name: title })).toBeVisible();
    }
    expect(
      screen.getByRole("button", { name: sessions[1].title }),
    ).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("button", { name: sessions[0].title }));
    expect(parameters.onSelect).toHaveBeenCalledExactlyOnceWith("history-1");
    expect(screen.getByRole("navigation", { name: "历史会话" })).toBeVisible();
  });

  it("只按已加载的标题搜索，支持清空并明确显示历史数量限制", async () => {
    const user = userEvent.setup();
    render(<PublicConversationLayout {...props()} moreCount={7} />);
    const search = screen.getByRole("searchbox", { name: "搜索历史会话" });
    await user.type(search, "AI OFFICE");
    expect(
      within(screen.getByRole("navigation", { name: "历史会话" })).getAllByRole(
        "button",
      ),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: sessions[1].title }),
    ).toBeVisible();
    expect(
      screen.getByText(/另有 7 个较早会话；搜索仅限当前列表/),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "清空会话搜索" }));
    expect(search).toHaveValue("");
    await user.type(search, "不存在的关键词");
    expect(screen.getByText("没有匹配的会话，试试其他关键词。")).toBeVisible();
  });

  it("收起和展开保留草稿，只记住布局偏好而不保存会话内容", async () => {
    const user = userEvent.setup();
    const parameters = props();
    const first = render(<PublicConversationLayout {...parameters} />);
    await user.type(
      screen.getByRole("textbox", { name: "向湾小度提问" }),
      "还没发送的问题",
    );
    await user.click(screen.getByRole("button", { name: "收起会话侧栏" }));
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "向湾小度提问" })).toHaveValue(
      "还没发送的问题",
    );
    expect(parameters.onSelect).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("wanshitong-sidebar-collapsed")).toBe(
      "true",
    );
    expect(Object.values(window.localStorage).join()).not.toContain(
      sessions[0].title,
    );
    first.unmount();
    render(<PublicConversationLayout {...props()} />);
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "展开会话侧栏" }));
    expect(screen.getByRole("navigation", { name: "历史会话" })).toBeVisible();
  });

  it("忙碌时不能换会话或新建，历史关闭时也不展示列表和搜索", () => {
    const parameters = props();
    const view = render(<PublicConversationLayout {...parameters} busy />);
    expect(screen.getByRole("button", { name: "新对话" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: sessions[0].title }),
    ).toBeDisabled();
    view.rerender(
      <PublicConversationLayout {...parameters} historyEnabled={false} />,
    );
    expect(
      screen.queryByRole("navigation", { name: "历史会话" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新对话" })).toBeEnabled();
  });

  it("不可访问的会话显示提示，恢复后可以重试", async () => {
    const user = userEvent.setup();
    const parameters = props();
    parameters.onSelect.mockRejectedValueOnce(
      new PublicApiError("无法访问", { status: 403, code: "forbidden" }),
    );
    render(<PublicConversationLayout {...parameters} />);
    await user.click(screen.getByRole("button", { name: sessions[0].title }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "当前会话已不可访问",
    );
    await user.click(screen.getByRole("button", { name: sessions[0].title }));
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(parameters.onSelect).toHaveBeenCalledTimes(2);
  });

  it("等待历史时新建对话，迟到的失败不会覆盖新会话状态", async () => {
    const user = userEvent.setup();
    const parameters = props();
    let reject!: (error: Error) => void;
    parameters.onSelect.mockReturnValueOnce(
      new Promise<void>((_, failure) => {
        reject = failure;
      }),
    );
    render(<PublicConversationLayout {...parameters} />);
    await user.click(screen.getByRole("button", { name: sessions[0].title }));
    expect(screen.getByRole("status")).toHaveTextContent("打开中");
    await user.click(screen.getByRole("button", { name: "新对话" }));
    expect(parameters.onNewTopic).toHaveBeenCalledOnce();
    await act(async () => {
      reject(new Error("network"));
      await Promise.resolve();
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("窄屏默认抽屉关闭，Esc、遮罩和选中会话均可关闭，并保持焦点可达", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const user = userEvent.setup();
    render(<PublicConversationLayout {...props()} />);
    const trigger = screen.getByRole("button", { name: "展开会话侧栏" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "会话侧栏" })).toBeVisible();
    expect(document.body.style.overflow).toBe("hidden");
    expect(screen.getByRole("button", { name: "关闭侧栏" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(
      screen.getByRole("button", { name: sessions[3].title }),
    ).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(document.body.style.overflow).toBe("");
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "关闭会话侧栏遮罩" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: sessions[0].title }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("textbox", { name: "向湾小度提问" }),
      ).toHaveFocus(),
    );
  });
});
