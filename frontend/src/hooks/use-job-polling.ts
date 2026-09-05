import { useEffect } from "react";

export function useJobPolling(
  load: () => Promise<boolean | void>,
  onError: (error: unknown) => void = console.error,
): void {
  useEffect(() => {
    let timer = 0;
    let stopped = false;
    let complete = false;
    let inFlight = false;
    let delay = 1200;
    const poll = async () => {
      if (
        stopped ||
        complete ||
        inFlight ||
        document.visibilityState === "hidden"
      )
        return;
      inFlight = true;
      try {
        complete = (await load()) === false;
        delay = 1200;
      } catch (error) {
        onError(error);
        delay = Math.min(delay * 2, 15000);
      } finally {
        inFlight = false;
      }
      if (!stopped && !complete)
        timer = window.setTimeout(() => void poll(), delay);
    };
    const visible = () => {
      window.clearTimeout(timer);
      if (!complete) void poll();
    };
    document.addEventListener("visibilitychange", visible);
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [load, onError]);
}
