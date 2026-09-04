import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, type Tokens } from "../api/client";

export interface Scope {
  projectId: string;
  kbId: string;
  revisionId: string;
}

interface SessionState {
  authenticated: boolean;
  ready: boolean;
}

interface ConsoleState {
  tokens: Tokens;
  session: SessionState;
  login: (bootstrapToken: string) => Promise<void>;
  logout: () => Promise<void>;
  rotateSession: () => Promise<void>;
  scope: Scope;
  setProject: (projectId: string) => void;
  setKnowledgeBase: (kbId: string, revisionId?: string | null) => void;
  setRevision: (revisionId: string) => void;
}

const Context = createContext<ConsoleState | null>(null);
const EMPTY_SCOPE: Scope = { projectId: "", kbId: "", revisionId: "" };

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

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>({
    authenticated: false,
    ready: false,
  });
  const [scope, setScopeState] = useState<Scope>(readScope);
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
  const updateScope = (next: Scope) => {
    persistScope(next);
    setScopeState(next);
  };
  const value = useMemo<ConsoleState>(
    () => ({
      tokens: session.authenticated
        ? { admin: "cookie-session", query: "cookie-session" }
        : { admin: "", query: "" },
      session,
      login: async (bootstrapToken) => {
        sessionGeneration.current += 1;
        await api.login(bootstrapToken);
        setSession({ authenticated: true, ready: true });
      },
      logout: async () => {
        sessionGeneration.current += 1;
        await api.logout();
        setSession({ authenticated: false, ready: true });
      },
      rotateSession: async () => {
        await api.rotateSession();
      },
      scope,
      setProject: (projectId) =>
        updateScope({ projectId, kbId: "", revisionId: "" }),
      setKnowledgeBase: (kbId, revisionId) =>
        updateScope({
          ...scope,
          kbId,
          revisionId: revisionId ?? "",
        }),
      setRevision: (revisionId) => updateScope({ ...scope, revisionId }),
    }),
    [scope, session],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useConsole(): ConsoleState {
  const value = useContext(Context);
  if (!value) throw new Error("ConsoleProvider 未初始化");
  return value;
}
