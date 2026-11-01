import {
  Archive,
  ArchiveRestore,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import {
  useEffect,
  useCallback,
  useId,
  memo,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { redirectToSso } from "./authNavigation";
import { PublicApiError, type PublicNaturalSession } from "./publicApi";
import { AnchoredPopover } from "./AnchoredPopover";

const COMPACT_QUERY = "(max-width: 900px)";
const COLLAPSED_KEY = "wanshitong-sidebar-collapsed";

function subscribeViewport(callback: () => void) {
  const media = window.matchMedia?.(COMPACT_QUERY);
  media?.addEventListener("change", callback);
  return () => media?.removeEventListener("change", callback);
}

function compactSnapshot() {
  return window.matchMedia?.(COMPACT_QUERY).matches ?? false;
}

function savedCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

function dateGroup(value: string, today: Date): string {
  const updated = new Date(value);
  if (!Number.isFinite(updated.getTime())) return "更早";
  const day = Date.UTC(
    updated.getFullYear(),
    updated.getMonth(),
    updated.getDate(),
  );
  const current = Date.UTC(
    today.getFullYear(),
    today.getMonth(),
    today.getDate(),
  );
  const age = Math.floor((current - day) / 86_400_000);
  if (age <= 0) return "今天";
  if (age === 1) return "昨天";
  return age < 7 ? "近 7 天" : "更早";
}

const ConversationItem = memo(function ConversationItem({
  item,
  selected,
  disabled,
  opening,
  onSelect,
  onArchive,
  onDelete,
  archived,
  active,
}: {
  item: PublicNaturalSession;
  selected: boolean;
  disabled: boolean;
  opening: boolean;
  onSelect: (id: string) => Promise<void>;
  onArchive?: (item: PublicNaturalSession) => void;
  onDelete?: (item: PublicNaturalSession) => void;
  archived: boolean;
  active: boolean;
}) {
  const [menu, setMenu] = useState({ active, open: false });
  // 保留列表节点和滚动位置，但收起侧栏时重置菜单，重新展开不会弹回旧菜单。
  if (menu.active !== active) setMenu({ active, open: false });
  const open = active && menu.open;
  const trigger = useRef<HTMLButtonElement>(null);
  const dismiss = useCallback(
    () => setMenu((value) => (value.open ? { ...value, open: false } : value)),
    [],
  );
  const id = useId();
  return (
    <div className="wst-sidebar-session-row">
      <button
        aria-current={selected ? "page" : undefined}
        aria-label={item.title}
        className="wst-sidebar-session"
        disabled={disabled}
        onClick={() => {
          void onSelect(item.conversation_id);
        }}
        title={item.title}
        type="button"
      >
        <span>{item.title}</span>
        {opening && <small role="status">打开中…</small>}
      </button>
      {onArchive && onDelete && (
        <>
          <button
            aria-label={`会话操作：${item.title}`}
            aria-controls={id}
            aria-expanded={open}
            className="wst-sidebar-session-actions wst-sidebar-icon"
            disabled={disabled}
            onClick={() => setMenu((value) => ({ active, open: !value.open }))}
            ref={trigger}
            title="会话操作"
            type="button"
          >
            <MoreHorizontal aria-hidden="true" size={17} />
          </button>
          <AnchoredPopover
            open={active && open}
            onDismiss={dismiss}
            triggerRef={trigger}
            label={`操作：${item.title}`}
            className="wst-sidebar-session-popover"
            id={id}
            role="group"
            width={176}
          >
            <button
              onClick={() => {
                dismiss();
                onArchive(item);
              }}
              type="button"
            >
              {archived ? (
                <ArchiveRestore aria-hidden="true" size={15} />
              ) : (
                <Archive aria-hidden="true" size={15} />
              )}
              {archived ? "恢复会话" : "归档会话"}
            </button>
            <button
              className="is-destructive"
              onClick={() => {
                dismiss();
                onDelete(item);
              }}
              type="button"
            >
              <Trash2 aria-hidden="true" size={15} />
              删除会话
            </button>
          </AnchoredPopover>
        </>
      )}
    </div>
  );
});

export interface PublicNavigationItem {
  label: string;
  icon: ReactNode;
  active?: boolean;
  disabled?: boolean;
  href?: string;
  onSelect?: () => void;
}

export function PublicConversationLayout({
  busy,
  conversationId,
  historyEnabled,
  sessions,
  moreCount,
  onNewTopic,
  onSelect,
  historyArchived = false,
  historyLoading = false,
  historyActionBusy = false,
  historyError,
  onHistoryViewChange,
  onArchive,
  onDelete,
  toolbar,
  navigation = [],
  viewLabel,
  children,
}: {
  busy: boolean;
  conversationId?: string;
  historyEnabled: boolean;
  sessions: readonly PublicNaturalSession[];
  moreCount: number;
  onNewTopic: () => void;
  onSelect: (conversationId: string) => Promise<void>;
  historyArchived?: boolean;
  historyLoading?: boolean;
  historyActionBusy?: boolean;
  historyError?: string;
  onHistoryViewChange?: (archived: boolean) => Promise<void>;
  onArchive?: (id: string, archived: boolean) => Promise<void>;
  onDelete?: (id: string) => Promise<void>;
  toolbar: ReactNode;
  navigation?: PublicNavigationItem[];
  viewLabel: string;
  children: ReactNode;
}) {
  const compact = useSyncExternalStore(
    subscribeViewport,
    compactSnapshot,
    () => false,
  );
  const [collapsed, setCollapsed] = useState(savedCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [openingId, setOpeningId] = useState<string>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [actionId, setActionId] = useState<string>();
  const historyToggleRef = useRef<HTMLButtonElement>(null);
  const requestRef = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const focusFrameRef = useRef<number | undefined>(undefined);
  const focusComposerOnCloseRef = useRef(false);
  const sidebarId = useId();
  const expanded = compact ? drawerOpen : !collapsed;
  const previouslyExpanded = useRef(expanded);
  useEffect(() => {
    if (previouslyExpanded.current && !expanded && !compact) {
      triggerRef.current?.focus();
    }
    previouslyExpanded.current = expanded;
  }, [compact, expanded]);
  const keyword = query.trim().toLocaleLowerCase();
  const { groups, count } = useMemo(() => {
    const today = new Date();
    const groups = new Map<string, PublicNaturalSession[]>();
    const filtered = sessions.filter((item) =>
      item.title.toLocaleLowerCase().includes(keyword),
    );
    for (const item of [...filtered].sort(
      (a, b) =>
        (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0),
    )) {
      const group = dateGroup(item.updated_at, today);
      const items = groups.get(group) ?? [];
      items.push(item);
      groups.set(group, items);
    }
    return { groups, count: filtered.length };
  }, [sessions, keyword]);

  const focusComposer = useCallback(() => {
    if (focusFrameRef.current !== undefined)
      cancelAnimationFrame(focusFrameRef.current);
    focusFrameRef.current = requestAnimationFrame(() => {
      document
        .querySelector<HTMLTextAreaElement>(".wst-composer textarea")
        ?.focus();
    });
  }, []);
  useEffect(
    () => () => {
      requestRef.current += 1;
      if (focusFrameRef.current !== undefined)
        cancelAnimationFrame(focusFrameRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!compact || !drawerOpen) return;
    const trigger = triggerRef.current;
    focusComposerOnCloseRef.current = false;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setDrawerOpen(false);
      } else if (event.key === "Tab") {
        const controls = panelRef.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), a[href]",
        );
        const first = controls?.[0];
        const last = controls?.[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKey);
      // 等主区域解除 inert 后恢复焦点，浏览器会拒绝聚焦仍被隔离的内容。
      if (focusComposerOnCloseRef.current) {
        focusComposerOnCloseRef.current = false;
        focusComposer();
      } else {
        trigger?.focus();
      }
    };
  }, [compact, drawerOpen, focusComposer]);

  const changeExpanded = useCallback(
    (next: boolean) => {
      if (compact) {
        setDrawerOpen(next);
      } else {
        setCollapsed(!next);
        try {
          window.localStorage.setItem(COLLAPSED_KEY, String(!next));
        } catch {
          // 只记住布局偏好，不保存历史会话内容。
        }
      }
      if (!next && !compact) triggerRef.current?.focus();
    },
    [compact],
  );
  const newTopic = () => {
    if (busy || historyActionBusy || actionId) return;
    requestRef.current += 1;
    setOpeningId(undefined);
    setError(undefined);
    setNotice(undefined);
    setQuery("");
    onNewTopic();
    if (compact) {
      focusComposerOnCloseRef.current = true;
      changeExpanded(false);
    }
    focusComposer();
  };
  const selectSession = useCallback(
    async (id: string) => {
      if (busy || openingId || historyActionBusy || actionId) return;
      const requestId = ++requestRef.current;
      setOpeningId(id);
      setError(undefined);
      try {
        await onSelect(id);
        if (requestRef.current !== requestId) return;
        if (compact) {
          focusComposerOnCloseRef.current = true;
          changeExpanded(false);
        }
        focusComposer();
      } catch (failure) {
        if (requestRef.current !== requestId) return;
        if (failure instanceof PublicApiError && failure.status === 401) {
          redirectToSso();
        } else {
          setError(
            failure instanceof PublicApiError &&
              [403, 404].includes(failure.status)
              ? "当前会话已不可访问，请选择其他会话。"
              : "暂时无法打开会话，请重试。",
          );
        }
      } finally {
        if (requestRef.current === requestId) setOpeningId(undefined);
      }
    },
    [
      busy,
      openingId,
      historyActionBusy,
      actionId,
      onSelect,
      compact,
      changeExpanded,
      focusComposer,
    ],
  );

  const changeHistoryView = async () => {
    if (!onHistoryViewChange || openingId || actionId || historyActionBusy)
      return;
    const requestId = ++requestRef.current;
    setError(undefined);
    setNotice(undefined);
    setQuery("");
    try {
      await onHistoryViewChange(!historyArchived);
    } catch (failure) {
      if (requestRef.current !== requestId) return;
      if (failure instanceof PublicApiError && failure.status === 401)
        redirectToSso();
      else setError("暂时无法加载会话，请重试。");
    }
  };

  const updateConversation = useCallback(
    async (item: PublicNaturalSession, remove: boolean) => {
      if (
        busy ||
        openingId ||
        historyActionBusy ||
        actionId ||
        !onArchive ||
        !onDelete
      )
        return;
      if (
        remove &&
        !window.confirm(`删除“${item.title}”及其中的问答记录？删除后无法恢复。`)
      )
        return;
      setActionId(item.conversation_id);
      setError(undefined);
      setNotice(undefined);
      try {
        if (remove) await onDelete(item.conversation_id);
        else await onArchive(item.conversation_id, !historyArchived);
        setNotice(
          remove
            ? "会话已删除。"
            : historyArchived
              ? "会话已恢复到历史列表。"
              : "会话已归档，可在“已归档”中恢复。",
        );
        historyToggleRef.current?.focus();
      } catch (failure) {
        if (failure instanceof PublicApiError && failure.status === 401)
          redirectToSso();
        else
          setError(
            failure instanceof PublicApiError && failure.status === 409
              ? "会话正在回答，请停止或等待回答结束后再操作。"
              : failure instanceof PublicApiError &&
                  [403, 404].includes(failure.status)
                ? "会话已删除或不可访问，请刷新历史列表。"
                : "暂时无法更新会话，请重试。",
          );
      } finally {
        setActionId(undefined);
      }
    },
    [
      busy,
      openingId,
      historyActionBusy,
      actionId,
      onArchive,
      onDelete,
      historyArchived,
    ],
  );
  const archiveConversation = useCallback(
    (item: PublicNaturalSession) => {
      void updateConversation(item, false);
    },
    [updateConversation],
  );
  const deleteConversation = useCallback(
    (item: PublicNaturalSession) => {
      void updateConversation(item, true);
    },
    [updateConversation],
  );

  return (
    <div
      className={`wst-app-layout${!expanded ? " is-sidebar-collapsed" : ""}${compact ? " is-compact" : ""}`}
    >
      {compact && (
        <button
          aria-hidden={!expanded}
          aria-label="关闭会话侧栏遮罩"
          className="wst-sidebar-backdrop"
          onClick={() => changeExpanded(false)}
          tabIndex={-1}
          type="button"
        />
      )}
      <aside
        aria-label="会话侧栏"
        aria-hidden={!expanded}
        aria-modal={compact && expanded ? true : undefined}
        inert={!expanded ? true : undefined}
        className="wst-conversation-sidebar"
        id={sidebarId}
        ref={panelRef}
        role={compact ? "dialog" : undefined}
      >
        <div className="wst-sidebar-heading">
          <span className="wst-sidebar-brand">
            <span aria-hidden="true">湾</span>湾小度
          </span>
          <button
            aria-label={compact ? "关闭侧栏" : "收起会话侧栏"}
            className="wst-sidebar-icon"
            onClick={() => changeExpanded(false)}
            ref={closeRef}
            title="收起侧栏"
            type="button"
          >
            <PanelLeftClose aria-hidden="true" size={19} />
          </button>
        </div>
        <button
          className="wst-sidebar-new"
          disabled={busy || historyActionBusy || !!actionId}
          onClick={newTopic}
          type="button"
        >
          <Plus aria-hidden="true" size={18} />
          新对话
        </button>
        {navigation.length > 0 && (
          <nav aria-label="工作区导航" className="wst-sidebar-navigation">
            {navigation.map((item) =>
              item.href ? (
                <a
                  key={item.label}
                  href={item.href}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {item.icon}
                  <span>{item.label}</span>
                </a>
              ) : (
                <button
                  key={item.label}
                  type="button"
                  aria-current={item.active ? "page" : undefined}
                  disabled={item.disabled}
                  onClick={() => {
                    item.onSelect?.();
                    if (compact) changeExpanded(false);
                  }}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </button>
              ),
            )}
          </nav>
        )}
        {historyEnabled && (
          <>
            <div className="wst-sidebar-search">
              <Search aria-hidden="true" size={16} />
              <input
                aria-label="搜索历史会话"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索会话标题"
                type="search"
                value={query}
              />
              {query && (
                <button
                  aria-label="清空会话搜索"
                  className="wst-sidebar-icon"
                  onClick={() => setQuery("")}
                  type="button"
                >
                  <X aria-hidden="true" size={15} />
                </button>
              )}
            </div>
            <div className="wst-sidebar-history-heading">
              <h2 className="wst-sidebar-label">
                {historyArchived ? "已归档" : "历史会话"}
              </h2>
              {onHistoryViewChange && (
                <button
                  aria-label={
                    historyArchived ? "返回历史会话" : "查看已归档会话"
                  }
                  aria-pressed={historyArchived}
                  className="wst-sidebar-archive-toggle"
                  disabled={historyActionBusy || !!actionId || !!openingId}
                  onClick={() => {
                    void changeHistoryView();
                  }}
                  ref={historyToggleRef}
                  type="button"
                >
                  <Archive aria-hidden="true" size={14} />
                  {historyArchived ? "返回历史" : "已归档"}
                </button>
              )}
            </div>
            {(error || historyError) && (
              <p className="wst-sidebar-error" role="alert">
                {error ?? historyError}
              </p>
            )}
            {notice && (
              <p className="wst-sidebar-footnote" role="status">
                {notice}
              </p>
            )}
            {(historyActionBusy || actionId) && (
              <p className="wst-sidebar-footnote" role="status">
                正在更新会话…
              </p>
            )}
            <nav
              aria-label="历史会话"
              aria-busy={
                !!openingId || historyLoading || historyActionBusy || !!actionId
              }
              className="wst-sidebar-history"
            >
              {historyLoading ? (
                <p className="wst-sidebar-empty" role="status">
                  正在加载会话…
                </p>
              ) : count === 0 ? (
                <p className="wst-sidebar-empty">
                  {keyword
                    ? "没有匹配的会话，试试其他关键词。"
                    : historyArchived
                      ? "还没有归档的会话。"
                      : "还没有历史会话，开始一次新对话吧。"}
                </p>
              ) : (
                [...groups].map(([label, items]) => (
                  <section className="wst-sidebar-group" key={label}>
                    <h3>{label}</h3>
                    {items.map((item) => (
                      <ConversationItem
                        item={item}
                        archived={historyArchived}
                        active={expanded}
                        selected={item.conversation_id === conversationId}
                        opening={openingId === item.conversation_id}
                        disabled={
                          busy || !!openingId || historyActionBusy || !!actionId
                        }
                        key={item.conversation_id}
                        onSelect={selectSession}
                        onArchive={onArchive ? archiveConversation : undefined}
                        onDelete={onDelete ? deleteConversation : undefined}
                      />
                    ))}
                  </section>
                ))
              )}
            </nav>
            <p className="wst-sidebar-footnote">
              {moreCount > 0
                ? `当前显示最近 ${sessions.length} 个会话，另有 ${moreCount} 个较早会话；搜索仅限当前列表。`
                : historyArchived
                  ? "归档不会删除问答，仍可打开继续追问。"
                  : "选择会话，即可继续追问。"}
            </p>
          </>
        )}
      </aside>
      <div
        aria-hidden={compact && expanded ? true : undefined}
        aria-busy={historyActionBusy || !!actionId}
        className="wst-main-pane"
        inert={
          (compact && expanded) || historyActionBusy || actionId
            ? true
            : undefined
        }
      >
        <header className="wst-app-header">
          <div className="wst-app-header-heading">
            <button
              aria-controls={sidebarId}
              aria-expanded={expanded}
              aria-label={expanded ? "收起会话侧栏" : "展开会话侧栏"}
              className="wst-sidebar-icon"
              hidden={expanded}
              onClick={() => changeExpanded(!expanded)}
              ref={triggerRef}
              type="button"
            >
              <PanelLeftOpen aria-hidden="true" size={20} />
            </button>
            {!expanded && (
              <button
                aria-label="新对话"
                className="wst-sidebar-icon"
                disabled={busy || historyActionBusy || !!actionId}
                onClick={newTopic}
                title="新对话"
                type="button"
              >
                <Plus aria-hidden="true" size={20} />
              </button>
            )}
            <span className="wst-app-view-label" title={viewLabel}>
              {viewLabel}
            </span>
          </div>
          {toolbar}
        </header>
        <div className="wst-content-pane">{children}</div>
      </div>
    </div>
  );
}
