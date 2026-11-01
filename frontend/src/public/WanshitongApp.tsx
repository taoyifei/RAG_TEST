import {
  Files,
  LogIn,
  MessageCircle,
  Moon,
  RotateCcw,
  Sparkles,
  Sun,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { withAppBase } from "../app/basePath";
import { consumeLoginDraft } from "./authNavigation";
import { AccountMenu } from "./AccountMenu";
import { LibraryWorkspace } from "./LibraryWorkspace";
import { WorkbenchWorkspace } from "./WorkbenchWorkspace";
import {
  PublicConversationLayout,
  type PublicNavigationItem,
} from "./PublicConversationLayout";
import { UserRolesWorkspace } from "./UserRolesWorkspace";
import type { PublicPopularQuestion } from "./publicApi";
import type { SuggestedQuestion } from "./suggestedQuestions";
import { WanshitongChat } from "./WanshitongChat";
import { WanshitongHome } from "./WanshitongHome";
import { usePublicChat } from "./usePublicChat";
import { usePopularQuestions } from "./usePopularQuestions";
import { useSuggestedQuestions } from "./useSuggestedQuestions";

export function WanshitongApp() {
  const [restoredDraft, setRestoredDraft] = useState(consumeLoginDraft);
  const [view, setView] = useState(
    () => new URLSearchParams(window.location.search).get("view") ?? "chat",
  );
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
  const { startNewTopic: resetTopic, openHistory: openHistorySession } = chat;
  const role = chat.user?.role ?? "user";
  useEffect(() => {
    document.title = "湾小度";
    const onLocation = () =>
      setView(
        new URLSearchParams(window.location.search).get("view") ?? "chat",
      );
    window.addEventListener("popstate", onLocation);
    return () => window.removeEventListener("popstate", onLocation);
  }, []);
  const openView = useCallback(
    (next: "chat" | "library" | "users" | "workbench") => {
      if (next === view) return;
      const url = new URL(window.location.href);
      if (next === "chat") url.searchParams.delete("view");
      else url.searchParams.set("view", next);
      window.history.pushState({}, "", `${url.pathname}${url.search}`);
      setView(next);
    },
    [view],
  );
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
  const popular = usePopularQuestions(
    chat.sessionReady && chat.pageSettings.show_recommendations,
    suggestionIdentityKey,
    chat.retrySession,
  );
  const approvedSuggestions = useMemo<readonly SuggestedQuestion[]>(
    () =>
      popular.items.map((item) => ({
        id: item.id,
        question: item.question,
        documentId: item.id,
        style: "SHORT",
        topicKey: item.topic_key,
        enabled: true,
      })),
    [popular.items],
  );
  const suggestions = useSuggestedQuestions(
    chat.turns,
    suggestionIdentityKey,
    approvedSuggestions,
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
  const startNewTopic = useCallback(() => {
    setRestoredDraft("");
    resetTopic();
  }, [resetTopic]);
  const selectConversation = useCallback(
    async (id: string) => {
      openView("chat");
      await openHistorySession(id);
    },
    [openView, openHistorySession],
  );
  const newConversation = useCallback(() => {
    openView("chat");
    startNewTopic();
  }, [openView, startNewTopic]);
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      window.localStorage.setItem("wanshitong-theme", next);
    } catch {
      /* 当前页面仍可切换。 */
    }
  };
  const navigation: PublicNavigationItem[] = [
    {
      label: "知识问答",
      icon: <MessageCircle size={17} aria-hidden="true" />,
      active: view === "chat",
      disabled: chat.busy,
      onSelect: () => openView("chat"),
    },
    {
      label: "我的资料",
      icon: <Files size={17} aria-hidden="true" />,
      active: view === "library",
      disabled: chat.busy,
      onSelect: () => openView("library"),
    },
    ...(chat.workbenchEnabled
      ? [
          {
            label: "工作助手",
            icon: <Sparkles size={17} aria-hidden="true" />,
            active: view === "workbench",
            disabled: chat.busy,
            onSelect: () => openView("workbench"),
          },
        ]
      : []),
    ...(role === "admin"
      ? [
          {
            label: "用户与角色",
            icon: <Users size={17} aria-hidden="true" />,
            active: view === "users",
            disabled: chat.busy,
            onSelect: () => openView("users"),
          },
          {
            label: "管理后台",
            icon: <Files size={17} aria-hidden="true" />,
            href: withAppBase("/admin/overview"),
          },
        ]
      : []),
  ];
  const themeToggle = (
    <button
      aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
      className="wst-theme-toggle"
      onClick={toggleTheme}
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
          <span className="wst-wordmark">湾小度</span>
          <h1>已退出</h1>
          <p>本机的湾小度会话已清理，RDMS 登录状态未改变。</p>
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
          <span className="wst-wordmark">湾小度</span>
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
      <PublicConversationLayout
        busy={chat.busy}
        conversationId={view === "chat" ? chat.conversationId : undefined}
        historyEnabled={chat.pageSettings.show_history}
        sessions={chat.historySessions}
        moreCount={chat.historyMoreCount}
        historyArchived={chat.historyArchived}
        historyLoading={chat.historyLoading}
        historyActionBusy={chat.historyActionBusy}
        historyError={chat.historyError}
        onHistoryViewChange={chat.changeHistoryView}
        onArchive={chat.archiveHistory}
        onDelete={chat.deleteHistory}
        onNewTopic={newConversation}
        onSelect={selectConversation}
        viewLabel={
          view === "library"
            ? "我的资料"
            : view === "workbench" && chat.workbenchEnabled
              ? "工作助手"
              : view === "users" && role === "admin"
                ? "用户与角色"
                : (chat.historySessions.find(
                    (item) => item.conversation_id === chat.conversationId,
                  )?.title ?? "知识问答")
        }
        navigation={navigation}
        toolbar={
          <AccountMenu
            name={chat.user?.displayName}
            theme={theme}
            onThemeChange={toggleTheme}
            onLogout={
              chat.user
                ? () => {
                    setRestoredDraft("");
                    chat.logout();
                  }
                : undefined
            }
          />
        }
      >
        {chat.logoutError ? (
          <p className="wst-toolbar-error" role="alert">
            {chat.logoutError}
          </p>
        ) : null}
        {view === "workbench" && chat.workbenchEnabled ? (
          <WorkbenchWorkspace
            key={`${chat.deploymentId}:${chat.user?.userId}`}
            getCsrfToken={chat.getCsrfToken}
            scopeOptions={chat.searchScopeOptions}
            theme={theme}
          />
        ) : view === "library" ? (
          <LibraryWorkspace
            userRole={role}
            isSuperAdmin={chat.user?.isSuperAdmin === true}
            getCsrfToken={chat.getCsrfToken}
            onBack={() => openView("chat")}
          />
        ) : view === "users" && role === "admin" ? (
          <UserRolesWorkspace
            getCsrfToken={chat.getCsrfToken}
            onBack={() => openView("chat")}
          />
        ) : chat.turns.length === 0 ? (
          <WanshitongHome
            busy={chat.busy}
            conversationId={chat.conversationId}
            initialQuestion={restoredDraft}
            placeholder={chat.pageSettings.input_placeholder}
            searchScopes={chat.searchScopes}
            searchScopeOptions={chat.searchScopeOptions}
            scopeNotice={chat.scopeNotice}
            onSearchScopesChange={chat.changeSearchScopes}
            welcomeText={chat.pageSettings.welcome_text}
            showRecommendations={
              chat.pageSettings.show_recommendations &&
              chat.searchScopes.includes("public")
            }
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
            allowFeedback={chat.pageSettings.allow_feedback}
            allowSourceDownload={chat.pageSettings.allow_source_download}
            placeholder={chat.pageSettings.input_placeholder}
            searchScopes={chat.searchScopes}
            searchScopeOptions={chat.searchScopeOptions}
            scopeNotice={chat.scopeNotice}
            onSearchScopesChange={chat.changeSearchScopes}
            showRecommendations={
              chat.pageSettings.show_recommendations &&
              chat.searchScopes.includes("public")
            }
            initialQuestion={restoredDraft}
            onFeedback={chat.submitFeedback}
            onFeedbackLogin={chat.login}
            onDownloadReference={chat.downloadReference}
            onPopularQuestionSubmit={submitPopularQuestion}
            onRefresh={suggestions.refresh}
            onRetry={chat.retry}
            onRecover={chat.recover}
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
      </PublicConversationLayout>
    </div>
  );
}
