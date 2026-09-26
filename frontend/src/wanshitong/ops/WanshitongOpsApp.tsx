import { useCallback, useEffect, useState } from "react";

import { withAppBase } from "../../app/basePath";
import { RecommendationManager } from "./RecommendationManager";

interface TraceSummary {
  trace_id: string;
  created_at: string;
  status: string;
  asker_id: string;
  asker_name: string | null;
  question: string;
  feedback_useful: number | null;
  review_status: string | null;
}

interface TraceDetail {
  bridge_trace_id: string;
  native_session_id: string;
  native_message_id: string | null;
  native_request_id: string | null;
  status: string;
  asker_id: string;
  asker_name: string | null;
  question: string;
  answer: string | null;
  events: { sequence: number; event_type: string; created_at: string }[];
  references: { reference_id: string; native_knowledge_id?: string | null; native_chunk_id?: string | null }[];
  feedback: { useful: boolean; reason_detail: string | null; comment?: string | null } | null;
  review: {
    status: string;
    note: string;
    root_cause: string;
    fix_reference: string;
    verification_references: string[];
    evaluation_candidate: boolean;
  } | null;
}

interface OpsSummary {
  turns: number;
  users: number;
  completed: number;
  failed: number;
  negative_feedback: number;
  feedback_count: number;
  helpful_feedback: number;
  pending_feedback: number;
}

interface QuestionStat {
  question: string;
  count: number;
  user_count: number;
  negative_feedback: number;
  last_asked_at: string;
}

type Tab = "overview" | "history" | "questions" | "feedback" | "trace" | "system";
const PAGE_SIZE = 20;
const TABS: [Tab, string][] = [
  ["overview", "概览"],
  ["history", "问答历史"],
  ["questions", "问题运营榜"],
  ["feedback", "反馈待办"],
  ["trace", "技术 Trace"],
  ["system", "系统状态"],
];

async function jsonRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(withAppBase(path), {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) throw new Error(`请求失败（${response.status}）`);
  return (await response.json()) as T;
}

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function actorName(item: { asker_id: string; asker_name: string | null }): string {
  return item.asker_name
    ? `${item.asker_name}（${item.asker_id}）`
    : `RDMS ${item.asker_id}`;
}

