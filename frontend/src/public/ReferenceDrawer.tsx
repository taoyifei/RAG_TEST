import { X } from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { ReadingOverlay } from "./ReadingOverlay";
import type { PublicCitation } from "./publicSse";

const COMPACT_READING = "(max-width: 1100px)";

function subscribeViewport(onChange: () => void) {
  const media = window.matchMedia?.(COMPACT_READING);
  media?.addEventListener("change", onChange);
  return () => media?.removeEventListener("change", onChange);
}

function compactSnapshot() {
  return window.matchMedia?.(COMPACT_READING).matches ?? false;
}

type SourceResult =
  | { status: "loading" }
  | { status: "ready"; text: string }
  | { status: "error" };

function ReferenceText({
  referenceId,
  reader,
  onRetry,
}: {
  referenceId: string;
  reader: (referenceId: string) => Promise<string>;
  onRetry: () => void;
}) {
  const [result, setResult] = useState<SourceResult>({ status: "loading" });
  const readerRef = useRef(reader);
  useLayoutEffect(() => {
    readerRef.current = reader;
  }, [reader]);
  useEffect(() => {
    let current = true;
    void Promise.resolve()
      .then(() => readerRef.current(referenceId))
      .then((text) => {
        if (current) setResult({ status: "ready", text });
      })
      .catch(() => {
        if (current) setResult({ status: "error" });
      });
    return () => {
      current = false;
    };
  }, [referenceId]);

  if (result.status === "loading") {
    return <p role="status">正在读取引用原文…</p>;
  }
  if (result.status === "error") {
    return (
      <div className="wst-reading-error" role="alert">
        <p>原文暂时读不到，资料可能已更新或访问权限已变化。</p>
        <button onClick={onRetry} type="button">
          重新读取
        </button>
      </div>
    );
  }
  return result.text.trim() ? (
    <div className="wst-reference-text">{result.text}</div>
  ) : (
    <p className="wst-reading-muted">
      这条引用暂未提供更多原文，可以先参考上方片段。
    </p>
  );
}

/** 只展示本次引用的实际定位与受保护来源，不把片段冒充整份文档。 */
export function ReferenceDrawer({
  citation,
  onClose,
  onReadReference,
  returnFocusTo,
}: {
  citation: PublicCitation;
  onClose: () => void;
  onReadReference?: (referenceId: string) => Promise<string>;
  returnFocusTo?: HTMLElement | null;
}) {
  const compact = useSyncExternalStore(
    subscribeViewport,
    compactSnapshot,
    () => false,
  );
  const [attempt, setAttempt] = useState(0);
  const title = citation.document_title?.trim() || citation.document_name;
  const readable =
    citation.source_available !== false &&
    citation.reference_id &&
    onReadReference;
  return (
    <ReadingOverlay
      kind="reference"
      label={`来源阅读：${title}`}
      modal={compact}
      onClose={onClose}
      returnFocusTo={returnFocusTo}
    >
      <header className="wst-reading-header">
        <div>
          <p className="wst-reading-eyebrow">来源阅读</p>
          <h2>{title}</h2>
        </div>
        <button
          aria-label="关闭来源阅读"
          className="wst-reading-close"
          onClick={onClose}
          type="button"
        >
          <X aria-hidden="true" size={20} />
        </button>
      </header>
      <div className="wst-reading-body">
        {citation.locator && (
          <p className="wst-reading-location">{citation.locator}</p>
        )}
        {citation.quote && (
          <section aria-label="本次引用片段" className="wst-reference-excerpt">
            <h3>本次引用片段</h3>
            <blockquote>{citation.quote}</blockquote>
          </section>
        )}
        <section aria-label="引用原文" className="wst-reference-source">
          <h3>引用原文</h3>
          {readable && citation.reference_id && onReadReference ? (
            <ReferenceText
              key={`${citation.reference_id}:${attempt}`}
              onRetry={() => setAttempt((value) => value + 1)}
              reader={onReadReference}
              referenceId={citation.reference_id}
            />
          ) : (
            <p className="wst-reading-muted">
              这条引用没有可读取的原文，可以先参考本次片段。
            </p>
          )}
        </section>
      </div>
    </ReadingOverlay>
  );
}
