import {
  Activity,
  Database,
  History,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Network,
  RefreshCw,
  Route,
} from "lucide-react";
import { useState } from "react";

import {
  useWanshitongRouter,
  wanshitongRoutes,
} from "../../app/router";
import { EmptyState, ErrorPanel } from "../../components/ui";
import { FirstRunWizard } from "../../pages/FirstRunWizard";
import {
  HistoryPage,
  type HistoryPageServices,
} from "../../pages/HistoryPage";
import {
  OperationalTracesPage,
  type OperationalTraceServices,
} from "../../pages/OperationalTracesPage";
import { useConsole } from "../../state/console-context";
import { AdminDashboardPage } from "./AdminDashboardPage";
import { AdminDocumentsPage } from "./AdminDocumentsPage";
import { AdminJobsPage } from "./AdminJobsPage";
import { AdminModelsPage } from "./AdminModelsPage";
import { AdminSystemPage } from "./AdminSystemPage";
import { wanshitongAdminApi } from "./adminApi";

const navItems = [
  [wanshitongRoutes.workspace, "概览", LayoutDashboard],
  [wanshitongRoutes.documents, "文档管理", Database],
  [wanshitongRoutes.jobs, "处理任务", Activity],
  [wanshitongRoutes.history, "问答历史", History],
  [wanshitongRoutes.traces, "Operational Trace", Route],
  [wanshitongRoutes.models, "模型与服务状态", KeyRound],
  [wanshitongRoutes.system, "系统状态", Network],
] as const;

const historyServices: HistoryPageServices = {
  listHistory: wanshitongAdminApi.listHistory,
  historyDetail: wanshitongAdminApi.history,
  clearHistory: wanshitongAdminApi.clearHistory,
  exportSupport: wanshitongAdminApi.exportHistoryTraces,
};

const traceServices: OperationalTraceServices = {
  list: wanshitongAdminApi.listOperationalTraces,
  detail: wanshitongAdminApi.operationalTrace,
  artifact: wanshitongAdminApi.operationalTraceArtifact,
  exportOne: wanshitongAdminApi.exportOperationalTrace,
  exportMany: wanshitongAdminApi.exportOperationalTraces,
};

export function WanshitongAdminShell() {
  const { path, go } = useWanshitongRouter();
  const {
    fixedScope,
    logout,
    recheckFixedScope,
    rotateSession,
    session,
  } = useConsole();
  const [menuOpen, setMenuOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [shellError, setShellError] = useState<unknown>();
  const active = navItems.find(([route]) => route === path)?.[1] ?? "管理员控制台";
  const scopeReady = fixedScope.state === "ready";

  return (
    <div className="app-shell wst-admin-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <button className="menu-button" onClick={() => setMenuOpen(false)}>
          关闭导航
        </button>
        <div className="brand">
          <span className="brand-mark">湾</span>
          <div>
            <strong>湾事通</strong>
            <small>管理员控制台</small>
          </div>
        </div>
        <nav aria-label="管理员导航">
          {navItems.map(([route, label, Icon]) => (
            <button
              key={route}
              className={path === route ? "active" : ""}
              aria-current={path === route ? "page" : undefined}
              onClick={() => {
                go(route);
                setMenuOpen(false);
              }}
            >
              <Icon aria-hidden="true" size={18} />
              {label}
            </button>
          ))}
        </nav>
        {scopeReady && (
          <div className="scope-card" aria-label="固定知识范围">
            <span>固定知识范围</span>
            <strong>{fixedScope.projectName}</strong>
            <span>{fixedScope.knowledgeBaseName}</span>
          </div>
        )}
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
            <span className="eyebrow">固定 Scope 管理控制台</span>
            <h1>{active}</h1>
          </div>
          <div className="topbar-actions">
            {session.authenticated ? (
              <>
                <button
                  className="secondary"
                  onClick={() => void rotateSession().catch(setShellError)}
                >
                  <RefreshCw aria-hidden="true" size={17} />
                  轮换会话
                </button>
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
          {!session.ready && <EmptyState title="正在恢复管理员会话" />}
          {session.ready && !session.authenticated && (
            <EmptyState title="需要管理员登录">
              使用现有 Universal Console Session 登录；公共问答会话不受影响。
            </EmptyState>
          )}
          {session.authenticated &&
            ["idle", "loading"].includes(fixedScope.state) && (
            <EmptyState title="正在检查固定知识范围" />
          )}
          {session.authenticated && fixedScope.state === "blocked" && (
            <section className="panel scope-blocker" role="alert">
              <h2>固定知识范围尚未就绪</h2>
              <p>{fixedScope.reason}</p>
              <button className="primary" onClick={recheckFixedScope}>
                <RefreshCw aria-hidden="true" size={17} />
                重新检查
              </button>
            </section>
          )}
          {session.authenticated && scopeReady && (
            <>
              {path === wanshitongRoutes.workspace && (
                <AdminDashboardPage go={go} />
              )}
              {path === wanshitongRoutes.documents && <AdminDocumentsPage />}
              {path === wanshitongRoutes.jobs && <AdminJobsPage />}
              {path === wanshitongRoutes.history && (
                <HistoryPage fixedScope go={go} services={historyServices} />
              )}
              {path === wanshitongRoutes.traces && (
                <OperationalTracesPage
                  fixedScope
                  go={go}
                  services={traceServices}
                />
              )}
              {path === wanshitongRoutes.models && <AdminModelsPage />}
              {path === wanshitongRoutes.system && <AdminSystemPage />}
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