function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function WanshitongOpsApp() {
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [csrfToken, setCsrfToken] = useState("");
  const [tab, setTab] = useState<Tab>("overview");
  const [summary, setSummary] = useState<OpsSummary>();
  const [migrationCount, setMigrationCount] = useState<number>();
  const [questions, setQuestions] = useState<QuestionStat[]>([]);
  const [board, setBoard] = useState<"frequent" | "unresolved">("frequent");
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [reviewStatus, setReviewStatus] = useState("in_review");
  const [reviewNote, setReviewNote] = useState("");
  const [rootCause, setRootCause] = useState("");
  const [fixReference, setFixReference] = useState("");
  const [verificationReferences, setVerificationReferences] = useState("");
  const [evaluationCandidate, setEvaluationCandidate] = useState(false);
  const [seedQuestion, setSeedQuestion] = useState("");
  const [error, setError] = useState("");

  const loadBase = useCallback(async () => {
    const [counts, migration, questionBoard] = await Promise.all([
      jsonRequest<OpsSummary>("/api/admin/ops/summary"),
      jsonRequest<{ items: unknown[] }>("/api/admin/ops/migration"),
      jsonRequest<{ items: QuestionStat[] }>("/api/admin/ops/questions"),
    ]);
    setSummary(counts);
    setMigrationCount(migration.items.length);
    setQuestions(questionBoard.items);
  }, []);

  const loadTraces = useCallback(async () => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
      query,
      feedback_only: String(tab === "feedback"),
    });
    const result = await jsonRequest<{ items: TraceSummary[]; total: number }>(
      `/api/admin/ops/traces?${params}`,
    );
    setTraces(result.items);
    setTotal(result.total);
  }, [offset, query, tab]);

  useEffect(() => {
    void jsonRequest<{ csrf_token: string }>("/api/v1/console/session")
      .then((session) => setCsrfToken(session.csrf_token))
      .catch(() => {
        // 未登录时由页面展示管理员口令表单。
      });
  }, []);

  useEffect(() => {
    if (!csrfToken) return;
    void Promise.resolve()
      .then(() => Promise.all([loadBase(), loadTraces()]))
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : "读取运营数据失败"),
      );
  }, [csrfToken, loadBase, loadTraces]);

  const login = async () => {
    setError("");
    try {
      const session = await jsonRequest<{ csrf_token: string }>(
        "/api/v1/console/session",
        {
          method: "POST",
          body: JSON.stringify({ bootstrap_token: bootstrapToken }),
        },
      );
      setBootstrapToken("");
      setCsrfToken(session.csrf_token);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败");
    }
  };

  const logout = async () => {
    setError("");
    try {
      const response = await fetch(withAppBase("/api/v1/console/session"), {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) throw new Error(`退出失败（${response.status}）`);
      setCsrfToken("");
      setSelected(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "退出失败");
    }
  };

  const selectTrace = async (traceId: string) => {
    setError("");
    try {
      const detail = await jsonRequest<TraceDetail>(
        `/api/admin/ops/traces/${encodeURIComponent(traceId)}`,
      );
      setSelected(detail);
      setReviewStatus(detail.review?.status ?? "in_review");
      setReviewNote(detail.review?.note ?? "");
      setRootCause(detail.review?.root_cause ?? "");
      setFixReference(detail.review?.fix_reference ?? "");
      setVerificationReferences(detail.review?.verification_references.join("\n") ?? "");
      setEvaluationCandidate(detail.review?.evaluation_candidate ?? false);
      setTab("trace");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 Trace 失败");
    }
  };

  const exportTrace = async (traceId: string, includeContent: boolean) => {
    if (
      includeContent &&
      !window.confirm("此文件包含原始问题、回答和引用正文。确认下载？")
    ) return;
    setError("");
    try {
      const response = await fetch(
        withAppBase(`/api/admin/ops/traces/${encodeURIComponent(traceId)}/export`),
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            include_content: includeContent,
            confirm_id: includeContent ? traceId : null,
          }),
        },
      );
      if (!response.ok) throw new Error(`导出失败（${response.status}）`);
      downloadBlob(await response.blob(), `${traceId}.json`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    }
  };

  const exportBatch = async () => {
    setError("");
    try {
      const response = await fetch(withAppBase("/api/admin/ops/traces/export"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ include_content: false }),
      });
      if (!response.ok) throw new Error(`批量导出失败（${response.status}）`);
      downloadBlob(await response.blob(), "wanshitong-traces.jsonl");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "批量导出失败");
    }
  };

  const saveReview = async () => {
    if (!selected) return;
    setError("");
    try {
      await jsonRequest(
        `/api/admin/ops/traces/${encodeURIComponent(selected.bridge_trace_id)}/review`,
        {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken },
          body: JSON.stringify({
            status: reviewStatus,
            note: reviewNote,
            root_cause: rootCause,
            fix_reference: fixReference,
            verification_references: verificationReferences.split("\n").map((item) => item.trim()).filter(Boolean),
            evaluation_candidate: evaluationCandidate,
          }),
        },
      );
      await selectTrace(selected.bridge_trace_id);
      await loadTraces();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存复核失败");
    }
  };

  const changeTab = (next: Tab) => {
    setTab(next);
    setOffset(0);
    setQuery("");
    setSearch("");
  };

  const filteredQuestions = questions.filter((item) =>
    board === "frequent"
      ? item.count >= 2
      : item.count >= 2 && item.negative_feedback > 0,
  );

  return (
    <main className="wst-ops" id="main-content">
      <header className="wst-ops-header">
        <div><p className="wst-ops-eyebrow">湾事通 · 管理中心</p><h1>运营管理</h1></div>
        <nav aria-label="管理导航">
          <a href={withAppBase("/admin/platform/knowledge-bases")}>知识库与文件</a>
          <a href={withAppBase("/admin/platform/settings?section=models")}>模型设置</a>
          <a href={withAppBase("/admin/platform/settings?section=runtime-queues")}>处理任务</a>
          <a href={withAppBase("/")}>普通问答</a>
        </nav>
      </header>
      {!csrfToken ? (
        <section className="wst-ops-login">
          <h2>管理员登录</h2>
          <p>使用 8289 测试环境的湾事通管理员口令。此口令与普通 RDMS 登录密码不同。</p>
          <form onSubmit={(event) => { event.preventDefault(); void login(); }}>
            <label htmlFor="wst-ops-token">管理员口令</label>
            <input autoComplete="off" id="wst-ops-token" onChange={(event) => setBootstrapToken(event.target.value)} type="password" value={bootstrapToken} />
            <button disabled={!bootstrapToken} type="submit">登录</button>
          </form>
        </section>
      ) : (
        <>
          <nav aria-label="运营栏目" className="wst-ops-tabs">
            {TABS.map(([key, label]) => (
              <button aria-current={tab === key ? "page" : undefined} className={tab === key ? "is-active" : ""} key={key} onClick={() => changeTab(key)} type="button">{label}</button>
            ))}
          </nav>
          <div className="wst-ops-actions">
            <span>数据来自 8289 独立候选环境</span>
            <button onClick={() => { void Promise.all([loadBase(), loadTraces()]).catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "刷新失败")); }} type="button">刷新</button>
            <button onClick={() => void logout()} type="button">退出管理</button>
          </div>
          {tab === "overview" && (
            <>
              <section aria-label="运营概览" className="wst-ops-metrics">
                {[
                  ["问答次数", summary?.turns], ["提问人数", summary?.users],
                  ["完成回答", summary?.completed], ["失败回答", summary?.failed],
                  ["待改进反馈", summary?.negative_feedback], ["已映射原件", migrationCount],
                ].map(([label, value]) => (
                  <div key={label}><span>{label}</span><strong>{value ?? "—"}</strong></div>
                ))}
              </section>
              <section>
                <h2>能力入口</h2>
                <div className="wst-ops-links">
                  <a href={withAppBase("/admin/platform/knowledge-bases")}>管理知识库、文件与分块</a>
                  <a href={withAppBase("/admin/platform/settings?section=models")}>选择和配置模型</a>
                  <a href={withAppBase("/admin/platform/settings?section=runtime-queues")}>查看原生处理任务</a>
                </div>
              </section>
              <section>
                <h2>最近问答</h2>
                {traces.length === 0 ? <p>暂无新引擎问答记录。</p> : (
                  <ul className="wst-ops-recent">{traces.slice(0, 5).map((item) => (
                    <li key={item.trace_id}><button onClick={() => void selectTrace(item.trace_id)} type="button">{item.question}</button><span>{actorName(item)} · {displayTime(item.created_at)}</span></li>
                  ))}</ul>
                )}
              </section>
            </>
          )}
          {(tab === "history" || tab === "feedback" || tab === "trace") && (
            <section>
              <div className="wst-ops-section-heading">
                <div><h2>{tab === "feedback" ? "反馈与复核" : tab === "trace" ? "技术 Trace" : "问答历史"}</h2><p>提问者来自 RDMS 身份；问题、回答和引用来自本候选的新引擎记录。</p></div>
                <button onClick={() => void exportBatch()} type="button">导出全部脱敏 Trace</button>
              </div>
              {tab === "feedback" && <div className="wst-ops-metrics">
                <div><span>用户评价</span><strong>{summary?.feedback_count ?? "—"}</strong></div>
                <div><span>有帮助</span><strong>{summary?.helpful_feedback ?? "—"}</strong></div>
                <div><span>待处理</span><strong>{summary?.pending_feedback ?? "—"}</strong></div>
              </div>}
              <form className="wst-ops-search" onSubmit={(event) => { event.preventDefault(); setOffset(0); setQuery(search.trim()); }}>
                <label htmlFor="wst-ops-search">搜索问题、姓名或 RDMS 用户 ID</label>
                <input id="wst-ops-search" onChange={(event) => setSearch(event.target.value)} value={search} />
                <button type="submit">搜索</button>
              </form>
              <div className="wst-ops-table-wrap"><table>
                <thead><tr><th>时间</th><th>提问者</th><th>问题</th><th>状态</th><th>反馈 / 复核</th><th>Trace</th></tr></thead>
                <tbody>{traces.map((item) => (
                  <tr key={item.trace_id}>
                    <td>{displayTime(item.created_at)}</td><td>{actorName(item)}</td><td>{item.question}</td><td>{item.status}</td>
                    <td>{item.feedback_useful === null ? "未反馈" : item.feedback_useful ? "有帮助" : "待改进"}{item.review_status ? ` · ${item.review_status}` : ""}</td>
                    <td><button onClick={() => void selectTrace(item.trace_id)} type="button">查看</button></td>
                  </tr>
                ))}</tbody>
              </table></div>
              {traces.length === 0 && <p>当前条件下没有记录。</p>}
              <div className="wst-ops-pagination">
                <span>共 {total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
                <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} type="button">上一页</button>
                <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)} type="button">下一页</button>
              </div>
            </section>
          )}
          {tab === "questions" && (
            <section>
              <h2>问题运营榜</h2>
              <p>近 7 天按完全相同的问题文本统计。人数是不同 RDMS 用户数；回答次数不代表答案正确。</p>
              <div className="wst-ops-board-tabs">
                <button aria-pressed={board === "frequent"} onClick={() => setBoard("frequent")} type="button">高频问题</button>
                <button aria-pressed={board === "unresolved"} onClick={() => setBoard("unresolved")} type="button">高频但答不好</button>
              </div>
              {filteredQuestions.length === 0 ? <p>当前没有符合条件的重复问题。</p> : (
                <div className="wst-ops-table-wrap"><table>
                  <thead><tr><th>问题</th><th>次数</th><th>人数</th><th>待改进反馈</th><th>最近提问</th><th>运营</th></tr></thead>
                  <tbody>{filteredQuestions.map((item) => (
                    <tr key={item.question}><td>{item.question}</td><td>{item.count}</td><td>{item.user_count}</td><td>{item.negative_feedback}</td><td>{displayTime(item.last_asked_at)}</td><td><button onClick={() => setSeedQuestion(item.question)} type="button">建推荐草稿</button></td></tr>
                  ))}</tbody>
                </table></div>
              )}
              <RecommendationManager csrfToken={csrfToken} seedQuestion={seedQuestion} />
            </section>
          )}
          {tab === "trace" && selected && (
            <section className="wst-ops-detail">
              <h2>Trace 详情</h2>
              <dl>
                <dt>提问者</dt><dd>{actorName(selected)}</dd>
                <dt>原问题</dt><dd>{selected.question}</dd>
                <dt>原生回答</dt><dd className="wst-ops-answer">{selected.answer ?? "尚无完整回答"}</dd>
                <dt>状态</dt><dd>{selected.status}</dd>
                <dt>桥接 Trace</dt><dd>{selected.bridge_trace_id}</dd>
                <dt>原生会话</dt><dd>{selected.native_session_id}</dd>
                <dt>原生消息</dt><dd>{selected.native_message_id ?? "未返回"}</dd>
                <dt>原生请求</dt><dd>{selected.native_request_id ?? "未返回"}</dd>
              </dl>
              <p>实际流事件 {selected.events.length} 条 · 引用 {selected.references.length} 段</p>
              {selected.references.length > 0 && <details><summary>查看引用 ID</summary><ul>{selected.references.map((reference) => (
                <li key={reference.reference_id}>文件 {reference.native_knowledge_id ?? "未提供"} · 分块 {reference.native_chunk_id ?? "未提供"}</li>
              ))}</ul></details>}
              <details><summary>查看事件时序</summary><ol>{selected.events.map((event) => (
                <li key={event.sequence}>{event.sequence} · {event.event_type} · {displayTime(event.created_at)}</li>
              ))}</ol></details>
              <div className="wst-ops-actions">
                <button onClick={() => void exportTrace(selected.bridge_trace_id, false)} type="button">下载脱敏 Trace</button>
                <button onClick={() => void exportTrace(selected.bridge_trace_id, true)} type="button">确认后下载完整 Trace</button>
              </div>
              {selected.feedback && (
                <div className="wst-ops-review">
                  <h3>反馈复核</h3>
                  <p>用户反馈：{selected.feedback.useful ? "有帮助" : "待改进"}{selected.feedback.reason_detail ? ` · ${selected.feedback.reason_detail}` : ""}{selected.feedback.comment ? ` · ${selected.feedback.comment}` : ""}</p>
                  <label htmlFor="wst-review-status">处理状态</label>
                  <select id="wst-review-status" onChange={(event) => setReviewStatus(event.target.value)} value={reviewStatus}>
                    <option value="open">待处理</option><option value="in_review">复核中</option><option value="resolved">已处理</option>
                  </select>
                  <label htmlFor="wst-review-note">复核备注</label>
                  <textarea id="wst-review-note" maxLength={2000} onChange={(event) => setReviewNote(event.target.value)} value={reviewNote} />
                  <label htmlFor="wst-review-cause">根因</label>
                  <input id="wst-review-cause" maxLength={100} onChange={(event) => setRootCause(event.target.value)} value={rootCause} />
                  <label htmlFor="wst-review-fix">修复引用 / 批次</label>
                  <input id="wst-review-fix" maxLength={500} onChange={(event) => setFixReference(event.target.value)} value={fixReference} />
                  <label htmlFor="wst-review-verification">验证记录（每行一条）</label>
                  <textarea id="wst-review-verification" onChange={(event) => setVerificationReferences(event.target.value)} value={verificationReferences} />
                  <label><input checked={evaluationCandidate} onChange={(event) => setEvaluationCandidate(event.target.checked)} type="checkbox" /> 标记为 Evaluation Candidate（只记录候选）</label>
                  <button onClick={() => void saveReview()} type="button">保存复核</button>
                </div>
              )}
            </section>
          )}
          {tab === "system" && (
            <section>
              <h2>系统状态与边界</h2>
              <p>湾事通提供身份、问答界面、运营记录和引用访问；知识库引擎负责解析、检索与回答。</p>
              <p>已映射原件：{migrationCount ?? "读取中"}。新上传、解析分块、模型选择和任务队列使用原生管理页。</p>
              <div className="wst-ops-links">
                <a href={withAppBase("/admin/platform/knowledge-bases")}>知识库与文件</a>
                <a href={withAppBase("/admin/platform/settings?section=models")}>模型管理</a>
                <a href={withAppBase("/admin/platform/settings?section=runtime-queues")}>任务队列</a>
              </div>
            </section>
          )}
        </>
      )}
      {error && <p className="wst-ops-error" role="alert">{error}</p>}
    </main>
  );
}
