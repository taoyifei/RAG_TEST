import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, createIdempotencyKey, type Project } from "../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function ProjectsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setProject } = useConsole();
  const [items, setItems] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState<unknown>();
  const [confirmDelete, setConfirmDelete] = useState<Project | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const load = useCallback(
    () =>
      api
        .listProjects(tokens.admin, offset)
        .then((p) => {
          setItems(p.items.filter((item) => item.status === "active"));
          setNextOffset(p.next_offset ?? null);
        })
        .catch(setError),
    [tokens.admin, offset],
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
  async function remove() {
    if (!confirmDelete || deletingId !== null) return;
    const projectId = confirmDelete.project_id;
    setDeletingId(projectId);
    setError(undefined);
    try {
      await api.deleteProject(tokens.admin, projectId);
      setItems((old) => old.filter((item) => item.project_id !== projectId));
      setConfirmDelete(null);
      if (scope.projectId === projectId) {
        setProject("");
        go("/projects");
      }
    } catch (reason) {
      setError(reason);
    } finally {
      setDeletingId(null);
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
      <div className="row-actions" aria-label="分页">
        <button disabled={offset === 0} onClick={() => setOffset(0)}>
          返回首页
        </button>
        <span>从第 {offset + 1} 项开始</span>
        <button
          disabled={nextOffset === null}
          onClick={() => nextOffset !== null && setOffset(nextOffset)}
        >
          下一页
        </button>
      </div>
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
            <div className="row-actions">
              <button
                className="secondary"
                onClick={() => {
                  setProject(item.project_id);
                  go("/knowledge-bases");
                }}
              >
                进入
              </button>
              <button
                className="danger"
                disabled={deletingId !== null}
                onClick={() => setConfirmDelete(item)}
              >
                删除
              </button>
            </div>
          </article>
        ))}
      </div>
      {!items.length && (
        <EmptyState title="暂无项目">创建第一个项目后继续。</EmptyState>
      )}
      {confirmDelete && (
        <Modal
          title={`删除项目“${confirmDelete.name}”`}
          onClose={() => {
            if (deletingId === null) setConfirmDelete(null);
          }}
        >
          <p>将从当前 Demo 列表移除此项目。</p>
          <p>本轮采用逻辑归档，不执行物理数据清理。</p>
          <div className="row-actions">
            <button
              type="button"
              disabled={deletingId !== null}
              onClick={() => setConfirmDelete(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="danger"
              disabled={deletingId !== null}
              onClick={() => void remove()}
            >
              {deletingId === confirmDelete.project_id
                ? "归档中…"
                : "确认归档项目"}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
