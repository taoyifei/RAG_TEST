import { useCallback, useEffect, useRef, useState } from "react";

import {
  createPublicSession,
  getPublicCapabilities,
  logoutPublicSession,
  openPublicChat,
  PublicApiError,
  publicErrorMessage,
  sendPublicFeedback,
  type PublicFeedbackSubmission,
  type PublicSessionUser,
  type PublicUsageContext,
} from "./publicApi";
import * as authNavigation from "./authNavigation";
import {
  openPublicSessionChannel,
  recordPublicIdentity,
  type PublicSessionChannel,
} from "./sessionCoordination";
import {
  consumePublicSse,
  publicStageLabel,
  type PublicCitation,
  type PublicErrorEvent,
  type PublicStreamEvent,
} from "./publicSse";

export type PublicChatPhase =
  | "idle"
  | "creating_session"
  | "ready"
  | "submitting"
  | "streaming"
  | "completed"
  | "failed"
  | "cancelled";

export interface PublicClaim {
  claimIndex: number;
  sequence: number;
  text: string;
}

export interface PublicTurn {
  id: string;
  conversationId: string;
  question: string;
  status: "submitting" | "streaming" | "completed" | "failed" | "cancelled";
  stageMessage?: string;
  stageHistory: string[];
  startedAt: number;
  stageStartedAt: number;
  lastSignalAt: number;
  claims: PublicClaim[];
  answer?: string;
  citations: PublicCitation[];
  errorMessage?: string;
  partial: boolean;
  traceId?: string;
  feedback: "idle" | "submitting" | "sent" | "failed";
  feedbackUseful?: boolean;
  feedbackError?: string;
}

interface StreamTracker {
  claimCount: number;
  claimIndexes: Set<number>;
  lastSequence: number;
  terminal: boolean;
}

