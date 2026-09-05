import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, createIdempotencyKey, type KnowledgeBase } from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function KnowledgeBasesPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setKnowledgeBase } = useConsole();
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState<unknown>();
  const load = useCallback(() => {
    if (!scope.projectId) return;
    api
      .listKnowledgeBases(tokens.admin, scope.projectId, offset)
      .then((p) => {
        setItems(p.items);
        setNextOffset(p.next_offset ?? null);
      })
      .catch(setError);
  }, [scope.projectId, tokens.admin, offset]);
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
