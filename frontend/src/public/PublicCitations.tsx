import type { PublicCitation } from "./publicSse";
import { FileDown } from "lucide-react";
import { useCallback, useRef, useState } from "react";

import { ReferenceDrawer } from "./ReferenceDrawer";
import { DocumentAttachments } from "./DocumentAttachments";
import type { PublicNaturalImageContext } from "./publicApi";

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
      : citation.source_kind === "weknora" && citation.native_knowledge_id
        ? `native:${citation.native_knowledge_id}`
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
  allowSourceDownload = true,
  citations,
  onDownloadReference,
  onReadReference,
  imageContext,
}: {
  allowSourceDownload?: boolean;
  citations: PublicCitation[];
  onDownloadReference?: (
    referenceId: string,
    documentName: string,
    original?: boolean,
  ) => Promise<void>;
  onReadReference?: (referenceId: string) => Promise<string>;
  imageContext?: PublicNaturalImageContext;
}) {
  const [downloadError, setDownloadError] = useState<{
    groupKey: string;
    message: string;
  }>();
  const [downloadingGroup, setDownloadingGroup] = useState<string>();
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const downloadInProgress = useRef(false);
  const [reading, setReading] = useState<{
    citation: PublicCitation;
    trigger: HTMLElement;
  }>();
  const closeReading = useCallback(() => setReading(undefined), []);
  const visible = citations.filter(
    (citation) =>
      typeof citation.document_name === "string" &&
      citation.document_name.trim(),
  );
  if (visible.length === 0) return null;
  const groups = groupCitations(visible);
  return (
    <>
      <details className="wst-citations">
        <summary>引用依据（{visible.length}段）</summary>
        <div className="wst-citation-list">
          {groups.map((group) => {
            const documentIds = [
              ...new Set(
                group.citations.flatMap((item) =>
                  item.source_kind === "weknora" &&
                  typeof item.native_knowledge_id === "string" &&
                  item.native_knowledge_id.trim()
                    ? [item.native_knowledge_id]
                    : [],
                ),
              ),
            ];
            const documentId =
              documentIds.length === 1 ? documentIds[0] : undefined;
            const original = group.citations.find(
              (citation) =>
                citation.reference_id &&
                (citation.source_kind !== "weknora" ||
                  citation.original_available === true),
            );
            return (
              <article key={group.key}>
                <details
                  className="wst-citation-group"
                  onToggle={(event) => {
                    const open = event.currentTarget.open;
                    setExpandedGroups((previous) => {
                      if (previous.has(group.key) === open) return previous;
                      const next = new Set(previous);
                      if (open) next.add(group.key);
                      else next.delete(group.key);
                      return next;
                    });
                  }}
                >
                  <summary className="wst-citation-heading">
                    <strong>
                      {onReadReference ? (
                        <button
                          aria-haspopup="dialog"
                          aria-label={`阅读来源：${group.documentName}`}
                          className="wst-citation-document-open"
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            setReading({
                              citation:
                                group.citations.find(
                                  (citation) =>
                                    citation.reference_id &&
                                    citation.source_available !== false,
                                ) ?? group.citations[0],
                              trigger: event.currentTarget,
                            });
                          }}
                          type="button"
                        >
                          {group.documentName}
                        </button>
                      ) : (
                        group.documentName
                      )}
                    </strong>
                    <span className="wst-citation-heading-actions">
                      <span className="wst-citation-count">
                        {group.citations.length}段
                      </span>
                      {allowSourceDownload &&
                        onDownloadReference &&
                        original && (
                          <button
                            aria-label={`下载原件：${group.documentName}`}
                            aria-busy={downloadingGroup === group.key}
                            className="wst-citation-download"
                            disabled={Boolean(downloadingGroup)}
                            onClick={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              if (downloadInProgress.current) return;
                              downloadInProgress.current = true;
                              setDownloadingGroup(group.key);
                              setDownloadError(undefined);
                              void onDownloadReference(
                                original.reference_id!,
                                original.document_name,
                                original.source_kind === "weknora"
                                  ? true
                                  : undefined,
                              )
                                .catch(() => {
                                  setDownloadError({
                                    groupKey: group.key,
                                    message: "原件暂不可用，请刷新页面后重试。",
                                  });
                                })
                                .finally(() => {
                                  downloadInProgress.current = false;
                                  setDownloadingGroup(undefined);
                                });
                            }}
                            type="button"
                          >
                            <FileDown size={15} aria-hidden="true" />
                            {downloadingGroup === group.key
                              ? "下载中…"
                              : "下载原件"}
                          </button>
                        )}
                    </span>
                    <span className="wst-citation-toggle">展开全部片段</span>
                  </summary>
                  <div className="wst-citation-group-body">
                    {expandedGroups.has(group.key) &&
                      typeof documentId === "string" && (
                        <DocumentAttachments documentId={documentId} />
                      )}
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
                          {citation.source_kind === "weknora" &&
                            citation.reference_id &&
                            citation.source_available &&
                            (onReadReference || onDownloadReference) && (
                              <button
                                onClick={(event) => {
                                  if (
                                    citation.source_kind === "weknora" &&
                                    onReadReference
                                  ) {
                                    setReading({
                                      citation,
                                      trigger: event.currentTarget,
                                    });
                                    return;
                                  }
                                  setDownloadError(undefined);
                                  void onDownloadReference?.(
                                    citation.reference_id!,
                                    citation.document_name,
                                  ).catch(() => {
                                    setDownloadError({
                                      groupKey: group.key,
                                      message: "引用内容暂不可用，请稍后重试。",
                                    });
                                  });
                                }}
                                type="button"
                              >
                                查看引用片段
                              </button>
                            )}
                        </li>
                      ))}
                    </ol>
                  </div>
                </details>
                {downloadError?.groupKey === group.key && (
                  <p className="wst-citation-download-error" role="alert">
                    {downloadError.message}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      </details>
      {reading && (
        <ReferenceDrawer
          citation={reading.citation}
          key={`${reading.citation.reference_id ?? reading.citation.document_name}:${reading.citation.locator ?? ""}`}
          onClose={closeReading}
          onReadReference={onReadReference}
          imageContext={imageContext}
          returnFocusTo={reading.trigger}
        />
      )}
    </>
  );
}
