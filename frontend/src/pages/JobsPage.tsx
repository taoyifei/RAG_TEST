import { RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";
import { api, type Job } from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useJobPolling } from "../hooks/use-job-polling";
import { useConsole } from "../state/console-context";

export function JobsPage({ go }: { go: (path: string) => void }) {
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
        .then((p) => {
          setItems(p.items);
          setError(undefined);
          return p.items.some((item) =>
            ["queued", "pending", "running", "failed_retryable"].includes(
              item.state,
            ),
          );
        }),
    [scope, tokens.admin],
  );
  useJobPolling(load, setError);
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>任务</h2>
          <p>只在页面可见且存在活动任务时刷新，任务完成后停止。</p>
        </div>
        <button
          className="secondary"
          onClick={() => void load().catch(setError)}
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
