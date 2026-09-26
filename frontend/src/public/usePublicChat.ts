import { useCallback, useEffect, useRef, useState } from "react";

import {
  continuePublicNaturalChat,
  createPublicSession,
  getPublicNaturalHistory,
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
  WEKNORA_STREAM_PROTOCOL,
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

export interface PublicNativeActivity {
  sequence: number;
  responseType: string;
  payload: Record<string, unknown>;
}

export interface PublicTurn {
  id: string;
  conversationId: string;
  question: string;
  status: "submitting" | "streaming" | "completed" | "failed" | "cancelled" | "stop_requested" | "pending_confirmation";
  stageMessage?: string;
  stageHistory: string[];
  startedAt: number;
  stageStartedAt: number;
  lastSignalAt: number;
  claims: PublicClaim[];
  nativeEvents?: PublicNativeActivity[];
  nativeProtocol?: boolean;
  answer?: string;
  provisionalAnswer?: string;
  citations: PublicCitation[];
  errorMessage?: string;
  partial: boolean;
  traceId?: string;
  truncated?: boolean;
  finishReason?: string | null;
  nativeMessageId?: string | null;
  nativeRequestId?: string | null;
  retryOfTraceId?: string;
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
    nativeEvents: [],
    answer: undefined,
    provisionalAnswer: undefined,
    citations: [],
    errorMessage: undefined,
    partial: false,
    traceId: undefined,
    truncated: undefined,
    finishReason: undefined,
    nativeMessageId: undefined,
    nativeRequestId: undefined,
    feedback: "idle",
    feedbackError: undefined,
    feedbackUseful: undefined,
  };
}

