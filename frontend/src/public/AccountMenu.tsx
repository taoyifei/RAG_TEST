import { ChevronDown, LogOut, Moon, Sun, UserRound } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";

import { useDismissiblePopover } from "./useDismissiblePopover";

export function AccountMenu({
  name,
  theme,
  onThemeChange,
  onLogout,
}: {
  name?: string;
  theme: "dark" | "light";
  onThemeChange: () => void;
  onLogout?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const dismiss = useCallback(() => setOpen(false), []);
  const container = useDismissiblePopover<HTMLDivElement>(
    open,
    dismiss,
    trigger,
  );
  return (
    <div className="wst-account-menu" ref={container}>
      <button
        aria-label="账户与外观"
        aria-expanded={open}
        aria-controls={id}
        className="wst-account-trigger"
        onClick={() => setOpen((value) => !value)}
        ref={trigger}
        type="button"
      >
        <UserRound aria-hidden="true" size={17} />
        <span>{name ?? "外观设置"}</span>
        <ChevronDown aria-hidden="true" size={14} />
      </button>
      {open && (
        <div
          aria-label="账户与外观设置"
          className="wst-account-popover"
          id={id}
          role="group"
        >
          {name && <p>{name}</p>}
          <button
            aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
            onClick={onThemeChange}
            type="button"
          >
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            {theme === "dark" ? "浅色模式" : "深色模式"}
          </button>
          {onLogout && (
            <button aria-label="退出湾小度" onClick={onLogout} type="button">
              <LogOut aria-hidden="true" size={17} />
              退出登录
            </button>
          )}
        </div>
      )}
    </div>
  );
}
