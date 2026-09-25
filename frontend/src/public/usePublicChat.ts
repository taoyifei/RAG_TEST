import { useCallback, useEffect, useRef, useState } from "react";

import {
  createPublicSession,
  continuePublicNaturalChat,
  getPublicNaturalConversation,
  getPublicNaturalSessions,
  getPublicNaturalSource,
  getPublicCapabilities,
  logoutPublicSession,
  openPublicChat,
  PublicApiError,
  publicErrorMessage,
  sendPublicFeedback,
  stopPublicNaturalChat,
  type PublicFeedbackSubmission,
  type PublicSessionUser,
  type PublicUsageContext,
  type PublicNaturalHistoryTurn,
  type PublicNaturalSession,
  type PublicNaturalActiveRun,
} from "./publicApi";
import * as authNavigation from "./authNavigation";
import {
  openPublicSessionChannel,
  recordPublicIdentity,
  type PublicSessionChannel,
} from "./sessionCoordination";
import {
  consumePublicSse,
  PublicSseParseError,
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
  publicationStatus?: string;
  naturalProtocol?: "wanshitong-natural-sse-v1" | "wanshitong-natural-sse-v2";
  provisionalAnswer?: string;
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
  deltaCount: number;
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
    publicationStatus: undefined,
    provisionalAnswer: undefined,
    citations: [],
    errorMessage: undefined,
    partial: false,
    traceId: undefined,
    feedback: "idle",
    feedbackError: undefined,
    feedbackUseful: undefined,
  };
}