function restoreNaturalTurn(
  conversationId: string,
  item: PublicNaturalHistoryTurn,
  nativeProtocol: boolean,
): PublicTurn {
  const startedAt = Date.parse(item.created_at) || Date.now();
  const status: PublicTurn["status"] =
    item.status === "completed"
      ? "completed"
      : item.status === "cancelled"
        ? "cancelled"
        : item.status === "stop_requested"
          ? "stop_requested"
        : item.status === "pending_confirmation" ||
            item.status === "streaming"
          ? "pending_confirmation"
          : "failed";
  const errorMessage =
    item.status === "SOURCE_UNAVAILABLE"
      ? "原生消息已不存在，这条历史回答暂不可展示。"
      : status === "pending_confirmation"
        ? "原回答状态待确认；可恢复原回答，重新生成会另起一次请求。"
        : status === "cancelled"
          ? "已停止回答。"
          : status === "stop_requested"
            ? "停止请求已接收；最终状态待确认。"
          : status === "failed"
            ? "回答未完成，以下内容不能作为最终结论。"
            : undefined;
  return {
    id: item.turn_id,
    conversationId,
    question: item.question,
    status,
    stageHistory: [],
    startedAt,
    stageStartedAt: startedAt,
    lastSignalAt: startedAt,
    claims: [],
    nativeProtocol,
    answer: item.answer ?? undefined,
    citations: item.citations,
    partial: item.partial === true,
    truncated: item.truncated,
    finishReason: item.finish_reason,
    nativeMessageId: item.native_message_id,
    nativeRequestId: item.native_request_id,
    errorMessage,
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
    "wanshitong-natural-sse-v1" | typeof WEKNORA_STREAM_PROTOCOL | undefined
  >();
  const [turns, setTurns] = useState<PublicTurn[]>([]);
  const [historySessions, setHistorySessions] = useState<
    PublicNaturalSession[]
  >([]);
  const [historyMoreCount, setHistoryMoreCount] = useState(0);
  const [conversationId, setConversationId] = useState(() => randomId("wst"));
  const csrfRef = useRef<string | undefined>(undefined);
  const conversationRef = useRef(conversationId);
  const sessionControllerRef = useRef<AbortController | undefined>(undefined);
  const streamControllerRef = useRef<AbortController | undefined>(undefined);
  const activeTurnRef = useRef<string | undefined>(undefined);
  const activeTraceIdRef = useRef<string | undefined>(undefined);
  const requestIdRef = useRef(0);
  const busyRef = useRef(false);
  const feedbackAttemptsRef = useRef(new Set<string>());
  const sessionChannelRef = useRef<PublicSessionChannel | undefined>(undefined);
  const identityChangedRef = useRef(false);
  const historyKeyRef = useRef<string | undefined>(undefined);

  const clearLocalSession = useCallback((showLoggedOut: boolean) => {
    requestIdRef.current += 1;
    busyRef.current = false;
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
    setHistoryMoreCount(0);
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
    const nextConversationId = randomId("wst");
    conversationRef.current = nextConversationId;
    if (historyKeyRef.current) {
      try {
        window.sessionStorage.setItem(
          historyKeyRef.current,
          nextConversationId,
        );
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
    setNaturalProtocol(
      capabilities.natural_stream_protocol ??
        (capabilities.stream_protocol === WEKNORA_STREAM_PROTOCOL
          ? WEKNORA_STREAM_PROTOCOL
          : undefined),
    );
    setLoggedOut(false);
    if (session.deploymentId && session.user) {
      identityChangedRef.current = recordPublicIdentity(
        session.deploymentId,
        session.user.userId,
      );
    }
    if (
      capabilities.natural_stream_protocol ||
      capabilities.stream_protocol === WEKNORA_STREAM_PROTOCOL
    ) {
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
        const history = await getPublicNaturalHistory({
          conversationId: restored,
          csrfToken: session.csrfToken,
          signal,
        });
        setTurns(
          history.map((item) =>
            restoreNaturalTurn(
              restored,
              item,
              (capabilities.natural_stream_protocol ??
                capabilities.stream_protocol) === WEKNORA_STREAM_PROTOCOL,
            ),
          ),
        );
        const sessions = await getPublicNaturalSessions({
          csrfToken: session.csrfToken,
          signal,
        });
        setHistorySessions(sessions.items);
        setHistoryMoreCount(sessions.has_more ? sessions.total - sessions.items.length : 0);
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
      recoverTraceId?: string,
      recoverOriginal?: PublicTurn,
    ) => {
      if (busyRef.current || !sessionReady) return;
      busyRef.current = true;
      activeTurnRef.current = turnId;
      activeTraceIdRef.current = recoverTraceId;
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
        updateTurn(turnId, (turn) =>
          recoverTraceId
            ? {
                ...turn,
                status: "submitting",
                stageMessage: "正在恢复原回答",
                errorMessage: undefined,
              }
            : resetTurn(turn),
        );
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
            nativeProtocol: naturalProtocol === WEKNORA_STREAM_PROTOCOL,
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
        let response: Response;
        if (recoverTraceId) {
          const history = await getPublicNaturalHistory({
            conversationId: turnConversationId,
            csrfToken,
            signal: controller.signal,
          });
          const original = history.find((item) => item.trace_id === recoverTraceId);
          if (!original) throw new Error("原回答状态无法确认，请联系管理员。");
          if (original.status === "completed") {
            updateTurn(turnId, () =>
              restoreNaturalTurn(turnConversationId, original, true),
            );
            setPhase("completed");
            return;
          }
          if (!original.native_message_id) {
            throw new Error("原生消息尚未建立，无法续接这次回答。");
          }
          response = await continuePublicNaturalChat({
            conversationId: turnConversationId,
            csrfToken,
            traceId: recoverTraceId,
            signal: controller.signal,
          });
        } else {
          response = await openPublicChat({
            conversationId: turnConversationId,
            csrfToken,
            question,
            signal: controller.signal,
            clientContext: usageContextEnabled ? clientContext : undefined,
            naturalProtocol,
          });
        }
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
            if (recoverTraceId) {
              updateTurn(turnId, (turn) => ({
                ...turn,
                answer: undefined,
                provisionalAnswer: undefined,
                citations: [],
                claims: [],
                partial: false,
              }));
            }
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
              nativeMessageId: event.native_message_id ?? turn.nativeMessageId,
              nativeRequestId: event.native_request_id ?? turn.nativeRequestId,
              lastSignalAt: Date.now(),
            }));
            return true;
          }
          if (event.type === "references") {
            updateTurn(turnId, (turn) => ({
              ...turn,
              traceId: traceId ?? turn.traceId,
              citations: event.items,
              lastSignalAt: Date.now(),
            }));
            return true;
          }
          if (event.type === "native_event") {
            updateTurn(turnId, (turn) => ({
              ...turn,
              traceId: traceId ?? turn.traceId,
              nativeEvents: [
                ...(turn.nativeEvents ?? []),
                {
                  sequence: event.sequence,
                  responseType: event.response_type,
                  payload: event.native,
                },
              ],
              lastSignalAt: Date.now(),
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
              answer:
                event.protocol === WEKNORA_STREAM_PROTOCOL
                  ? (event.answer ?? "")
                  : finalAnswerMessage(event),
              provisionalAnswer: undefined,
              citations: event.citations,
              errorMessage: undefined,
              partial: false,
              traceId: traceId ?? turn.traceId,
              truncated: event.truncated,
              finishReason: event.finish_reason,
              nativeMessageId: event.native_message_id ?? turn.nativeMessageId,
              nativeRequestId: event.native_request_id ?? turn.nativeRequestId,
            }));
            setPhase("completed");
            return false;
          }
          if (event.type === "error") {
            tracker.terminal = true;
            const partial =
              tracker.claimCount > 0 ||
              tracker.deltaCount > 0 ||
              event.partial === true ||
              Boolean(recoverOriginal?.answer) ||
              Boolean(recoverOriginal?.provisionalAnswer);
            updateTurn(turnId, (turn) => ({
              ...turn,
              status: "failed",
              stageMessage: undefined,
              answer: tracker.deltaCount === 0
                ? (turn.answer ?? recoverOriginal?.answer)
                : turn.answer,
              provisionalAnswer: tracker.deltaCount === 0
                ? (turn.provisionalAnswer ?? recoverOriginal?.provisionalAnswer)
                : turn.provisionalAnswer,
              citations: turn.citations.length > 0
                ? turn.citations
                : (recoverOriginal?.citations ?? []),
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
        if (naturalProtocol && isCurrent()) {
          void getPublicNaturalSessions({ csrfToken })
            .then((sessions) => {
              setHistorySessions(sessions.items);
              setHistoryMoreCount(sessions.has_more ? sessions.total - sessions.items.length : 0);
            })
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
          const partial =
            turn.claims.length > 0 ||
            Boolean(turn.provisionalAnswer) ||
            Boolean(recoverOriginal?.provisionalAnswer) ||
            Boolean(recoverOriginal?.answer);
          return {
            ...turn,
            status: "failed",
            stageMessage: undefined,
            answer: turn.answer ?? recoverOriginal?.answer,
            provisionalAnswer:
              turn.provisionalAnswer ?? recoverOriginal?.provisionalAnswer,
            citations: turn.citations.length > 0
              ? turn.citations
              : (recoverOriginal?.citations ?? []),
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
          activeTraceIdRef.current = undefined;
        }
      }
    },
    [
      clearLocalSession,
      naturalProtocol,
      sessionReady,
      updateTurn,
      usageContextEnabled,
    ],
  );

  const submit = useCallback(
    (question: string) => {
      if (!question.trim() || busyRef.current) return;
      void runTurn(randomId("turn"), question, conversationRef.current, false, {
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
      const history = await getPublicNaturalHistory({
        conversationId: selectedConversationId,
        csrfToken,
      });
      requestIdRef.current += 1;
      conversationRef.current = selectedConversationId;
      setConversationId(selectedConversationId);
      setTurns(
        history.map((item) =>
          restoreNaturalTurn(
            selectedConversationId,
            item,
            naturalProtocol === WEKNORA_STREAM_PROTOCOL,
          ),
        ),
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
      if (!question.trim() || busyRef.current) return;
      void runTurn(randomId("turn"), question, conversationRef.current, false, {
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
      if (naturalProtocol) {
        const previous = turns.find((item) => item.id === turnId);
        void runTurn(randomId("turn"), question, turnConversationId, false, {
          entrypoint: "retry",
          ...(previous?.traceId ? { retry_of_trace_id: previous.traceId } : {}),
        });
      } else {
        void runTurn(turnId, question, turnConversationId, true, {
          entrypoint: "retry",
        });
      }
    },
    [naturalProtocol, runTurn, turns],
  );

  const recover = useCallback(
    (turnId: string, question: string, turnConversationId: string) => {
      if (busyRef.current || conversationRef.current !== turnConversationId)
        return;
      const original = turns.find((item) => item.id === turnId);
      if (!original?.traceId || !original.nativeMessageId) return;
      void runTurn(turnId, question, turnConversationId, true, {
        entrypoint: "retry",
      }, original.traceId, original);
    },
    [runTurn, turns],
  );

  const stop = useCallback(() => {
    const turnId = activeTurnRef.current;
    if (!busyRef.current || !turnId) return;
    const traceId = activeTraceIdRef.current;
    const csrfToken = csrfRef.current;
    const turnConversationId = conversationRef.current;
    requestIdRef.current += 1;
    streamControllerRef.current?.abort();
    busyRef.current = false;
    streamControllerRef.current = undefined;
    activeTurnRef.current = undefined;
    activeTraceIdRef.current = undefined;
    updateTurn(turnId, (turn) => ({
      ...turn,
      status: naturalProtocol ? "pending_confirmation" : "cancelled",
      stageMessage: undefined,
      errorMessage: naturalProtocol
        ? "已停止显示；服务端取消未确认。"
        : "已停止回答",
      partial: turn.claims.length > 0 || Boolean(turn.provisionalAnswer),
    }));
    setPhase("cancelled");
    if (naturalProtocol && traceId && csrfToken) {
      void stopPublicNaturalChat({
        conversationId: turnConversationId,
        csrfToken,
        traceId,
      }).then((cancelled) => {
        if (cancelled) {
          updateTurn(turnId, (turn) => ({
            ...turn,
            status: "stop_requested",
            errorMessage: "停止请求已接收；最终状态待确认。",
          }));
        }
      }).catch(() => undefined);
    }
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
      original = false,
    ) => {
      const csrfToken = csrfRef.current;
      if (!csrfToken) throw new Error("公共会话已失效。请重新登录。");
      const blob = await getPublicNaturalSource({
        conversationId: turnConversationId,
        traceId,
        referenceId,
        csrfToken,
        original,
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      if (!original && blob.type.startsWith("text/plain")) {
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
      } else {
        anchor.download = documentName.replace(/[\\/:*?"<>|]/g, "_");
      }
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
    historyMoreCount,
    loggedOut,
    login: () => authNavigation.redirectToSso(),
    logout,
    logoutError,
    openHistory,
    recover,
    phase,
    retry,
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
