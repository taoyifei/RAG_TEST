import { Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  api,
  type Evidence,
  type QueryResponse,
  type RetrievalDiagnostics,
} from "../api/client";
import {
  EmptyState,
  ErrorPanel,
  EvidenceDrawer,
  StatusBadge,
} from "../components/ui";
import { useConsole } from "../state/console-context";

export function QueryPage({ mode }: { mode: "search" | "answer" }) {
  const { tokens, scope } = useConsole();
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<QueryResponse>();
  const [diagnostics, setDiagnostics] = useState<RetrievalDiagnostics>();
  const [diagnosticsError, setDiagnosticsError] = useState<unknown>();
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const activeRequest = useRef<AbortController | undefined>(undefined);
  useEffect(
    () => () => {
      activeRequest.current?.abort();
    },
    [],
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    setError(undefined);
    setDiagnostics(undefined);
    setDiagnosticsError(undefined);
    try {
      const response =
        mode === "search"
          ? await api.search(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
            )
          : await api.answer(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
            );
      setResult(response);
      if (mode === "search") {
        void api
          .diagnostics(tokens.admin, response.trace_id)
          .then(setDiagnostics)
          .catch(setDiagnosticsError);
      }
    } catch (reason) {
      setError(
        reason instanceof DOMException && reason.name === "AbortError"
          ? new Error("查询已中断，请重新提交查询。")
          : reason,
      );
    } finally {
      if (activeRequest.current === controller) setBusy(false);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>{mode === "search" ? "检索诊断" : "证据问答"}</h2>
          <p>
            {mode === "search"
              ? "查看真实通道、RRF 贡献、重排与证据选择。"
              : "按文档提供参考原文。当前未配置生成模型。"}
          </p>
        </div>
      </div>
      <form className="query-box" onSubmit={submit}>
        <label htmlFor={`${mode}-query`}>查询文本</label>
        <div>
          <input
            id={`${mode}-query`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="例如：青岛啤酒"
            required
          />
          <button className="primary" disabled={busy}>
            <Search aria-hidden="true" size={18} />
            {busy ? "执行中…" : "执行"}
          </button>
        </div>
      </form>
      {error !== undefined && <ErrorPanel error={error} />}
      {result && (
        <>
          <div className="metric-grid">
            <article>
              <span>状态</span>
              <StatusBadge value={result.status} />
              <details>
                <summary>技术详情</summary>
                <code>{result.reason_code}</code>
              </details>
            </article>
            <article>
              <span>检索方式</span>
              <strong>
                {result.selected_embedding_slot ? "向量与原文检索" : "原文检索"}
              </strong>
              <details>
                <summary>技术详情</summary>
                <code>
                  {result.route_reason_code} · {result.selected_embedding_slot}
                </code>
              </details>
            </article>
            <article>
              <span>证据</span>
              <strong>{result.evidence_count}</strong>
              <details>
                <summary>技术详情</summary>
                <code>{result.quality_profile_status}</code>
              </details>
            </article>
          </div>
          {mode === "answer" && result.answer && (
            <article className="answer">
              <span className="eyebrow">参考原文</span>
              <p>{result.answer}</p>
            </article>
          )}
          <div className="evidence-grid">
            {result.evidence.map((item) => (
              <button
                key={item.evidence_id}
                className="evidence-card"
                onClick={() => setEvidence(item)}
              >
                <span>{item.source_label}</span>
                <p>{item.citation_text}</p>
                <small>原文引用 · 排序 {item.fusion_rank ?? "—"}</small>
              </button>
            ))}
          </div>
          {!result.evidence.length && (
            <EmptyState title="没有可发布证据">
              系统不会为无证据结果生成伪引用。
            </EmptyState>
          )}
          {diagnostics && <DiagnosticsView value={diagnostics} />}
          {diagnosticsError !== undefined && (
            <details className="panel">
              <summary>诊断信息暂不可用</summary>
              <ErrorPanel error={diagnosticsError} />
            </details>
          )}
        </>
      )}
      <EvidenceDrawer evidence={evidence} onClose={() => setEvidence(null)} />
    </section>
  );
}

function DiagnosticsView({ value }: { value: RetrievalDiagnostics }) {
  return (
    <div className="panel">
      <h3>安全检索诊断</h3>
      <div className="diagnostic-columns">
        <div>
          <h4>通道候选</h4>
          {value.channel_chunk_ids.map(([channel, ids]) => (
            <details key={channel}>
              <summary>
                {channel} · {ids.length}
              </summary>
              <pre>{ids.join("\n")}</pre>
            </details>
          ))}
        </div>
        <div>
          <h4>RRF 融合贡献</h4>
          {value.fusion.map((item) => (
            <details key={item.chunk_id}>
              <summary>
                #{item.rank} · {item.chunk_id}
              </summary>
              <pre>{JSON.stringify(item.contributions, null, 2)}</pre>
            </details>
          ))}
        </div>
        <div>
          <h4>阶段耗时</h4>
          {value.stage_timings.map((item) => (
            <p key={item.stage}>
              {item.stage}
              <strong>{item.elapsed_ms.toFixed(2)} ms</strong>
            </p>
          ))}
        </div>
      </div>
    </div>
  );
}