function randomId(prefix: string): string {
  const value = globalThis.crypto?.randomUUID?.();
  if (value) return `${prefix}-${value}`;
  const fallback = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${fallback}`;
}

function isSessionExpired(error: unknown): boolean {
  return (
    error instanceof PublicApiError &&
    (error.status === 401 || error.status === 403)
  );
}

function streamErrorMessage(event: PublicErrorEvent): string {
  if (event.code === "RATE_LIMITED") {
    return "当前使用人数较多，请稍后再试。";
  }
  if (event.code === "QUEUE_LIMIT_EXCEEDED") {
    return "当前服务较忙，请稍后再试。";
  }
  const message = event.user_message ?? event.message;
  if (typeof message === "string" && message.trim()) return message.trim();
  return "暂时无法完成回答，请稍后再试。";
}

function finalAnswerMessage(
  event: Extract<PublicStreamEvent, { type: "final" }>,
) {
  const answer = event.answer?.trim();
  if (answer) return answer;
  const userMessage = event.user_message?.trim();
  if (userMessage) return userMessage;
  const status = event.status ?? event.reason_code;
  const messages: Readonly<Record<string, string>> = {
    AMBIGUOUS_NEEDS_CLARIFICATION: "还需要补充一些信息，才能继续查找答案。",
    BUDGET_BLOCKED: "回答服务当前不可用，请稍后再试。",
    CONFIGURATION_REQUIRED: "回答服务尚未配置完成，请稍后再试。",
    INDEX_NOT_READY: "内部资料仍在准备中，请稍后再试。",
    INSUFFICIENT_EVIDENCE: "暂未在内部资料中找到足以回答这个问题的内容。",
    POLICY_DENIED: "当前问题无法通过公共问答处理。",
    PROVIDER_UNAVAILABLE: "回答服务暂时不可用，请稍后再试。",
  };
  return status
    ? (messages[status] ?? "暂未获得可核对的答复。")
    : "暂未获得可核对的答复。";
}

function resetTurn(turn: PublicTurn): PublicTurn {
  return {
    ...turn,
    status: "submitting",
    stageMessage: "正在提交问题",
    stageHistory: [],
    startedAt: Date.now(),
    stageStartedAt: Date.now(),
    lastSignalAt: Date.now(),
    claims: [],
    answer: undefined,
    citations: [],
    errorMessage: undefined,
    partial: false,
    traceId: undefined,
    feedback: "idle",
    feedbackError: undefined,
    feedbackUseful: undefined,
  };
}

export function usePublicChat() {
  const [phase, setPhase] = useState<PublicChatPhase>("idle");
  const [sessionReady, setSessionReady] = useState(false);
  const [sessionError, setSessionError] = useState<string>();
  const [logoutError, setLogoutError] = useState<string>();
  const [loggedOut, setLoggedOut] = useState(false);
  const [user, setUser] = useState<PublicSessionUser>();
  const [deploymentId, setDeploymentId] = useState<string>();
  const [feedbackDetailsEnabled, setFeedbackDetailsEnabled] = useState(false);
  const [usageContextEnabled, setUsageContextEnabled] = useState(false);
  const [turns, setTurns] = useState<PublicTurn[]>([]);
  const [conversationId, setConversationId] = useState(() => randomId("wst"));
  const csrfRef = useRef<string | undefined>(undefined);
  const conversationRef = useRef(conversationId);
  const sessionControllerRef = useRef<AbortController | undefined>(undefined);
  const streamControllerRef = useRef<AbortController | undefined>(undefined);
  const activeTurnRef = useRef<string | undefined>(undefined);
  const requestIdRef = useRef(0);
  const busyRef = useRef(false);
  const feedbackAttemptsRef = useRef(new Set<string>());
  const sessionChannelRef = useRef<PublicSessionChannel | undefined>(undefined);
  const identityChangedRef = useRef(false);

  const clearLocalSession = useCallback((showLoggedOut: boolean) => {
    requestIdRef.current += 1;
    busyRef.current = false;
    sessionControllerRef.current?.abort();
    streamControllerRef.current?.abort();
    sessionControllerRef.current = undefined;
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    csrfRef.current = undefined;
    const nextConversationId = randomId("wst");
    conversationRef.current = nextConversationId;
    setConversationId(nextConversationId);
    feedbackAttemptsRef.current.clear();
    setTurns([]);
    setSessionReady(false);
    setSessionError(undefined);
    setLogoutError(undefined);
    setUser(undefined);
    setFeedbackDetailsEnabled(false);
    setUsageContextEnabled(false);
    setLoggedOut(showLoggedOut);
    setPhase(showLoggedOut ? "idle" : "creating_session");
  }, []);

  const resetConversationState = useCallback((): string | undefined => {
    if (busyRef.current || !sessionReady) return undefined;
    requestIdRef.current += 1;
    streamControllerRef.current?.abort();
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    const nextConversationId = randomId("wst");
    conversationRef.current = nextConversationId;
    setConversationId(nextConversationId);
    feedbackAttemptsRef.current.clear();
    setTurns([]);
    setPhase("ready");
    return nextConversationId;
  }, [sessionReady]);

  const updateTurn = useCallback(
    (turnId: string, update: (turn: PublicTurn) => PublicTurn) => {
      setTurns((current) =>
        current.map((turn) => (turn.id === turnId ? update(turn) : turn)),
      );
    },
    [],
  );

  const initializeSession = useCallback(async (signal: AbortSignal) => {
    const session = await createPublicSession(signal);
    const capabilities = await getPublicCapabilities(signal);
    csrfRef.current = session.csrfToken;
    setUser(session.user);
    setDeploymentId(session.deploymentId);
    setFeedbackDetailsEnabled(capabilities.feedback_details === true);
    setUsageContextEnabled(capabilities.request_usage_context === true);
    setLoggedOut(false);
    if (session.deploymentId && session.user) {
      identityChangedRef.current = recordPublicIdentity(
        session.deploymentId,
        session.user.userId,
      );
    }
    setSessionReady(true);
    setSessionError(undefined);
    return session.csrfToken;
  }, []);

  const startSession = useCallback(() => {
    sessionControllerRef.current?.abort();
    const controller = new AbortController();
    sessionControllerRef.current = controller;
    setPhase("creating_session");
    setSessionError(undefined);
    void initializeSession(controller.signal)
      .then(() => {
        if (!controller.signal.aborted) setPhase("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (isSessionExpired(error)) {
          clearLocalSession(false);
          authNavigation.redirectToSso();
          return;
        }
        setSessionReady(false);
        setPhase("failed");
        setSessionError(
          error instanceof TypeError
            ? "暂时无法连接湾事通，请检查网络后重试。"
            : "湾事通暂时无法初始化，请重试。",
        );
      });
  }, [clearLocalSession, initializeSession]);

  useEffect(() => {
    startSession();
    const abortActive = () => streamControllerRef.current?.abort();
    window.addEventListener("pagehide", abortActive);
    return () => {
      requestIdRef.current += 1;
      busyRef.current = false;
      sessionControllerRef.current?.abort();
      streamControllerRef.current?.abort();
      window.removeEventListener("pagehide", abortActive);
    };
  }, [startSession]);

  useEffect(() => {
    if (!deploymentId) return;
    const channel = openPublicSessionChannel(deploymentId, (event) => {
      clearLocalSession(event === "logout");
      if (event === "account-changed") startSession();
    });
    sessionChannelRef.current = channel;
    if (identityChangedRef.current) {
      identityChangedRef.current = false;
      channel.publish("account-changed");
    }
    return () => {
      channel.close();
      if (sessionChannelRef.current === channel) {
        sessionChannelRef.current = undefined;
      }
    };
  }, [clearLocalSession, deploymentId, startSession, user?.userId]);

  const runTurn = useCallback(
    async (
      turnId: string,
      question: string,
      turnConversationId: string,
      retry: boolean,
      clientContext: PublicUsageContext,
    ) => {
      if (busyRef.current || !sessionReady) return;
      busyRef.current = true;
      activeTurnRef.current = turnId;
      const requestId = ++requestIdRef.current;
      const controller = new AbortController();
      streamControllerRef.current = controller;
      const tracker: StreamTracker = {
        claimCount: 0,
        claimIndexes: new Set(),
        lastSequence: -1,
        terminal: false,
      };
      if (retry) {
        updateTurn(turnId, resetTurn);
      } else {
        setTurns((current) => [
          ...current,
          {
            id: turnId,
            conversationId: turnConversationId,
            question,
            status: "submitting",
            stageMessage: "正在提交问题",
            stageHistory: [],
            startedAt: Date.now(),
            stageStartedAt: Date.now(),
            lastSignalAt: Date.now(),
            claims: [],
            citations: [],
            partial: false,
            feedback: "idle",
          },
        ]);
      }
      setPhase("submitting");

      const isCurrent = () => requestIdRef.current === requestId;
      try {
        const csrfToken = csrfRef.current;
        if (!csrfToken) {
          clearLocalSession(false);
          authNavigation.redirectToSso(question);
          return;
        }
        const response = await openPublicChat({
          conversationId: turnConversationId,
          csrfToken,
          question,
          signal: controller.signal,
          clientContext: usageContextEnabled ? clientContext : undefined,
        });
        if (!response.body) {
          throw new TypeError("public stream body missing");
        }
        if (!isCurrent()) return;
        setPhase("streaming");
        updateTurn(turnId, (turn) => ({
          ...turn,
          status: "streaming",
          stageMessage: "已收到问题",
          lastSignalAt: Date.now(),
        }));

        const handleEvent = (event: PublicStreamEvent): boolean => {
          if (
            !isCurrent() ||
            tracker.terminal ||
            event.sequence <= tracker.lastSequence
          ) {
            return !tracker.terminal;
          }
          tracker.lastSequence = event.sequence;
          const traceId =
            typeof event.trace_id === "string" ? event.trace_id : undefined;
          if (event.type === "meta") {
            if (traceId) {
              updateTurn(turnId, (turn) => ({ ...turn, traceId }));
            }
            return true;
          }
          if (event.type === "stage") {
            const stageLabel = publicStageLabel(event.stage);
            updateTurn(turnId, (turn) => ({
              ...turn,
              traceId: traceId ?? turn.traceId,
              stageMessage: stageLabel,
              stageStartedAt: Date.now(),
              lastSignalAt: Date.now(),
              stageHistory: turn.stageHistory.includes(stageLabel)
                ? turn.stageHistory
                : [...turn.stageHistory, stageLabel],
            }));
            return true;
          }
          if (event.type === "claim") {
            if (tracker.claimIndexes.has(event.claim_index)) return true;
            tracker.claimIndexes.add(event.claim_index);
            tracker.claimCount += 1;
            updateTurn(turnId, (turn) => ({
              ...turn,
              traceId: traceId ?? turn.traceId,
              claims: [
                ...turn.claims,
                {
                  claimIndex: event.claim_index,
                  sequence: event.sequence,
                  text: event.claim.text,
                },
              ].sort((left, right) => left.sequence - right.sequence),
            }));
            return true;
          }
          if (event.type === "final") {
            tracker.terminal = true;
            updateTurn(turnId, (turn) => ({
              ...turn,
              status: "completed",
              stageMessage: undefined,
              claims: [],
              answer: finalAnswerMessage(event),
              citations: event.citations,
              errorMessage: undefined,
              partial: false,
              traceId: traceId ?? turn.traceId,
            }));
            setPhase("completed");
            return false;
          }
          if (event.type === "error") {
            tracker.terminal = true;
            const partial = tracker.claimCount > 0 || event.partial === true;
            updateTurn(turnId, (turn) => ({
              ...turn,
              status: "failed",
              stageMessage: undefined,
              errorMessage: partial
                ? "回答未完成，以下内容不能作为最终结论"
                : streamErrorMessage(event),
              partial,
              traceId: traceId ?? turn.traceId,
            }));
            setPhase("failed");
            return false;
          }
          tracker.terminal = true;
          updateTurn(turnId, (turn) => ({
            ...turn,
            status: "cancelled",
            stageMessage: undefined,
            errorMessage: "已停止回答",
            partial: turn.claims.length > 0,
            traceId: traceId ?? turn.traceId,
          }));
          setPhase("cancelled");
          return false;
        };

        await consumePublicSse(
          response.body,
          handleEvent,
          controller.signal,
          () => {
            if (isCurrent() && !tracker.terminal) {
              updateTurn(turnId, (turn) => ({
                ...turn,
                lastSignalAt: Date.now(),
              }));
            }
          },
        );
        if (!tracker.terminal && isCurrent()) {
          throw new TypeError("public stream disconnected before terminal");
        }
      } catch (error) {
        if (!isCurrent() || controller.signal.aborted) return;
        if (isSessionExpired(error)) {
          clearLocalSession(false);
          authNavigation.redirectToSso(question);
          return;
        }
        tracker.terminal = true;
        updateTurn(turnId, (turn) => {
          const partial = turn.claims.length > 0;
          return {
            ...turn,
            status: "failed",
            stageMessage: undefined,
            errorMessage: partial
              ? "回答未完成，以下内容不能作为最终结论"
              : publicErrorMessage(error),
            partial,
          };
        });
        setPhase("failed");
      } finally {
        if (isCurrent()) {
          busyRef.current = false;
          streamControllerRef.current = undefined;
          activeTurnRef.current = undefined;
        }
      }
    },
    [clearLocalSession, sessionReady, updateTurn, usageContextEnabled],
  );

  const submit = useCallback(
    (question: string) => {
      const value = question.trim();
      if (!value || busyRef.current) return;
      void runTurn(randomId("turn"), value, conversationRef.current, false, {
        entrypoint: "manual",
      });
    },
    [runTurn],
  );

  const startNewTopic = useCallback(() => {
    resetConversationState();
  }, [resetConversationState]);

  const submitNewTopic = useCallback(
    (
      question: string,
      recommendationId?: string,
      entrypoint: "suggestion" | "popular" = "suggestion",
    ) => {
      const value = question.trim();
      if (!value) return;
      const nextConversationId = resetConversationState();
      if (!nextConversationId) return;
      void runTurn(randomId("turn"), value, nextConversationId, false, {
        entrypoint: recommendationId ? entrypoint : "manual",
        ...(recommendationId ? { recommendation_id: recommendationId } : {}),
      });
    },
    [resetConversationState, runTurn],
  );

  const retry = useCallback(
    (turnId: string, question: string, turnConversationId: string) => {
      if (busyRef.current || conversationRef.current !== turnConversationId)
        return;
      void runTurn(turnId, question, turnConversationId, true, {
        entrypoint: "retry",
      });
    },
    [runTurn],
  );

  const stop = useCallback(() => {
    const turnId = activeTurnRef.current;
    if (!busyRef.current || !turnId) return;
    requestIdRef.current += 1;
    streamControllerRef.current?.abort();
    busyRef.current = false;
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    updateTurn(turnId, (turn) => ({
      ...turn,
      status: "cancelled",
      stageMessage: undefined,
      errorMessage: "已停止回答",
      partial: turn.claims.length > 0,
    }));
    setPhase("cancelled");
  }, [updateTurn]);

  const submitFeedback = useCallback(
    (turnId: string, traceId: string, feedback: PublicFeedbackSubmission) => {
      const csrfToken = csrfRef.current;
      if (!csrfToken || feedbackAttemptsRef.current.has(turnId)) return;
      feedbackAttemptsRef.current.add(turnId);
      updateTurn(turnId, (turn) => {
        if (
          turn.feedback === "submitting" ||
          turn.feedback === "sent" ||
          !["completed", "failed"].includes(turn.status)
        )
          return turn;
        return {
          ...turn,
          feedback: "submitting",
          feedbackError: undefined,
          feedbackUseful: feedback.useful,
        };
      });
      void sendPublicFeedback({ csrfToken, traceId, feedback })
        .then(() => {
          updateTurn(turnId, (turn) => ({ ...turn, feedback: "sent" }));
        })
        .catch((error: unknown) => {
          feedbackAttemptsRef.current.delete(turnId);
          updateTurn(turnId, (turn) => ({
            ...turn,
            feedback: "failed",
            feedbackError: isSessionExpired(error)
              ? "登录会话已过期，请重新登录后再次提交。"
              : "反馈暂未提交成功，已保留你的选择和说明。",
          }));
        });
    },
    [updateTurn],
  );

  const logout = useCallback(() => {
    const csrfToken = csrfRef.current;
    if (!csrfToken || !user) return;
    const controller = new AbortController();
    setLogoutError(undefined);
    void logoutPublicSession(csrfToken, controller.signal)
      .then(() => {
        sessionChannelRef.current?.publish("logout");
        clearLocalSession(true);
      })
      .catch((error: unknown) => {
        if (isSessionExpired(error)) {
          sessionChannelRef.current?.publish("logout");
          clearLocalSession(true);
          return;
        }
        setLogoutError("暂时无法退出，请稍后重试。");
      });
  }, [clearLocalSession, user]);

  const busy =
    phase === "creating_session" ||
    phase === "submitting" ||
    phase === "streaming";
  const announcement =
    turns.at(-1)?.stageMessage ??
    turns.at(-1)?.errorMessage ??
    (phase === "creating_session" ? "正在连接湾事通" : "");

  return {
    announcement,
    busy,
    conversationId,
    deploymentId,
    feedbackDetailsEnabled,
    loggedOut,
    login: () => authNavigation.redirectToSso(),
    logout,
    logoutError,
    phase,
    retry,
    retrySession: startSession,
    sessionError,
    sessionReady,
    startNewTopic,
    stop,
    submit,
    submitNewTopic,
    submitFeedback,
    turns,
    user,
  };
}
