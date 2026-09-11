import { useEffect, useState } from "react";
import {
  ApiError,
  api,
  type ChunkPage,
  type RevisionInspection,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function RevisionPage({ go }: { go?: (path: string) => void }) {
  const { tokens, scope, setRevision: setScopeRevision } = useConsole();
  const [revision, setRevision] = useState<RevisionInspection>();
  const [chunks, setChunks] = useState<ChunkPage>();
  const [reports, setReports] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState<{
    revisionId: string;
    reason: unknown;
  }>();
  const [unavailableRevisionId, setUnavailableRevisionId] = useState<string>();
  useEffect(() => {
    if (!scope.revisionId) return;
    let active = true;
    const requestedRevisionId = scope.revisionId;
    void api
      .inspectRevision(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        requestedRevisionId,
      )
      .then(async (r) => {
        const [c, p] = await Promise.all([
          api.listChunks(
            tokens.admin,
            scope.projectId,
            scope.kbId,
            requestedRevisionId,
          ),
          api.revisionReports(
            tokens.admin,
            scope.projectId,
            scope.kbId,
            requestedRevisionId,
          ),
        ]);
        if (!active) return;
        setRevision(r);
        setChunks(c);
        setReports(p.items);
        setError(undefined);
        setUnavailableRevisionId(undefined);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        if (reason instanceof ApiError && reason.status === 404) {
          setScopeRevision("");
          setUnavailableRevisionId(requestedRevisionId);
          return;
        }
        setError({ revisionId: requestedRevisionId, reason });
      });
    return () => {
      active = false;
    };
  }, [scope, setScopeRevision, tokens.admin]);
  if (
    unavailableRevisionId &&
    (!scope.revisionId || unavailableRevisionId === scope.revisionId)
  )
    return (
      <EmptyState title="索引版本尚不可用">
        {unavailableRevisionId}{" "}
        尚未生成或已不存在。请从成功任务重新选择可读版本。{" "}
        {go && <button onClick={() => go("/jobs")}>返回任务列表</button>}
      </EmptyState>
    );
  if (!scope.revisionId)
    return (
      <EmptyState title="尚未选择索引版本">
        任务成功后，请从任务列表选择可用的索引版本。
      </EmptyState>
    );
  if (error?.revisionId === scope.revisionId)
    return <ErrorPanel error={error.reason} />;
  if (!revision || revision.revision_id !== scope.revisionId || !chunks)
    return <p className="loading">正在读取版本事实…</p>;
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>索引版本</h2>
          <code>{revision.revision_id}</code>
        </div>
        <StatusBadge value={revision.active ? "active" : "inactive"} />
      </div>
      {go && (
        <button
          className="secondary"
          onClick={() => {
            const url = new URL(window.location.href);
            url.searchParams.set("revision_id", revision.revision_id);
            url.searchParams.delete("trace_id");
            url.searchParams.delete("job_id");
            url.searchParams.delete("document_id");
            window.history.replaceState({}, "", url.pathname + url.search);
            go("/operational-traces");
          }}
        >
          打开技术 Trace
        </button>
      )}
      <div className="metric-grid">
        <article>
          <span>文档</span>
          <strong>
            {revision.actual_document_count} /{" "}
            {revision.expected_document_count}
          </strong>
        </article>
        <article>
          <span>内容块 / 关键词索引</span>
          <strong>
            {revision.actual_chunk_count} / {revision.fts_count}
          </strong>
        </article>
        <article>
          <span>写入状态</span>
          <strong>{revision.writer_status}</strong>
        </article>
      </div>
      <div className="panel">
        <h3>向量槽覆盖</h3>
        <pre>{JSON.stringify(revision.slot_coverages, null, 2)}</pre>
      </div>
      <div className="panel">
        <h3>标准内容块</h3>
        {chunks.items.map((chunk) => (
          <details key={chunk.chunk_id}>
            <summary>
              {chunk.role} · {chunk.section_id} · {chunk.chunk_id}
            </summary>
            <h4>引用文本</h4>
            <p>{chunk.citation_text}</p>
            <h4>向量文本</h4>
            <p>{chunk.embedding_text}</p>
            <h4>关键词文本</h4>
            <p>{chunk.lexical_text}</p>
            <pre>{JSON.stringify(chunk.source_spans, null, 2)}</pre>
          </details>
        ))}
      </div>
      <div className="panel">
        <h3>解析与分块报告</h3>
        <pre>{JSON.stringify(reports, null, 2)}</pre>
      </div>
    </section>
  );
}
