import { useEffect, useRef, useState } from "react";

import { redirectToSso } from "./authNavigation";
import { PublicApiError, type PublicNaturalSession } from "./publicApi";
import { ReadingOverlay } from "./ReadingOverlay";
import "./RenameConversationDialog.css";

export function RenameConversationDialog({
  item,
  onSave,
  onClose,
  returnFocusTo,
  returnFocusFallback,
}: {
  item: PublicNaturalSession;
  onSave: (title: string) => Promise<void>;
  onClose: () => void;
  returnFocusTo: HTMLElement | null;
  returnFocusFallback: () => HTMLElement | null;
}) {
  const [value, setValue] = useState(item.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const input = useRef<HTMLInputElement>(null);
  const running = useRef(false);
  const title = value.trim();
  useEffect(() => {
    input.current?.select();
  }, []);

  const close = () => {
    if (!running.current) onClose();
  };
  const save = async () => {
    if (running.current) return;
    if (!title || title.length > 120) {
      setError("请输入1到120个字符的会话名称。");
      return;
    }
    if (title === item.title) {
      onClose();
      return;
    }
    running.current = true;
    setSaving(true);
    setError(undefined);
    try {
      await onSave(title);
      onClose();
    } catch (failure) {
      if (failure instanceof PublicApiError && failure.status === 401) {
        redirectToSso();
      } else {
        setError(
          failure instanceof PublicApiError && failure.status === 409
            ? "会话正在回答，请等待回答结束后再修改名称。"
            : failure instanceof PublicApiError &&
                [403, 404].includes(failure.status)
              ? "该会话已删除或不可访问。"
              : "暂时无法保存名称，请重试。",
        );
      }
    } finally {
      running.current = false;
      setSaving(false);
    }
  };

  return (
    <ReadingOverlay
      kind="rename"
      label="重命名会话"
      modal
      onClose={close}
      returnFocusTo={returnFocusTo}
      returnFocusFallback={returnFocusFallback}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <h2>重命名会话</h2>
        <label htmlFor="wst-conversation-title">会话名称</label>
        <input
          aria-describedby={error ? "wst-rename-error" : "wst-rename-hint"}
          aria-invalid={Boolean(error)}
          autoComplete="off"
          disabled={saving}
          id="wst-conversation-title"
          maxLength={120}
          onChange={(event) => {
            setValue(event.target.value);
            setError(undefined);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && event.nativeEvent.isComposing)
              event.preventDefault();
          }}
          ref={input}
          value={value}
        />
        <p className="wst-rename-hint" id="wst-rename-hint">
          最多120个字符
        </p>
        {error && (
          <p className="wst-rename-error" id="wst-rename-error" role="alert">
            {error}
          </p>
        )}
        <div className="wst-rename-actions">
          <button disabled={saving} onClick={close} type="button">
            取消
          </button>
          <button
            className="is-primary"
            disabled={saving || !title || title.length > 120}
            type="submit"
          >
            {saving ? "正在保存…" : "保存"}
          </button>
        </div>
      </form>
    </ReadingOverlay>
  );
}
