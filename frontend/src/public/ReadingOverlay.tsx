import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import "./reading.css";

const CONTROLS =
  'button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function controlsWithin(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(CONTROLS)].filter(
    (element) =>
      !element.matches(":disabled") &&
      !element.closest("[hidden], [inert]") &&
      getComputedStyle(element).display !== "none" &&
      getComputedStyle(element).visibility !== "hidden",
  );
}

/** 阅读浮层共用主题、焦点和模态隔离，不创建额外资源入口。 */
export function ReadingOverlay({
  children,
  kind,
  label,
  modal,
  onClose,
  returnFocusTo,
  returnFocusFallback,
  suspended = false,
}: {
  children: ReactNode;
  kind: "reference" | "image";
  label: string;
  modal: boolean;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  returnFocusFallback?: () => HTMLElement | null;
  suspended?: boolean;
}) {
  const [host] = useState(() =>
    typeof document === "undefined"
      ? null
      : (returnFocusTo?.closest<HTMLElement>(".wst-root") ??
        document.querySelector<HTMLElement>(".wst-root") ??
        document.body),
  );
  const layerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  const modalRef = useRef(modal);
  const suspendedRef = useRef(suspended);
  const restoreFocus = useRef(true);
  const fallbackFocusRef = useRef(returnFocusFallback);

  useLayoutEffect(() => {
    closeRef.current = onClose;
    modalRef.current = modal;
    suspendedRef.current = suspended;
    fallbackFocusRef.current = returnFocusFallback;
  }, [onClose, modal, suspended, returnFocusFallback]);
  useEffect(() => {
    if (!host) return;
    const panel = panelRef.current;
    const previous = returnFocusTo ?? document.activeElement;
    if (!panel) return;
    const focusFirst = () => (controlsWithin(panel)[0] ?? panel).focus();
    focusFirst();
    const outside = (event: Event) => {
      if (suspendedRef.current) return;
      if (!(event.target instanceof Node) || panel.contains(event.target))
        return;
      if (modalRef.current) return;
      restoreFocus.current = false;
      closeRef.current();
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || suspendedRef.current) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        restoreFocus.current = true;
        closeRef.current();
        return;
      }
      if (
        event.key !== "Tab" ||
        !modalRef.current ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey
      ) {
        return;
      }
      const controls = controlsWithin(panel);
      if (!controls.length) {
        event.preventDefault();
        panel.focus();
      } else if (
        event.shiftKey &&
        (document.activeElement === controls[0] ||
          document.activeElement === panel)
      ) {
        event.preventDefault();
        controls.at(-1)?.focus();
      } else if (
        !event.shiftKey &&
        document.activeElement === controls.at(-1)
      ) {
        event.preventDefault();
        controls[0].focus();
      }
    };
    const handleFocus = (event: FocusEvent) => {
      if (suspendedRef.current) return;
      if (
        modalRef.current &&
        event.target instanceof Node &&
        !panel.contains(event.target)
      ) {
        focusFirst();
      } else {
        outside(event);
      }
    };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", handleFocus, true);
    document.addEventListener("keydown", handleKey, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", handleFocus, true);
      document.removeEventListener("keydown", handleKey, true);
      // 等其他清理恢复 inert 后再还焦点；外部点击继续完成自己的动作。
      if (restoreFocus.current) {
        queueMicrotask(() => {
          const target =
            previous instanceof HTMLElement &&
            previous.isConnected &&
            !previous.closest("[inert]")
              ? previous
              : fallbackFocusRef.current?.();
          if (target?.isConnected && !target.closest("[inert]")) {
            target.focus({ preventScroll: true });
          }
        });
      }
    };
  }, [host, returnFocusTo]);

  useEffect(() => {
    // 查看大图时，来源层暂停隔离和键盘处理；由图片层独占一次滚动/焦点锁。
    if (!host || !modal || suspended) return;
    const previousOverflow = document.body.style.overflow;
    const siblings = [...host.children].filter(
      (element): element is HTMLElement =>
        element instanceof HTMLElement && element !== layerRef.current,
    );
    const priorInert = siblings.map((element) => element.hasAttribute("inert"));
    document.body.style.overflow = "hidden";
    siblings.forEach((element) => {
      element.setAttribute("inert", "");
    });
    return () => {
      document.body.style.overflow = previousOverflow;
      siblings.forEach((element, index) => {
        if (!priorInert[index]) element.removeAttribute("inert");
      });
    };
  }, [host, modal, suspended]);

  if (!host) return null;
  return createPortal(
    <div
      className={`wst-reading-layer wst-${kind}-reading-layer${modal ? " is-modal" : ""}`}
      ref={layerRef}
    >
      {modal && (
        <button
          aria-label={`关闭${kind === "image" ? "图片预览" : "来源阅读"}遮罩`}
          className="wst-reading-backdrop"
          onClick={onClose}
          tabIndex={-1}
          type="button"
        />
      )}
      <section
        aria-label={label}
        aria-modal={(!suspended && modal) || undefined}
        aria-hidden={suspended || undefined}
        className={`wst-reading-panel wst-${kind}-panel`}
        ref={panelRef}
        role="dialog"
        inert={suspended || undefined}
        tabIndex={-1}
      >
        {children}
      </section>
    </div>,
    host,
  );
}
