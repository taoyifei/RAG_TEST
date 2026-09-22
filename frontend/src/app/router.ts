import { useEffect, useState } from "react";

import {
  applicationPath,
  appHistoryLocation,
  withAppBase,
} from "./basePath";

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

export const wanshitongRoutes = {
  workspace: "/admin",
  documents: "/admin/documents",
  jobs: "/admin/jobs",
  history: "/admin/history",
  feedback: "/admin/feedback",
  traces: "/admin/operational-traces",
  models: "/admin/models",
  system: "/admin/system",
} as const;

export type WanshitongRoute =
  (typeof wanshitongRoutes)[keyof typeof wanshitongRoutes];

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

const wanshitongAliases: Readonly<Record<string, WanshitongRoute>> = {
  "/documents": wanshitongRoutes.documents,
  "/jobs": wanshitongRoutes.jobs,
  "/history": wanshitongRoutes.history,
  "/feedback": wanshitongRoutes.feedback,
  "/operational-traces": wanshitongRoutes.traces,
  "/models": wanshitongRoutes.models,
  "/model-services": wanshitongRoutes.models,
  "/admin/model-services": wanshitongRoutes.models,
  "/system": wanshitongRoutes.system,
  "/projects": wanshitongRoutes.workspace,
  "/knowledge-bases": wanshitongRoutes.workspace,
  "/revision": wanshitongRoutes.workspace,
  "/retrieval": wanshitongRoutes.workspace,
  "/chat": wanshitongRoutes.workspace,
  "/retrieval-profiles": wanshitongRoutes.workspace,
  "/access": wanshitongRoutes.workspace,
  "/admin/projects": wanshitongRoutes.workspace,
  "/admin/knowledge-bases": wanshitongRoutes.workspace,
  "/admin/revision": wanshitongRoutes.workspace,
  "/admin/retrieval": wanshitongRoutes.workspace,
  "/admin/chat": wanshitongRoutes.workspace,
  "/admin/retrieval-profiles": wanshitongRoutes.workspace,
  "/admin/access": wanshitongRoutes.workspace,
};

function isAppRoute(value: string): value is AppRoute {
  return Object.values(routes).includes(value as AppRoute);
}

function resolveAppRoute(pathname: string): AppRoute {
  if (isAppRoute(pathname)) return pathname;
  return legacyAdminRoutes[pathname] ?? routes.workspace;
}

function isWanshitongRoute(value: string): value is WanshitongRoute {
  return Object.values(wanshitongRoutes).includes(value as WanshitongRoute);
}

export function isLegacyConsolePath(pathname: string): boolean {
  const path = applicationPath(pathname);
  return path !== "/" && path in legacyAdminRoutes;
}

export function isWanshitongAdminPath(pathname: string): boolean {
  const path = applicationPath(pathname);
  return (
    path === "/admin" ||
    path.startsWith("/admin/") ||
    path in wanshitongAliases
  );
}

export function useRouter() {
  const [path, setPath] = useState<AppRoute>(() =>
    resolveAppRoute(applicationPath()),
  );
  useEffect(() => {
    const listener = () => {
      setPath(resolveAppRoute(applicationPath()));
    };
    window.addEventListener("popstate", listener);
    return () => window.removeEventListener("popstate", listener);
  }, []);
  const go = (next: string) => {
    const resolved = isAppRoute(next) ? next : legacyAdminRoutes[next];
    if (!resolved) throw new Error(`未知页面路径：${next}`);
    window.history.pushState({}, "", appHistoryLocation(resolved));
    setPath(resolved);
  };
  return { path, go };
}

function resolveWanshitongRoute(pathname: string): WanshitongRoute {
  if (isWanshitongRoute(pathname)) return pathname;
  return wanshitongAliases[pathname] ?? wanshitongRoutes.workspace;
}

export function useWanshitongRouter() {
  const [path, setPath] = useState<WanshitongRoute>(() =>
    resolveWanshitongRoute(applicationPath()),
  );
  useEffect(() => {
    const synchronize = () => {
      const resolved = resolveWanshitongRoute(applicationPath());
      if (window.location.pathname !== withAppBase(resolved)) {
        window.history.replaceState({}, "", appHistoryLocation(resolved));
      }
      setPath(resolved);
    };
    synchronize();
    window.addEventListener("popstate", synchronize);
    return () => window.removeEventListener("popstate", synchronize);
  }, []);
  const go = (next: string) => {
    const resolved = resolveWanshitongRoute(next);
    window.history.pushState({}, "", appHistoryLocation(resolved));
    setPath(resolved);
  };
  return { path, go };
}
