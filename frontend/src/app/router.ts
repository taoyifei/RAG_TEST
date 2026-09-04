import { useEffect, useState } from "react";

export const routes = {
  workspace: "/",
  projects: "/projects",
  knowledgeBases: "/knowledge-bases",
  documents: "/documents",
  jobs: "/jobs",
  revision: "/revision",
  retrieval: "/retrieval",
  chat: "/chat",
  modelServices: "/model-services",
  retrievalProfiles: "/retrieval-profiles",
  system: "/system",
  access: "/access",
} as const;

export type AppRoute = (typeof routes)[keyof typeof routes];

function isAppRoute(value: string): value is AppRoute {
  return Object.values(routes).includes(value as AppRoute);
}

export function useRouter() {
  const initial = isAppRoute(window.location.pathname)
    ? window.location.pathname
    : routes.workspace;
  const [path, setPath] = useState<AppRoute>(initial);
  useEffect(() => {
    const listener = () => {
      setPath(
        isAppRoute(window.location.pathname)
          ? window.location.pathname
          : routes.workspace,
      );
    };
    window.addEventListener("popstate", listener);
    return () => window.removeEventListener("popstate", listener);
  }, []);
  const go = (next: string) => {
    if (!isAppRoute(next)) throw new Error(`未知页面路径：${next}`);
    const url = new URL(window.location.href);
    url.pathname = next;
    window.history.pushState({}, "", `${url.pathname}${url.search}`);
    setPath(next);
  };
  return { path, go };
}
