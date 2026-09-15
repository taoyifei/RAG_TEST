import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ErrorPanel, StatusBadge } from "../../components/ui";
import { wanshitongAdminApi, type WanshitongSystem } from "./adminApi";

export function AdminSystemPage() {
  const [status, setStatus] = useState<WanshitongSystem>();
  const [error, setError] = useState<unknown>();
  const load = useCallback(() => {
    setError(undefined);
    void wanshitongAdminApi.system().then(setStatus).catch(setError);
  }, []);
  useEffect(() => {
    void wanshitongAdminApi.system().then(setStatus).catch(setError);
  }, []);
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>系统状态</h2>
          <p>只读取轻量健康信息，不主动发送模型测试请求。</p>
        </div>
        <button className="secondary" onClick={load}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      {!status && error === undefined && <p role="status">正在读取系统状态…</p>}
      {status && (
        <>
          <div className="metric-grid">
            <article>
              <span>产品模式</span>
              <strong>{status.mode}</strong>
            </article>
            <article>
              <span>活动 Index Revision</span>
              <strong>{status.active_index_revision_id ? "已激活" : "未激活"}</strong>
              <code>{status.active_index_revision_id || "—"}</code>
            </article>
            <article>
              <span>前端构建版本</span>
              <strong>{status.frontend_build_id || "未提供"}</strong>
            </article>
          </div>
          <div className="system-status-grid">
            <article className="panel">
              <span>App / Live / Ready</span>
              <div className="row-actions">
                <StatusBadge value={status.app.status} />
                <StatusBadge value={status.live} />
                <StatusBadge value={status.ready} />
              </div>
            </article>
            <article className="panel">
              <span>Qdrant</span>
              <StatusBadge value={status.qdrant.status} />
              <small>{status.qdrant.mode === "memory" ? "内存模式" : "远程 URL 已配置"}</small>
            </article>
            <article className="panel">
              <span>SQLite</span>
              <StatusBadge value={status.sqlite.ready} />
              <small>完整性：{status.sqlite.integrity_status}</small>
            </article>
            <article className="panel">
              <span>固定 Scope</span>
              <StatusBadge value={status.scope.ready} />
              <small>{status.scope.project_id} / {status.scope.knowledge_base_id}</small>
            </article>
            <article className="panel">
              <span>QueryExecutor capacity</span>
              <strong>{status.query_executor.in_flight} / {status.query_executor.max_workers}</strong>
              <small>
                最大队列 {status.query_executor.max_queue} · 重试等待 {status.query_executor.retry_after_seconds}s
              </small>
            </article>
            <article className="panel">
              <span>History</span>
              <StatusBadge value={status.history.ready} />
              <small>保留 {status.history.retention_days} 天</small>
            </article>
            <article className="panel">
              <span>Operational Trace</span>
              <StatusBadge value={status.operational_trace.ready} />
              <small>
                已提交 {status.operational_trace.metrics.submitted ?? 0} · 已写入 {status.operational_trace.metrics.written ?? 0} · 丢弃 {status.operational_trace.metrics.dropped ?? 0}
              </small>
            </article>
            <article className="panel">
              <span>公共 Session</span>
              <StatusBadge value={status.public_session.ready} />
            </article>
          </div>
        </>
      )}
    </section>
  );
}
