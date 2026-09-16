import { PublicChatComposer } from "./PublicChatComposer";

export function WanshitongHome({
  busy,
  onStop,
  onSubmit,
}: {
  busy: boolean;
  onStop: () => void;
  onSubmit: (question: string) => void;
}) {
  return (
    <main className="wst-home" id="main-content">
      <div className="wst-home-content">
        <div className="wst-hero-copy">
          <span className="wst-wordmark">湾事通</span>
          <h1>你的内部知识助手</h1>
        </div>
        <PublicChatComposer busy={busy} onStop={onStop} onSubmit={onSubmit} />
      </div>
    </main>
  );
}
