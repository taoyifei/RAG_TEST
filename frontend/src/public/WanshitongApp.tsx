import { LogIn, LogOut, Moon, RotateCcw, Sun } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { consumeLoginDraft } from "./authNavigation";
import type { PublicPopularQuestion } from "./publicApi";
import type { SuggestedQuestion } from "./suggestedQuestions";
import { WanshitongChat } from "./WanshitongChat";
import { WanshitongHome } from "./WanshitongHome";
import { usePublicChat } from "./usePublicChat";
import { usePopularQuestions } from "./usePopularQuestions";
import { useSuggestedQuestions } from "./useSuggestedQuestions";

export function WanshitongApp() {
  const [restoredDraft, setRestoredDraft] = useState(consumeLoginDraft);
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
  const draftConversationRef = useRef(chat.conversationId);
  useEffect(() => {
    if (draftConversationRef.current === chat.conversationId) return;
    draftConversationRef.current = chat.conversationId;
    setRestoredDraft("");
  }, [chat.conversationId]);
  const suggestionIdentityKey =
    chat.deploymentId && chat.user
      ? `${chat.deploymentId}:${chat.user.userId}`
      : undefined;
  const suggestions = useSuggestedQuestions(chat.turns, suggestionIdentityKey);
  const popular = usePopularQuestions(
    chat.sessionReady,
    suggestionIdentityKey,
    chat.retrySession,
  );
  const submitSuggestedQuestion = (question: SuggestedQuestion) => {
    setRestoredDraft("");
    chat.submitRecommendation(question.question, question.id);
  };
  const submitCurrentQuestion = (question: string) => {
    setRestoredDraft("");
    chat.submit(question);
  };
  const submitPopularQuestion = (question: PublicPopularQuestion) => {
    setRestoredDraft("");
    chat.submitRecommendation(question.question, question.id, "popular");
  };
  const startNewTopic = () => {
    setRestoredDraft("");
    chat.startNewTopic();
  };
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
      {theme === "dark" ? (
        <Sun aria-hidden="true" size={17} />
      ) : (
        <Moon aria-hidden="true" size={17} />
      )}
      <span>{theme === "dark" ? "浅色" : "深色"}</span>
    </button>
  );

  if (chat.loggedOut) {
    return (
      <main
        className="wst-root wst-session-screen"
        data-theme={theme}
        id="main-content"
      >
        <div className="wst-toolbar">{themeToggle}</div>
        <div className="wst-session-card">
          <span className="wst-wordmark">湾事通</span>
          <h1>已退出</h1>
          <p>本机的湾事通会话已清理，RDMS 登录状态未改变。</p>
          <button onClick={chat.login} type="button">
            <LogIn aria-hidden="true" size={16} />
            重新登录
          </button>
        </div>
      </main>
    );
  }

  if (!chat.sessionReady) {
    return (
      <main
        className="wst-root wst-session-screen"
        data-theme={theme}
        id="main-content"
      >
        <div className="wst-toolbar">{themeToggle}</div>
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
      <div className="wst-toolbar">
        {chat.user ? (
          <>
            <span className="wst-account-name">{chat.user.displayName}</span>
            <button
              aria-label="退出湾事通"
              className="wst-session-action"
              onClick={() => {
                setRestoredDraft("");
                chat.logout();
              }}
              type="button"
            >
              <LogOut aria-hidden="true" size={16} />
              退出
            </button>
          </>
        ) : null}
        {themeToggle}
      </div>
      {chat.logoutError ? (
        <p className="wst-toolbar-error" role="alert">
          {chat.logoutError}
        </p>
      ) : null}
      {chat.turns.length === 0 ? (
        <WanshitongHome
          busy={chat.busy}
          conversationId={chat.conversationId}
          initialQuestion={restoredDraft}
          onRefresh={suggestions.refresh}
          onPopularQuestionSubmit={submitPopularQuestion}
          onSuggestedQuestionSubmit={submitSuggestedQuestion}
          onStop={chat.stop}
          onSubmit={submitCurrentQuestion}
          questions={suggestions.questions}
          popular={popular}
        />
      ) : (
        <WanshitongChat
          busy={chat.busy}
          conversationId={chat.conversationId}
          feedbackDetailsEnabled={chat.feedbackDetailsEnabled}
          initialQuestion={restoredDraft}
          onFeedback={chat.submitFeedback}
          onFeedbackLogin={chat.login}
          onNewTopic={startNewTopic}
          onPopularQuestionSubmit={submitPopularQuestion}
          onRefresh={suggestions.refresh}
          onRetry={chat.retry}
          onStop={chat.stop}
          onSubmit={submitCurrentQuestion}
          onSuggestedQuestionSubmit={submitSuggestedQuestion}
          questions={suggestions.questions}
          popular={popular}
          turns={chat.turns}
        />
      )}
      <div aria-live="polite" className="sr-only" role="status">
        {chat.announcement}
      </div>
    </div>
  );
}
