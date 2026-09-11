import { RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";
import {
  api,
  type HistoryEntry,
  type Job,
  type RetrievalIngestionAuthorizationStatus,
} from "../api/client";
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
  const [authorization, setAuthorization] = useState<{
    job: Job;
    status: RetrievalIngestionAuthorizationStatus;
  }>();
  const [authorizationError, setAuthorizationError] = useState<unknown>();
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
  async function inspectAuthorization(job: Job) {
    setError(undefined);
    setAuthorizationError(undefined);
    setBusy(job.job_id);
    try {
      const status = await api.retrievalIngestionAuthorization(job.job_id);
      setAuthorization({ job, status });
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(undefined);
    }
  }
  async function approveAuthorization() {
    if (!authorization) return;
    const { job, status } = authorization;
    setAuthorizationError(undefined);
    setBusy(job.job_id);
    try {
      if (["approve", "reauthorize"].includes(status.next_action)) {
        if (!status.approval_allowed) {
          throw new Error("服务端未允许批准此任务，请先处理当前修复动作。");
        }
        const expires = new Date();
        expires.setUTCDate(expires.getUTCDate() + 30);
        const approved = await api.approveRetrievalIngestionAuthorization(
          job.job_id,
          {
            expires_at: expires.toISOString(),
            request_limit: status.recommended_request_limit,
            estimated_token_limit: status.recommended_estimated_token_limit,
            operation_request_limits:
              status.recommended_operation_request_limits,
          },
        );
        setAuthorization({ job, status: approved });
        if (!authorizationReady(approved)) {
          throw new Error(
            "服务端尚未确认授权与预算可用，任务没有自动重试。请按最新状态处理。",
          );
        }
      } else if (status.next_action !== "continue") {
        throw new Error("当前任务需要先完成页面提示的修复动作。");
      }
      await api.retryJob(tokens.admin, job.job_id);
      setAuthorization(undefined);
      setAuthorizationError(undefined);
      setReload((value) => value + 1);
    } catch (reason) {
      setAuthorizationError(reason);
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
            {item.required_action === "approve_retrieval" && (
              <button
                className="primary"
                disabled={busy === item.job_id}
                onClick={() => void inspectAuthorization(item)}
              >
                处理检索授权
              </button>
            )}
            {item.state === "failed_retryable" &&
              item.retryable &&
              !item.required_action && (
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
            {item.revision_available ? (
              <button
                className="secondary"
                onClick={() => {
                  setRevision(item.revision_id);
                  go("/revision");
                }}
              >
                检查版本
              </button>
            ) : (
              <small>此任务尚无可读索引版本。</small>
            )}
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
            <dt>
              {selected.revision_available
                ? "索引版本"
                : "计划 Revision 标识（当前不可读取）"}
            </dt>
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
          <button
            className="secondary"
            onClick={() => {
              const url = new URL(window.location.href);
              url.searchParams.set("job_id", selected.job_id);
              url.searchParams.delete("trace_id");
              window.history.replaceState(
                {},
                "",
                `${url.pathname}${url.search}`,
              );
              go("/operational-traces");
            }}
          >
            打开技术 Trace
          </button>
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
      {authorization && (
        <Modal
          title={authorizationTitle(authorization.status)}
          onClose={() => {
            setAuthorization(undefined);
            setAuthorizationError(undefined);
          }}
        >
          <p role="alert">{authorizationMessage(authorization.status)}</p>
          {authorizationError !== undefined && (
            <ErrorPanel error={authorizationError} />
          )}
          <dl className="detail-grid">
            <dt>当前状态</dt>
            <dd>
              {authorization.status.authorization_state} /{" "}
              {authorization.status.budget_state} /{" "}
              {authorization.status.connection_budget_state}
            </dd>
            <dt>资料数量</dt>
            <dd>{authorization.status.source_document_count}</dd>
            <dt>原文件总大小</dt>
            <dd>{authorization.status.source_size_bytes} bytes</dd>
            <dt>预算估算</dt>
            <dd>
              构建前不可精确估算；以下数值是逐次发送前执行的硬上限，并同时受各模型连接更低的累计上限约束。
            </dd>
            <dt>授权有效期</dt>
            <dd>批准时设为 30 天；到期、资料或方案变化后必须重新批准。</dd>
            <dt>累计请求上限</dt>
            <dd>{authorization.status.recommended_request_limit}</dd>
            <dt>累计 Token 上限</dt>
            <dd>{authorization.status.recommended_estimated_token_limit}</dd>
            <dt>目标 Revision</dt>
            <dd>{authorization.status.target_index_revision_id}</dd>
            <dt>状态原因</dt>
            <dd>{authorization.status.reason_codes.join("、") || "无"}</dd>
          </dl>
          <div>
            <strong>逐项请求硬上限</strong>
            <ul>
              {authorization.status.required_operations.map((operation) => (
                <li key={operation}>
                  {operationLabel(operation)}：
                  {authorization.status.recommended_operation_request_limits[
                    operation
                  ] ?? 0}
                </li>
              ))}
            </ul>
          </div>
          {["approve", "reauthorize", "continue"].includes(
            authorization.status.next_action,
          ) && (
            <button
              className="primary"
              disabled={
                busy === authorization.job.job_id ||
                (["approve", "reauthorize"].includes(
                  authorization.status.next_action,
                ) &&
                  !authorization.status.approval_allowed)
              }
              aria-busy={busy === authorization.job.job_id}
              onClick={() => void approveAuthorization()}
            >
              {authorizationActionLabel(authorization.status.next_action)}
            </button>
          )}
          {authorization.status.next_action === "repair_profile" && (
            <button
              className="primary"
              onClick={() => {
                setAuthorization(undefined);
                go("/retrieval-profiles");
              }}
            >
              前往检索方案与模型服务
            </button>
          )}
          {authorization.status.next_action === "review_document" && (
            <button
              className="primary"
              onClick={() => {
                setAuthorization(undefined);
                go("/documents");
              }}
            >
              返回文档管理
            </button>
          )}
        </Modal>
      )}
    </section>
  );
}

function authorizationReady(
  status: RetrievalIngestionAuthorizationStatus,
): boolean {
  return (
    status.authorization_state === "APPROVED" &&
    status.budget_state === "AVAILABLE" &&
    status.connection_budget_state === "READY" &&
    status.next_action === "continue"
  );
}

function authorizationTitle(
  status: RetrievalIngestionAuthorizationStatus,
): string {
  if (
    status.next_action === "review_document" &&
    status.reason_codes.includes("RETRIEVAL_INGESTION_PROFILE_CHANGED")
  ) {
    return "按当前检索方案重新创建任务";
  }
  return (
    {
      approve: "批准此任务的真实检索",
      continue: "继续已批准的入库任务",
      repair_profile: "先修复检索方案",
      reauthorize: "重新批准此任务的真实检索",
      review_document: "检查已被替代的文档版本",
    } as const
  )[status.next_action];
}

function authorizationMessage(
  status: RetrievalIngestionAuthorizationStatus,
): string {
  if (status.next_action === "repair_profile") {
    return "当前检索方案或模型连接不可用，服务端不允许批准或继续任务。请先修复方案与连接。";
  }
  if (status.next_action === "review_document") {
    if (status.reason_codes.includes("RETRIEVAL_INGESTION_PROFILE_CHANGED")) {
      return "任务冻结的检索方案已被另一方案替代，旧任务不能安全改绑。请回到文档管理，按当前方案重新上传这个版本。";
    }
    return "目标文档已有更新版本；继续旧任务可能造成回滚，因此服务端已拒绝批准。";
  }
  if (status.next_action === "continue") {
    return "服务端已确认这批资料、检索方案和预算仍有效，可以继续同一个任务。";
  }
  if (status.next_action === "reauthorize") {
    return "此前授权已过期、预算不足或任务绑定已变化。系统当前不会继续发送；此前是否发生过发送，请以任务安全日志和预算记录为准。重新批准后会继续同一个任务。";
  }
  return "系统尚未获得继续发送这批资料的许可。批准后会把最终资料集合发送给已激活的 Embedding 服务，并允许同一资料后续用于查询 Embedding 和重排。";
}

function authorizationActionLabel(
  action: RetrievalIngestionAuthorizationStatus["next_action"],
): string {
  if (action === "reauthorize") return "重新批准并继续同一任务";
  if (action === "continue") return "继续同一任务";
  return "批准并继续同一任务";
}

function operationLabel(operation: string): string {
  return (
    (
      {
        "embedding.document": "文档向量",
        "embedding.query": "查询向量",
        reranking: "重排",
      } as Record<string, string>
    )[operation] ?? operation
  );
}

function stageLabel(stage: string): string {
  return (
    (
      {
        queued: "等待处理",
        claimed: "准备文档",
        parsing: "解析文档",
        document_persistence: "保存文档解析结果",
        chunk_persistence: "保存分块与检索索引",
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
