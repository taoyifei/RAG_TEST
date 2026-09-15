import { useEffect, useState } from "react";

export const routes = {
  workspace: "/admin",
  projects: "/admin/projects",
  knowledgeBases: "/admin/knowledge-bases",
  documents: "/admin/documents",
  jobs: "/admin/jobs",
  revision: "/admin/revision",
  retrieval: "/admin/retrieval",
  chat: "/admin/chat",
  history: "/admin/history",
  traces: "/admin/operational-traces",
  modelServices: "/admin/model-services",
  retrievalProfiles: "/admin/retrieval-profiles",
  system: "/admin/system",
  access: "/admin/access",
} as const;

export type AppRoute = (typeof routes)[keyof typeof routes];

const legacyAdminRoutes: Readonly<Record<string, AppRoute>> = {
  "/": routes.workspace,
  "/projects": routes.projects,
  "/knowledge-bases": routes.knowledgeBases,
  "/documents": routes.documents,
  "/jobs": routes.jobs,
  "/revision": routes.revision,
  "/retrieval": routes.retrieval,
  "/chat": routes.chat,
  "/history": routes.history,
  "/operational-traces": routes.traces,
  "/model-services": routes.modelServices,
  "/retrieval-profiles": routes.retrievalProfiles,
  "/system": routes.system,
  "/access": routes.access,
};

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
    const resolved = isAppRoute(next) ? next : legacyAdminRoutes[next];
    if (!resolved) throw new Error(`未知页面路径：${next}`);
    const url = new URL(window.location.href);
    url.pathname = resolved;
    window.history.pushState({}, "", `${url.pathname}${url.search}`);
    setPath(resolved);
  };
  return { path, go };
}
