import { ArrowUp, ChevronDown, Library, Square } from "lucide-react";
import { useId, useState, type KeyboardEvent } from "react";

import type { PublicSearchScope, PublicSearchScopeOption } from "./publicApi";

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
  const scopeId = useId();
  const send = () => {
    if (!question.trim() || busy) return;
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
        disabled={busy}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={compact ? 2 : 3}
        value={question}
      />
      <div className="wst-composer-footer">
        <div className="wst-search-scopes">
          <button
            aria-controls={scopeId}
            aria-expanded={scopeOpen}
            className="wst-search-scopes-trigger"
            disabled={busy || !onSearchScopesChange}
            onClick={() => setScopeOpen((value) => !value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setScopeOpen(false);
            }}
            type="button"
          >
            <Library aria-hidden="true" size={15} />
            <span>
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
                        const next = searchScopeOptions
                          .filter((item) =>
                            item.id === option.id
                              ? !checked
                              : searchScopes.includes(item.id),
                          )
                          .map((item) => item.id);
                        if (next.length) onSearchScopesChange?.(next);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") setScopeOpen(false);
                      }}
                      type="checkbox"
                    />
                    {option.label}
                  </label>
                );
              })}
              <p>至少选择一项；切换范围会开启新对话。</p>
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
      {scopeNotice && (
        <p className="wst-search-scopes-notice" role="status">
          {scopeNotice}
        </p>
      )}
    </div>
  );
}
