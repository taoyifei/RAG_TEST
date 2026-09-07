import { Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  api,
  type Evidence,
  type QueryResponse,
  type RetrievalDiagnostics,
  type RelatedContent,
  type SourceChunk,
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
  const [identity, setIdentity] = useState({ ...tokens, generation: 0 });
  if (identity.query !== tokens.query || identity.admin !== tokens.admin) {
    setIdentity({ ...tokens, generation: identity.generation + 1 });
  }
  return (
    <ScopedQueryPage
      key={`${mode}:${scope.projectId}:${scope.kbId}:${scope.revisionId}:${identity.generation}`}
      mode={mode}
    />
  );
}

function ScopedQueryPage({ mode }: { mode: "search" | "answer" }) {
  const { tokens, scope } = useConsole();
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<QueryResponse>();
  const [diagnostics, setDiagnostics] = useState<RetrievalDiagnostics>();
  const [diagnosticsError, setDiagnosticsError] = useState<unknown>();
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [source, setSource] = useState<SourceChunk | null>(null);
  const [sourceError, setSourceError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const activeRequest = useRef<AbortController | undefined>(undefined);
  const sourceRequest = useRef<AbortController | undefined>(undefined);
  useEffect(
    () => () => {
      activeRequest.current?.abort();
      sourceRequest.current?.abort();
    },
    [],
  );
  async function submit(event: FormEvent) {
    event.preventDefault();
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setBusy(true);
    sourceRequest.current?.abort();
    setResult(undefined);
    setEvidence(null);
    setSource(null);
    setSourceError(undefined);
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
              true,
            )
          : await api.answer(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
              true,
            );
      if (controller.signal.aborted) return;
      setResult(response);
      if (mode === "search") {
        void api
          .diagnostics(tokens.admin, response.trace_id)
          .then((value) => {
            if (!controller.signal.aborted) setDiagnostics(value);
          })
          .catch((reason) => {
            if (!controller.signal.aborted) setDiagnosticsError(reason);
          });
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      setError(
        reason instanceof DOMException && reason.name === "AbortError"
          ? new Error("查询已中断，请重新提交查询。")
          : reason,
      );
    } finally {
      if (activeRequest.current === controller) setBusy(false);
    }
  }
  async function openRelated(item: RelatedContent) {
    sourceRequest.current?.abort();
    const controller = new AbortController();
    sourceRequest.current = controller;
    setSource(null);
    setSourceError(undefined);
    try {
      const chunk = await api.readRelatedSource(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        item,
        controller.signal,
      );
      if (!controller.signal.aborted) setSource(chunk);
    } catch (reason) {
      if (controller.signal.aborted) return;
      setResult(undefined);
      setSourceError(reason);
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
      {sourceError !== undefined && <ErrorPanel error={sourceError} />}
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
            <section
              className="answer"
              aria-label="正式答案"
              data-raw-answer={result.answer}
            >
              <span className="eyebrow">正式答案</span>
              <p>{result.answer}</p>
            </section>
          )}
          {!!result.evidence.length && (mode === "search" || result.answer) && (
            <section
              aria-label={mode === "search" ? "检索候选" : "引用依据"}
              data-content-role={
                mode === "search" ? "diagnostic-evidence" : "answer-citations"
              }
            >
              <h3>{mode === "search" ? "检索候选" : "引用依据"}</h3>
              {mode === "search" && (
                <p>供管理员检查检索结果，不代表已发布的答案或正式引用。</p>
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
                    <small>
                      {mode === "search" ? "未发布候选" : "原文引用"} · 排序{" "}
                      {item.fusion_rank ?? "—"}
                    </small>
                  </button>
                ))}
              </div>
            </section>
          )}
          {!result.answer && result.display_message && (
            <p role="status">{result.display_message}</p>
          )}
          {!result.answer && !!result.related_contents?.length && (
            <section aria-label="相关内容" data-content-role="related-content">
              <h3>相关内容</h3>
              <p>以下为原文片段，不代表已确认答案。</p>
              <div className="evidence-grid">
                {result.related_contents.map((item) => (
                  <article className="evidence-card" key={item.related_id}>
                    <strong>{item.document_name}</strong>
                    <p>{item.heading_path.join(" / ")}</p>
                    <blockquote>{item.excerpt}</blockquote>
                    <p>
                      <small>
                        {item.rerank_verified
                          ? "仅供参考"
                          : "未完成相关性复核，仅供查阅。"}
                      </small>
                    </p>
                    <button onClick={() => void openRelated(item)}>
                      查看原文
                    </button>
                  </article>
                ))}
              </div>
            </section>
          )}
          {!result.evidence.length && !result.display_message && (
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
      <EvidenceDrawer
        evidence={evidence}
        purpose={mode === "search" ? "diagnostic" : "citation"}
        onClose={() => setEvidence(null)}
      />
      {source && (
        <section className="panel" aria-label="相关原文详情">
          <h3>相关原文 · 仅供参考</h3>
          <button onClick={() => setSource(null)}>关闭原文</button>
          <blockquote>{source.citation_text}</blockquote>
          <p>章节：{source.heading_path.join(" / ") || "原文片段"}</p>
          <details>
            <summary>原文位置</summary>
            <ul>
              {source.source_spans
                .filter(
                  (span) =>
                    span.source_start_char !== null &&
                    span.source_start_char !== undefined,
                )
                .map((span, index) => (
                  <li key={index}>
                    第 {index + 1} 处原文，第{" "}
                    {(span.source_start_char ?? 0) + 1} 至{" "}
                    {span.source_end_char} 字符
                  </li>
                ))}
            </ul>
          </details>
        </section>
      )}
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
