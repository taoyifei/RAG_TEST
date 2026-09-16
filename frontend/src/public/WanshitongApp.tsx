import { RotateCcw } from "lucide-react";

import { WanshitongChat } from "./WanshitongChat";
import { WanshitongHome } from "./WanshitongHome";
import { usePublicChat } from "./usePublicChat";
import { useSuggestedQuestions } from "./useSuggestedQuestions";

export function WanshitongApp() {
  const chat = usePublicChat();
  const suggestions = useSuggestedQuestions(chat.turns);

  if (!chat.sessionReady) {
    return (
      <main className="wst-root wst-session-screen" id="main-content">
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
    <div className="wst-root">
      <a className="skip-link wst-skip-link" href="#main-content">
        跳到主要内容
      </a>
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
