import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type SystemStatus } from "../api/client";
import { ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function SystemPage() {
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
            <p>以下为接口返回的配置状态；未完成连接测试时不代表可用。</p>
            <pre>{JSON.stringify(status.components, null, 2)}</pre>
          </section>
        </>
      )}
    </section>
  );
}
