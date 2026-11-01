import { ArrowUp, ChevronDown, Library, Square } from "lucide-react";
import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import type { PublicSearchScope, PublicSearchScopeOption } from "./publicApi";
import { toggleSearchScope } from "./searchScopes";
import { useDismissiblePopover } from "./useDismissiblePopover";

export interface PublicSearchSelectionProps {
  searchScopes?: PublicSearchScope[];
  searchScopeOptions?: PublicSearchScopeOption[];
  scopeNotice?: string;
  onSearchScopesChange?: (scopes: PublicSearchScope[]) => void;
}

export function PublicChatComposer({
  busy,
  compact = false,
  initialQuestion = "",
  placeholder = "今天想了解什么？",
  searchScopes = ["public"],
  searchScopeOptions = [{ id: "public", label: "公共知识库" }],
  scopeNotice,
  onSearchScopesChange,
  onStop,
  onSubmit,
}: PublicSearchSelectionProps & {
  busy: boolean;
  compact?: boolean;
  initialQuestion?: string;
  placeholder?: string;
  onStop: () => void;
  onSubmit: (question: string) => void;
}) {
  const [question, setQuestion] = useState(initialQuestion);
  const [scopeOpen, setScopeOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 176)}px`;
  }, [question]);
  const scopeId = useId();
  const scopeTriggerRef = useRef<HTMLButtonElement>(null);
  const scopeContainerRef = useDismissiblePopover<HTMLDivElement>(
    scopeOpen,
    () => setScopeOpen(false),
    scopeTriggerRef,
  );
  const send = () => {
    if (!question.trim() || busy) return;
    setScopeOpen(false);
    onSubmit(question);
    setQuestion("");
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing
    ) {
      return;
    }
    event.preventDefault();
    send();
  };

  return (
    <div className={`wst-composer ${compact ? "is-compact" : ""}`}>
      <label
        className="sr-only"
        htmlFor={`wst-question-${compact ? "chat" : "home"}`}
      >
        向湾小度提问
      </label>
      <textarea
        id={`wst-question-${compact ? "chat" : "home"}`}
        aria-label="向湾小度提问"
        ref={textareaRef}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={1}
        value={question}
      />
      <div className="wst-composer-footer">
        <div className="wst-search-scopes" ref={scopeContainerRef}>
          <button
            aria-controls={scopeId}
            aria-expanded={scopeOpen}
            className="wst-search-scopes-trigger"
            disabled={busy || !onSearchScopesChange}
            onClick={() => setScopeOpen((value) => !value)}
            ref={scopeTriggerRef}
            type="button"
          >
            <Library aria-hidden="true" size={15} />
            <span
              title={searchScopeOptions
                .filter((option) => searchScopes.includes(option.id))
                .map((option) => option.label)
                .join(" + ")}
            >
              资料范围：
              {searchScopeOptions
                .filter((option) => searchScopes.includes(option.id))
                .map((option) => option.label)
                .join(" + ") || "公共知识库"}
            </span>
            <ChevronDown aria-hidden="true" size={14} />
          </button>
          {scopeOpen && (
            <fieldset className="wst-search-scopes-options" id={scopeId}>
              <legend className="sr-only">选择问答资料范围</legend>
              {searchScopeOptions.map((option) => {
                const checked = searchScopes.includes(option.id);
                return (
                  <label key={option.id}>
                    <input
                      checked={checked}
                      disabled={busy || (checked && searchScopes.length === 1)}
                      onChange={() => {
                        const next = toggleSearchScope(
                          searchScopes,
                          option.id,
                          searchScopeOptions,
                        );
                        if (next.length) onSearchScopesChange?.(next);
                      }}
                      type="checkbox"
                    />
                    {option.label}
                  </label>
                );
              })}
              <p>至少选择一项；切换范围会开启新对话。</p>
              {searchScopeOptions.some((option) => option.id === "hr_public") && (
                <p>公共知识库包含公开人力制度，也可单独选择制度缩小检索范围。</p>
              )}
            </fieldset>
          )}
        </div>
        {busy ? (
          <button className="wst-stop-button" onClick={onStop} type="button">
            <Square aria-hidden="true" size={15} />
            停止
          </button>
        ) : (
          <button
            aria-label="发送问题"
            className="wst-send-button"
            disabled={!question.trim()}
            onClick={send}
            type="button"
          >
            <ArrowUp aria-hidden="true" size={20} strokeWidth={2.4} />
          </button>
        )}
      </div>
      <p className="wst-composer-hint">
        {busy
          ? "可以先写下一问，当前回答结束后再发送"
          : "Enter 发送 · Shift + Enter 换行"}
      </p>
      {scopeNotice && (
        <p className="wst-search-scopes-notice" role="status">
          {scopeNotice}
        </p>
      )}
    </div>
  );
}
