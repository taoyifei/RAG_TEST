import { Moon, RotateCcw, Sun } from "lucide-react";
import { useState } from "react";

import { WanshitongChat } from "./WanshitongChat";
import { WanshitongHome } from "./WanshitongHome";
import { usePublicChat } from "./usePublicChat";
import { useSuggestedQuestions } from "./useSuggestedQuestions";

export function WanshitongApp() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      return window.localStorage.getItem("wanshitong-theme") === "light"
        ? "light"
        : "dark";
    } catch {
      return "dark";
    }
  });
  const chat = usePublicChat();
  const suggestions = useSuggestedQuestions(chat.turns);
  const themeToggle = (
    <button
      aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
      className="wst-theme-toggle"
      onClick={() => {
        const next = theme === "dark" ? "light" : "dark";
        setTheme(next);
        try {
          window.localStorage.setItem("wanshitong-theme", next);
        } catch {
          // 存储不可用时仍允许当前页面切换。
        }
      }}
      type="button"
    >
      {theme === "dark" ? <Sun aria-hidden="true" size={17} /> : <Moon aria-hidden="true" size={17} />}
      <span>{theme === "dark" ? "浅色" : "深色"}</span>
    </button>
  );

  if (!chat.sessionReady) {
    return (
      <main className="wst-root wst-session-screen" data-theme={theme} id="main-content">
        {themeToggle}
        <div className="wst-session-card">
          <span className="wst-wordmark">湾事通</span>
          {chat.sessionError ? (
            <>
              <h1>暂时无法打开公共问答</h1>
              <p role="alert">{chat.sessionError}</p>
              <button onClick={chat.retrySession} type="button">
                <RotateCcw aria-hidden="true" size={16} />
                重新连接
              </button>
            </>
          ) : (
            <>
              <h1>正在连接内部知识服务</h1>
              <p role="status">请稍候…</p>
            </>
          )}
        </div>
      </main>
    );
  }

  return (
    <div className="wst-root" data-theme={theme}>
      <a className="skip-link wst-skip-link" href="#main-content">
        跳到主要内容
      </a>
      {themeToggle}
      {chat.turns.length === 0 ? (
        <WanshitongHome
          busy={chat.busy}
          onRefresh={suggestions.refresh}
          onStop={chat.stop}
          onSubmit={chat.submit}
          questions={suggestions.questions}
        />
      ) : (
        <WanshitongChat
          busy={chat.busy}
          onFeedback={chat.submitFeedback}
          onRefresh={suggestions.refresh}
          onRetry={chat.retry}
          onStop={chat.stop}
          onSubmit={chat.submit}
          questions={suggestions.questions}
          turns={chat.turns}
        />
      )}
      <div aria-live="polite" className="sr-only" role="status">
        {chat.announcement}
      </div>
    </div>
  );
}
