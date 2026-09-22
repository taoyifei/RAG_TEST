import { Download, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { withAppBase } from "../../app/basePath";
import { ApiError } from "../../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../../components/ui";
import { historyTime } from "../../components/HistoryTrace";
import {
  wanshitongAdminApi,
  type FeedbackDetail,
  type FeedbackListItem,
  type FeedbackReviewInput,
  type FeedbackReviewStatus,
  type FeedbackStatistics,
} from "./adminApi";

const reviewStatuses: ReadonlyArray<[FeedbackReviewStatus, string]> = [
  ["NEW", "待处理"],
  ["REVIEWED", "已复核"],
  ["FIX_PLANNED", "已计划修复"],
  ["RESOLVED", "已解决"],
  ["EXPECTED_BEHAVIOR", "符合预期"],
];

const rootCauseGroups = [
  [
    "问题理解",
    ["CONTEXT_RESOLUTION_WRONG", "DEPARTMENT_ROUTE_WRONG"],
  ],
  [
    "检索与证据",
    [
      "RETRIEVAL_NO_CANDIDATE",
      "CORRECT_SOURCE_NOT_IN_EVIDENCE_PACK",
      "EVIDENCE_HARD_REJECTED_WRONGLY",
      "WRONG_EVIDENCE_SELECTED",
      "WRONG_SOURCE",
      "STRUCTURAL_SIBLING_CONTAMINATION",
      "TEMPLATE_BOUNDARY_ERROR",
    ],
  ],
  ["生成", ["GENERATION_EMPTY", "GENERATION_INCOMPLETE"]],
  [
    "校验与覆盖",
    [
      "CLAIM_ALL_REJECTED",
      "CLAIM_UNSUPPORTED_ACCEPTED",
      "CLAIM_NUMBER_OR_NEGATION_ERROR",
      "FALSE_REFUSAL",
      "EXPECTED_REFUSAL",
    ],
  ],
  ["体验与其他", ["TOO_SLOW", "OTHER"]],
] as const;

const sectionTitles: Record<string, string> = {
  question_understanding: "问题理解",
  retrieval_evidence: "检索 / 证据",
  generation: "生成",
  validation_coverage: "校验 / 覆盖",
};

const fieldLabels: Record<string, string> = {
  context_mode: "上下文模式",
  resolved_root_hash: "解析根摘要",
  planner_called: "是否调用规划器",
  atom_count: "Atom 数量",
  root_candidate_count: "Root 候选数",
  atom_candidate_count: "Atom 候选数",
  generation_evidence_count: "生成证据数",
  exclusion_reasons: "排除原因",
  source_group_status: "来源组状态",
  answer_path: "Answer Path",
  generation_called: "是否调用生成",
  generated_claim_count: "生成 Claim 数",
  fallback: "Fallback",
  provider_statuses: "Provider 状态",
  application_validation_status: "应用校验状态",
  accepted_claim_count: "接受 Claim 数",
  published_claim_count: "发布 Claim 数",
  rejection_distribution: "拒绝分布",
  atom_coverage: "Atom 覆盖",
  terminal_reason: "实际终态原因",
};

function reasonLabel(value: string | null): string {
  const labels: Record<string, string> = {
    INCORRECT: "答案不正确",
    INCOMPLETE: "答案不完整",
    WRONG_SOURCE: "来源不对",
    FALSE_REFUSAL: "应该回答但拒答",
    UNSAFE_ANSWER: "应该拒答却回答",
    TOO_SLOW: "回答太慢",
    OUTDATED: "内容过期",
    OTHER: "其他",
  };
  return value ? (labels[value] ?? value) : "未选择";
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未采集";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "bigint") {
    return value.toString();
  }
  return "未采集";
}

