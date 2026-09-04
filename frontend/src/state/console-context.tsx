import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import type { Tokens } from "../api/client";

interface Scope {
  projectId: string;
  kbId: string;
  revisionId: string;
}

interface ConsoleState {
  tokens: Tokens;
  setTokens: (tokens: Tokens) => void;
  scope: Scope;
  setProject: (projectId: string) => void;
  setKnowledgeBase: (kbId: string, revisionId?: string | null) => void;
  setRevision: (revisionId: string) => void;
}

const Context = createContext<ConsoleState | null>(null);

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [tokens, setTokens] = useState<Tokens>({ admin: "", query: "" });
  const [scope, setScope] = useState<Scope>({
    projectId: "",
    kbId: "",
    revisionId: "",
  });
  const value = useMemo<ConsoleState>(
    () => ({
      tokens,
      setTokens,
      scope,
      setProject: (projectId) =>
        setScope({ projectId, kbId: "", revisionId: "" }),
      setKnowledgeBase: (kbId, revisionId) =>
        setScope((current) => ({
          ...current,
          kbId,
          revisionId: revisionId ?? "",
        })),
      setRevision: (revisionId) =>
        setScope((current) => ({ ...current, revisionId })),
    }),
    [scope, tokens],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useConsole(): ConsoleState {
  const value = useContext(Context);
  if (!value) throw new Error("ConsoleProvider 未初始化");
  return value;
}
