import {
  Activity,
  Boxes,
  Database,
  FileSearch,
  FolderKanban,
  History,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquareText,
  Network,
  Route,
  RefreshCw,
  Settings,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api, type SystemStatus } from "../api/client";
import { EmptyState, ErrorPanel } from "../components/ui";
import { routes, useRouter } from "../app/router";
import { zhCN } from "../copy/zh-CN";
import { AccessTokensPage } from "../pages/AccessTokensPage";
import { FirstRunWizard } from "../pages/FirstRunWizard";
import { ModelServicesPage } from "../pages/ModelServicesPage";
import { RetrievalProfilesPage } from "../pages/RetrievalProfilesPage";
import { useConsole } from "../state/console-context";
import { Dashboard } from "../pages/Dashboard";
import { ProjectsPage } from "../pages/ProjectsPage";
import { KnowledgeBasesPage } from "../pages/KnowledgeBasesPage";
import { DocumentsPage } from "../pages/DocumentsPage";
import { JobsPage } from "../pages/JobsPage";
import { RevisionPage } from "../pages/RevisionPage";
import { QueryPage } from "../pages/QueryPage";
import { SystemPage } from "../pages/SystemPage";
import { HistoryPage } from "../pages/HistoryPage";
import { OperationalTracesPage } from "../pages/OperationalTracesPage";
const primaryNav = [
  [routes.workspace, zhCN.navigation.workspace, LayoutDashboard],
  [routes.knowledgeBases, zhCN.navigation.knowledge, FolderKanban],
  [routes.chat, zhCN.navigation.chat, MessageSquareText],
  [routes.history, "问答历史", History],
  [routes.modelServices, zhCN.navigation.modelServices, KeyRound],
] as const;

const operationsNav = [
  [routes.documents, zhCN.navigation.documents, Database],
  [routes.jobs, zhCN.navigation.jobs, Activity],
  [routes.traces, "Operational Trace", Route],
  [routes.revision, zhCN.navigation.revisions, Boxes],
  [routes.retrieval, zhCN.navigation.retrieval, FileSearch],
  [routes.retrievalProfiles, "检索方案", Settings],
  [routes.system, zhCN.navigation.system, Network],
  [routes.access, zhCN.navigation.access, KeyRound],
] as const;

function ScopeGuard({
  active,
  children,
}: {
  active: boolean;
  children: React.ReactNode;
}) {
  const { scope } = useConsole();
  if (!scope.projectId || !scope.kbId || !active) {
    return (
      <EmptyState title="请先选择知识库">
        从“项目”和“知识库”页面建立当前工作范围。
      </EmptyState>
    );
  }
  return children;
}

