import { useId, useRef, useState } from "react";

import type { PublicNaturalSession } from "./publicApi";
import { useDismissiblePopover } from "./useDismissiblePopover";

export function PublicHistoryMenu({
  busy,
  sessions,
  moreCount,
  onSelect,
}: {
  busy: boolean;
  sessions: readonly PublicNaturalSession[];
  moreCount: number;
  onSelect: (conversationId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const containerRef = useDismissiblePopover<HTMLDivElement>(
    open,
    () => setOpen(false),
    triggerRef,
  );

  return (
    <div className="wst-history-menu" ref={containerRef}>
      <button
        aria-controls={listId}
        aria-expanded={open}
        className="wst-history-trigger"
        onClick={() => setOpen((value) => !value)}
        ref={triggerRef}
        type="button"
      >
        历史会话
      </button>
      {open && (
        <div className="wst-history-list" id={listId}>
          {sessions.map((item) => (
            <button
              disabled={busy}
              key={item.conversation_id}
              onClick={() => {
                setOpen(false);
                triggerRef.current?.focus();
                onSelect(item.conversation_id);
              }}
              type="button"
            >
              {item.title}
            </button>
          ))}
          {moreCount > 0 && (
            <p>当前仅显示最近 50 个会话，另有 {moreCount} 个较早会话。</p>
          )}
        </div>
      )}
    </div>
  );
}
