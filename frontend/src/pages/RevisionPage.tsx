import { useEffect, useState } from "react";
import { api, type ChunkPage, type RevisionInspection } from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function RevisionPage() {
  const { tokens, scope } = useConsole();
  const [revision, setRevision] = useState<RevisionInspection>();
  const [chunks, setChunks] = useState<ChunkPage>();
  const [reports, setReports] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    if (!scope.revisionId) return;
    Promise.all([
      api.inspectRevision(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
      api.listChunks(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
      api.revisionReports(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        scope.revisionId,
      ),
    ])
      .then(([r, c, p]) => {
        setRevision(r);
        setChunks(c);
        setReports(p.items);
      })
      .catch(setError);
  }, [scope, tokens.admin]);
  if (!scope.revisionId)
    return (
      <EmptyState title="尚未选择索引版本">
        完成一次上传或从任务列表选择版本。
      </EmptyState>
    );
  if (error) return <ErrorPanel error={error} />;
  if (!revision || !chunks) return <p className="loading">正在读取版本事实…</p>;
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>索引版本</h2>
          <code>{revision.revision_id}</code>
        </div>
        <StatusBadge value={revision.active ? "active" : "inactive"} />
      </div>
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
