import { useEffect, useState } from "react";

import {
  getPopularQuestions,
  PublicApiError,
  type PublicPopularQuestions,
} from "./publicApi";

const EMPTY: PublicPopularQuestions = {
  mode: "EMPTY",
  generated_at: null,
  window_days: 7,
  items: [],
};

export function usePopularQuestions(
  sessionReady: boolean,
  identityKey: string | undefined,
  onSessionExpired: () => void,
): PublicPopularQuestions {
  const [loaded, setLoaded] = useState<{
    identityKey: string | undefined;
    value: PublicPopularQuestions;
  }>();

  useEffect(() => {
    if (!sessionReady) return;
    const controller = new AbortController();
    void getPopularQuestions(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setLoaded({ identityKey, value: result });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({ identityKey, value: EMPTY });
        if (
          error instanceof PublicApiError &&
          (error.status === 401 || error.status === 403)
        ) {
          onSessionExpired();
        }
      });
    return () => controller.abort();
  }, [sessionReady, identityKey, onSessionExpired]);

  return sessionReady && loaded !== undefined && loaded.identityKey === identityKey
    ? loaded.value
    : EMPTY;
}
