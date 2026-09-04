import { AlertCircle, CheckCircle2, X, XCircle } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

import { ApiError, type Evidence } from "../api/client";
import { localizeStatus } from "../copy/zh-CN";

function trapFocus(container: HTMLElement, event: KeyboardEvent) {
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    container.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  const first = focusable.at(0);
  const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function StatusBadge({ value }: { value: string | boolean }) {
  const raw = String(value);
  const text = localizeStatus(value);
  const good = [
    "active",
    "succeeded",
    "ANSWERABLE",
    "healthy",
    "就绪",
  ].includes(raw);
  const bad = [
    "failed",
    "failed_terminal",
    "INDEX_CORRUPT",
    "unhealthy",
  ].includes(raw);
  const Icon = good ? CheckCircle2 : bad ? XCircle : AlertCircle;
  return (
    <span className={`badge ${good ? "good" : bad ? "bad" : "neutral"}`}>
      <Icon aria-hidden="true" size={14} /> {text}
    </span>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}

export function ErrorPanel({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : "发生未知错误";
  const details = error instanceof ApiError ? error : null;
  return (
    <div className="error-panel" role="alert">
      <AlertCircle aria-hidden="true" size={18} />
      <div>
        <strong>请求未完成{details ? ` · ${details.code}` : ""}</strong>
        <p>{message}</p>
        {details && (
          <small>
            HTTP {details.status} · {details.stage ?? "unknown_stage"} ·
            {details.retryable ? " 可安全重试" : " 不自动重试"}
          </small>
        )}
      </div>
    </div>
  );
}

export function EvidenceDrawer({
  evidence,
  onClose,
}: {
  evidence: Evidence | null;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!evidence) return;
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (drawerRef.current) trapFocus(drawerRef.current, event);
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [evidence, onClose]);
  if (!evidence) return null;
  return (
    <div
      className="drawer-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        ref={drawerRef}
        aria-label="证据详情"
        aria-modal="true"
        className="drawer"
        role="dialog"
      >
        <header>
          <div>
            <span className="eyebrow">引用依据</span>
            <h2>{evidence.source_label}</h2>
          </div>
          <button
            ref={closeRef}
            className="icon-button"
            onClick={onClose}
            aria-label="关闭证据详情"
          >
            <X aria-hidden="true" size={20} />
          </button>
        </header>
        <blockquote>{evidence.citation_text}</blockquote>
        <dl className="detail-grid">
          <dt>Evidence ID</dt>
          <dd>{evidence.evidence_id}</dd>
          <dt>Chunk ID</dt>
          <dd>{evidence.chunk_id}</dd>
          <dt>Document</dt>
          <dd>{evidence.display_name ?? evidence.document_id ?? "—"}</dd>
          <dt>Version</dt>
          <dd>{evidence.document_version_id ?? "—"}</dd>
          <dt>Section</dt>
          <dd>{evidence.section_id ?? "—"}</dd>
          <dt>融合 / 重排排名</dt>
          <dd>
            {evidence.fusion_rank ?? "—"} / {evidence.rerank_rank ?? "—"}
          </dd>
          <dt>入选原因</dt>
          <dd>{evidence.selection_reason}</dd>
          <dt>发布状态</dt>
          <dd>{evidence.publishable ? "可发布" : "不可发布"}</dd>
          <dt>表格上下文</dt>
          <dd>
            {evidence.table_context ? (evidence.table_locator ?? "是") : "否"}
          </dd>
          <dt>检索来源</dt>
          <dd>{evidence.retrieval_origins.join("、") || "—"}</dd>
        </dl>
        <h3>精确来源范围</h3>
        <pre>{JSON.stringify(evidence.source_spans, null, 2)}</pre>
      </aside>
    </div>
  );
}