export default function AppShell() {
  const { path, go } = useRouter();
  const {
    scope,
    tokens,
    session,
    logout,
    rotateSession,
    setProject,
    setKnowledgeBase,
  } = useConsole();
  const [menuOpen, setMenuOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [shellError, setShellError] = useState<unknown>();
  const [projects, setProjects] = useState<
    { project_id: string; name: string }[]
  >([]);
  const [knowledgeBases, setKnowledgeBases] = useState<
    { knowledge_base_id: string; name: string }[]
  >([]);
  useEffect(() => {
    if (!tokens.admin) {
      return;
    }
    let cancelled = false;
    void api
      .listProjects(tokens.admin)
      .then((page) => {
        if (cancelled) return;
        const activeProjects = page.items.filter(
          (item) => item.status === "active",
        );
        setProjects(activeProjects);
        if (
          scope.projectId &&
          !activeProjects.some((item) => item.project_id === scope.projectId)
        ) {
          setProject("");
        }
      })
      .catch((reason) => {
        if (!cancelled) setShellError(reason);
      });
    return () => {
      cancelled = true;
    };
  }, [tokens.admin, scope.projectId, path, setProject]);
  const activeProjectSelected = projects.some(
    (item) => item.project_id === scope.projectId,
  );
  useEffect(() => {
    if (!tokens.admin || !scope.projectId || !activeProjectSelected) {
      return;
    }
    let cancelled = false;
    void api
      .listKnowledgeBases(tokens.admin, scope.projectId)
      .then((page) => {
        if (cancelled) return;
        const activeKnowledgeBases = page.items.filter(
          (item) => item.status === "active",
        );
        setKnowledgeBases(activeKnowledgeBases);
        if (
          scope.kbId &&
          !activeKnowledgeBases.some(
            (item) => item.knowledge_base_id === scope.kbId,
          )
        ) {
          setKnowledgeBase("");
        }
      })
      .catch((reason) => {
        if (!cancelled) setShellError(reason);
      });
    return () => {
      cancelled = true;
    };
  }, [
    tokens.admin,
    scope.projectId,
    scope.kbId,
    path,
    activeProjectSelected,
    setKnowledgeBase,
  ]);
  const activeKnowledgeBaseSelected = knowledgeBases.some(
    (item) => item.knowledge_base_id === scope.kbId,
  );
  const activeScope = activeProjectSelected && activeKnowledgeBaseSelected;
  const [shellStatus, setShellStatus] = useState<SystemStatus>();
  useEffect(() => {
    if (!tokens.admin) return;
    void api.system(tokens.admin).then(setShellStatus).catch(setShellError);
  }, [tokens.admin]);
  const active =
    [...primaryNav, ...operationsNav].find(([url]) => url === path)?.[1] ??
    "控制台";
  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <button className="menu-button" onClick={() => setMenuOpen(false)}>
          关闭导航
        </button>
        <div className="brand">
          <span className="brand-mark">UR</span>
          <div>
            <strong>{zhCN.brand}</strong>
            <small>{zhCN.productCaption}</small>
          </div>
        </div>
        <nav aria-label="主导航">
          {primaryNav.map(([url, label, Icon]) => (
            <button
              key={url}
              className={path === url ? "active" : ""}
              onClick={() => {
                go(url);
                setMenuOpen(false);
              }}
            >
              <Icon aria-hidden="true" size={18} />
              {label}
            </button>
          ))}
          <details className="nav-group">
            <summary>{zhCN.navigation.operations}</summary>
            {operationsNav.map(([url, label, Icon]) => (
              <button
                key={url}
                className={path === url ? "active" : ""}
                onClick={() => {
                  go(url);
                  setMenuOpen(false);
                }}
              >
                <Icon aria-hidden="true" size={18} />
                {label}
              </button>
            ))}
          </details>
        </nav>
        <div className="scope-card">
          <span>当前范围</span>
          <code>运行配置：{shellStatus?.profile_id ?? "未连接"}</code>
          <span>
            {projects.find((item) => item.project_id === scope.projectId)
              ?.name ?? "未选择项目"}
          </span>
          <span>
            {knowledgeBases.find(
              (item) => item.knowledge_base_id === scope.kbId,
            )?.name ?? "未选择知识库"}
          </span>
          <details>
            <summary>技术详情</summary>
            <code>{scope.projectId}</code>
            <code>{scope.kbId}</code>
            <code>索引版本：{scope.revisionId || "未选择"}</code>
          </details>
        </div>
      </aside>
      <main id="main-content">
        <header className="topbar">
          <button
            className="icon-button menu-button"
            onClick={() => setMenuOpen(!menuOpen)}
            aria-label="打开导航"
            aria-expanded={menuOpen}
          >
            <Menu aria-hidden="true" />
          </button>
          <div>
            <span className="eyebrow">安全管理控制台</span>
            <h1>{active}</h1>
          </div>
          <label className="space-selector">
            当前空间
            <select
              value={scope.projectId}
              onChange={(event) => {
                setProject(event.target.value);
                go(routes.knowledgeBases);
              }}
            >
              <option value="">选择项目</option>
              {projects.map((item) => (
                <option key={item.project_id} value={item.project_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <button className="secondary" onClick={() => go(routes.projects)}>
            管理项目
          </button>
          <div className="topbar-actions">
            {session.authenticated ? (
              <>
                <details className="user-settings">
                  <summary>设置</summary>
                  <button
                    className="secondary"
                    onClick={() => void rotateSession().catch(setShellError)}
                  >
                    <RefreshCw aria-hidden="true" size={17} />
                    轮换会话
                  </button>
                </details>
                <button
                  className="secondary"
                  onClick={() => void logout().catch(setShellError)}
                >
                  <LogOut aria-hidden="true" size={17} />
                  退出
                </button>
              </>
            ) : (
              <button className="secondary" onClick={() => setAuthOpen(true)}>
                <KeyRound aria-hidden="true" size={17} />
                管理员登录
              </button>
            )}
          </div>
        </header>
        <div className="page">
          {shellError !== undefined && <ErrorPanel error={shellError} />}
          {!session.ready && <EmptyState title="正在登录" />}
          {session.ready && !session.authenticated && (
            <EmptyState title="需要管理员登录">
              首次输入部署侧凭据后，浏览器只保留安全会话 Cookie。
            </EmptyState>
          )}
          {session.authenticated && (
            <>
              {path === routes.workspace && <Dashboard go={go} />}
              {path === routes.projects && <ProjectsPage go={go} />}
              {path === routes.knowledgeBases && (
                <KnowledgeBasesPage key={scope.projectId} go={go} />
              )}
              {path === routes.documents && (
                <ScopeGuard active={activeScope}>
                  <DocumentsPage key={scope.kbId} go={go} />
                </ScopeGuard>
              )}
              {path === routes.jobs && (
                <JobsPage key={`${scope.projectId}:${scope.kbId}`} go={go} />
              )}
              {path === routes.revision && (
                <ScopeGuard active={activeScope}>
                  <RevisionPage key={scope.kbId} go={go} />
                </ScopeGuard>
              )}
              {path === routes.retrieval && (
                <ScopeGuard active={activeScope}>
                  <QueryPage key={scope.kbId} mode="search" go={go} />
                </ScopeGuard>
              )}
              {path === routes.chat && (
                <ScopeGuard active={activeScope}>
                  <QueryPage key={scope.kbId} mode="answer" go={go} />
                </ScopeGuard>
              )}
              {path === routes.system && <SystemPage />}
              {path === routes.history && (
                <HistoryPage key={`${scope.projectId}:${scope.kbId}`} go={go} />
              )}
              {path === routes.traces && (
                <OperationalTracesPage
                  key={`${scope.projectId}:${scope.kbId}`}
                  go={go}
                />
              )}
              {path === routes.modelServices && <ModelServicesPage />}
              {path === routes.retrievalProfiles && (
                <RetrievalProfilesPage key={scope.kbId} />
              )}
              {path === routes.access && <AccessTokensPage />}
            </>
          )}
        </div>
      </main>
      <FirstRunWizard
        open={authOpen || (session.ready && !session.authenticated)}
        onClose={() => setAuthOpen(false)}
      />
    </div>
  );
}
