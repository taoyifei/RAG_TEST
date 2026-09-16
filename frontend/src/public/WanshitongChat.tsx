import { PublicAnswer } from "./PublicAnswer";
import { PublicChatComposer } from "./PublicChatComposer";
import type { PublicTurn } from "./usePublicChat";

export function WanshitongChat({
  busy,
  onFeedback,
  onRetry,
  onStop,
  onSubmit,
  turns,
}: {
  busy: boolean;
  onFeedback: (turnId: string, traceId: string, useful: boolean) => void;
  onRetry: (turnId: string, question: string) => void;
  onStop: () => void;
  onSubmit: (question: string) => void;
  turns: PublicTurn[];
}) {
  return (
    <div className="wst-chat-shell">
      <header className="wst-chat-header">
        <strong>湾事通</strong>
      </header>
      <main className="wst-chat" id="main-content">
        <div className="wst-turns">
          {turns.map((turn) => (
            <article className="wst-turn" key={turn.id}>
              <div className="wst-question">
                <span className="sr-only">你问：</span>
                {turn.question}
              </div>
              <PublicAnswer
                onFeedback={(useful) => {
                  if (turn.traceId) onFeedback(turn.id, turn.traceId, useful);
                }}
                onRetry={() => onRetry(turn.id, turn.question)}
                turn={turn}
              />
            </article>
          ))}
        </div>
        <div className="wst-chat-composer">
          <PublicChatComposer
            busy={busy}
            compact
            onStop={onStop}
            onSubmit={onSubmit}
          />
        </div>
      </main>
    </div>
  );
}