function ReviewForm({
  detail,
  onSaved,
}: {
  detail: FeedbackDetail;
  onSaved: (value: FeedbackDetail) => void;
}) {
  const [status, setStatus] = useState(detail.review.review_status);
  const [rootCause, setRootCause] = useState(detail.review.root_cause ?? "");
  const [note, setNote] = useState(detail.review.note ?? "");
  const [source, setSource] = useState(
    detail.review.selected_source_document_id &&
      detail.review.selected_source_version_id
      ? `${detail.review.selected_source_document_id}::${detail.review.selected_source_version_id}`
      : "",
  );
  const [evaluationCandidate, setEvaluationCandidate] = useState(
    detail.review.evaluation_candidate,
  );
  const [fixReference, setFixReference] = useState(
    detail.review.fix_reference ?? "",
  );
  const [verificationReferences, setVerificationReferences] = useState(
    detail.review.verification_references.join("\n"),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  function applyLatestReview(value: FeedbackDetail) {
    setStatus(value.review.review_status);
    setRootCause(value.review.root_cause ?? "");
    setNote(value.review.note ?? "");
    setSource(
      value.review.selected_source_document_id &&
        value.review.selected_source_version_id
        ? `${value.review.selected_source_document_id}::${value.review.selected_source_version_id}`
        : "",
    );
    setEvaluationCandidate(value.review.evaluation_candidate);
    setFixReference(value.review.fix_reference ?? "");
    setVerificationReferences(value.review.verification_references.join("\n"));
  }

  async function save() {
    const selected = detail.citations.find(
      (item) =>
        `${item.document_id}::${item.document_version_id}` === source,
    );
    const body: FeedbackReviewInput = {
      expected_version: detail.review.review_version,
      review_status: status,
      root_cause: rootCause || null,
      note: note.trim() || null,
      selected_source_document_id: selected?.document_id ?? null,
      selected_source_version_id: selected?.document_version_id ?? null,
      evaluation_candidate: evaluationCandidate,
      fix_reference: fixReference.trim() || null,
      verification_references: verificationReferences
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean),
    };
    setBusy(true);
    setError(undefined);
    try {
      onSaved(await wanshitongAdminApi.reviewFeedback(detail.trace_id, body));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        try {
          const latest = await wanshitongAdminApi.feedbackDetail(detail.trace_id);
          applyLatestReview(latest);
          onSaved(latest);
          setError(
            new Error("另一位管理员已更新此待办，页面已刷新为最新版本。"),
          );
        } catch (refreshError) {
          setError(refreshError);
        }
      } else {
        setError(reason);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="feedback-review-panel">
      <h3>复核与处理</h3>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="form-grid">
        <label>
          处理状态
          <select
            disabled={busy}
            onChange={(event) =>
              setStatus(event.target.value as FeedbackReviewStatus)
            }
            value={status}
          >
            {reviewStatuses.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          根因
          <select
            disabled={busy}
            onChange={(event) => setRootCause(event.target.value)}
            value={rootCause}
          >
            <option value="">尚未选择</option>
            {rootCauseGroups.map(([group, values]) => (
              <optgroup key={group} label={group}>
                {values.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label className="span-two">
          管理员 Note
          <textarea
            disabled={busy}
            maxLength={2000}
            onChange={(event) => setNote(event.target.value)}
            rows={4}
            value={note}
          />
        </label>
        <label className="span-two">
          更合适的已授权引用
          <select
            disabled={busy}
            onChange={(event) => setSource(event.target.value)}
            value={source}
          >
            <option value="">不选择</option>
            {detail.citations.map((citation, index) => (
              <option
                disabled={!citation.selected_source_eligible}
                key={`${citation.document_id}:${citation.document_version_id}:${index}`}
                value={`${citation.document_id}::${citation.document_version_id}`}
              >
                {citation.display_name ?? citation.source_label ?? `来源 ${index + 1}`}
              </option>
            ))}
          </select>
        </label>
        <label>
          修复引用 / 批次
          <input
            disabled={busy}
            maxLength={500}
            onChange={(event) => setFixReference(event.target.value)}
            value={fixReference}
          />
        </label>
        <label>
          验证记录（每行一条）
          <textarea
            disabled={busy}
            onChange={(event) => setVerificationReferences(event.target.value)}
            rows={3}
            value={verificationReferences}
          />
        </label>
        <label className="feedback-check span-two">
          <input
            checked={evaluationCandidate}
            disabled={busy}
            onChange={(event) => setEvaluationCandidate(event.target.checked)}
            type="checkbox"
          />
          标记为 Evaluation Candidate（仅候选，不自动改变模型或路由）
        </label>
      </div>
      <button className="primary" disabled={busy} onClick={() => void save()}>
        {busy ? "正在保存…" : "保存复核"}
      </button>
    </section>
  );
}

function FeedbackDrawer({
  detail,
  onClose,
  onSaved,
}: {
  detail: FeedbackDetail;
  onClose: () => void;
  onSaved: (value: FeedbackDetail) => void;
}) {
  return (
    <Modal drawer onClose={onClose} title="反馈与优化待办详情">
      {detail.review.new_feedback_pending && (
        <p className="warning" role="status">
          用户反馈已在上次复核后更新，需要重新核对。
        </p>
      )}
      <section className="feedback-detail-section">
        <h3>用户反馈</h3>
        <dl className="detail-grid">
          <dt>评价</dt>
          <dd>{detail.feedback.useful ? "有帮助" : "没帮助"}</dd>
          <dt>原因</dt>
          <dd>{reasonLabel(detail.feedback.reason_detail ?? detail.feedback.reason_code)}</dd>
          <dt>说明</dt>
          <dd>
            {detail.feedback.comment_available
              ? detail.feedback.comment || "未填写"
              : `无法读取（${detail.feedback.comment_unavailable_reason}）`}
          </dd>
          <dt>反馈版本</dt>
          <dd>{detail.feedback.feedback_revision}</dd>
        </dl>
      </section>
      <section className="feedback-detail-section">
        <h3>同一问答</h3>
        {detail.history.available ? (
          <>
            <p><strong>问题：</strong>{detail.history.question || "未保存正文"}</p>
            <div className="feedback-answer">
              {detail.history.answer || "本次没有可发布答案。"}
            </div>
          </>
        ) : (
          <p>问答正文不可用：{detail.history.unavailable_reason}</p>
        )}
        <h4>已授权引用</h4>
        {detail.citations.length ? (
          <ul className="feedback-citation-list">
            {detail.citations.map((citation, index) => (
              <li key={`${citation.document_id}:${citation.document_version_id}:${index}`}>
                <strong>{citation.display_name ?? citation.source_label ?? `来源 ${index + 1}`}</strong>
                <code>{citation.document_id ?? "无文档 ID"}</code>
                <code>{citation.document_version_id ?? "无版本 ID"}</code>
                {citation.document_id && (
                  <a
                    href={withAppBase(
                      `/api/v1/admin/wanshitong/documents/${encodeURIComponent(citation.document_id)}`,
                    )}
                    rel="noreferrer"
                    target="_blank"
                  >
                    查看受权文档记录
                  </a>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p>本次没有可读取的引用。</p>
        )}
      </section>
      <div className="feedback-stage-grid">
        {Object.entries(detail.trace_projection).map(([section, fields]) => (
          <section className="feedback-detail-section" key={section}>
            <h3>{sectionTitles[section] ?? section}</h3>
            <dl className="detail-grid">
              {Object.entries(fields).map(([field, value]) => (
                <div className="feedback-detail-row" key={field}>
                  <dt>{fieldLabels[field] ?? field}</dt>
                  <dd>{displayValue(value)}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
      {detail.operational_trace_unavailable_reason && (
        <p>Operational Trace：{detail.operational_trace_unavailable_reason}</p>
      )}
      <ReviewForm detail={detail} onSaved={onSaved} />
    </Modal>
  );
}

export function AdminFeedbackPage() {
  const [items, setItems] = useState<FeedbackListItem[]>([]);
  const [statistics, setStatistics] = useState<FeedbackStatistics>();
  const [selected, setSelected] = useState<FeedbackDetail>();
  const [reason, setReason] = useState("");
  const [reviewStatus, setReviewStatus] = useState<FeedbackReviewStatus | "">("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<unknown>();

  const requestData = useCallback(
    (signal?: AbortSignal) =>
      Promise.all([
        wanshitongAdminApi.listFeedback(
          {
            reason: reason || undefined,
            review_status: reviewStatus || undefined,
            created_from: createdFrom ? `${createdFrom}T00:00:00Z` : undefined,
            created_to: createdTo ? `${createdTo}T23:59:59Z` : undefined,
            page_size: 20,
            offset,
          },
          signal,
        ),
        wanshitongAdminApi.feedbackStatistics(signal),
      ]),
    [createdFrom, createdTo, offset, reason, reviewStatus],
  );

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const [page, metrics] = await requestData(signal);
      setItems(page.items);
      setNextOffset(page.next_offset);
      setStatistics(metrics);
    } catch (reasonValue) {
      if (!signal?.aborted) setError(reasonValue);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [requestData]);

  useEffect(() => {
    const controller = new AbortController();
    void requestData(controller.signal)
      .then(([page, metrics]) => {
        if (controller.signal.aborted) return;
        setItems(page.items);
        setNextOffset(page.next_offset);
        setStatistics(metrics);
        setError(undefined);
      })
      .catch((reasonValue: unknown) => {
        if (!controller.signal.aborted) setError(reasonValue);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [requestData]);

  async function inspect(traceId: string) {
    setBusy(traceId);
    setError(undefined);
    try {
      setSelected(await wanshitongAdminApi.feedbackDetail(traceId));
    } catch (reasonValue) {
      setError(reasonValue);
    } finally {
      setBusy(undefined);
    }
  }

  async function exportCurrentPage() {
    setBusy("export");
    setError(undefined);
    try {
      await wanshitongAdminApi.exportFeedback(items.map((item) => item.trace_id));
    } catch (reasonValue) {
      setError(reasonValue);
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>反馈 / 优化待办</h2>
          <p>用户反馈只形成待办与复核记录，不自动修改模型、路由或引用。</p>
        </div>
        <div className="row-actions">
          <button
            className="secondary"
            disabled={!items.length || busy === "export"}
            onClick={() => void exportCurrentPage()}
          >
            <Download aria-hidden="true" size={17} />
            导出当前页元数据
          </button>
          <button className="secondary" onClick={() => void load()}>
            <RefreshCw aria-hidden="true" size={17} />
            刷新
          </button>
        </div>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      {statistics && (
        <div className="metric-grid feedback-metrics">
          <article>
            <span>有帮助 / 明确评价</span>
            <strong>{statistics.helpful_count} / {statistics.evaluated_count}</strong>
            <small>
              {statistics.helpful_rate === null
                ? "暂无评价"
                : `帮助率 ${(statistics.helpful_rate * 100).toFixed(1)}%`}
            </small>
          </article>
          <article>
            <span>待处理</span>
            <strong>{statistics.pending_count}</strong>
            <small>含复核后出现的新反馈</small>
          </article>
          <article>
            <span>人工确认</span>
            <strong>{statistics.confirmed_wrong_source_count + statistics.confirmed_false_refusal_count}</strong>
            <small>
              错来源 {statistics.confirmed_wrong_source_count} · 误拒 {statistics.confirmed_false_refusal_count}
            </small>
          </article>
          <article>
            <span>真实 SSO 用户延迟</span>
            <strong>
              {statistics.latency_ms.actual_sso_users.p50 === null
                ? "待采集"
                : `${statistics.latency_ms.actual_sso_users.p50} ms`}
            </strong>
            <small>
              p95 {statistics.latency_ms.actual_sso_users.p95 ?? "—"} ms · {statistics.latency_ms.actual_sso_users.count} 次
            </small>
          </article>
        </div>
      )}
      <div className="history-filters feedback-filters">
        <label>
          用户原因
          <select
            onChange={(event) => {
              setOffset(0);
              setReason(event.target.value);
            }}
            value={reason}
          >
            <option value="">全部原因</option>
            {[
              "INCORRECT",
              "INCOMPLETE",
              "WRONG_SOURCE",
              "FALSE_REFUSAL",
              "UNSAFE_ANSWER",
              "TOO_SLOW",
              "OTHER",
              "OUTDATED",
            ].map((value) => (
              <option key={value} value={value}>{reasonLabel(value)}</option>
            ))}
          </select>
        </label>
        <label>
          复核状态
          <select
            onChange={(event) => {
              setOffset(0);
              setReviewStatus(event.target.value as FeedbackReviewStatus | "");
            }}
            value={reviewStatus}
          >
            <option value="">全部状态</option>
            {reviewStatuses.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          开始日期
          <input
            onChange={(event) => {
              setOffset(0);
              setCreatedFrom(event.target.value);
            }}
            type="date"
            value={createdFrom}
          />
        </label>
        <label>
          结束日期
          <input
            onChange={(event) => {
              setOffset(0);
              setCreatedTo(event.target.value);
            }}
            type="date"
            value={createdTo}
          />
        </label>
      </div>
      {loading && <p role="status">正在读取反馈待办…</p>}
      {!loading && !items.length && <EmptyState title="当前筛选下暂无反馈" />}
      {items.length > 0 && (
        <div className="table-wrap feedback-table">
          <table>
            <caption className="sr-only">反馈与优化待办</caption>
            <thead>
              <tr>
                <th>创建时间 / 问题</th>
                <th>终态</th>
                <th>评价 / 原因</th>
                <th>Answer Path / 耗时</th>
                <th>复核状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.trace_id}>
                  <td>
                    <strong>{item.question_summary || "问答正文不可用"}</strong>
                    <small>{historyTime(item.created_at)}</small>
                    <code>{item.trace_id}</code>
                  </td>
                  <td><StatusBadge value={item.final_status || "UNKNOWN"} /></td>
                  <td>
                    <strong>{item.useful ? "有帮助" : "没帮助"}</strong>
                    <small>{reasonLabel(item.reason_detail ?? item.reason_code)}</small>
                  </td>
                  <td>
                    <code>{item.answer_path || "未采集"}</code>
                    <small>
                      {item.duration_ms === null ? "耗时未采集" : `${item.duration_ms} ms`}
                    </small>
                  </td>
                  <td>
                    <StatusBadge value={item.review_status} />
                    {item.new_feedback_pending && <small>有新反馈待复查</small>}
                  </td>
                  <td>
                    <button
                      disabled={busy === item.trace_id}
                      onClick={() => void inspect(item.trace_id)}
                    >
                      查看并复核
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row-actions feedback-pagination">
        <button
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 20))}
        >
          上一页
        </button>
        <button
          disabled={nextOffset === null || nextOffset === undefined}
          onClick={() => nextOffset !== null && nextOffset !== undefined && setOffset(nextOffset)}
        >
          下一页
        </button>
      </div>
      {selected && (
        <FeedbackDrawer
          detail={selected}
          onClose={() => setSelected(undefined)}
          onSaved={(value) => {
            setSelected(value);
            void load();
          }}
        />
      )}
    </section>
  );
}
