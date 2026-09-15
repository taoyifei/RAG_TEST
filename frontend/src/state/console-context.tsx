import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, type Tokens } from "../api/client";
import type { ProductMode } from "../app/product-mode";
import { wanshitongAdminApi } from "../wanshitong/admin/adminApi";

export interface Scope {
  projectId: string;
  kbId: string;
  revisionId: string;
}

interface SessionState {
  authenticated: boolean;
  ready: boolean;
}

export interface FixedScopeState {
  state: "not_applicable" | "idle" | "loading" | "ready" | "blocked";
  projectName: string;
  knowledgeBaseName: string;
  reason: string;
}

interface ConsoleState {
  productMode: ProductMode;
  tokens: Tokens;
  session: SessionState;
  login: (bootstrapToken: string) => Promise<void>;
  logout: () => Promise<void>;
  rotateSession: () => Promise<void>;
  scope: Scope;
  setProject: (projectId: string) => void;
  setKnowledgeBase: (kbId: string, revisionId?: string | null) => void;
  setRevision: (revisionId: string) => void;
  fixedScope: FixedScopeState;
  recheckFixedScope: () => void;
}

const Context = createContext<ConsoleState | null>(null);
const EMPTY_SCOPE: Scope = { projectId: "", kbId: "", revisionId: "" };
const EMPTY_FIXED_SCOPE: FixedScopeState = {
  state: "idle",
  projectName: "",
  knowledgeBaseName: "",
  reason: "",
};

function readScope(): Scope {
  const parameters = new URLSearchParams(window.location.search);
  const fromUrl = {
    projectId: parameters.get("project") ?? "",
    kbId: parameters.get("knowledgeBase") ?? "",
    revisionId: parameters.get("revision") ?? "",
  };
  if (Object.values(fromUrl).some(Boolean)) return fromUrl;
  try {
    const stored = JSON.parse(
      sessionStorage.getItem("rag.console.scope") ?? "null",
    ) as Partial<Scope> | null;
    return stored
      ? {
          projectId: stored.projectId ?? "",
          kbId: stored.kbId ?? "",
          revisionId: stored.revisionId ?? "",
        }
      : EMPTY_SCOPE;
  } catch {
    return EMPTY_SCOPE;
  }
}

function persistScope(scope: Scope): void {
  sessionStorage.setItem("rag.console.scope", JSON.stringify(scope));
  const url = new URL(window.location.href);
  const values = {
    project: scope.projectId,
    knowledgeBase: scope.kbId,
    revision: scope.revisionId,
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}`);
}

export function ConsoleProvider({
  children,
  productMode = "universal",
}: {
  children: ReactNode;
  productMode?: ProductMode;
}) {
  const [session, setSession] = useState<SessionState>({
    authenticated: false,
    ready: false,
  });
  const [scope, setScopeState] = useState<Scope>(() =>
    productMode === "wanshitong" ? EMPTY_SCOPE : readScope(),
  );
  const [fixedScope, setFixedScope] = useState<FixedScopeState>(() =>
    productMode === "wanshitong"
      ? EMPTY_FIXED_SCOPE
      : { ...EMPTY_FIXED_SCOPE, state: "not_applicable" },
  );
  const [scopeRefresh, setScopeRefresh] = useState(0);
  const resumed = useRef(false);
  const sessionGeneration = useRef(0);
  useEffect(() => {
    if (resumed.current) return;
    resumed.current = true;
    const generation = ++sessionGeneration.current;
    void api
      .resumeSession()
      .then(() => {
        if (generation === sessionGeneration.current) {
          setSession({ authenticated: true, ready: true });
        }
      })
      .catch((error: unknown) => {
        if (generation !== sessionGeneration.current) return;
        if (!(error instanceof ApiError) || error.status !== 401) {
          console.warn("会话恢复失败", error);
        }
        setSession({ authenticated: false, ready: true });
      });
  }, []);
  const updateScope = useCallback(
    (next: Scope) => {
      if (productMode === "universal") persistScope(next);
      setScopeState(next);
    },
    [productMode],
  );
  useEffect(() => {
    if (productMode !== "wanshitong") return;
    if (!session.authenticated) return;
    const controller = new AbortController();
    void wanshitongAdminApi
      .scope(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        if (
          !value.ready ||
          !value.project_id ||
          !value.knowledge_base_id
        ) {
          setFixedScope({
            ...EMPTY_FIXED_SCOPE,
            state: "blocked",
            reason:
              value.blocker_message ||
              value.blocker_code ||
              "固定 Project 或 Knowledge Base 尚未就绪。",
          });
          return;
        }
        setScopeState({
          projectId: value.project_id,
          kbId: value.knowledge_base_id,
          revisionId: "",
        });
        setFixedScope({
          state: "ready",
          projectName: value.project_name || "湾事通",
          knowledgeBaseName: value.knowledge_base_name || "湾事通知识库",
          reason: "",
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setFixedScope({
          ...EMPTY_FIXED_SCOPE,
          state: "blocked",
          reason:
            error instanceof Error
              ? error.message
              : "固定 Scope 检查失败，请稍后重试。",
        });
      });
    return () => controller.abort();
  }, [productMode, scopeRefresh, session.authenticated]);
  const value = useMemo<ConsoleState>(
    () => ({
      productMode,
      tokens: session.authenticated
        ? { admin: "cookie-session", query: "cookie-session" }
        : { admin: "", query: "" },
      session,
      login: async (bootstrapToken) => {
        sessionGeneration.current += 1;
        await api.login(bootstrapToken);
        if (productMode === "wanshitong") {
          setScopeState(EMPTY_SCOPE);
          setFixedScope({ ...EMPTY_FIXED_SCOPE, state: "loading" });
        }
        setSession({ authenticated: true, ready: true });
      },
      logout: async () => {
        sessionGeneration.current += 1;
        await api.logout();
        if (productMode === "wanshitong") {
          setScopeState(EMPTY_SCOPE);
          setFixedScope(EMPTY_FIXED_SCOPE);
        }
        setSession({ authenticated: false, ready: true });
      },
      rotateSession: async () => {
        await api.rotateSession();
      },
      scope,
      setProject: (projectId) =>
        productMode === "universal" &&
        updateScope({ projectId, kbId: "", revisionId: "" }),
      setKnowledgeBase: (kbId, revisionId) =>
        productMode === "universal" &&
        updateScope({
          ...scope,
          kbId,
          revisionId: revisionId ?? "",
        }),
      setRevision: (revisionId) =>
        productMode === "universal" && updateScope({ ...scope, revisionId }),
      fixedScope,
      recheckFixedScope: () => {
        setScopeState(EMPTY_SCOPE);
        setFixedScope({ ...EMPTY_FIXED_SCOPE, state: "loading" });
        setScopeRefresh((value) => value + 1);
      },
    }),
    [fixedScope, productMode, scope, session, updateScope],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useConsole(): ConsoleState {
  const value = useContext(Context);
  if (!value) throw new Error("ConsoleProvider 未初始化");
  return value;
}
