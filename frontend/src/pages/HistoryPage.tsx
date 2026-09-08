import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  type HistoryFilters,
  type HistoryPageResult,
  type KnowledgeBase,
} from "../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../components/ui";
import { HistoryTrace, historyTime } from "../components/HistoryTrace";
import { useConsole } from "../state/console-context";

const PAGE_SIZE = 20;
const STATUSES = [
  ["", "全部结果"],
  ["ANSWERED", "已回答"],
  ["REFUSED", "拒答"],
  ["FAILED", "出错"],
  ["STARTED", "处理中"],
  ["INTERRUPTED", "进程中断"],
  ["CANCELLED", "取消"],
];

export function HistoryPage({ go }: { go?: (path: string) => void }) {
  const { scope, tokens } = useConsole();
  const [kbId, setKbId] = useState(scope.kbId);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [status, setStatus] = useState("");
  const [keyword, setKeyword] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [filters, setFilters] = useState<HistoryFilters>({
    project_id: scope.projectId,
    knowledge_base_id: scope.kbId,
    page_size: PAGE_SIZE,
    offset: 0,
  });
  const [page, setPage] = useState<HistoryPageResult>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [traceId, setTraceId] = useState<string | undefined>(() => {
    const value = new URLSearchParams(window.location.search).get("trace_id");
    return value || undefined;
  });
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .listHistory(filters, controller.signal)
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
  }, [filters, reload]);
  useEffect(() => {
    if (!scope.projectId) return;
    let active = true;
    void api
      .listKnowledgeBases(tokens.admin, scope.projectId)
      .then((value) => {
        if (active) setKbs(value.items);
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [scope.projectId, tokens.admin]);
  function search(event: FormEvent) {
    event.preventDefault();
    if (from && to && from > to) {
      setError(new Error("开始时间不能晚于结束时间。"));
      return;
    }
    setPage(undefined);
    setLoading(true);
    setError(undefined);
    setTraceId(undefined);
    setFilters({
      project_id: scope.projectId,
      knowledge_base_id: kbId,
      status,
      keyword,
      created_from: from ? new Date(from).toISOString() : undefined,
      created_to: to ? new Date(to).toISOString() : undefined,
      page_size: PAGE_SIZE,
      offset: 0,
    });
  }
  function turn(offset: number) {
    setLoading(true);
    setError(undefined);
    setPage(undefined);
    setFilters({ ...filters, offset });
  }
  async function clear() {
    setClearing(true);
    setError(undefined);
    try {
      await api.clearHistory();
      setClearOpen(false);
      setTraceId(undefined);
      turn(0);
      setReload((value) => value + 1);
    } catch (reason) {
      setError(reason);
    } finally {
      setClearing(false);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>问答历史</h2>
          <p>
            问题与答案在本机加密保存，当前保留 {page?.retention_days ?? 7}{" "}
            天。原文访问会重新检查权限。
          </p>
          {page?.body_enabled === false && (
            <p role="status">
              正文保存已关闭；新的请求只保留摘要信息，无法恢复问题与答案。
            </p>
          )}
        </div>
        <button className="secondary" onClick={() => setClearOpen(true)}>
          清理全部历史
        </button>
      </div>
      <form className="panel history-filters" onSubmit={search}>
        <label>
          知识库
          <select
            value={kbId}
            onChange={(event) => setKbId(event.target.value)}
          >
            <option value="">全部知识库</option>
            {scope.kbId &&
              !kbs.some((kb) => kb.knowledge_base_id === scope.kbId) && (
                <option value={scope.kbId}>当前知识库</option>
              )}
            {kbs.map((kb) => (
              <option key={kb.knowledge_base_id} value={kb.knowledge_base_id}>
                {kb.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          结果
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            {STATUSES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          开始时间
          <input
            type="datetime-local"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
          />
        </label>
        <label>
          结束时间
          <input
            type="datetime-local"
            value={to}
            onChange={(event) => setTo(event.target.value)}
          />
        </label>
        <label>
          问题关键词
          <input
            value={keyword}
            maxLength={200}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="仅搜索有权查看的正文"
          />
        </label>
        <button className="primary">筛选历史</button>
      </form>
      {error !== undefined && <ErrorPanel error={error} />}
      {loading && <p role="status">正在读取历史…</p>}
      {page && (
        <>
          <div className="row-actions" aria-label="历史分页">
            <button
              disabled={page.offset === 0}
              onClick={() => turn(Math.max(0, page.offset - PAGE_SIZE))}
            >
              上一页
            </button>
            <span>
              共 {page.total} 条 · 第 {Math.floor(page.offset / PAGE_SIZE) + 1}{" "}
              页
            </span>
            <button
              disabled={page.offset + page.items.length >= page.total}
              onClick={() => turn(page.offset + PAGE_SIZE)}
            >
              下一页
            </button>
            <button
              onClick={() => {
                setLoading(true);
                setReload((value) => value + 1);
              }}
            >
              刷新历史
            </button>
          </div>
          <div className="card-list history-list">
            {page.items.map((item) => (
              <article key={item.trace_id}>
                <div className="grow">
                  <h3>
                    {item.body_available
                      ? item.question || "无问题正文"
                      : item.body_message}
                  </h3>
                  <p className="history-summary">
                    {item.body_available
                      ? item.answer_summary || "本次没有发布答案。"
                      : "正文不可查看"}
                  </p>
                  <small>
                    <time dateTime={item.created_at}>
                      {historyTime(item.created_at)}
                    </time>{" "}
                    · {item.models?.join("、") || "未调用远程模型"} ·{" "}
                    {item.duration_ms == null
                      ? "尚未结束"
                      : `${item.duration_ms.toFixed(1)} ms`}
                  </small>
                  <code>{item.trace_id}</code>
                </div>
                <div>
                  <StatusBadge value={item.status} />
                  {item.cache_hit && <small>缓存命中</small>}
                </div>
                <button onClick={() => setTraceId(item.trace_id)}>
                  查看详情与过程
                </button>
                {go && (
                  <button
                    className="secondary"
                    onClick={() => {
                      const url = new URL(window.location.href);
                      url.searchParams.set("trace_id", item.trace_id);
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
                )}
              </article>
            ))}
          </div>
          {!page.items.length && (
            <EmptyState title="没有匹配的历史">
              新请求会显示在这里；未曾保存的旧请求无法恢复。
            </EmptyState>
          )}
        </>
      )}
      {traceId && (
        <HistoryTrace
          key={traceId}
          traceId={traceId}
          onClose={() => setTraceId(undefined)}
        />
      )}
      {clearOpen && (
        <Modal
          title="清理全部问答历史"
          onClose={() => !clearing && setClearOpen(false)}
        >
          <p>
            将删除本机所有项目和知识库的问答历史及
            Trace，无法撤销。源文档与索引保留。
          </p>
          <button
            className="danger"
            disabled={clearing}
            onClick={() => void clear()}
          >
            {clearing ? "清理中…" : "确认清理全部历史"}
          </button>
          <button disabled={clearing} onClick={() => setClearOpen(false)}>
            取消
          </button>
        </Modal>
      )}
    </section>
  );
}
