import { RotateCcw } from "lucide-react";

import { WanshitongChat } from "./WanshitongChat";
import { WanshitongHome } from "./WanshitongHome";
import { usePublicChat } from "./usePublicChat";

export function WanshitongApp() {
  const chat = usePublicChat();

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
          onStop={chat.stop}
          onSubmit={chat.submit}
          shortcuts={chat.shortcuts}
        />
      ) : (
        <WanshitongChat
          busy={chat.busy}
          onFeedback={chat.submitFeedback}
          onNewConversation={chat.newConversation}
          onRetry={chat.retry}
          onStop={chat.stop}
          onSubmit={chat.submit}
          turns={chat.turns}
        />
      )}
      <div aria-live="polite" className="sr-only" role="status">
        {chat.announcement}
      </div>
    </div>
  );
}
