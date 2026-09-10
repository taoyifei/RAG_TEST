import { useEffect, useRef, useState } from "react";
import {
  api,
  type Evidence,
  type HistoryEntry,
  type RetrievalDiagnostics,
  type SourceChunk,
} from "../api/client";
import { useConsole } from "../state/console-context";
import { downloadFile } from "../utils/download";
import { ErrorPanel, Modal, StatusBadge } from "./ui";
import { OcrEvidenceSource } from "./DocumentImages";
import { QueryFeedback } from "./QueryFeedback";

export function historyTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

export function HistoryTrace({
  traceId,
  onClose,
}: {
  traceId: string;
  onClose: () => void;
}) {
  const { tokens } = useConsole();
  const [entry, setEntry] = useState<HistoryEntry>();
  const [error, setError] = useState<unknown>();
  const [source, setSource] = useState<SourceChunk>();
  const [sourceError, setSourceError] = useState<unknown>();
  const [sourceBusy, setSourceBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<unknown>();
  const activeRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => {
    const controller = new AbortController();
    activeRequest.current = controller;
    void api
      .historyDetail(traceId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setEntry(value);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason);
      });
    return () => controller.abort();
  }, [traceId]);
  async function openSource(evidence: Evidence) {
    const controller = activeRequest.current;
    if (!entry?.active_index_revision_id || !controller) return;
    setSource(undefined);
    setSourceError(undefined);
    setSourceBusy(true);
    try {
      const fresh = await api.readEvidenceSource(
        tokens.admin,
        entry.project_id,
        entry.knowledge_base_id,
        entry.active_index_revision_id,
        evidence,
        controller.signal,
      );
      if (!controller.signal.aborted) setSource(fresh);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setSourceError(reason);
        // 来源权限变化后清除旧正文，重新读取受保护的历史详情。
        setEntry(undefined);
        void api
          .historyDetail(traceId, controller.signal)
          .then((value) => {
            if (!controller.signal.aborted) setEntry(value);
          })
          .catch((failure) => {
            if (!controller.signal.aborted) setError(failure);
          });
      }
    } finally {
      if (!controller.signal.aborted) setSourceBusy(false);
    }
  }
  async function exportSupport(includeBody: boolean) {
    if (
      includeBody &&
      !window.confirm("该文件可能含敏感问答和引用。确认保存到受控位置吗？")
    ) {
      return;
    }
    setExportBusy(true);
    setExportError(undefined);
    try {
      downloadFile(await api.exportHistoryTrace(traceId, includeBody));
    } catch (reason) {
      setExportError(reason);
    } finally {
      setExportBusy(false);
    }
  }
  async function exportTechnicalTrace() {
    setExportBusy(true);
    setExportError(undefined);
    try {
      downloadFile(await api.exportOperationalTrace(traceId));
    } catch (reason) {
      setExportError(reason);
    } finally {
      setExportBusy(false);
    }
  }
  return (
    <Modal title="问答与检索过程" onClose={onClose} drawer>
      <code>{traceId}</code>
      {!entry && error === undefined && <p role="status">正在读取本地记录…</p>}
      {error !== undefined && <ErrorPanel error={error} />}
      {entry && (
        <div className="stack history-detail">
          <div className="row-actions">
            <StatusBadge value={entry.status} />
            {entry.cache_hit && <span className="badge neutral">缓存命中</span>}
            <time dateTime={entry.created_at}>
              {historyTime(entry.created_at)}
            </time>
          </div>
          <div className="row-actions" aria-label="历史导出">
            <button
              type="button"
              disabled={exportBusy}
              onClick={() => void exportSupport(false)}
            >
              下载问答+Trace 支持包（仅元数据）
            </button>
            <button
              type="button"
              disabled={exportBusy}
              onClick={() => void exportSupport(true)}
            >
              包含敏感正文下载
            </button>
            <button
              type="button"
              disabled={exportBusy}
              onClick={() => void exportTechnicalTrace()}
            >
              仅下载技术 Trace JSON
            </button>
          </div>
          {exportError !== undefined && <ErrorPanel error={exportError} />}
          {entry.body_available ? (
            <>
              <h3>问题</h3>
              <p className="preserve-lines">{entry.question}</p>
              <h3>答案</h3>
              <p className="preserve-lines">
                {entry.answer || "本次没有发布答案。"}
              </p>
            </>
          ) : (
            <p role="status">{entry.body_message || "本次未保存正文"}</p>
          )}
          <dl className="detail-grid">
            <dt>结果原因</dt>
            <dd>{entry.reason_code ?? "请求尚未结束"}</dd>
            <dt>模型</dt>
            <dd>{entry.models?.join("、") || "未调用远程模型"}</dd>
            <dt>总耗时</dt>
            <dd>
              {entry.duration_ms == null
                ? "尚未结束"
                : `${entry.duration_ms.toFixed(1)} ms`}
            </dd>
            <dt>回答方式</dt>
            <dd>{entry.generation_mode ?? "—"}</dd>
            <dt>回答降级原因</dt>
            <dd>
              {entry.degraded_reason_codes?.join("、") ||
                entry.generation_reason_code ||
                "无"}
            </dd>
            <dt>所问类型 / 语义来源</dt>
            <dd>
              {entry.requested_answer_type ?? "旧记录未保存"} /{" "}
              {entry.query_semantic_source ?? "旧记录未保存"}
            </dd>
            {entry.data_plane && (
              <>
                <dt>实际检索数据面</dt>
                <dd>
                  {entry.data_plane.retrieval_data_plane ===
                  "default_local_fallback"
                    ? "本地确定性检索"
                    : "活动真实 Dense 检索"}
                </dd>
                <dt>Embedding / Reranker</dt>
                <dd>
                  {entry.data_plane.embedding_provider_id ?? "—"} /{" "}
                  {entry.data_plane.embedding_model ?? "—"} ·{" "}
                  {entry.data_plane.reranker_provider_id ?? "—"} /{" "}
                  {entry.data_plane.reranker_model ?? "—"}
                </dd>
                <dt>资料授权 / 预算</dt>
                <dd>
                  {entry.data_plane.corpus_authorization_state} /{" "}
                  {entry.data_plane.budget_state}
                </dd>
              </>
            )}
            {entry.error_stage && (
              <>
                <dt>失败阶段</dt>
                <dd>{entry.error_stage}</dd>
              </>
            )}
          </dl>
          <section aria-label="模型调用记录">
            <h3>模型调用</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>用途</th>
                    <th>实际调用</th>
                    <th>用量</th>
                    <th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  {entry.provider_usage?.map((usage) => (
                    <tr key={usage.operation}>
                      <td>{usage.operation}</td>
                      <td>{usage.call_count} 次</td>
                      <td>
                        {usage.call_count === 0
                          ? "未调用"
                          : (usage.usage ?? "unknown")}
                      </td>
                      <td>{usage.reason_code ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          {entry.body_available && !!entry.result?.evidence?.length && (
            <section aria-label="历史引用">
              <h3>引用与候选</h3>
              {entry.result.evidence.map((evidence) => (
                <article className="panel" key={evidence.evidence_id}>
                  <strong>{evidence.source_label}</strong>
                  <blockquote>{evidence.citation_text}</blockquote>
                  <OcrEvidenceSource
                    evidence={evidence}
                    projectId={entry.project_id}
                    kbId={entry.knowledge_base_id}
                  />
                  <small>{evidence.selection_reason}</small>
                  <button
                    disabled={sourceBusy || !entry.active_index_revision_id}
                    onClick={() => void openSource(evidence)}
                  >
                    核对当前原文
                  </button>
                </article>
              ))}
            </section>
          )}
          {entry.diagnostics && <DiagnosticsView value={entry.diagnostics} />}
          {new Set(["ANSWERED", "REFUSED"]).has(entry.status) && (
            <QueryFeedback
              key={entry.trace_id}
              projectId={entry.project_id}
              kbId={entry.knowledge_base_id}
              traceId={entry.trace_id}
            />
          )}
          <section aria-label="请求阶段">
            <h3>请求阶段与决定</h3>
            {!entry.events?.length && <p>本次未记录细分阶段。</p>}
            {entry.events?.map((event, index) => (
              <details key={`${event.event_name}:${index}`}>
                <summary>
                  {event.event_name} · {historyTime(event.occurred_at)}
                </summary>
                <pre>{JSON.stringify(event.attributes, null, 2)}</pre>
              </details>
            ))}
          </section>
        </div>
      )}
      {sourceError !== undefined && <ErrorPanel error={sourceError} />}
      {source && (
        <section className="panel" aria-label="重新鉴权的原文">
          <h3>当前可读取原文</h3>
          <blockquote>{source.citation_text}</blockquote>
          <p>{source.heading_path.join(" / ")}</p>
        </section>
      )}
    </Modal>
  );
}

export function DiagnosticsView({ value }: { value: RetrievalDiagnostics }) {
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
          <h4>融合与证据决定</h4>
          {value.fusion.map((item) => (
            <details key={item.chunk_id}>
              <summary>
                #{item.rank} · {item.chunk_id}
              </summary>
              <pre>{JSON.stringify(item.contributions, null, 2)}</pre>
            </details>
          ))}
          <details>
            <summary>入选与淘汰原因</summary>
            <pre>{JSON.stringify(value.evidence, null, 2)}</pre>
          </details>
        </div>
        <div>
          <h4>阶段耗时</h4>
          {value.stage_timings.map((item) => (
            <p key={item.stage}>
              {item.stage} <strong>{item.elapsed_ms.toFixed(2)} ms</strong>
            </p>
          ))}
        </div>
      </div>
    </div>
  );
}
