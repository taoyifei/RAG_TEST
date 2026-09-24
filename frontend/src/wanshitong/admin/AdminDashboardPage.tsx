import { RefreshCw, Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { EmptyState, ErrorPanel, StatusBadge } from "../../components/ui";
import { historyTime } from "../../components/HistoryTrace";
import { wanshitongAdminApi, type WanshitongOverview } from "./adminApi";

const supportedFormats = [
  ["docx", "DOCX"],
  ["md", "Markdown"],
  ["txt", "TXT"],
  ["pptx", "PPTX"],
  ["xlsx", "XLSX"],
  ["csv", "CSV"],
  ["pdf", "PDF"],
  ["doc", "DOC"],
  ["excel", "旧版 Excel"],
  ["zip", "ZIP"],
] as const;

function ProviderCard({
  label,
  provider,
}: {
  label: string;
  provider: WanshitongOverview["providers"]["embedding"];
}) {
  return (
    <article>
      <span>{label}</span>
      <StatusBadge value={provider.configured} />
      <strong>{provider.model || "未配置"}</strong>
      <small>{provider.status || provider.validation_status || "尚未验证"}</small>
    </article>
  );
}

export function AdminDashboardPage({ go }: { go: (path: string) => void }) {
  const [overview, setOverview] = useState<WanshitongOverview>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    setError(undefined);
    void wanshitongAdminApi
      .overview()
      .then(setOverview)
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void wanshitongAdminApi
      .overview(controller.signal)
      .then(setOverview)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>固定知识范围概览</h2>
          <p>所有管理员操作都限定在湾事通隐藏 Project 与 Knowledge Base。</p>
        </div>
        <div className="row-actions">
          <button className="primary" onClick={() => go("/documents")}>
            <Upload aria-hidden="true" size={17} />
            上传 DOCX
          </button>
          <button className="secondary" onClick={load}>
            <RefreshCw aria-hidden="true" size={17} />
            刷新
          </button>
        </div>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      {loading && !overview && <p role="status">正在读取概览…</p>}
      {overview && (
        <>
          <div className="metric-grid">
            <article>
              <span>Project / Knowledge Base</span>
              <strong>
                {overview.scope.project_name} / {overview.scope.knowledge_base_name}
              </strong>
              <small>固定隐藏 Scope</small>
            </article>
            <article>
              <span>文档</span>
              <strong>{overview.documents.total}</strong>
              <small>{overview.documents.retrievable} 个已可检索</small>
            </article>
            <article>
              <span>处理中任务</span>
              <strong>{overview.jobs.queued + overview.jobs.running}</strong>
              <small>
                排队 {overview.jobs.queued} · 运行 {overview.jobs.running}
              </small>
            </article>
            <article>
              <span>失败任务</span>
              <strong>
                {overview.jobs.failed_retryable + overview.jobs.failed_terminal}
              </strong>
              <small>
                可重试 {overview.jobs.failed_retryable} · 终止 {overview.jobs.failed_terminal}
              </small>
            </article>
            <article>
              <span>活动 Index Revision</span>
              <strong>{overview.active_index_revision_id ? "已激活" : "未激活"}</strong>
              <code>{overview.active_index_revision_id || "—"}</code>
            </article>
            <article>
              <span>公共问答</span>
              <StatusBadge value={overview.public_query_ready} />
              <small>{overview.public_query_ready ? "可以回答" : "尚未就绪"}</small>
            </article>
          </div>
          <div className="metric-grid">
            <ProviderCard label="Embedding" provider={overview.providers.embedding} />
            <ProviderCard label="Reranker" provider={overview.providers.reranker} />
            <ProviderCard label="LLM" provider={overview.providers.llm} />
          </div>
          <section className="panel">
            <h3>文档格式与保留策略</h3>
            <div className="format-status-grid">
              {supportedFormats.map(([key, label]) => (
                <span key={key}>
                  <strong>{label}</strong>
                  <StatusBadge
                    value={overview.supported_formats[key] === "enabled"}
                  />
                </span>
              ))}
            </div>
            <p>问答历史保留 {overview.history_retention_days} 天。</p>
          </section>
          <div className="admin-summary-grid">
            <section className="panel">
              <h3>最近问答</h3>
              {!overview.recent_history.length && (
                <EmptyState title="还没有问答记录" />
              )}
              {overview.recent_history.map((item) => (
                <article className="summary-row" key={item.trace_id}>
                  <div>
                    <strong>{item.question || item.answer_summary || "未保存正文"}</strong>
                    <small>{historyTime(item.created_at)}</small>
                  </div>
                  <StatusBadge value={item.status} />
                </article>
              ))}
            </section>
            <section className="panel">
              <h3>最近错误</h3>
              {!overview.recent_errors.length && (
                <EmptyState title="近期没有错误" />
              )}
              {overview.recent_errors.map((item, index) => (
                <article className="summary-row" key={`${item.code}:${index}`}>
                  <div>
                    <strong>{item.code || "UNKNOWN_ERROR"}</strong>
                    <small>{item.message || "未提供安全错误摘要"}</small>
                  </div>
                  <time dateTime={item.occurred_at || undefined}>
                    {item.occurred_at ? historyTime(item.occurred_at) : "—"}
                  </time>
                </article>
              ))}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
