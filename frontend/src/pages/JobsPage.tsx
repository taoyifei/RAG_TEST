import { RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";
import { api, type HistoryEntry, type Job } from "../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../components/ui";
import { useJobPolling } from "../hooks/use-job-polling";
import { useConsole } from "../state/console-context";

export function JobsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setRevision } = useConsole();
  const [items, setItems] = useState<Job[]>([]);
  const [error, setError] = useState<unknown>();
  const [selected, setSelected] = useState<Job>();
  const [events, setEvents] = useState<NonNullable<HistoryEntry["events"]>>([]);
  const [traceError, setTraceError] = useState<unknown>();
  const [busy, setBusy] = useState<string>();
  const [reload, setReload] = useState(0);
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
            ["queued", "pending", "running"].includes(item.state),
          );
        }),
    [scope, tokens.admin],
  );
  useJobPolling(load, setError, reload);
  async function details(job: Job) {
    setError(undefined);
    setBusy(job.job_id);
    setEvents([]);
    setTraceError(undefined);
    try {
      setSelected(await api.getJob(tokens.admin, job.job_id));
      try {
        setEvents((await api.jobTrace(job.job_id)).events);
      } catch (reason) {
        setTraceError(reason);
      }
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(undefined);
    }
  }
  async function action(job: Job, operation: "retry" | "cancel") {
    setError(undefined);
    setBusy(job.job_id);
    try {
      const changed = await (
        operation === "retry" ? api.retryJob : api.cancelJob
      )(tokens.admin, job.job_id);
      if (selected?.job_id === job.job_id) setSelected(changed);
      setReload((value) => value + 1);
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
              <h3>{stageLabel(item.stage)}</h3>
              <code>{item.job_id}</code>
              {item.safe_error && (
                <p
                  role="status"
                  className={
                    item.state === "succeeded" ? undefined : "error-text"
                  }
                >
                  {item.safe_error}
                </p>
              )}
              <small>
                已尝试 {item.attempt} 次 ·{" "}
                {item.retryable ? "可重试" : "不自动重试"}
              </small>
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
            </div>
            <button
              disabled={busy === item.job_id}
              onClick={() => void details(item)}
            >
              {item.safe_error && item.state !== "succeeded"
                ? "查看原因/日志"
                : "查看任务详情"}
            </button>
            {item.state === "failed_retryable" && item.retryable && (
              <button
                disabled={busy === item.job_id}
                onClick={() => void action(item, "retry")}
              >
                重试此任务
              </button>
            )}
            {["queued", "running"].includes(item.state) && (
              <button
                disabled={busy === item.job_id}
                onClick={() => void action(item, "cancel")}
              >
                取消任务
              </button>
            )}
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
      {selected && (
        <Modal
          title="任务原因与安全日志"
          onClose={() => setSelected(undefined)}
          drawer
        >
          <StatusBadge value={selected.state} />
          <h3>{selected.safe_error || "任务未报告错误。"}</h3>
          <dl className="detail-grid">
            <dt>任务阶段</dt>
            <dd>
              {stageLabel(selected.stage)}（{selected.stage}）
            </dd>
            <dt>错误码</dt>
            <dd>{selected.error_code || "无"}</dd>
            <dt>尝试次数</dt>
            <dd>{selected.attempt}</dd>
            <dt>可重试</dt>
            <dd>{selected.retryable ? "是，可使用重试此任务" : "否"}</dd>
            <dt>文档</dt>
            <dd>{selected.document_id || "—"}</dd>
            <dt>文档版本</dt>
            <dd>{selected.document_version_id || "尚无版本"}</dd>
            <dt>索引版本</dt>
            <dd>{selected.revision_id}</dd>
            <dt>任务标识</dt>
            <dd>{selected.job_id}</dd>
          </dl>
          <p>
            {selected.retryable
              ? "重试会沿用现有文档和作业，不新建同名文档。"
              : selected.safe_error && selected.state !== "succeeded"
                ? "可在文档管理中为原文档重新上传版本；无需重新登记一个同名文档。"
                : "完成后可在文档管理核对当前索引收录情况。"}
          </p>
          <section aria-label="任务安全日志">
            <h3>任务安全日志</h3>
            {traceError !== undefined && <ErrorPanel error={traceError} />}
            {!events.length && traceError === undefined && (
              <p>此任务暂无已保存的细分事件。</p>
            )}
            {events.map((event, index) => (
              <details key={`${event.event_name}:${index}`}>
                <summary>
                  {event.event_name} · {event.occurred_at}
                </summary>
                <pre>{JSON.stringify(event.attributes, null, 2)}</pre>
              </details>
            ))}
          </section>
          <details>
            <summary>写入隔离技术状态</summary>
            <p>
              {selected.fencing_safe_status === "released"
                ? "released：写入租约已释放，是作业收尾状态，不代表失败原因。"
                : selected.fencing_safe_status}
            </p>
          </details>
          <button onClick={() => go("/documents")}>返回文档管理</button>
        </Modal>
      )}
    </section>
  );
}

function stageLabel(stage: string): string {
  return (
    (
      {
        queued: "等待处理",
        claimed: "准备文档",
        parsing: "解析文档",
        ir_validation: "校验文档结构",
        chunking: "整理检索片段",
        embedding: "构建文档向量",
        indexing: "写入索引",
        validating: "校验索引",
        activated: "索引已启用",
        unchanged: "已在当前索引，无需重建",
        finalizing: "完成版本登记",
        failed: "处理失败",
        cancelled: "任务已取消",
        retry_queued: "等待重试",
      } as Record<string, string>
    )[stage] || "文档处理"
  );
}