function restoreNaturalTurn(
  conversationId: string,
  item: PublicNaturalHistoryTurn,
): PublicTurn {
  const startedAt = Date.parse(item.created_at) || Date.now();
  return {
    id: item.turn_id,
    conversationId,
    question: item.question,
    publicationStatus: item.publication_status ?? undefined,
    status: "completed",
    stageHistory: [],
    startedAt,
    stageStartedAt: startedAt,
    lastSignalAt: startedAt,
    claims: [],
    answer:
      item.answer ??
      (item.status === "SOURCE_UNAVAILABLE"
        ? "原引用资料已变化，这条历史回答暂不可展示。"
        : item.publication_status === "SOURCE_CLARIFICATION"
          ? "请明确要查询的资料或对象后重试。"
        : item.publication_status === "INSUFFICIENT_EVIDENCE"
          ? "当前资料不足以形成可核对的答复。"
        : "暂未找到可以回答该问题的资料。"),
    citations: item.citations,
    partial: false,
    traceId: item.trace_id,
    feedback: "idle",
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
  const [naturalProtocol, setNaturalProtocol] = useState<
    "wanshitong-natural-sse-v1" | "wanshitong-natural-sse-v2" | undefined
  >();
  const [pendingResume, setPendingResume] = useState<PublicNaturalActiveRun>();
  const [turns, setTurns] = useState<PublicTurn[]>([]);
  const [historySessions, setHistorySessions] = useState<PublicNaturalSession[]>([]);
  const [conversationId, setConversationId] = useState(() => randomId("wst"));
  const csrfRef = useRef<string | undefined>(undefined);
  const conversationRef = useRef(conversationId);
  const sessionControllerRef = useRef<AbortController | undefined>(undefined);
  const streamControllerRef = useRef<AbortController | undefined>(undefined);
  const activeTurnRef = useRef<string | undefined>(undefined);
  const activeTraceIdRef = useRef<string | undefined>(undefined);
  const requestIdRef = useRef(0);
  const busyRef = useRef(false);
  const stopPendingRef = useRef(false);
  const feedbackAttemptsRef = useRef(new Set<string>());
  const sessionChannelRef = useRef<PublicSessionChannel | undefined>(undefined);
  const identityChangedRef = useRef(false);
  const historyKeyRef = useRef<string | undefined>(undefined);

  const clearLocalSession = useCallback((showLoggedOut: boolean) => {
    requestIdRef.current += 1;
    busyRef.current = false;
    stopPendingRef.current = false;
    setPendingResume(undefined);
    sessionControllerRef.current?.abort();
    streamControllerRef.current?.abort();
    sessionControllerRef.current = undefined;
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    activeTraceIdRef.current = undefined;
    csrfRef.current = undefined;
    if (historyKeyRef.current) {
      try {
        window.sessionStorage.removeItem(historyKeyRef.current);
      } catch {
        // 浏览器禁用存储时不影响服务端会话授权。
      }
    }
    historyKeyRef.current = undefined;
    const nextConversationId = randomId("wst");
    conversationRef.current = nextConversationId;
    setConversationId(nextConversationId);
    feedbackAttemptsRef.current.clear();
    setTurns([]);
    setHistorySessions([]);
    setSessionReady(false);
    setSessionError(undefined);
    setLogoutError(undefined);
    setUser(undefined);
    setFeedbackDetailsEnabled(false);
    setUsageContextEnabled(false);
    setNaturalProtocol(undefined);
    setLoggedOut(showLoggedOut);
    setPhase(showLoggedOut ? "idle" : "creating_session");
  }, []);

  const resetConversationState = useCallback((): string | undefined => {
    if (busyRef.current || !sessionReady) return undefined;
    requestIdRef.current += 1;
    streamControllerRef.current?.abort();
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    activeTraceIdRef.current = undefined;
    setPendingResume(undefined);
    const nextConversationId = randomId("wst");
    conversationRef.current = nextConversationId;
    if (historyKeyRef.current) {
      try {
        window.sessionStorage.setItem(historyKeyRef.current, nextConversationId);
      } catch {
        // 本次会话仍可继续使用。
      }
    }
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
    setNaturalProtocol(capabilities.natural_stream_protocol);
    setLoggedOut(false);
    if (session.deploymentId && session.user) {
      identityChangedRef.current = recordPublicIdentity(
        session.deploymentId,
        session.user.userId,
      );
    }
    if (capabilities.natural_stream_protocol) {
      const identity = session.user?.userId ?? session.sessionId;
      const key = `wst-natural-conversation:${session.deploymentId ?? "local"}:${identity}`;
      historyKeyRef.current = key;
      try {
        const saved = window.sessionStorage.getItem(key);
        const restored =
          saved && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(saved)
            ? saved
            : conversationRef.current;
        window.sessionStorage.setItem(key, restored);
        conversationRef.current = restored;
        setConversationId(restored);
        const conversation = await getPublicNaturalConversation({
          conversationId: restored,
          csrfToken: session.csrfToken,
          signal,
        });
        setTurns(
          conversation.turns.map((item) => restoreNaturalTurn(restored, item)),
        );
        if (capabilities.natural_stream_protocol === "wanshitong-natural-sse-v2") {
          setPendingResume(conversation.active_run ?? undefined);
        }
        setHistorySessions(
          await getPublicNaturalSessions({
            csrfToken: session.csrfToken,
            signal,
          }),
        );
      } catch {
        // 历史恢复失败时保留当前页面会话，后续请求仍由服务端鉴权。
      }
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
      resumeTraceId?: string,
      sourceMode: "auto" | "open" = "auto",
    ) => {
      if (busyRef.current || !sessionReady) return;
      busyRef.current = true;
      activeTurnRef.current = turnId;
      activeTraceIdRef.current = resumeTraceId;
      const requestId = ++requestIdRef.current;
      const controller = new AbortController();
      streamControllerRef.current = controller;
      const tracker: StreamTracker = {
        claimCount: 0,
        deltaCount: 0,
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
            naturalProtocol,
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
      setPhase(resumeTraceId ? "streaming" : "submitting");

      const isCurrent = () => requestIdRef.current === requestId;
      try {
        const csrfToken = csrfRef.current;
        if (!csrfToken) {
          clearLocalSession(false);
          authNavigation.redirectToSso(question);
          return;
        }
        const response = resumeTraceId
          ? await continuePublicNaturalChat({
              conversationId: turnConversationId,
              csrfToken,
              traceId: resumeTraceId,
              lastSequence: -1,
              signal: controller.signal,
            })
          : await openPublicChat({
              conversationId: turnConversationId,
              csrfToken,
              question,
              signal: controller.signal,
              clientContext: usageContextEnabled ? clientContext : undefined,
              naturalProtocol,
              sourceMode,
            });
        if (!response.body) {
          throw new TypeError("public stream body missing");
        }
        if (!isCurrent()) return;
        const responseTraceId = response.headers.get("X-Trace-Id");
        if (responseTraceId && /^trace_[0-9a-f]{32}$/.test(responseTraceId)) {
          activeTraceIdRef.current = responseTraceId;
        }
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
              activeTraceIdRef.current = traceId;
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
          if (event.type === "answer_delta") {
            tracker.deltaCount += 1;
            updateTurn(turnId, (turn) => ({
              ...turn,
              traceId: traceId ?? turn.traceId,
              provisionalAnswer: (turn.provisionalAnswer ?? "") + event.text,
              lastSignalAt: Date.now(),
            }));
            return true;
          }
          if (event.type === "references") return true;
          if (event.type === "final") {
            tracker.terminal = true;
            updateTurn(turnId, (turn) => ({
              ...turn,
              status: "completed",
              stageMessage: undefined,
              claims: [],
              answer: finalAnswerMessage(event),
              publicationStatus: event.status ?? event.reason_code ?? undefined,
              provisionalAnswer: undefined,
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
            const partial =
              tracker.claimCount > 0 ||
              tracker.deltaCount > 0 ||
              event.partial === true;
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
            partial: turn.claims.length > 0 || Boolean(turn.provisionalAnswer),
            traceId: traceId ?? turn.traceId,
          }));
          setPhase("cancelled");
          return false;
        };

        let currentResponse = response;
        let reconnects = 0;
        while (!tracker.terminal && isCurrent()) {
          try {
            await consumePublicSse(
              currentResponse.body!,
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
          } catch (error) {
            if (
              naturalProtocol !== "wanshitong-natural-sse-v2" ||
              !activeTraceIdRef.current ||
              reconnects >= 2 ||
              error instanceof PublicSseParseError ||
              controller.signal.aborted
            ) throw error;
          }
          if (tracker.terminal || !isCurrent() || controller.signal.aborted) break;
          if (
            naturalProtocol !== "wanshitong-natural-sse-v2" ||
            !activeTraceIdRef.current ||
            reconnects >= 2
          ) {
            throw new TypeError("public stream disconnected before terminal");
          }
          reconnects += 1;
          currentResponse = await continuePublicNaturalChat({
            conversationId: turnConversationId,
            csrfToken,
            traceId: activeTraceIdRef.current,
            lastSequence: tracker.lastSequence,
            signal: controller.signal,
          });
        }
        if (naturalProtocol && isCurrent()) {
          void getPublicNaturalSessions({ csrfToken })
            .then(setHistorySessions)
            .catch(() => undefined);
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
          const partial = turn.claims.length > 0 || Boolean(turn.provisionalAnswer);
          return {
            ...turn,
            status: "failed",
            stageMessage: undefined,
            errorMessage: error instanceof PublicApiError && error.status === 410
              ? "续接事件已过期，请从历史记录查看最终结果。"
              : partial
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
          activeTraceIdRef.current = undefined;
        }
      }
    },
    [clearLocalSession, naturalProtocol, sessionReady, updateTurn, usageContextEnabled],
  );

  useEffect(() => {
    if (
      !pendingResume ||
      !sessionReady ||
      naturalProtocol !== "wanshitong-natural-sse-v2" ||
      busyRef.current
    ) return;
    setPendingResume(undefined);
    void runTurn(
      pendingResume.trace_id,
      pendingResume.question,
      conversationRef.current,
      false,
      { entrypoint: "manual" },
      pendingResume.trace_id,
    );
  }, [pendingResume, sessionReady, naturalProtocol, runTurn]);

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

  const openHistory = useCallback(
    async (selectedConversationId: string) => {
      if (busyRef.current || !sessionReady || !naturalProtocol) return;
      const csrfToken = csrfRef.current;
      if (!csrfToken) return;
      const conversation = await getPublicNaturalConversation({
        conversationId: selectedConversationId,
        csrfToken,
      });
      requestIdRef.current += 1;
      conversationRef.current = selectedConversationId;
      setConversationId(selectedConversationId);
      setTurns(
        conversation.turns.map((item) => restoreNaturalTurn(selectedConversationId, item)),
      );
      setPendingResume(
        naturalProtocol === "wanshitong-natural-sse-v2"
          ? conversation.active_run ?? undefined
          : undefined,
      );
      if (historyKeyRef.current) {
        try {
          window.sessionStorage.setItem(
            historyKeyRef.current,
            selectedConversationId,
          );
        } catch {
          // 服务端历史仍可访问。
        }
      }
      setPhase("ready");
    },
    [naturalProtocol, sessionReady],
  );

  const submitRecommendation = useCallback(
    (
      question: string,
      recommendationId?: string,
      entrypoint: "suggestion" | "popular" = "suggestion",
    ) => {
      const value = question.trim();
      if (!value || busyRef.current) return;
      void runTurn(randomId("turn"), value, conversationRef.current, false, {
        entrypoint: recommendationId ? entrypoint : "manual",
        ...(recommendationId ? { recommendation_id: recommendationId } : {}),
      });
    },
    [runTurn],
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

  const releaseSource = useCallback(
    (turnId: string, question: string, turnConversationId: string) => {
      if (
        naturalProtocol !== "wanshitong-natural-sse-v2" ||
        busyRef.current ||
        conversationRef.current !== turnConversationId ||
        !turns.some(
          (turn) => turn.id === turnId && turn.publicationStatus === "SOURCE_CLARIFICATION",
        )
      ) return;
      void runTurn(
        randomId("turn"),
        question,
        turnConversationId,
        false,
        { entrypoint: "retry" },
        undefined,
        "open",
      );
    },
    [naturalProtocol, runTurn, turns],
  );

  const stop = useCallback(() => {
    const turnId = activeTurnRef.current;
    if (!busyRef.current || !turnId) return;
    const traceId = activeTraceIdRef.current;
    const csrfToken = csrfRef.current;
    const turnConversationId = conversationRef.current;
    const markStopped = () => {
      requestIdRef.current += 1;
      streamControllerRef.current?.abort();
      busyRef.current = false;
      streamControllerRef.current = undefined;
      activeTurnRef.current = undefined;
      activeTraceIdRef.current = undefined;
      updateTurn(turnId, (turn) => ({
        ...turn,
        status: "cancelled",
        stageMessage: undefined,
        errorMessage: "已停止回答",
        partial: turn.claims.length > 0 || Boolean(turn.provisionalAnswer),
      }));
      setPhase("cancelled");
    };
    if (naturalProtocol === "wanshitong-natural-sse-v2") {
      if (!traceId || !csrfToken || stopPendingRef.current) {
        updateTurn(turnId, (turn) => ({
          ...turn,
          errorMessage: "连接建立后即可停止，请稍候。",
        }));
        return;
      }
      stopPendingRef.current = true;
      void stopPublicNaturalChat({
        conversationId: turnConversationId,
        csrfToken,
        traceId,
      }).then((cancelled) => {
        stopPendingRef.current = false;
        if (cancelled) {
          markStopped();
        } else {
          updateTurn(turnId, (turn) => ({
            ...turn,
            errorMessage: "停止请求未生效，请查看当前运行状态。",
          }));
        }
      }).catch(() => {
        stopPendingRef.current = false;
        updateTurn(turnId, (turn) => ({
          ...turn,
          errorMessage: "服务端取消未确认，请重试。",
        }));
      });
      return;
    }
    if (naturalProtocol && traceId && csrfToken) {
      void stopPublicNaturalChat({
        conversationId: turnConversationId,
        csrfToken,
        traceId,
      }).catch(() => undefined);
    }
    markStopped();
  }, [naturalProtocol, updateTurn]);

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

  const downloadReference = useCallback(
    async (
      turnConversationId: string,
      traceId: string,
      referenceId: string,
      documentName: string,
    ) => {
      const csrfToken = csrfRef.current;
      if (!csrfToken) throw new Error("公共会话已失效。请重新登录。");
      const blob = await getPublicNaturalSource({
        conversationId: turnConversationId,
        traceId,
        referenceId,
        csrfToken,
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = documentName.replace(/[\\/:*?"<>|]/g, "_");
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
    [],
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
    downloadReference,
    feedbackDetailsEnabled,
    historySessions,
    loggedOut,
    login: () => authNavigation.redirectToSso(),
    logout,
    logoutError,
    openHistory,
    phase,
    retry,
    releaseSource,
    retrySession: startSession,
    sessionError,
    sessionReady,
    startNewTopic,
    stop,
    submit,
    submitRecommendation,
    submitFeedback,
    turns,
    user,
  };
}
