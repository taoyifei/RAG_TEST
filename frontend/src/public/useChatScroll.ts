import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

export function useChatScroll(conversationId: string, turnCount: number) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const frameRef = useRef<number | undefined>(undefined);
  const [awayFromLatest, setAwayFromLatest] = useState(false);
  const jumpToLatest = useCallback(() => {
    followingRef.current = true;
    if (frameRef.current !== undefined) cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = undefined;
      setAwayFromLatest(false);
      const viewport = scrollRef.current;
      if (viewport)
        viewport.scrollTop = Math.max(
          0,
          viewport.scrollHeight - viewport.clientHeight,
        );
    });
  }, []);
  const onScroll = useCallback(() => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const nearBottom =
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
    followingRef.current = nearBottom;
    setAwayFromLatest(!nearBottom);
  }, []);
  useLayoutEffect(() => {
    // 切换会话或主动提交新问题后定位末尾；流式增量不重置用户上翻状态。
    jumpToLatest();
  }, [conversationId, turnCount, jumpToLatest]);
  useEffect(() => {
    if (!contentRef.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (followingRef.current) jumpToLatest();
    });
    observer.observe(contentRef.current);
    if (scrollRef.current) observer.observe(scrollRef.current);
    return () => observer.disconnect();
  }, [jumpToLatest]);
  useEffect(
    () => () => {
      if (frameRef.current !== undefined)
        cancelAnimationFrame(frameRef.current);
    },
    [],
  );
  return { scrollRef, contentRef, onScroll, awayFromLatest, jumpToLatest };
}
