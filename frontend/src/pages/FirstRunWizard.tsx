import { KeyRound, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { ErrorPanel } from "../components/ui";
import { useConsole } from "../state/console-context";

function trapFocus(container: HTMLElement, event: KeyboardEvent) {
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    container.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  const first = focusable.at(0);
  const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function FirstRunWizard({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { login, session } = useConsole();
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) return;
    firstFieldRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && session.authenticated) onClose();
      if (dialogRef.current) trapFocus(dialogRef.current, event);
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose, open, session.authenticated]);
  if (!open) return null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await login(bootstrapToken);
      setBootstrapToken("");
      onClose();
    } catch (reason) {
      setError(reason);
      setBootstrapToken("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-backdrop" role="presentation">
      <section
        ref={dialogRef}
        className="modal onboarding"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
      >
        <header>
          <div>
            <span className="eyebrow">首次使用</span>
            <h2 id="onboarding-title">连接管理控制台</h2>
          </div>
          {session.authenticated && (
            <button className="icon-button" onClick={onClose} aria-label="关闭">
              <X aria-hidden="true" size={20} />
            </button>
          )}
        </header>
        <ol className="onboarding-steps">
          <li>输入部署人员提供的一次性管理口令。</li>
          <li>在“模型服务”中保存或引用服务凭据。</li>
          <li>创建知识库并应用检索方案。</li>
        </ol>
        <form className="stack" onSubmit={submit}>
          <label>
            管理口令
            <input
              ref={firstFieldRef}
              type="password"
              value={bootstrapToken}
              onChange={(event) => setBootstrapToken(event.target.value)}
              autoComplete="off"
              minLength={16}
              required
            />
          </label>
          <p className="muted">
            口令只用于交换 HttpOnly 会话 Cookie，不会写入浏览器存储。
          </p>
          {error !== undefined && <ErrorPanel error={error} />}
          <button className="primary" disabled={busy}>
            <KeyRound aria-hidden="true" size={17} />
            {busy ? "正在连接…" : "进入工作台"}
          </button>
        </form>
      </section>
    </div>
  );
}
