import { RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";

import { type Job } from "../../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../../components/ui";
import { useJobPolling } from "../../hooks/use-job-polling";
import { stageLabel } from "../../pages/JobsPage";
import { wanshitongAdminApi } from "./adminApi";

function activeJob(job: Job): boolean {
  return ["queued", "pending", "running"].includes(job.state);
}

export function AdminJobsPage() {
  const [items, setItems] = useState<Job[]>([]);
  const [selected, setSelected] = useState<Job>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState<string>();
  const [refreshKey, setRefreshKey] = useState(0);
  const load = useCallback(async () => {
    const page = await wanshitongAdminApi.listJobs();
    setItems(page.items);
    setError(undefined);
    return page.items.some(activeJob);
  }, []);
  useJobPolling(load, setError, refreshKey);

  async function inspect(job: Job) {
    setBusy(job.job_id);
    setError(undefined);
    try {
      setSelected(await wanshitongAdminApi.job(job.job_id));
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(undefined);
    }
  }

  async function operate(job: Job, action: "retry" | "cancel") {
    setBusy(job.job_id);
    setError(undefined);
    try {
      const changed = await (action === "retry"
        ? wanshitongAdminApi.retryJob(job.job_id)
        : wanshitongAdminApi.cancelJob(job.job_id));
      setItems((current) =>
        current.map((item) => (item.job_id === changed.job_id ? changed : item)),
      );
      if (selected?.job_id === changed.job_id) setSelected(changed);
      setRefreshKey((value) => value + 1);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>处理任务</h2>
          <p>状态直接来自 Universal Job；活动任务完成后自动停止轮询。</p>
        </div>
        <button
          className="secondary"
          onClick={() => setRefreshKey((value) => value + 1)}
        >
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="card-list">
        {items.map((item) => (
          <article key={item.job_id}>
            <div className="grow">
              <h3>{stageLabel(item.stage)}</h3>
              <code>{item.job_id}</code>
              <small>关联文档：{item.document_id || "—"}</small>
              {item.safe_error && (
                <p className="error-text" role="status">
                  {item.safe_error}
                </p>
              )}
              <small>
                当前阶段 {item.stage} · 已尝试 {item.attempt} 次 ·{" "}
                {item.retryable ? "可重试" : "不可自动重试"}
              </small>
            </div>
            <StatusBadge value={item.state} />
            <div className="row-actions">
              <button
                disabled={busy === item.job_id}
                onClick={() => void inspect(item)}
              >
                查看详情
              </button>
              {item.retryable && item.state === "failed_retryable" && (
                <button
                  disabled={busy === item.job_id}
                  onClick={() => void operate(item, "retry")}
                >
                  重试
                </button>
              )}
              {activeJob(item) && (
                <button
                  disabled={busy === item.job_id}
                  onClick={() => void operate(item, "cancel")}
                >
                  取消
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
      {!items.length && <EmptyState title="暂无处理任务" />}
      {selected && (
        <Modal title="任务详情" drawer onClose={() => setSelected(undefined)}>
          <StatusBadge value={selected.state} />
          <dl className="detail-grid">
            <dt>当前阶段</dt>
            <dd>{stageLabel(selected.stage)}（{selected.stage}）</dd>
            <dt>安全错误码</dt>
            <dd>{selected.error_code || "无"}</dd>
            <dt>安全错误</dt>
            <dd>{selected.safe_error || "无"}</dd>
            <dt>可重试</dt>
            <dd>{selected.retryable ? "是" : "否"}</dd>
            <dt>关联文档</dt>
            <dd>{selected.document_id || "—"}</dd>
            <dt>文档版本</dt>
            <dd>{selected.document_version_id || "—"}</dd>
            <dt>Index Revision</dt>
            <dd>{selected.revision_id || "—"}</dd>
            <dt>任务标识</dt>
            <dd>{selected.job_id}</dd>
          </dl>
        </Modal>
      )}
    </section>
  );
}
