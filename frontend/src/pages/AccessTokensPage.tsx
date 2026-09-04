import { Copy, KeyRound, ShieldX } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { api, type AccessTokenSummary } from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function AccessTokensPage() {
  const { scope } = useConsole();
  const [items, setItems] = useState<AccessTokenSummary[]>([]);
  const [name, setName] = useState("");
  const [scopeName, setScopeName] = useState("query:read");
  const [issued, setIssued] = useState("");
  const [error, setError] = useState<unknown>();
  const load = useCallback(
    () => api.listAccessTokens().then((page) => setItems(page.items)),
    [],
  );
  useEffect(() => {
    void load().catch(setError);
  }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setIssued("");
    setError(undefined);
    try {
      const created = await api.createAccessToken({
        name,
        scopes: [scopeName],
        project_id: scope.projectId || null,
        knowledge_base_id: scope.kbId || null,
      });
      setIssued(created.token ?? "");
      setName("");
      await load();
    } catch (reason) {
      setError(reason);
    }
  }

  async function revoke(tokenId: string) {
    try {
      await api.revokeAccessToken(tokenId);
      await load();
    } catch (reason) {
      setError(reason);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <span className="eyebrow">外部系统集成</span>
          <h2>接口访问</h2>
          <p>完整访问令牌只显示一次，服务端仅保存带密钥摘要。</p>
        </div>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <form className="panel inline-form" onSubmit={create}>
        <label className="sr-only" htmlFor="access-name">
          令牌名称
        </label>
        <input
          id="access-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="令牌名称"
          required
        />
        <label className="sr-only" htmlFor="access-scope">
          访问范围
        </label>
        <select
          id="access-scope"
          value={scopeName}
          onChange={(event) => setScopeName(event.target.value)}
        >
          <option value="query:read">只允许查询</option>
          <option value="knowledge:read">只读知识库</option>
          <option value="knowledge:write">管理知识库</option>
          <option value="system:read">读取系统状态</option>
        </select>
        <button className="primary">
          <KeyRound aria-hidden="true" size={17} />
          创建令牌
        </button>
      </form>
      {issued && (
        <section className="one-time-token" role="alert">
          <div>
            <strong>请立即复制</strong>
            <p>关闭此提示后无法再次查看完整令牌。</p>
            <code>{issued}</code>
          </div>
          <button
            onClick={() => void navigator.clipboard.writeText(issued)}
            aria-label="复制完整访问令牌"
          >
            <Copy aria-hidden="true" size={17} />
            复制
          </button>
          <button onClick={() => setIssued("")}>我已保存</button>
        </section>
      )}
      <div className="card-list">
        {items.map((item) => (
          <article key={item.token_id}>
            <div className="grow">
              <h3>{item.name}</h3>
              <code>{item.token_id}</code>
              <small>{item.scopes.join("、")}</small>
            </div>
            <StatusBadge value={item.revoked_at ? "revoked" : "active"} />
            {!item.revoked_at && (
              <button className="danger" onClick={() => void revoke(item.token_id)}>
                <ShieldX aria-hidden="true" size={17} />
                吊销
              </button>
            )}
          </article>
        ))}
      </div>
      {!items.length && <EmptyState title="尚未创建接口访问令牌" />}
    </section>
  );
}
