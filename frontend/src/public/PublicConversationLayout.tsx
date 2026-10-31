import {
  MessageCircle,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  X,
} from "lucide-react";
import {
  useEffect,
  useCallback,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { redirectToSso } from "./authNavigation";
import { PublicApiError, type PublicNaturalSession } from "./publicApi";

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

function ConversationItem({
  item,
  selected,
  disabled,
  opening,
  onSelect,
}: {
  item: PublicNaturalSession;
  selected: boolean;
  disabled: boolean;
  opening: boolean;
  onSelect: (id: string) => Promise<void>;
}) {
  return (
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
      <MessageCircle aria-hidden="true" size={15} />
      <span>{item.title}</span>
      {opening && <small role="status">打开中…</small>}
    </button>
  );
}

export function PublicConversationLayout({
  busy,
  conversationId,
  historyEnabled,
  sessions,
  moreCount,
  onNewTopic,
  onSelect,
  toolbar,
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
  toolbar: ReactNode;
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
  const requestRef = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const focusFrameRef = useRef<number | undefined>(undefined);
  const focusComposerOnCloseRef = useRef(false);
  const sidebarId = useId();
  const expanded = compact ? drawerOpen : !collapsed;
  const today = new Date();
  const keyword = query.trim().toLocaleLowerCase();
  const groups = new Map<string, PublicNaturalSession[]>();
  const filtered = sessions.filter((item) =>
    item.title.toLocaleLowerCase().includes(keyword),
  );
  for (const item of [...filtered].sort(
    (a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0),
  )) {
    const group = dateGroup(item.updated_at, today);
    groups.set(group, [...(groups.get(group) ?? []), item]);
  }

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
    if (busy) return;
    requestRef.current += 1;
    setOpeningId(undefined);
    setError(undefined);
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
      if (busy || openingId) return;
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
    [busy, openingId, onSelect, compact, changeExpanded, focusComposer],
  );

  return (
    <div
      className={`wst-app-layout${!expanded ? " is-sidebar-collapsed" : ""}${compact ? " is-compact" : ""}`}
    >
      {compact && expanded && (
        <button
          aria-label="关闭会话侧栏遮罩"
          className="wst-sidebar-backdrop"
          onClick={() => changeExpanded(false)}
          tabIndex={-1}
          type="button"
        />
      )}
      {expanded && (
        <aside
          aria-label="会话侧栏"
          aria-modal={compact ? true : undefined}
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
              aria-label="关闭侧栏"
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
            disabled={busy}
            onClick={newTopic}
            type="button"
          >
            <Plus aria-hidden="true" size={18} />
            新对话
          </button>
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
              <h2 className="wst-sidebar-label">历史会话</h2>
              {error && (
                <p className="wst-sidebar-error" role="alert">
                  {error}
                </p>
              )}
              <nav
                aria-label="历史会话"
                aria-busy={!!openingId}
                className="wst-sidebar-history"
              >
                {filtered.length === 0 ? (
                  <p className="wst-sidebar-empty">
                    {keyword
                      ? "没有匹配的会话，试试其他关键词。"
                      : "还没有历史会话，开始一次新对话吧。"}
                  </p>
                ) : (
                  [...groups].map(([label, items]) => (
                    <section className="wst-sidebar-group" key={label}>
                      <h3>{label}</h3>
                      {items.map((item) => (
                        <ConversationItem
                          item={item}
                          selected={item.conversation_id === conversationId}
                          opening={openingId === item.conversation_id}
                          disabled={busy || !!openingId}
                          key={item.conversation_id}
                          onSelect={selectSession}
                        />
                      ))}
                    </section>
                  ))
                )}
              </nav>
              <p className="wst-sidebar-footnote">
                {moreCount > 0
                  ? `当前显示最近 ${sessions.length} 个会话，另有 ${moreCount} 个较早会话；搜索仅限当前列表。`
                  : "选择会话，即可继续追问。"}
              </p>
            </>
          )}
        </aside>
      )}
      <div
        aria-hidden={compact && expanded ? true : undefined}
        className="wst-main-pane"
        inert={compact && expanded ? true : undefined}
      >
        <header className="wst-app-header">
          <div className="wst-app-header-heading">
            <button
              aria-controls={sidebarId}
              aria-expanded={expanded}
              aria-label={expanded ? "收起会话侧栏" : "展开会话侧栏"}
              className="wst-sidebar-icon"
              onClick={() => changeExpanded(!expanded)}
              ref={triggerRef}
              type="button"
            >
              {expanded ? (
                <PanelLeftClose aria-hidden="true" size={20} />
              ) : (
                <PanelLeftOpen aria-hidden="true" size={20} />
              )}
            </button>
            {!expanded && (
              <button
                aria-label="新对话"
                className="wst-sidebar-icon"
                disabled={busy}
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
        {children}
      </div>
    </div>
  );
}
