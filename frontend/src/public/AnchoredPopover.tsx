import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type AriaRole,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { useDismissiblePopover } from "./useDismissiblePopover";

type Side = "top" | "bottom";

interface Position {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  side: Side;
}

const FOCUSABLE =
  'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';

function focusableWithin(container: ParentNode): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (element) =>
      element.tabIndex >= 0 &&
      !element.matches(":disabled") &&
      !element.closest('[hidden], [inert], [aria-hidden="true"]') &&
      getComputedStyle(element).display !== "none" &&
      getComputedStyle(element).visibility !== "hidden",
  );
}

export interface AnchoredPopoverProps {
  open: boolean;
  onDismiss: () => void;
  triggerRef: RefObject<HTMLElement | null>;
  children: ReactNode;
  className?: string;
  id?: string;
  label?: string;
  role?: AriaRole;
  as?: "div" | "fieldset";
  width?: number;
  preferredSide?: Side;
  gap?: number;
}

/** 将浮层留在主题根节点内，同时避开聊天滚动区的裁剪。 */
export function AnchoredPopover({
  open,
  onDismiss,
  triggerRef,
  children,
  className,
  id,
  label,
  role,
  as: Element = "div",
  width = 304,
  preferredSide = "bottom",
  gap = 8,
}: AnchoredPopoverProps) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  const [position, setPosition] = useState<Position>();
  const insideRefs = useMemo(() => [triggerRef], [triggerRef]);
  const popoverRef = useDismissiblePopover<HTMLElement>(
    open,
    onDismiss,
    triggerRef,
    insideRefs,
  );
  const attachPopover = useCallback(
    (element: HTMLDivElement | HTMLFieldSetElement | null) => {
      popoverRef.current = element;
    },
    [popoverRef],
  );

  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    if (!trigger) return;
    // 实际产品树总有 .wst-root；body 仅兼容独立挂载的组件测试。
    setHost(
      trigger.closest<HTMLElement>(".wst-root") ?? trigger.ownerDocument.body,
    );
  }, [open, triggerRef]);

  useLayoutEffect(() => {
    if (!open || !host) return;
    const trigger = triggerRef.current;
    const popover = popoverRef.current;
    if (!trigger || !popover) return;
    const viewport = window.visualViewport;
    const update = () => {
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const padding = Math.min(12, viewportWidth / 2, viewportHeight / 2);
      const leftEdge = viewportLeft + padding;
      const rightEdge = viewportLeft + viewportWidth - padding;
      const topEdge = viewportTop + padding;
      const bottomEdge = viewportTop + viewportHeight - padding;
      const anchor = trigger.getBoundingClientRect();
      const naturalHeight = Math.max(
        popover.offsetHeight,
        popover.scrollHeight + popover.offsetHeight - popover.clientHeight,
      );
      const available = {
        top: Math.max(0, anchor.top - gap - topEdge),
        bottom: Math.max(0, bottomEdge - anchor.bottom - gap),
      };
      const opposite = preferredSide === "bottom" ? "top" : "bottom";
      const side =
        naturalHeight <= available[preferredSide]
          ? preferredSide
          : naturalHeight <= available[opposite]
            ? opposite
            : available[preferredSide] >= available[opposite]
              ? preferredSide
              : opposite;
      const resolvedWidth = Math.min(width, Math.max(1, rightEdge - leftEdge));
      const maxHeight = Math.min(available[side], bottomEdge - topEdge);
      const height = Math.min(naturalHeight, maxHeight);
      const next = {
        left: Math.max(
          leftEdge,
          Math.min(anchor.left, rightEdge - resolvedWidth),
        ),
        top: Math.max(
          topEdge,
          Math.min(
            side === "bottom" ? anchor.bottom + gap : anchor.top - gap - height,
            bottomEdge - height,
          ),
        ),
        width: resolvedWidth,
        maxHeight,
        side,
      };
      setPosition((previous) =>
        previous &&
        previous.left === next.left &&
        previous.top === next.top &&
        previous.width === next.width &&
        previous.maxHeight === next.maxHeight &&
        previous.side === next.side
          ? previous
          : next,
      );
    };
    let frame: number | undefined;
    const schedule = () => {
      if (frame !== undefined) return;
      frame = window.requestAnimationFrame(() => {
        frame = undefined;
        update();
      });
    };
    update();
    window.addEventListener("resize", schedule);
    document.addEventListener("scroll", schedule, true);
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    const observer =
      typeof ResizeObserver === "undefined"
        ? undefined
        : new ResizeObserver(schedule);
    observer?.observe(trigger);
    observer?.observe(popover);
    return () => {
      if (frame !== undefined) window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      document.removeEventListener("scroll", schedule, true);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      observer?.disconnect();
    };
  }, [open, host, triggerRef, popoverRef, width, preferredSide, gap]);

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const enterPopover = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Tab" || event.shiftKey) return;
      const first =
        popoverRef.current && focusableWithin(popoverRef.current)[0];
      if (!first) return;
      event.preventDefault();
      first.focus();
    };
    trigger?.addEventListener("keydown", enterPopover);
    return () => trigger?.removeEventListener("keydown", enterPopover);
  }, [open, triggerRef, popoverRef]);

  const handleTab = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab" || !popoverRef.current) return;
    const controls = focusableWithin(popoverRef.current);
    if (event.shiftKey && event.target === controls[0]) {
      event.preventDefault();
      triggerRef.current?.focus();
    } else if (!event.shiftKey && event.target === controls.at(-1)) {
      const trigger = triggerRef.current;
      if (!trigger) return;
      const outside = focusableWithin(trigger.ownerDocument).filter(
        (control) => !popoverRef.current?.contains(control),
      );
      const next = outside[outside.indexOf(trigger) + 1];
      onDismiss();
      if (next) {
        event.preventDefault();
        next.focus();
      }
    }
  };

  if (!open || !host) return null;
  return createPortal(
    <Element
      aria-label={label}
      className={`wst-anchored-popover${className ? ` ${className}` : ""}`}
      data-side={position?.side ?? preferredSide}
      id={id}
      onKeyDown={handleTab}
      ref={attachPopover}
      role={role}
      style={{
        position: "fixed",
        left: position?.left ?? 0,
        top: position?.top ?? 0,
        right: "auto",
        bottom: "auto",
        width: position?.width ?? width,
        maxWidth: "none",
        maxHeight: position?.maxHeight,
        minWidth: 0,
        margin: 0,
        boxSizing: "border-box",
        overflowY: "auto",
        overscrollBehavior: "contain",
        visibility: position ? "visible" : "hidden",
      }}
    >
      {children}
    </Element>,
    host,
  );
}
