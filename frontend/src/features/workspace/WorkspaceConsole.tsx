import {
  Activity,
  Boxes,
  Database,
  FileSearch,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquareText,
  Network,
  RefreshCw,
  Search,
  Settings,
  Upload,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import {
  ApiError,
  api,
  createIdempotencyKey,
  type ChunkPage,
  type Document,
  type DocumentVersion,
  type Evidence,
  type Job,
  type KnowledgeBase,
  type Project,
  type QueryResponse,
  type RetrievalDiagnostics,
  type RevisionInspection,
  type SystemStatus,
} from "../../api/client";
import {
  EmptyState,
  ErrorPanel,
  EvidenceDrawer,
  StatusBadge,
} from "../../components/ui";
import { routes, useRouter } from "../../app/router";
import { zhCN } from "../../copy/zh-CN";
import { useJobPolling } from "../../hooks/use-job-polling";
import { AccessTokensPage } from "../../pages/AccessTokensPage";
import { FirstRunWizard } from "../../pages/FirstRunWizard";
import { ModelServicesPage } from "../../pages/ModelServicesPage";
import { RetrievalProfilesPage } from "../../pages/RetrievalProfilesPage";
import { useConsole } from "../../state/console-context";

const primaryNav = [
  [routes.workspace, zhCN.navigation.workspace, LayoutDashboard],
  [routes.projects, zhCN.navigation.knowledge, FolderKanban],
  [routes.chat, zhCN.navigation.chat, MessageSquareText],
  [routes.modelServices, zhCN.navigation.modelServices, KeyRound],
] as const;

const operationsNav = [
  [routes.documents, zhCN.navigation.documents, Database],
  [routes.jobs, zhCN.navigation.jobs, Activity],
  [routes.revision, zhCN.navigation.revisions, Boxes],
  [routes.retrieval, zhCN.navigation.retrieval, FileSearch],
  [routes.retrievalProfiles, "检索方案", Settings],
  [routes.system, zhCN.navigation.system, Network],
  [routes.access, zhCN.navigation.access, KeyRound],
] as const;

function ScopeGuard({ children }: { children: React.ReactNode }) {
  const { scope } = useConsole();
  if (!scope.projectId || !scope.kbId) {
    return (
      <EmptyState title="请先选择知识库">
        从“项目”和“知识库”页面建立当前工作范围。
      </EmptyState>
    );
  }
  return children;
}

export default function App() {
  const { path, go } = useRouter();
  const { scope, tokens, session, logout, rotateSession } = useConsole();
  const [menuOpen, setMenuOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [shellStatus, setShellStatus] = useState<SystemStatus>();
  useEffect(() => {
    if (!tokens.admin) return;
    void api
      .system(tokens.admin)
      .then(setShellStatus)
      .catch(() => undefined);
  }, [tokens.admin]);
  const active = [...primaryNav, ...operationsNav].find(
    ([url]) => url === path,
  )?.[1] ?? "控制台";
  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
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
          <details className="nav-group" open>
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
          <code>{scope.projectId || "未选择项目"}</code>
          <code>{scope.kbId || "未选择知识库"}</code>
          <code>索引版本：{scope.revisionId || "未选择"}</code>
        </div>
      </aside>
      <main id="main-content">
        <header className="topbar">
          <button
            className="icon-button menu-button"
            onClick={() => setMenuOpen(!menuOpen)}
            aria-label="打开导航"
          >
            <Menu aria-hidden="true" />
          </button>
          <div>
            <span className="eyebrow">安全管理控制台</span>
            <h1>{active}</h1>
          </div>
          <div className="topbar-actions">
            {session.authenticated ? (
              <>
                <button className="secondary" onClick={() => void rotateSession()}>
                  <RefreshCw aria-hidden="true" size={17} />
                  轮换会话
                </button>
                <button className="secondary" onClick={() => void logout()}>
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
          {!session.ready && <EmptyState title="正在恢复安全会话" />}
          {session.ready && !session.authenticated && (
            <EmptyState title="需要管理员登录">
              首次输入部署侧凭据后，浏览器只保留安全会话 Cookie。
            </EmptyState>
          )}
          {session.authenticated && (
            <>
              {path === "/" && <Dashboard go={go} />}
              {path === "/projects" && <ProjectsPage go={go} />}
              {path === "/knowledge-bases" && <KnowledgeBasesPage go={go} />}
              {path === "/documents" && (
                <ScopeGuard>
                  <DocumentsPage go={go} />
                </ScopeGuard>
              )}
              {path === "/jobs" && <JobsPage go={go} />}
              {path === "/revision" && (
                <ScopeGuard>
                  <RevisionPage />
                </ScopeGuard>
              )}
              {path === "/retrieval" && (
                <ScopeGuard>
                  <QueryPage mode="search" />
                </ScopeGuard>
              )}
              {path === "/chat" && (
                <ScopeGuard>
                  <QueryPage mode="answer" />
                </ScopeGuard>
              )}
              {path === "/system" && <SystemPage />}
              {path === routes.modelServices && <ModelServicesPage />}
              {path === routes.retrievalProfiles && <RetrievalProfilesPage />}
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

function Dashboard({ go }: { go: (path: string) => void }) {
  const { scope } = useConsole();
  return (
    <>
      <section className="dashboard-actions">
        <div>
          <h2>当前工作范围</h2>
          <p>选择项目和知识库后即可管理文档与检索。</p>
        </div>
        <button className="primary" onClick={() => go("/documents")}>
          <Upload aria-hidden="true" size={18} />
          上传文档
        </button>
      </section>
      <div className="metric-grid">
        <article>
          <span>项目</span>
          <strong>{scope.projectId ? "已选择" : "待选择"}</strong>
          <code>{scope.projectId || "—"}</code>
        </article>
        <article>
          <span>知识库</span>
          <strong>{scope.kbId ? "已选择" : "待选择"}</strong>
          <code>{scope.kbId || "—"}</code>
        </article>
        <article>
          <span>当前索引版本</span>
          <strong>{scope.revisionId ? "已绑定" : "待构建"}</strong>
          <code>{scope.revisionId || "—"}</code>
        </article>
      </div>
    </>
  );
}

function ProjectsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setProject } = useConsole();
  const [items, setItems] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState<unknown>();
  const load = useCallback(
    () =>
      api
        .listProjects(tokens.admin)
        .then((p) => setItems(p.items))
        .catch(setError),
    [tokens.admin],
  );
  useEffect(() => {
    if (tokens.admin) void load();
  }, [load, tokens.admin]);
  async function create(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    try {
      const item = await api.createProject(
        tokens.admin,
        name,
        createIdempotencyKey("project"),
      );
      setItems((old) => [item, ...old]);
      setName("");
    } catch (reason) {
      setError(reason);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>项目</h2>
          <p>项目是知识库的最高隔离边界。</p>
        </div>
        <form className="inline-form" onSubmit={create}>
          <label className="sr-only" htmlFor="project-name">
            项目名称
          </label>
          <input
            id="project-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="新项目名称"
            required
          />
          <button className="primary">创建</button>
        </form>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="card-list">
        {items.map((item) => (
          <article
            key={item.project_id}
            className={scope.projectId === item.project_id ? "selected" : ""}
          >
            <div>
              <h3>{item.name}</h3>
              <code>{item.project_id}</code>
            </div>
            <StatusBadge value={item.status} />
            <button
              className="secondary"
              onClick={() => {
                setProject(item.project_id);
                go("/knowledge-bases");
              }}
            >
              进入
            </button>
          </article>
        ))}
      </div>
      {!items.length && (
        <EmptyState title="暂无项目">创建第一个项目后继续。</EmptyState>
      )}
    </section>
  );
}

function KnowledgeBasesPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setKnowledgeBase } = useConsole();
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState<unknown>();
  const load = useCallback(() => {
    if (!scope.projectId) return;
    api
      .listKnowledgeBases(tokens.admin, scope.projectId)
      .then((p) => setItems(p.items))
      .catch(setError);
  }, [scope.projectId, tokens.admin]);
  useEffect(load, [load]);
  async function create(event: FormEvent) {
    event.preventDefault();
    try {
      const item = await api.createKnowledgeBase(
        tokens.admin,
        scope.projectId,
        name,
        createIdempotencyKey("kb"),
      );
      setItems((old) => [item, ...old]);
      setName("");
    } catch (reason) {
      setError(reason);
    }
  }
  if (!scope.projectId)
    return (
      <EmptyState title="请先选择项目">知识库必须归属于一个项目。</EmptyState>
    );
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>知识库</h2>
          <p>选择后，文档与检索页只在当前范围工作。</p>
        </div>
        <form className="inline-form" onSubmit={create}>
          <label className="sr-only" htmlFor="kb-name">
            知识库名称
          </label>
          <input
            id="kb-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="新知识库名称"
            required
          />
          <button className="primary">创建</button>
        </form>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="card-list">
        {items.map((item) => (
          <article
            key={item.knowledge_base_id}
            className={scope.kbId === item.knowledge_base_id ? "selected" : ""}
          >
            <div>
              <h3>{item.name}</h3>
              <code>{item.knowledge_base_id}</code>
              <small>
                索引版本：{item.active_index_revision_id ?? "未构建"}
              </small>
            </div>
            <StatusBadge value={item.status} />
            <button
              className="secondary"
              onClick={() => {
                setKnowledgeBase(
                  item.knowledge_base_id,
                  item.active_index_revision_id,
                );
                go("/documents");
              }}
            >
              进入
            </button>
          </article>
        ))}
      </div>
      {!items.length && (
        <EmptyState title="暂无知识库">创建一个知识库以接收文档。</EmptyState>
      )}
    </section>
  );
}

function DocumentsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setRevision } = useConsole();
  const [items, setItems] = useState<Document[]>([]);
  const [error, setError] = useState<unknown>();
  const [uploading, setUploading] = useState(false);
  const [detail, setDetail] = useState<{
    document: Document;
    versions: DocumentVersion[];
  }>();
  const load = useCallback(
    () =>
      api
        .listDocuments(tokens.admin, scope.projectId, scope.kbId)
        .then((p) => setItems(p.items))
        .catch(setError),
    [scope, tokens.admin],
  );
  useEffect(() => {
    void load();
  }, [load]);
  async function upload(file: File) {
    setUploading(true);
    setError(undefined);
    try {
      const job = await api.uploadDocument(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        file,
        createIdempotencyKey("document"),
      );
      setRevision(job.revision_id);
      go("/jobs");
    } catch (reason) {
      setError(reason);
    } finally {
      setUploading(false);
    }
  }
  async function inspect(document: Document) {
    setError(undefined);
    try {
      const [fresh, versions] = await Promise.all([
        api.getDocument(
          tokens.admin,
          scope.projectId,
          scope.kbId,
          document.document_id,
        ),
        api.listVersions(
          tokens.admin,
          scope.projectId,
          scope.kbId,
          document.document_id,
        ),
      ]);
      setDetail({ document: fresh, versions: versions.items });
    } catch (reason) {
      setError(reason);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>文档</h2>
          <p>
            新文档会获得新的 document_id；相同文件字节可以复用物理
            Artifact。新版本保留 document_id。
          </p>
        </div>
        <label className="primary file-button">
          <Upload aria-hidden="true" size={18} />
          {uploading ? "上传中…" : "新建文档"}
          <input
            data-testid="new-document-file"
            type="file"
            accept=".doc,.docx"
            disabled={uploading}
            onChange={(e) =>
              e.target.files?.[0] && void upload(e.target.files[0])
            }
          />
        </label>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>显示名</th>
              <th>文档标识</th>
              <th>当前版本</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.document_id}>
                <td>{item.display_name}</td>
                <td>
                  <code>{item.document_id}</code>
                </td>
                <td>
                  <code>{item.current_version_id ?? "—"}</code>
                </td>
                <td>
                  <StatusBadge value={item.status} />
                </td>
                <td>
                  <DocumentActions
                    document={item}
                    reload={load}
                    go={go}
                    inspect={() => void inspect(item)}
                    onError={setError}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <section className="panel" aria-label="文档详情">
          <div className="section-heading">
            <div>
              <span className="eyebrow">逻辑文档</span>
              <h3>{detail.document.display_name}</h3>
              <code>{detail.document.document_id}</code>
            </div>
            <StatusBadge value={detail.document.status} />
          </div>
          <h4>不可变版本时间线</h4>
          <div className="version-list">
            {detail.versions.map((version) => (
              <article key={version.document_version_id}>
                <div>
                  <strong>{version.document_version_id}</strong>
                  <small>{version.created_at}</small>
                </div>
                <dl className="detail-grid">
                  <dt>内容指纹</dt>
                  <dd>{version.content_sha256}</dd>
                  <dt>来源文件指纹</dt>
                  <dd>{version.source_artifact_id}</dd>
                  <dt>大小</dt>
                  <dd>{version.size_bytes} bytes</dd>
                </dl>
              </article>
            ))}
          </div>
        </section>
      )}
      {!items.length && (
        <EmptyState title="暂无文档">
          上传 DOC 或 DOCX 后，系统会创建不可变版本与新的索引版本。
        </EmptyState>
      )}
    </section>
  );
}

