import { useEffect, useRef } from "react";

const ACTIVE_DELAY = 1_200;
const MAX_DELAY = 15_000;

export function useJobPolling(load: () => Promise<void>): void {
  const delay = useRef(ACTIVE_DELAY);
  useEffect(() => {
    let timer = 0;
    let stopped = false;
    const poll = async () => {
      if (document.visibilityState === "hidden") {
        delay.current = Math.min(delay.current * 2, MAX_DELAY);
      } else {
        await load();
        delay.current = ACTIVE_DELAY;
      }
      if (!stopped) timer = window.setTimeout(() => void poll(), delay.current);
    };
    const visible = () => {
      if (document.visibilityState === "visible") delay.current = ACTIVE_DELAY;
    };
    document.addEventListener("visibilitychange", visible);
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [load]);
}
