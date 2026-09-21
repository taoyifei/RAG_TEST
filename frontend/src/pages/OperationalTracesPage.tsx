import { Download, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import {
  api,
  type OperationalTraceArtifactContent,
  type OperationalTraceDetail,
  type OperationalTraceFilters,
  type OperationalTracePage,
  type OperationalTraceSpan,
  type TraceMode,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";
import { downloadFile } from "../utils/download";

const PAGE_SIZE = 30;
const INITIAL_SPAN_LIMIT = 120;
const INITIAL_DECISION_LIMIT = 160;

type TraceTab = "waterfall" | "funnel" | "providers" | "artifacts";

interface DepartmentShadowView {
  routeRevision?: string;
  profileRevision?: string;
  top1DepartmentKey?: string;
  top1ScoreBucket?: string;
  top2DepartmentKey?: string;
  top2ScoreBucket?: string;
  confidence: string;
  recommendedScope: string;
  actualScopeKind: string;
  citedDepartmentKeys: string[];
  departmentFilterApplied: boolean;
  embeddingReused: boolean;
  extraProviderCalls: number;
  status: string;
  reasonCodes: string[];
}

export interface OperationalTraceServices {
  list: (
    filters: OperationalTraceFilters,
    signal?: AbortSignal,
  ) => Promise<OperationalTracePage>;
  detail: (
    traceId: string,
    signal?: AbortSignal,
  ) => Promise<OperationalTraceDetail>;
  artifact?: (
    traceId: string,
    artifactId: string,
    signal?: AbortSignal,
  ) => Promise<OperationalTraceArtifactContent>;
  exportOne?: (traceId: string) => ReturnType<typeof api.exportOperationalTrace>;
  exportMany?: (
    traceIds: string[],
  ) => ReturnType<typeof api.exportOperationalTraces>;
  prune?: () => Promise<{ pruned: number }>;
}

const defaultTraceServices: OperationalTraceServices = {
  list: (filters, signal) => api.listOperationalTraces(filters, signal),
  detail: (traceId, signal) => api.operationalTraceDetail(traceId, signal),
  artifact: (traceId, artifactId) =>
    api.operationalTraceArtifact(traceId, artifactId),
  exportOne: (traceId) => api.exportOperationalTrace(traceId),
  exportMany: (traceIds) => api.exportOperationalTraces(traceIds),
  prune: () => api.pruneOperationalTraces(),
};

function initialDeepLink(): Pick<
  OperationalTraceFilters,
  "trace_id" | "job_id" | "document_id" | "revision_id"
> {
  const params = new URLSearchParams(window.location.search);
  return {
    trace_id: params.get("trace_id") || undefined,
    job_id: params.get("job_id") || undefined,
    document_id: params.get("document_id") || undefined,
    revision_id: params.get("revision_id") || undefined,
  };
}

export function OperationalTracesPage({
  go,
  fixedScope = false,
  services = defaultTraceServices,
}: {
  go?: (path: string) => void;
  fixedScope?: boolean;
  services?: OperationalTraceServices;
}) {
  const { scope, setRevision } = useConsole();
  const deepLink = useMemo(() => initialDeepLink(), []);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [captureMode, setCaptureMode] = useState<TraceMode | "">("");
  const [complete, setComplete] = useState("");
  const [feedback, setFeedback] = useState("");
  const [identity, setIdentity] = useState(
    deepLink.trace_id ??
      deepLink.job_id ??
      deepLink.document_id ??
      deepLink.revision_id ??
      "",
  );
  const [filters, setFilters] = useState<OperationalTraceFilters>({
    page: 1,
    page_size: PAGE_SIZE,
    project_id: fixedScope ? undefined : scope.projectId || undefined,
    knowledge_base_id: fixedScope ? undefined : scope.kbId || undefined,
    ...deepLink,
  });
  const [page, setPage] = useState<OperationalTracePage>();
  const [selectedId, setSelectedId] = useState(deepLink.trace_id);
  const [selected, setSelected] = useState<OperationalTraceDetail>();
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [exportBusy, setExportBusy] = useState(false);
  const [pruneBusy, setPruneBusy] = useState(false);
  const [pruneStatus, setPruneStatus] = useState<string>();
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void services
      .list(filters, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setPage(value);
          setLoading(false);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [filters, reload, services]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    void services
      .detail(selectedId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setSelected(value);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason);
      });
    return () => controller.abort();
  }, [reload, selectedId, services]);

  function search(event: FormEvent) {
    event.preventDefault();
    const traceIdentity = /^(?:trace_)?[0-9a-f]{32}$/.test(identity)
      ? identity
      : undefined;
    const jobIdentity = identity.startsWith("job_") ? identity : undefined;
    const documentIdentity = identity.startsWith("doc_") ? identity : undefined;
    const revisionIdentity = identity.startsWith("irev_")
      ? identity
      : undefined;
    setError(undefined);
    setLoading(true);
    setSelected(undefined);
    setSelectedId(traceIdentity);
    setChecked(new Set());
    setFilters({
      page: 1,
      page_size: PAGE_SIZE,
      project_id: fixedScope ? undefined : scope.projectId || undefined,
      knowledge_base_id: fixedScope ? undefined : scope.kbId || undefined,
      kind: kind || undefined,
      status: status || undefined,
      capture_mode: captureMode,
      capture_complete:
        complete === "complete"
          ? true
          : complete === "incomplete"
            ? false
            : undefined,
      feedback_useful:
        feedback === "useful"
          ? true
          : feedback === "not-useful"
            ? false
            : undefined,
      trace_id: traceIdentity,
      job_id: jobIdentity,
      document_id: documentIdentity,
      revision_id: revisionIdentity,
    });
  }

  function inspect(traceId: string) {
    setSelected(undefined);
    setSelectedId(traceId);
    const url = new URL(window.location.href);
    url.searchParams.set("trace_id", traceId);
    url.searchParams.delete("job_id");
    window.history.replaceState({}, "", url.pathname + url.search);
  }

  function closeDetail() {
    setSelectedId(undefined);
    setSelected(undefined);
    const url = new URL(window.location.href);
    url.searchParams.delete("trace_id");
    window.history.replaceState({}, "", url.pathname + url.search);
  }

  async function exportSelected() {
    if (!checked.size || !services.exportMany) return;
    setExportBusy(true);
    setError(undefined);
    try {
      downloadFile(
        await services.exportMany([...checked].sort()),
      );
    } catch (reason) {
      setError(reason);
    } finally {
      setExportBusy(false);
    }
  }

  async function pruneExpired() {
    if (!services.prune) return;
    if (
      !window.confirm(
        "只会清理已到期且不在导出中的 Operational Trace。确认继续吗？",
      )
    ) {
      return;
    }
    setPruneBusy(true);
    setError(undefined);
    setPruneStatus(undefined);
    try {
      const value = await services.prune();
      setPruneStatus(`已清理 ${value.pruned} 条到期 Operational Trace。`);
      setChecked(new Set());
      setReload((current) => current + 1);
    } catch (reason) {
      setError(reason);
    } finally {
      setPruneBusy(false);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>Operational Trace</h2>
          <p>技术诊断只显示安全身份、时序、候选决定与显式调试制品。</p>
          {fixedScope && (
            <p>需要批量下载原问题、完整答案和引用？请到问答历史下载原文支持包。</p>
          )}
        </div>
        <div className="row-actions">
          {fixedScope && go && (
            <button className="secondary" onClick={() => go("/history")}>
              前往问答历史批量下载
            </button>
          )}
          {services.prune && (
            <button
              className="secondary"
              disabled={pruneBusy}
              onClick={() => void pruneExpired()}
            >
              {pruneBusy ? "清理中…" : "清理已到期 Trace"}
            </button>
          )}
          <button
            className="secondary"
            onClick={() => {
              setLoading(true);
              setReload((value) => value + 1);
            }}
          >
            <RefreshCw aria-hidden="true" size={17} />
            刷新
          </button>
        </div>
      </div>
      <form className="panel trace-filters" onSubmit={search}>
        <label>
          Trace、Job、Document 或 Revision ID
          <input
            value={identity}
            onChange={(event) => setIdentity(event.target.value.trim())}
            placeholder="trace_32hex / 旧 32hex / job_… / doc_… / irev_…"
          />
        </label>
        <label>
          类型
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="">全部</option>
            <option value="query">查询</option>
            <option value="ingestion">入库</option>
          </select>
        </label>
        <label>
          终态
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">全部</option>
            {[
              "RUNNING",
              "SUCCEEDED",
              "ANSWERED",
              "REFUSED",
              "FAILED",
              "CANCELLED",
              "INTERRUPTED",
            ].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          捕获模式
          <select
            value={captureMode}
            onChange={(event) =>
              setCaptureMode(event.target.value as TraceMode | "")
            }
          >
            <option value="">全部</option>
            <option value="SAFE">SAFE</option>
            <option value="DIAGNOSTIC">DIAGNOSTIC</option>
            <option value="FULL">FULL</option>
          </select>
        </label>
        <label>
          捕获完整性
          <select
            value={complete}
            onChange={(event) => setComplete(event.target.value)}
          >
            <option value="">全部</option>
            <option value="complete">完整</option>
            <option value="incomplete">不完整</option>
          </select>
        </label>
        <label>
          用户反馈
          <select
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
          >
            <option value="">全部</option>
            <option value="useful">有用</option>
            <option value="not-useful">无用</option>
          </select>
        </label>
        <button className="primary">筛选 Trace</button>
      </form>
      {error !== undefined && <ErrorPanel error={error} />}
      {pruneStatus && <p role="status">{pruneStatus}</p>}
      {loading && <p role="status">正在读取 Trace…</p>}
      {page && (
        <>
          <div className="row-actions" aria-label="Trace 分页与导出">
            <button
              disabled={page.page <= 1}
              onClick={() => {
                setLoading(true);
                setFilters({ ...filters, page: page.page - 1 });
              }}
            >
              上一页
            </button>
            <span>
              共 {page.total} 条 · 第 {page.page} 页
            </span>
            <button
              disabled={page.page * page.page_size >= page.total}
              onClick={() => {
                setLoading(true);
                setFilters({ ...filters, page: page.page + 1 });
              }}
            >
              下一页
            </button>
            {services.exportMany && (
              <>
                <button
                  type="button"
                  disabled={!page.items.length}
                  onClick={() =>
                    setChecked(
                      new Set([
                        ...checked,
                        ...page.items.map((item) => item.trace_id),
                      ]),
                    )
                  }
                >
                  选择本页
                </button>
                <button
                  type="button"
                  disabled={!checked.size}
                  onClick={() => setChecked(new Set())}
                >
                  清空选择
                </button>
                <span role="status">已选择 {checked.size} 条</span>
                <button
                  disabled={!checked.size || exportBusy}
                  onClick={() => void exportSelected()}
                >
                  <Download aria-hidden="true" size={16} />
                  {exportBusy ? "正在导出…" : "导出已选技术 Trace"}
                </button>
              </>
            )}
          </div>
          <div className="table-wrap">
            <table className="trace-table">
              <thead>
                <tr>
                  <th scope="col">选择</th>
                  <th scope="col">Trace</th>
                  <th scope="col">类型/终态</th>
                  <th scope="col">耗时</th>
                  <th scope="col">捕获</th>
                  <th scope="col">时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((item) => (
                  <tr key={item.trace_id}>
                    <td>
                      <input
                        aria-label={"选择 " + item.trace_id}
                        type="checkbox"
                        checked={checked.has(item.trace_id)}
                        onChange={(event) => {
                          const next = new Set(checked);
                          if (event.target.checked) next.add(item.trace_id);
                          else next.delete(item.trace_id);
                          setChecked(next);
                        }}
                      />
                    </td>
                    <td>
                      <code>{item.trace_id}</code>
                      <small>{item.job_id ?? item.request_id ?? "—"}</small>
                    </td>
                    <td>
                      <strong>
                        {item.kind === "ingestion" ? "入库" : "查询"}
                      </strong>
                      <StatusBadge value={item.status} />
                      {item.feedback_useful !== null &&
                        item.feedback_useful !== undefined && (
                          <small>
                            用户反馈：{item.feedback_useful ? "有用" : "无用"}
                          </small>
                        )}
                    </td>
                    <td>
                      {item.duration_ms == null
                        ? "—"
                        : item.duration_ms + " ms"}
                    </td>
                    <td>
                      <strong>{item.mode}</strong>
                      <small>
                        {item.capture_complete
                          ? "完整"
                          : item.capture_incomplete_reason || "不完整"}
                      </small>
                    </td>
                    <td>
                      <time dateTime={item.created_at}>
                        {formatTime(item.created_at)}
                      </time>
                    </td>
                    <td>
                      <button onClick={() => inspect(item.trace_id)}>
                        查看技术详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.items.length && (
            <EmptyState title="没有匹配的 Trace">
              可清除 ID 或放宽状态筛选后重试。
            </EmptyState>
          )}
        </>
      )}
      {selectedId && !selected && <p role="status">正在读取 Trace 详情…</p>}
      {selected && (
        <TraceDetailPanel
          detail={selected}
          onClose={closeDetail}
          onError={setError}
          go={go}
          setRevision={fixedScope ? undefined : setRevision}
          services={services}
        />
      )}
    </section>
  );
}

export function TraceDetailPanel({
  detail,
  onClose,
  onError,
  go,
  setRevision,
  services = defaultTraceServices,
}: {
  detail: OperationalTraceDetail;
  onClose: () => void;
  onError: (error: unknown) => void;
  go?: (path: string) => void;
  setRevision?: (revisionId: string) => void;
  services?: OperationalTraceServices;
}) {
  const [tab, setTab] = useState<TraceTab>("waterfall");
  const [spanLimit, setSpanLimit] = useState(INITIAL_SPAN_LIMIT);
  const [decisionLimit, setDecisionLimit] = useState(INITIAL_DECISION_LIMIT);
  const [stage, setStage] = useState("");
  const [artifact, setArtifact] = useState<OperationalTraceArtifactContent>();
  const [artifactBusy, setArtifactBusy] = useState<string>();
  const [exportBusy, setExportBusy] = useState(false);
  const decisions = detail.candidate_decisions.filter(
    (item) => !stage || item.stage === stage,
  );
  const stages = [
    ...new Set(detail.candidate_decisions.map((item) => item.stage)),
  ];
  const providerSpans = detail.spans.filter((span) =>
    span.name.startsWith("provider."),
  );

  async function readArtifact(artifactId: string) {
    setArtifactBusy(artifactId);
    setArtifact(undefined);
    try {
      if (!services.artifact) return;
      setArtifact(await services.artifact(detail.trace.trace_id, artifactId));
    } catch (reason) {
      onError(reason);
    } finally {
      setArtifactBusy(undefined);
    }
  }

  async function exportOne() {
    if (!services.exportOne) return;
    setExportBusy(true);
    try {
      downloadFile(await services.exportOne(detail.trace.trace_id));
    } catch (reason) {
      onError(reason);
    } finally {
      setExportBusy(false);
    }
  }

  return (
    <section className="panel trace-detail" aria-label="Operational Trace 详情">
      <div className="section-heading">
        <div>
          <span className="eyebrow">{detail.trace.mode} 技术追踪</span>
          <h2>{detail.trace.trace_id}</h2>
          <p>
            {detail.trace.kind} · {detail.trace.duration_ms ?? "—"} ms ·{" "}
            {detail.trace.capture_complete ? "捕获完整" : "捕获不完整"}
          </p>
        </div>
        <div className="row-actions">
          {services.exportOne && (
            <button disabled={exportBusy} onClick={() => void exportOne()}>
              <Download aria-hidden="true" size={16} />
              {exportBusy ? "导出中…" : "导出 JSON"}
            </button>
          )}
          <button onClick={onClose}>关闭详情</button>
        </div>
      </div>
      <dl className="detail-grid">
        <dt>Project / KB</dt>
        <dd>
          {(detail.trace.project_id ?? "—") +
            " / " +
            (detail.trace.knowledge_base_id ?? "—")}
        </dd>
        <dt>Job / Document / Revision</dt>
        <dd>
          {(detail.trace.job_id ?? "—") +
            " / " +
            (detail.trace.document_id ?? "—") +
            " / " +
            (detail.trace.revision_id ?? "—")}
        </dd>
        <dt>Profile / Index</dt>
        <dd>
          {(detail.trace.profile_id ?? "—") +
            " / " +
            (detail.trace.index_fingerprint ?? "—")}
        </dd>
        <dt>Capture</dt>
        <dd>
          queue high-water {detail.trace.writer_queue_high_water} · dropped
          spans {detail.trace.dropped_span_count} · dropped decisions{" "}
          {detail.trace.dropped_decision_count}
        </dd>
      </dl>
      {go && (
        <div className="row-actions" aria-label="Trace 关联资源">
          {detail.trace.request_id && (
            <button
              onClick={() =>
                openRelated("/history", "trace_id", detail.trace.trace_id, go)
              }
            >
              打开问答历史
            </button>
          )}
          {detail.trace.job_id && (
            <button
              onClick={() =>
                openRelated("/jobs", "job_id", detail.trace.job_id!, go)
              }
            >
              打开任务
            </button>
          )}
          {detail.trace.document_id && (
            <button
              onClick={() =>
                openRelated(
                  "/documents",
                  "document_id",
                  detail.trace.document_id!,
                  go,
                )
              }
            >
              打开文档
            </button>
          )}
          {detail.trace.revision_id && setRevision && (
            <button
              onClick={() => {
                setRevision(detail.trace.revision_id!);
                go("/revision");
              }}
            >
              打开索引版本
            </button>
          )}
        </div>
      )}
      {detail.trace.kind !== "ingestion" && (
        <DepartmentShadowPanel detail={detail} />
      )}
      <div className="trace-tabs" role="tablist" aria-label="Trace 详情视图">
        {(
          [
            ["waterfall", "Waterfall"],
            ["funnel", "候选漏斗"],
            ["providers", "Provider / Usage"],
            ["artifacts", "Artifact"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={tab === value}
            className={tab === value ? "active" : ""}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "waterfall" && (
        <div role="tabpanel" aria-label="Waterfall">
          <Waterfall
            rootDuration={detail.trace.duration_ms ?? 0}
            spans={detail.spans.slice(0, spanLimit)}
          />
          {spanLimit < detail.spans.length && (
            <button
              onClick={() =>
                setSpanLimit((value) => value + INITIAL_SPAN_LIMIT)
              }
            >
              再显示{" "}
              {Math.min(INITIAL_SPAN_LIMIT, detail.spans.length - spanLimit)} 个
              Span
            </button>
          )}
        </div>
      )}
      {tab === "funnel" && (
        <div role="tabpanel" aria-label="候选漏斗">
          <label className="trace-stage-filter">
            阶段
            <select
              value={stage}
              onChange={(event) => setStage(event.target.value)}
            >
              <option value="">全部阶段</option>
              {stages.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">阶段</th>
                  <th scope="col">候选 / Evidence</th>
                  <th scope="col">通道 / Rank</th>
                  <th scope="col">Score</th>
                  <th scope="col">决定</th>
                </tr>
              </thead>
              <tbody>
                {decisions.slice(0, decisionLimit).map((item) => (
                  <tr key={item.sequence + ":" + item.chunk_id}>
                    <td>{item.stage}</td>
                    <td>
                      <code>{item.candidate_id ?? item.chunk_id}</code>
                      {item.evidence_id && <small>{item.evidence_id}</small>}
                    </td>
                    <td>
                      {(item.channel ?? "—") + " / " + (item.rank ?? "—")}
                    </td>
                    <td>
                      {(item.score_type ?? "—") + ": " + (item.score ?? "—")}
                    </td>
                    <td>
                      <strong>{item.selected ? "保留" : "淘汰"}</strong>
                      <small>{item.reason_code}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {decisionLimit < decisions.length && (
            <button
              onClick={() =>
                setDecisionLimit((value) => value + INITIAL_DECISION_LIMIT)
              }
            >
              再显示候选决定
            </button>
          )}
          {!decisions.length && <p>此捕获模式没有候选 ID 与分数。</p>}
        </div>
      )}
      {tab === "providers" && (
        <div role="tabpanel" aria-label="Provider 与 Usage">
          {!providerSpans.length && <p>本次没有实际 Provider 子调用。</p>}
          {providerSpans.map((span) => (
            <details key={span.span_id}>
              <summary>
                {span.name +
                  " · " +
                  (span.duration_ms ?? 0) +
                  " ms · " +
                  span.reason_code}
              </summary>
              <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
            </details>
          ))}
        </div>
      )}
      {tab === "artifacts" && (
        <div role="tabpanel" aria-label="Artifact">
          {!detail.artifacts.length && (
            <p>此 Trace 没有 FULL Artifact；列表和详情不会隐式读取内容。</p>
          )}
          {detail.artifacts.map((item) => (
            <article className="trace-artifact" key={item.artifact_id}>
              <div>
                <strong>{item.kind}</strong>
                <code>{item.sha256}</code>
                <small>
                  {item.media_type} · {item.original_bytes} bytes
                </small>
              </div>
              {services.artifact && (
                <button
                  disabled={artifactBusy === item.artifact_id}
                  onClick={() => void readArtifact(item.artifact_id)}
                >
                  {artifactBusy === item.artifact_id ? "读取中…" : "按需读取"}
                </button>
              )}
            </article>
          ))}
          {artifact && (
            <details open>
              <summary>
                {"已校验内容 · " + artifact.mediaType + " · " + artifact.sha256}
              </summary>
              <pre>{formatArtifact(artifact)}</pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

function DepartmentShadowPanel({ detail }: { detail: OperationalTraceDetail }) {
  const observation = departmentShadowObservation(detail);
  return (
    <section
      className="department-shadow-panel"
      aria-labelledby="department-shadow-title"
    >
      <div className="department-shadow-heading">
        <div>
          <span className="eyebrow">只读观察</span>
          <h3 id="department-shadow-title">部门影子路由</h3>
        </div>
        {observation ? (
          <StatusBadge value={observation.status} />
        ) : (
          <StatusBadge value="未采集" />
        )}
      </div>
      {!observation ? (
        <p className="department-shadow-empty">
          未采集。这通常表示旧 Trace，或本次查询未启用部门影子路由。
        </p>
      ) : (
        <>
          <p className="department-shadow-notice">
            {observation.departmentFilterApplied
              ? "Trace 报告已应用部门过滤，请立即核查配置。"
              : "实际检索未使用部门过滤；建议只用于管理员事后评估。"}
          </p>
          <dl className="department-shadow-grid">
            <dt>建议 / 置信</dt>
            <dd>
              {formatRecommendedScope(observation.recommendedScope)} ·{" "}
              {formatConfidence(observation.confidence)}
            </dd>
            <dt>Top 1</dt>
            <dd>
              {formatDepartmentCandidate(
                observation.top1DepartmentKey,
                observation.top1ScoreBucket,
              )}
            </dd>
            <dt>Top 2</dt>
            <dd>
              {formatDepartmentCandidate(
                observation.top2DepartmentKey,
                observation.top2ScoreBucket,
              )}
            </dd>
            <dt>实际 SourceScope</dt>
            <dd>{formatActualScope(observation.actualScopeKind)}</dd>
            <dt>最终引用部门</dt>
            <dd>
              {observation.citedDepartmentKeys.length
                ? observation.citedDepartmentKeys.join("、")
                : "无"}
            </dd>
            <dt>已有向量 / 新增调用</dt>
            <dd>
              {observation.embeddingReused ? "已复用" : "未复用"} ·{" "}
              {observation.extraProviderCalls} 次
            </dd>
            <dt>Profile / Route</dt>
            <dd>
              <code>{observation.profileRevision ?? "—"}</code>
              <small>{observation.routeRevision ?? "—"}</small>
            </dd>
            <dt>原因</dt>
            <dd className="department-shadow-reasons">
              {observation.reasonCodes.length
                ? observation.reasonCodes.map((reason) => (
                    <code key={reason}>{reason}</code>
                  ))
                : "—"}
            </dd>
          </dl>
        </>
      )}
    </section>
  );
}

function Waterfall({
  rootDuration,
  spans,
}: {
  rootDuration: number;
  spans: OperationalTraceSpan[];
}) {
  const rootStart = spans.length ? Date.parse(spans[0].started_at) : 0;
  const safeDuration = Math.max(1, rootDuration);
  const byId = new Map(spans.map((span) => [span.span_id, span]));
  function depth(span: OperationalTraceSpan): number {
    let value = 0;
    let parent = span.parent_span_id
      ? byId.get(span.parent_span_id)
      : undefined;
    const visited = new Set<string>();
    while (parent && value < 8 && !visited.has(parent.span_id)) {
      visited.add(parent.span_id);
      value += 1;
      parent = parent.parent_span_id
        ? byId.get(parent.parent_span_id)
        : undefined;
    }
    return value;
  }
  return (
    <div className="waterfall" aria-label="Span waterfall">
      {spans.map((span) => {
        const offset = Math.max(0, Date.parse(span.started_at) - rootStart);
        const left = Math.min(98, (offset / safeDuration) * 100);
        const width = Math.max(
          1.5,
          Math.min(100 - left, ((span.duration_ms ?? 0) / safeDuration) * 100),
        );
        return (
          <details key={span.span_id} className="waterfall-row">
            <summary style={{ paddingInlineStart: depth(span) * 16 + "px" }}>
              <span>{span.name}</span>
              <span>{span.duration_ms ?? 0} ms</span>
              <span className="waterfall-track" aria-hidden="true">
                <i
                  style={{
                    left: left + "%",
                    width: width + "%",
                  }}
                />
              </span>
            </summary>
            <div>
              <code>
                {span.kind + " · " + span.status + " · " + span.reason_code}
              </code>
              <pre>{JSON.stringify(span.attributes, null, 2)}</pre>
            </div>
          </details>
        );
      })}
      {!spans.length && <p>没有可显示的 Span；旧记录仅保留平面事件。</p>}
    </div>
  );
}

function departmentShadowObservation(
  detail: OperationalTraceDetail,
): DepartmentShadowView | undefined {
  const current = [...detail.spans]
    .reverse()
    .find((span) => span.name === "retrieval.department_route_shadow");
  const legacy = [...(detail.legacy_flat_events ?? [])]
    .reverse()
    .find((event) => event.event_name === "retrieval.department_route_shadow");
  const attributes = current?.attributes ?? legacy?.attributes;
  if (!isRecord(attributes)) return undefined;
  return {
    routeRevision: stringValue(attributes.route_revision),
    profileRevision: stringValue(attributes.profile_revision),
    top1DepartmentKey: stringValue(attributes.top1_department_key),
    top1ScoreBucket: stringValue(attributes.top1_score_bucket),
    top2DepartmentKey: stringValue(attributes.top2_department_key),
    top2ScoreBucket: stringValue(attributes.top2_score_bucket),
    confidence: stringValue(attributes.confidence) ?? "UNKNOWN",
    recommendedScope: stringValue(attributes.recommended_scope) ?? "UNKNOWN",
    actualScopeKind: stringValue(attributes.actual_scope_kind) ?? "UNKNOWN",
    citedDepartmentKeys: stringArray(attributes.final_cited_department_keys),
    departmentFilterApplied: attributes.department_filter_applied === true,
    embeddingReused: attributes.embedding_reused === true,
    extraProviderCalls:
      typeof attributes.extra_provider_calls === "number"
        ? attributes.extra_provider_calls
        : 0,
    status: stringValue(attributes.status) ?? "UNKNOWN",
    reasonCodes: stringArray(attributes.reason_codes),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function formatRecommendedScope(value: string): string {
  return (
    {
      TOP1: "优先 Top 1 部门",
      TOP2: "优先 Top 2 部门",
      GLOBAL: "全局（不加部门过滤）",
    }[value] ?? value
  );
}

function formatConfidence(value: string): string {
  return (
    {
      HIGH: "高置信",
      MEDIUM: "中置信",
      LOW: "低置信",
    }[value] ?? value
  );
}

function formatDepartmentCandidate(
  departmentKey?: string,
  scoreBucket?: string,
): string {
  if (!departmentKey) return "—";
  const bucket = scoreBucket
    ? ({
        EXPLICIT: "显式唯一部门",
        EXPLICIT_SOURCE: "显式来源部门",
        GE_0_50: "高分段",
        "0_30_TO_0_49": "中分段",
        LT_0_30: "低分段",
        ZERO: "零重合",
      }[scoreBucket] ?? scoreBucket)
    : "未分段";
  return `${departmentKey} · ${bucket}`;
}

function formatActualScope(value: string): string {
  return (
    {
      OPEN: "开放 SourceScope（仍受权限与活动版本限制）",
      SOURCE_RESOLVED: "显式文档范围（已解析）",
      SOURCE_CATALOG_ONLY: "显式文档范围（仅目录）",
      SOURCE_AMBIGUOUS: "显式来源有歧义",
      SOURCE_UNRESOLVED: "显式来源未解析",
    }[value] ?? value
  );
}

function formatArtifact(value: OperationalTraceArtifactContent): string {
  if (value.mediaType.includes("json")) {
    try {
      return JSON.stringify(JSON.parse(value.body), null, 2);
    } catch {
      return value.body;
    }
  }
  return value.body;
}

function openRelated(
  path: string,
  key: string,
  value: string,
  go: (path: string) => void,
) {
  const url = new URL(window.location.href);
  for (const identity of ["trace_id", "job_id", "document_id", "revision_id"])
    url.searchParams.delete(identity);
  url.searchParams.set(key, value);
  window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  go(path);
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value));
}