function DocumentActions({
  document,
  reload,
  go,
  inspect,
  onError,
}: {
  document: Document;
  reload: () => void;
  go: (path: string) => void;
  inspect: () => void;
  onError: (error: unknown) => void;
}) {
  const { tokens, scope, setRevision } = useConsole();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(document.display_name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  async function rename() {
    await api.renameDocument(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
      name,
    );
    await api.getDocument(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
    );
    setRenaming(false);
    reload();
  }
  async function version(file: File) {
    const job = await api.uploadVersion(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
      file,
      createIdempotencyKey("version"),
    );
    setRevision(job.revision_id);
    go("/jobs");
  }
  async function remove() {
    try {
      await api.deleteDocument(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        document.document_id,
      );
      setConfirmDelete(false);
      reload();
    } catch (reason) {
      onError(reason);
    }
  }
  return (
    <div className="row-actions">
      {renaming ? (
        <>
          <input
            aria-label="新显示名"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button onClick={() => void rename()}>保存</button>
          <small>只改显示名，不创建新 dver 或重建索引。</small>
        </>
      ) : (
        <button onClick={() => setRenaming(true)}>重命名</button>
      )}
      <label className="button-link">
        创建新版本
        <input
          data-testid={`version-${document.document_id}`}
          type="file"
          accept=".doc,.docx"
          onChange={(e) =>
            e.target.files?.[0] && void version(e.target.files[0])
          }
        />
      </label>
      <button onClick={inspect}>详情</button>
      <button
        className={confirmDelete ? "danger" : ""}
        onClick={() => (confirmDelete ? void remove() : setConfirmDelete(true))}
      >
        {confirmDelete ? "确认删除" : "删除"}
      </button>
    </div>
  );
}

function JobsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setRevision } = useConsole();
  const [items, setItems] = useState<Job[]>([]);
  const [error, setError] = useState<unknown>();
  const load = useCallback(
    () =>
      api
        .listJobs(
          tokens.admin,
          scope.projectId || undefined,
          scope.kbId || undefined,
        )
        .then((p) => setItems(p.items))
        .catch(setError),
    [scope, tokens.admin],
  );
  useJobPolling(load);
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>任务</h2>
          <p>页面可见时快速刷新，转入后台后自动退避。</p>
        </div>
        <button className="secondary" onClick={load}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="card-list">
        {items.map((item) => (
          <article key={item.job_id}>
            <div className="grow">
              <h3>{item.stage}</h3>
              <code>{item.job_id}</code>
              <small>索引版本：{item.revision_id}</small>
              <div className="progress-list">
                {item.slot_progress.map((slot) => (
                  <span key={slot.slot_id}>
                    {slot.slot_id}: {slot.completed}/{slot.total}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <StatusBadge value={item.state} />
              <small>写入隔离：{item.fencing_safe_status}</small>
            </div>
            <button
              className="secondary"
              onClick={() => {
                setRevision(item.revision_id);
                go("/revision");
              }}
            >
              检查版本
            </button>
          </article>
        ))}
      </div>
      {!items.length && <EmptyState title="暂无任务" />}
    </section>
  );
}

function RevisionPage() {
  const { tokens, scope } = useConsole();
  const [revision, setRevision] = useState<RevisionInspection>();
  const [chunks, setChunks] = useState<ChunkPage>();
  const [reports, setReports] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    if (!scope.revisionId) return;
    Promise.all([
      api.inspectRevision(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
      api.listChunks(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
      api.revisionReports(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
    ])
      .then(([r, c, p]) => {
        setRevision(r);
        setChunks(c);
        setReports(p.items);
      })
      .catch(setError);
  }, [scope, tokens.admin]);
  if (!scope.revisionId)
    return (
      <EmptyState title="尚未选择索引版本">
        完成一次上传或从任务列表选择版本。
      </EmptyState>
    );
  if (error) return <ErrorPanel error={error} />;
  if (!revision || !chunks) return <p className="loading">正在读取版本事实…</p>;
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>索引版本</h2>
          <code>{revision.revision_id}</code>
        </div>
        <StatusBadge value={revision.active ? "active" : "inactive"} />
      </div>
      <div className="metric-grid">
        <article>
          <span>文档</span>
          <strong>
            {revision.actual_document_count} /{" "}
            {revision.expected_document_count}
          </strong>
        </article>
        <article>
          <span>内容块 / 关键词索引</span>
          <strong>
            {revision.actual_chunk_count} / {revision.fts_count}
          </strong>
        </article>
        <article>
          <span>写入状态</span>
          <strong>{revision.writer_status}</strong>
        </article>
      </div>
      <div className="panel">
        <h3>向量槽覆盖</h3>
        <pre>{JSON.stringify(revision.slot_coverages, null, 2)}</pre>
      </div>
      <div className="panel">
        <h3>标准内容块</h3>
        {chunks.items.map((chunk) => (
          <details key={chunk.chunk_id}>
            <summary>
              {chunk.role} · {chunk.section_id} · {chunk.chunk_id}
            </summary>
            <h4>引用文本</h4>
            <p>{chunk.citation_text}</p>
            <h4>向量文本</h4>
            <p>{chunk.embedding_text}</p>
            <h4>关键词文本</h4>
            <p>{chunk.lexical_text}</p>
            <pre>{JSON.stringify(chunk.source_spans, null, 2)}</pre>
          </details>
        ))}
      </div>
      <div className="panel">
        <h3>解析与分块报告</h3>
        <pre>{JSON.stringify(reports, null, 2)}</pre>
      </div>
    </section>
  );
}

export function QueryPage({ mode }: { mode: "search" | "answer" }) {
  const { tokens, scope } = useConsole();
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<QueryResponse>();
  const [diagnostics, setDiagnostics] = useState<RetrievalDiagnostics>();
  const [diagnosticsError, setDiagnosticsError] = useState<unknown>();
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const activeRequest = useRef<AbortController | undefined>(undefined);
  useEffect(
    () => () => {
      activeRequest.current?.abort();
    },
    [],
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError(undefined);
    setDiagnostics(undefined);
    setDiagnosticsError(undefined);
    try {
      const response =
        mode === "search"
          ? await api.search(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
            )
          : await api.answerStream(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
            );
      setResult(response);
      if (mode === "search") {
        void api
          .diagnostics(tokens.admin, response.trace_id)
          .then(setDiagnostics)
          .catch(setDiagnosticsError);
      }
    } catch (reason) {
      setError(
        reason instanceof DOMException && reason.name === "AbortError"
          ? new Error("流式回答已中断，请重新提交查询。")
          : reason,
      );
    } finally {
      if (activeRequest.current === controller) setBusy(false);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>{mode === "search" ? "检索诊断" : "证据问答"}</h2>
          <p>
            {mode === "search"
              ? "查看真实通道、RRF 贡献、重排与证据选择。"
              : "答案仅在证据满足发布门禁时展示。"}
          </p>
        </div>
      </div>
      <form className="query-box" onSubmit={submit}>
        <label htmlFor={`${mode}-query`}>查询文本</label>
        <div>
          <input
            id={`${mode}-query`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="例如：青岛啤酒"
            required
          />
          <button className="primary" disabled={busy}>
            <Search aria-hidden="true" size={18} />
            {busy ? "执行中…" : "执行"}
          </button>
        </div>
      </form>
      {error !== undefined && <ErrorPanel error={error} />}
      {result && (
        <>
          <div className="metric-grid">
            <article>
              <span>状态</span>
              <StatusBadge value={result.status} />
              <small>{result.reason_code}</small>
            </article>
            <article>
              <span>路由</span>
              <strong>{result.route_reason_code}</strong>
              <small>{result.selected_embedding_slot ?? "无 dense slot"}</small>
            </article>
            <article>
              <span>证据</span>
              <strong>{result.evidence_count}</strong>
              <small>{result.quality_profile_status}</small>
            </article>
          </div>
          {mode === "answer" && result.answer && (
            <article className="answer">
              <span className="eyebrow">有依据的回答</span>
              <p>{result.answer}</p>
            </article>
          )}
          <div className="evidence-grid">
            {result.evidence.map((item) => (
              <button
                key={item.evidence_id}
                className="evidence-card"
                onClick={() => setEvidence(item)}
              >
                <span>{item.source_label}</span>
                <p>{item.citation_text}</p>
                <small>
                  {item.selection_reason} · fusion #{item.fusion_rank ?? "—"}
                </small>
              </button>
            ))}
          </div>
          {!result.evidence.length && (
            <EmptyState title="没有可发布证据">
              系统不会为无证据结果生成伪引用。
            </EmptyState>
          )}
          {diagnostics && <DiagnosticsView value={diagnostics} />}
          {diagnosticsError !== undefined && (
            <details className="panel">
              <summary>诊断信息暂不可用</summary>
              <ErrorPanel error={diagnosticsError} />
            </details>
          )}
        </>
      )}
      <EvidenceDrawer evidence={evidence} onClose={() => setEvidence(null)} />
    </section>
  );
}

function DiagnosticsView({ value }: { value: RetrievalDiagnostics }) {
  return (
    <div className="panel">
      <h3>安全检索诊断</h3>
      <div className="diagnostic-columns">
        <div>
          <h4>通道候选</h4>
          {value.channel_chunk_ids.map(([channel, ids]) => (
            <details key={channel}>
              <summary>
                {channel} · {ids.length}
              </summary>
              <pre>{ids.join("\n")}</pre>
            </details>
          ))}
        </div>
        <div>
          <h4>RRF 融合贡献</h4>
          {value.fusion.map((item) => (
            <details key={item.chunk_id}>
              <summary>
                #{item.rank} · {item.chunk_id}
              </summary>
              <pre>{JSON.stringify(item.contributions, null, 2)}</pre>
            </details>
          ))}
        </div>
        <div>
          <h4>阶段耗时</h4>
          {value.stage_timings.map((item) => (
            <p key={item.stage}>
              {item.stage}
              <strong>{item.elapsed_ms.toFixed(2)} ms</strong>
            </p>
          ))}
        </div>
      </div>
    </div>
  );
}

function SystemPage() {
  const { tokens } = useConsole();
  const [status, setStatus] = useState<SystemStatus>();
  const [error, setError] = useState<unknown>();
  const load = useCallback(
    () => api.system(tokens.admin).then(setStatus).catch(setError),
    [tokens.admin],
  );
  useEffect(() => {
    if (tokens.admin) void load();
  }, [load, tokens.admin]);
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>系统状态</h2>
          <p>状态来自兼容清单、持久化配置与最近连接验证。</p>
        </div>
        <button className="secondary" onClick={() => void load()}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      {status && (
        <>
          <div className="metric-grid">
            <article>
              <span>离线评测证据</span>
              <StatusBadge value={status.offline_evaluation_v3_ready} />
            </article>
            <article>
              <span>主向量连接</span>
              <StatusBadge value={status.primary_live_evaluation_status} />
            </article>
            <article>
              <span>备用向量连接</span>
              <StatusBadge value={status.standby_live_evaluation_status} />
            </article>
          </div>
          <dl className="detail-grid panel">
            <dt>运行配置</dt>
            <dd>{status.profile_id}</dd>
            <dt>索引结构</dt>
            <dd>{status.active_revision_schema}</dd>
            <dt>关键词检索</dt>
            <dd>
              {status.lexical_schema} · {status.lexical_analyzer_id}
            </dd>
            <dt>需要重建索引</dt>
            <dd>{status.reindex_required ? "是" : "否"}</dd>
            <dt>数据完整性</dt>
            <dd>{status.integrity_status}</dd>
            <dt>待清理项目</dt>
            <dd>{status.pending_gc_items}</dd>
            <dt>索引语义指纹</dt>
            <dd>{status.index_fingerprint}</dd>
            <dt>查询配置指纹</dt>
            <dd>{status.serving_fingerprint}</dd>
          </dl>
          <section className="panel">
            <h3>组件与服务身份</h3>
            <p>
              以下为接口返回的配置状态；未完成连接测试时不代表可用。
            </p>
            <pre>{JSON.stringify(status.components, null, 2)}</pre>
          </section>
        </>
      )}
    </section>
  );
}

export function formatApiError(error: unknown): string {
  if (!(error instanceof ApiError)) return "未知错误";
  return `${error.code}: ${error.message}`;
}
