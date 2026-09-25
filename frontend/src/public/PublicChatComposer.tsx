import { ArrowUp, Square } from "lucide-react";
import { useState, type KeyboardEvent } from "react";

const PLACEHOLDER = "今天想了解什么？";

export function PublicChatComposer({
  busy,
  compact = false,
  initialQuestion = "",
  onStop,
  onSubmit,
}: {
  busy: boolean;
  compact?: boolean;
  initialQuestion?: string;
  onStop: () => void;
  onSubmit: (question: string) => void;
}) {
  const [question, setQuestion] = useState(initialQuestion);
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
        向湾事通提问
      </label>
      <textarea
        id={`wst-question-${compact ? "chat" : "home"}`}
        aria-label="向湾事通提问"
        disabled={busy}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={PLACEHOLDER}
        rows={compact ? 2 : 3}
        value={question}
      />
      <div className="wst-composer-footer">
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
    </div>
  );
}
