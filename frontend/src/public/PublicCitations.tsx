import type { PublicCitation } from "./publicSse";
import { useState } from "react";

interface CitationGroup {
  key: string;
  documentName: string;
  department?: string;
  category?: string;
  sourceRelativePath?: string;
  citations: PublicCitation[];
}

function categoryLabel(value: PublicCitation["category_path"]): string {
  if (Array.isArray(value)) return value.join(" / ");
  return value ?? "";
}

function groupCitations(citations: PublicCitation[]): CitationGroup[] {
  const groups = new Map<string, CitationGroup>();
  citations.forEach((citation, index) => {
    const documentName =
      citation.document_title?.trim() || citation.document_name.trim();
    const department =
      citation.department_name?.trim() || citation.department?.trim();
    const category = categoryLabel(citation.category_path).trim() || undefined;
    const sourceRelativePath =
      citation.source_relative_path?.trim() || undefined;
    const key = sourceRelativePath
      ? `path:${JSON.stringify([
          sourceRelativePath,
          documentName,
          department ?? "",
          category ?? "",
        ])}`
      : `unpathed:${index}`;
    const existing = groups.get(key);
    if (existing) {
      existing.citations.push(citation);
      return;
    }
    groups.set(key, {
      key,
      documentName,
      department,
      category,
      sourceRelativePath,
      citations: [citation],
    });
  });
  return Array.from(groups.values());
}

export function PublicCitations({
  citations,
  onDownloadReference,
}: {
  citations: PublicCitation[];
  onDownloadReference?: (referenceId: string, documentName: string) => Promise<void>;
}) {
  const [downloadError, setDownloadError] = useState<string>();
  const visible = citations.filter(
    (citation) =>
      typeof citation.document_name === "string" &&
      citation.document_name.trim(),
  );
  if (visible.length === 0) return null;
  const groups = groupCitations(visible);
  return (
    <details className="wst-citations">
      <summary>引用依据（{visible.length}段）</summary>
      <div className="wst-citation-list">
        {groups.map((group) => (
          <article key={group.key}>
            <details className="wst-citation-group">
              <summary className="wst-citation-heading">
                <strong>{group.documentName}</strong>
                <span className="wst-citation-count">
                  {group.citations.length}段
                </span>
                <span className="wst-citation-toggle">展开全部片段</span>
              </summary>
              <div className="wst-citation-group-body">
                {(group.department || group.category) && (
                  <p className="wst-citation-meta">
                    {[group.department, group.category]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                )}
                {group.sourceRelativePath && (
                  <>
                    <p className="wst-citation-locator">
                      {group.sourceRelativePath}
                    </p>
                    <p className="wst-citation-group-note">
                      按本次返回路径整理
                    </p>
                  </>
                )}
                <ol className="wst-citation-segments">
                  {group.citations.map((citation, index) => (
                    <li
                      className="wst-citation-segment"
                      key={`${group.key}-segment-${index}`}
                    >
                      <p className="wst-citation-segment-label">
                        片段 {index + 1}
                      </p>
                      {citation.locator && (
                        <p className="wst-citation-locator">
                          {citation.locator}
                        </p>
                      )}
                      {citation.quote && (
                        <blockquote>{citation.quote}</blockquote>
                      )}
                      {citation.reference_id && onDownloadReference && (
                        <button
                          onClick={() => {
                            setDownloadError(undefined);
                            void onDownloadReference(
                              citation.reference_id!,
                              citation.document_name,
                            ).catch(() => {
                              setDownloadError("原件暂不可用，请稍后重试。");
                            });
                          }}
                          type="button"
                        >
                          下载本次引用的原件
                        </button>
                      )}
                    </li>
                  ))}
                </ol>
                {downloadError && <p role="alert">{downloadError}</p>}
              </div>
            </details>
          </article>
        ))}
      </div>
    </details>
  );
}
