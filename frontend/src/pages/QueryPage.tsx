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
  type StreamedAnswerClaim,
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
import { QueryFeedback } from "../components/QueryFeedback";

function newConversationId(): string {
  if (typeof crypto.randomUUID === "function") {
    return `conversation-${crypto.randomUUID()}`;
  }
  return `conversation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function QueryPage({
  mode,
  go,
}: {
  mode: "search" | "answer";
  go?: (path: string) => void;
}) {
  const { tokens, scope } = useConsole();
  const [identity, setIdentity] = useState({ ...tokens, generation: 0 });
  if (identity.query !== tokens.query || identity.admin !== tokens.admin) {
    setIdentity({ ...tokens, generation: identity.generation + 1 });
  }
  return (
    <ScopedQueryPage
      key={`${mode}:${scope.projectId}:${scope.kbId}:${scope.revisionId}:${identity.generation}`}
      mode={mode}
      go={go}
    />
  );
}

function answerDeliveryMessage(result: QueryResponse): string {
  if (result.result_origin === "singleflight") {
    return "本次复用了同一主体、权限、索引版本和会话下的等价在途计算；历史与 Trace 仍独立记录。";
  }
  if (result.result_origin === "cache" || result.cache_hit) {
    return "本次结果来自查询缓存；本次请求没有调用回答模型或问题改写模型。";
  }
  if (result.generation_mode === "llm") {
    return result.generation_called_this_request === false
      ? "答案由已核验的模型结果提供，但本次没有记录到新的模型调用。"
      : "本次答案由回答模型基于所列证据生成，并已通过来源校验。";
  }
  if (result.generation_mode === "extractive") {
    return "本次答案由本地结构化回答器从已验证证据生成；未调用回答模型。";
  }
  const reason = result.generation_reason_code ?? "";
  if (result.generation_mode === "extractive_fallback") {
    if (reason.includes("BUDGET")) {
      return "回答模型因预算不可用，本次已安全回退为本地结构化回答。";
    }
    if (reason.includes("AUTHORIZED") || reason.includes("POLICY_DENIED")) {
      return "回答模型未获本次资料出网授权，本次已安全回退为本地结构化回答。";
    }
    return "回答模型本次调用失败或输出未通过校验，已安全回退为本地结构化回答。";
  }
  if (result.status === "AMBIGUOUS_NEEDS_CLARIFICATION") {
    return "当前问题含义不足以可靠确定，请补充对象、范围或所问关系。";
  }
  if (result.status === "CONFIGURATION_REQUIRED") {
    return "已找到可供模型核验的相关来源，但本知识库的回答模型尚未完成配置，当前本地证据不足以发布答案。";
  }
  if (result.status === "BUDGET_BLOCKED") {
    return "已找到可供模型核验的相关来源，但模型累计预算不可用，当前本地证据不足以发布答案。";
  }
  if (result.status === "POLICY_DENIED") {
    return "已找到可供模型核验的相关来源，但本次资料或模型操作未获授权，当前本地证据不足以发布答案。";
  }
  if (result.status === "PROVIDER_UNAVAILABLE") {
    return "已找到可供模型核验的相关来源，但模型服务暂不可用，当前本地证据不足以发布答案。";
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

const STRUCTURED_ANSWER_TYPES = new Set([
  "DUTIES",
  "ENUMERATION",
  "COUNT",
  "ORDINAL_ITEM",
  "PROCEDURE",
]);

function AnswerContent({ result }: { result: QueryResponse }) {
  const answer = result.answer;
  if (!answer) return null;
  const lines = answer.split("\n").filter((line) => line.trim().length > 0);
  if (
    lines.length > 1 &&
    STRUCTURED_ANSWER_TYPES.has(result.requested_answer_type)
  ) {
    return (
      <ul
        className="structured-answer"
        data-answer-type={result.requested_answer_type}
        aria-label="结构化答案"
      >
        {lines.map((line, index) => (
          <li key={`${index}:${line}`}>{line}</li>
        ))}
      </ul>
    );
  }
  return <p>{answer}</p>;
}

function retrievalDeliveryMessage(result: QueryResponse): string {
  const dataPlane = result.data_plane;
  if (!dataPlane) return "原文检索（旧记录未保存数据面）";
  if (dataPlane.retrieval_data_plane === "default_local_fallback") {
    return "本地确定性检索";
  }
  return "活动真实 Dense 检索";
}

function ScopedQueryPage({
  mode,
  go,
}: {
  mode: "search" | "answer";
  go?: (path: string) => void;
}) {
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
  const [streamClaims, setStreamClaims] = useState<StreamedAnswerClaim[]>([]);
  const [streamStage, setStreamStage] = useState<string>();
  const [conversationId, setConversationId] = useState(newConversationId);
  const [conversationBusy, setConversationBusy] = useState(false);
  const [conversationStatus, setConversationStatus] = useState<string>();
  const [conversationError, setConversationError] = useState<unknown>();
  const activeRequest = useRef<AbortController | undefined>(undefined);
  const requestGeneration = useRef(0);
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
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
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
    setStreamClaims([]);
    setStreamStage(mode === "answer" ? "accepted" : undefined);
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
          : await api.answerStream(
              tokens.query,
              scope.projectId,
              scope.kbId,
              query,
              controller.signal,
              true,
              saveBody ? "full" : "metadata_only",
              {
                onStage: (stage) => {
                  if (
                    activeRequest.current === controller &&
                    requestGeneration.current === generation &&
                    !controller.signal.aborted
                  ) {
                    setStreamStage(stage);
                  }
                },
                onClaim: (claim) => {
                  if (
                    activeRequest.current !== controller ||
                    requestGeneration.current !== generation ||
                    controller.signal.aborted
                  ) {
                    return;
                  }
                  setStreamClaims((current) => [...current, claim]);
                },
              },
              conversationId,
            );
      if (controller.signal.aborted) return;
      setResult(response);
      setStreamClaims([]);
      setStreamStage("final");
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
  function stopAnswer() {
    const controller = activeRequest.current;
    if (!controller || controller.signal.aborted) return;
    controller.abort();
    setStreamStage("cancelled");
    setError(new Error("流式查询已停止；暂存内容不是最终答案。"));
  }
  function startNewConversation() {
    activeRequest.current?.abort();
    setConversationId(newConversationId());
    setConversationStatus("已开始新会话；先前会话仍按保留策略保存。");
    setConversationError(undefined);
    setResult(undefined);
    setStreamClaims([]);
    setStreamStage(undefined);
    setHistoryTrace(undefined);
  }
  async function clearConversation() {
    activeRequest.current?.abort();
    setConversationBusy(true);
    setConversationError(undefined);
    try {
      const cleared = await api.clearConversation(
        scope.projectId,
        scope.kbId,
        conversationId,
      );
      setConversationStatus(
        cleared.deleted
          ? `已清空当前会话的 ${cleared.deleted_turns} 轮。`
          : "当前会话没有已保存轮次。",
      );
      setResult(undefined);
      setStreamClaims([]);
      setStreamStage(undefined);
      setHistoryTrace(undefined);
    } catch (reason) {
      setConversationError(reason);
    } finally {
      setConversationBusy(false);
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
      {mode === "answer" && (
        <section className="panel conversation-controls" aria-label="当前会话">
          <div>
            <span className="eyebrow">当前会话</span>
            <code>{conversationId}</code>
            <small>
              仅保存有界问题和已验证事实摘要；切换知识库或退出后不会串用。
            </small>
          </div>
          <div className="row-actions">
            <button
              type="button"
              disabled={busy || conversationBusy}
              onClick={startNewConversation}
            >
              新会话
            </button>
            <button
              type="button"
              disabled={busy || conversationBusy}
              onClick={() => void clearConversation()}
            >
              {conversationBusy ? "清空中…" : "清空当前会话"}
            </button>
          </div>
          {conversationStatus && <p role="status">{conversationStatus}</p>}
          {conversationError !== undefined && (
            <ErrorPanel error={conversationError} />
          )}
        </section>
      )}
      <form className="query-box" onSubmit={submit}>
        <label htmlFor={`${mode}-query`}>查询文本</label>
        <div>
          <input
            id={`${mode}-query`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入需要从资料中查证的问题"
            maxLength={mode === "answer" ? 2000 : 8000}
            required
          />
          <button className="primary" type="submit" disabled={busy}>
            <Search aria-hidden="true" size={18} />
            {busy ? "执行中…" : "执行"}
          </button>
          {mode === "answer" && busy && (
            <button className="secondary" type="button" onClick={stopAnswer}>
              停止
            </button>
          )}
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
      {mode === "answer" && busy && streamStage && (
        <p role="status">流式阶段：{streamStage}</p>
      )}
      {mode === "answer" && !result && streamClaims.length > 0 && (
        <section className="answer" aria-label="已核验暂存内容">
          <span className="eyebrow">已核验暂存内容</span>
          <p>以下事实已通过来源校验，仍以最终答案为唯一权威结果。</p>
          <ol>
            {streamClaims.map((claim) => (
              <li key={claim.claim_index}>{claim.text}</li>
            ))}
          </ol>
        </section>
      )}
      {result && (
        <>
          {mode === "answer" && result.answer && (
            <section
              className="answer"
              aria-label="正式答案"
              data-raw-answer={result.answer}
            >
              <span className="eyebrow">正式答案</span>
              <AnswerContent result={result} />
            </section>
          )}
          <div className="row-actions">
            <StatusBadge value={result.status} />
            <button onClick={() => setHistoryTrace(result.trace_id)}>
              查看检索过程
            </button>
            {go && (
              <button
                className="secondary"
                onClick={() => openOperationalTrace(result.trace_id, go)}
              >
                打开技术 Trace
              </button>
            )}
            <small>
              {savedBody
                ? "已请求本机保存；可在问答历史中查看"
                : "本次未保存正文"}
            </small>
          </div>
          <p role="status">{answerDeliveryMessage(result)}</p>
          {!result.answer &&
            go &&
            [
              "CONFIGURATION_REQUIRED",
              "POLICY_DENIED",
              "BUDGET_BLOCKED",
            ].includes(result.status) && (
              <button className="secondary" onClick={() => go("/documents")}>
                {result.status === "CONFIGURATION_REQUIRED"
                  ? "配置回答模型"
                  : result.status === "BUDGET_BLOCKED"
                    ? "检查授权与预算"
                    : "检查资料授权"}
              </button>
            )}
          <QueryFeedback
            key={result.trace_id}
            projectId={scope.projectId}
            kbId={scope.kbId}
            traceId={result.trace_id}
          />
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
                <strong>{retrievalDeliveryMessage(result)}</strong>
                <details>
                  <summary>技术详情</summary>
                  <code>
                    {result.route_reason_code} · slot={
                      result.selected_embedding_slot ?? "none"
                    }
                    {result.data_plane && (
                      <>
                        {" · "}
                        {result.data_plane.embedding_provider_id ?? "none"} /{" "}
                        {result.data_plane.embedding_model ?? "none"} /{" "}
                        {result.data_plane.selected_vector_space ?? "none"}
                      </>
                    )}
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
              本次意图解释模型调用：
              {result.interpret_called_this_request ? "是" : "否"} ·
              本次问题改写模型调用：
              {result.rewrite_called_this_request ? "是" : "否"}
            </p>
            <p>
              所问类型：{result.requested_answer_type} · 语义来源：
              {result.query_semantic_source}
            </p>
            {result.data_plane && (
              <p>
                Profile：
                {result.data_plane.active_retrieval_profile_revision_id ??
                  "未激活"}
                {" · "}Reranker：
                {result.data_plane.reranker_provider_id ?? "未配置"} /{" "}
                {result.data_plane.reranker_model ?? "未配置"}（
                {result.data_plane.rerank_mode}） · 生成配置：
                {result.data_plane.model_configuration_state} · 模型授权：
                {result.data_plane.model_authorization_state} · 资料授权：
                {result.data_plane.corpus_authorization_state} · 预算：
                {result.data_plane.budget_state}
              </p>
            )}
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

function openOperationalTrace(traceId: string, go: (path: string) => void) {
  const url = new URL(window.location.href);
  url.searchParams.set("trace_id", traceId);
  window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  go("/operational-traces");
}
