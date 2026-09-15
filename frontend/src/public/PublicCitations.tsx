import type { PublicCitation } from "./publicSse";

function categoryLabel(value: PublicCitation["category_path"]): string {
  if (Array.isArray(value)) return value.join(" / ");
  return value ?? "";
}

export function PublicCitations({
  citations,
}: {
  citations: PublicCitation[];
}) {
  const visible = citations.filter(
    (citation) =>
      typeof citation.document_name === "string" && citation.document_name,
  );
  if (visible.length === 0) return null;
  return (
    <details className="wst-citations">
      <summary>已核验来源（{visible.length}）</summary>
      <div className="wst-citation-list">
        {visible.map((citation, index) => {
          const category = categoryLabel(citation.category_path);
          return (
            <article key={`${citation.document_name}-${index}`}>
              <div className="wst-citation-heading">
                <strong>{citation.document_name}</strong>
                <span>已核验来源</span>
              </div>
              {(citation.department || category) && (
                <p className="wst-citation-meta">
                  {[citation.department, category].filter(Boolean).join(" · ")}
                </p>
              )}
              {citation.locator && (
                <p className="wst-citation-locator">{citation.locator}</p>
              )}
              {citation.quote && <blockquote>{citation.quote}</blockquote>}
            </article>
          );
        })}
      </div>
    </details>
  );
}
