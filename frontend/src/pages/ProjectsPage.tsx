import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, createIdempotencyKey, type Project } from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function ProjectsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setProject } = useConsole();
  const [items, setItems] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState<unknown>();
  const load = useCallback(
    () =>
      api
        .listProjects(tokens.admin, offset)
        .then((p) => {
          setItems(p.items);
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
