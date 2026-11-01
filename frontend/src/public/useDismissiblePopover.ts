import { useEffect, useRef, type RefObject } from "react";

export function useDismissiblePopover<T extends HTMLElement>(
  open: boolean,
  onDismiss: () => void,
  triggerRef: RefObject<HTMLElement | null>,
  additionalInsideRefs?: readonly RefObject<HTMLElement | null>[],
) {
  const containerRef = useRef<T>(null);

  useEffect(() => {
    if (!open) return;

    const dismissOutside = (event: Event) => {
      if (
        event.target instanceof Node &&
        !containerRef.current?.contains(event.target) &&
        !additionalInsideRefs?.some((ref) =>
          ref.current?.contains(event.target as Node),
        )
      ) {
        onDismiss();
      }
    };
    const dismissEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onDismiss();
      triggerRef.current?.focus();
    };

    // 只收起浮层，不拦截外部点击，鼠标和触屏均可一次完成下一步操作。
    document.addEventListener("pointerdown", dismissOutside, true);
    document.addEventListener("focusin", dismissOutside, true);
    document.addEventListener("keydown", dismissEscape, true);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside, true);
      document.removeEventListener("focusin", dismissOutside, true);
      document.removeEventListener("keydown", dismissEscape, true);
    };
  }, [open, onDismiss, triggerRef, additionalInsideRefs]);

  return containerRef;
}
