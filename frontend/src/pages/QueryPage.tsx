import { Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  api,
  ApiError,
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
import { DiagnosticsView, HistoryTrace } from "../components/HistoryTrace";
import { useConsole } from "../state/console-context";
import { isOcrEvidence, OcrEvidenceSource } from "../components/DocumentImages";

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

function answerDeliveryMessage(result: QueryResponse): string {
  if (result.result_origin === "cache" || result.cache_hit) {
    return "本次结果来自查询缓存；本次请求没有调用回答模型或问题改写模型。";
  }
  if (result.generation_mode === "llm") {
    return result.generation_called_this_request === false
      ? "答案由已核验的模型结果提供，但本次没有记录到新的模型调用。"
      : "本次答案由回答模型基于所列证据生成，并已通过来源校验。";
  }
  if (result.generation_mode === "extractive") {
    return "本次答案采用原文摘录；未使用回答模型生成。";
  }
  const reason = result.generation_reason_code ?? "";
  if (result.generation_mode === "extractive_fallback") {
    if (reason.includes("BUDGET")) {
      return "回答模型因预算不可用，本次已安全回退为原文摘录。";
    }
    if (reason.includes("AUTHORIZED") || reason.includes("POLICY_DENIED")) {
      return "回答模型未获本次资料出网授权，本次已安全回退为原文摘录。";
    }
    return "回答模型本次调用失败或输出未通过校验，已安全回退为原文摘录。";
  }
  if (result.status === "AMBIGUOUS_NEEDS_CLARIFICATION") {
    return "当前问题含义不足以可靠确定，请补充对象、范围或所问关系。";
  }
  if (reason.includes("BUDGET")) {
    return "回答模型预算不可用，且当前证据不足以提供原文摘录答案。";
  }
  if (reason.includes("AUTHORIZED") || reason.includes("POLICY_DENIED")) {
    return "回答模型未获本次资料出网授权，且当前证据不足以提供答案。";
  }
  if (reason === "GENERATOR_NOT_CONFIGURED") {
    return "本知识库未配置回答模型；当前资料也没有足以直接回答的证据。";
  }
  return "当前资料没有足以直接回答这个问题的证据。";
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
  const [saveBody, setSaveBody] = useState(true);
  const [savedBody, setSavedBody] = useState(true);
  const [historyTrace, setHistoryTrace] = useState<string>();
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
    setHistoryTrace(undefined);
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
              saveBody ? "full" : "metadata_only",
            )
          : await api.answer(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
              true,
              saveBody ? "full" : "metadata_only",
            );
      if (controller.signal.aborted) return;
      setResult(response);
      setSavedBody(saveBody);
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
              : "基于当前知识库回答问题，每条引用均可核对原文。"}
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
            placeholder="输入需要从资料中查证的问题"
            required
          />
          <button className="primary" disabled={busy}>
            <Search aria-hidden="true" size={18} />
            {busy ? "执行中…" : "执行"}
          </button>
        </div>
        <label className="query-options">
          <input
            type="checkbox"
            checked={saveBody}
            onChange={(event) => setSaveBody(event.target.checked)}
          />
          在本机加密保存本次问题与答案（默认保留 7 天）
        </label>
      </form>
      {error !== undefined && <ErrorPanel error={error} />}
      {error instanceof ApiError && error.traceId && (
        <button onClick={() => setHistoryTrace(error.traceId)}>
          查看失败请求过程
        </button>
      )}
      {sourceError !== undefined && <ErrorPanel error={sourceError} />}
      {result && (
        <>
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
          <div className="row-actions">
            <StatusBadge value={result.status} />
            <button onClick={() => setHistoryTrace(result.trace_id)}>
              查看检索过程
            </button>
            <small>
              {savedBody
                ? "已请求本机保存；可在问答历史中查看"
                : "本次未保存正文"}
            </small>
          </div>
          <p role="status">{answerDeliveryMessage(result)}</p>
          <details className="panel">
            <summary>技术统计与检索状态</summary>
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
                  {result.selected_embedding_slot
                    ? "向量与原文检索"
                    : "原文检索"}
                </strong>
                <details>
                  <summary>技术详情</summary>
                  <code>
                    {result.route_reason_code} ·{" "}
                    {result.selected_embedding_slot}
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
            <p>
              回答方式：{result.generation_mode} ·{" "}
              {result.degraded_reason_codes?.join("、") || "未报告降级"}
            </p>
            <p>
              结果来源：{result.result_origin ?? "fresh"} · 本次回答模型调用：
              {result.generation_called_this_request ? "是" : "否"} ·
              本次问题改写模型调用：
              {result.rewrite_called_this_request ? "是" : "否"}
            </p>
            <code>{result.trace_id}</code>
          </details>
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
                      {isOcrEvidence(item) && "图片识别文字 · "}
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
          {diagnostics && (
            <details className="panel">
              <summary>本次检索诊断</summary>
              <DiagnosticsView value={diagnostics} />
            </details>
          )}
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
        imageSource={
          evidence && (
            <OcrEvidenceSource
              evidence={evidence}
              projectId={scope.projectId}
              kbId={scope.kbId}
            />
          )
        }
        purpose={mode === "search" ? "diagnostic" : "citation"}
        onClose={() => setEvidence(null)}
      />
      {historyTrace && (
        <HistoryTrace
          key={historyTrace}
          traceId={historyTrace}
          onClose={() => setHistoryTrace(undefined)}
        />
      )}
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
