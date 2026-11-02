import { X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { ReadingOverlay } from "./ReadingOverlay";
import { ImagePreview } from "./ImagePreview";
import { SourceText, sourceDisplayText } from "./SourceText";
import type { SourceImagePreview } from "./SourceText";
import type { PublicNaturalImageContext } from "./publicApi";
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
  | { status: "error" }
  | { status: "unavailable" };

function ReferenceContent({
  quote,
  result,
  onRetry,
  imageContext,
  onPreviewImage,
}: {
  quote?: string;
  result: SourceResult;
  onRetry?: () => void;
  imageContext?: PublicNaturalImageContext;
  onPreviewImage: (image: SourceImagePreview) => void;
}) {
  const sameText =
    result.status === "ready" &&
    Boolean(quote?.trim()) &&
    quote?.trim() === result.text.trim();
  return (
    <>
      {quote?.trim() && !sameText && (
        <section aria-label="本次引用片段" className="wst-reference-excerpt">
          <h3>本次引用片段</h3>
          <blockquote>
            <SourceText
              text={quote}
              imageContext={imageContext}
              onPreviewImage={onPreviewImage}
            />
          </blockquote>
        </section>
      )}
      <section aria-label="引用原文" className="wst-reference-source">
        <h3>引用原文</h3>
        {result.status === "loading" && <p role="status">正在读取引用原文…</p>}
        {result.status === "error" && (
          <div className="wst-reading-error" role="alert">
            <p>原文暂时读不到，资料可能已更新或访问权限已变化。</p>
            <button onClick={onRetry} type="button">
              重新读取
            </button>
          </div>
        )}
        {result.status === "ready" &&
          (result.text.trim() ? (
            <SourceText
              text={result.text}
              imageContext={imageContext}
              onPreviewImage={onPreviewImage}
            />
          ) : (
            <p className="wst-reading-muted">
              这条引用暂未提供更多原文，可以先参考上方片段。
            </p>
          ))}
        {result.status === "unavailable" && (
          <p className="wst-reading-muted">
            这条引用没有可读取的原文，可以先参考本次片段。
          </p>
        )}
      </section>
    </>
  );
}

function ReferenceText({
  referenceId,
  reader,
  onRetry,
  quote,
  imageContext,
  onPreviewImage,
}: {
  referenceId: string;
  reader: (referenceId: string) => Promise<string>;
  onRetry: () => void;
  quote?: string;
  imageContext?: PublicNaturalImageContext;
  onPreviewImage: (image: SourceImagePreview) => void;
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

  return (
    <ReferenceContent
      quote={quote}
      result={result}
      onRetry={onRetry}
      imageContext={imageContext}
      onPreviewImage={onPreviewImage}
    />
  );
}

/** 只展示本次引用的实际定位与受保护来源，不把片段冒充整份文档。 */
export function ReferenceDrawer({
  citation,
  onClose,
  onReadReference,
  returnFocusTo,
  imageContext,
}: {
  citation: PublicCitation;
  onClose: () => void;
  onReadReference?: (referenceId: string) => Promise<string>;
  returnFocusTo?: HTMLElement | null;
  imageContext?: PublicNaturalImageContext;
}) {
  const compact = useSyncExternalStore(
    subscribeViewport,
    compactSnapshot,
    () => false,
  );
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState<SourceImagePreview>();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const returnToSource = useCallback(() => {
    const close = closeButtonRef.current;
    const panel = close?.closest(".wst-reference-panel");
    // 原文完成加载或缩略图读取失败时，原按钮可能已被替换。
    const matching = panel?.querySelectorAll<HTMLButtonElement>(
      ".wst-source-image-button",
    );
    return (
      [...(matching ?? [])].find(
        (button) =>
          button.querySelector("img")?.getAttribute("src") === preview?.src,
      ) ?? close
    );
  }, [preview?.src]);
  const title = sourceDisplayText(
    citation.document_title?.trim() || citation.document_name,
  );
  const readable =
    citation.source_available !== false &&
    citation.reference_id &&
    onReadReference;
  return (
    <>
      <ReadingOverlay
        kind="reference"
        label={`来源阅读：${title}`}
        modal={compact}
        onClose={onClose}
        returnFocusTo={returnFocusTo}
        suspended={Boolean(preview)}
      >
        <header className="wst-reading-header">
          <div>
            <p className="wst-reading-eyebrow">来源阅读</p>
            <h2>{title}</h2>
          </div>
          <button
            aria-label="关闭来源阅读"
            className="wst-reading-close"
            ref={closeButtonRef}
            onClick={onClose}
            type="button"
          >
            <X aria-hidden="true" size={20} />
          </button>
        </header>
        <div className="wst-reading-body">
          {citation.locator && (
            <p className="wst-reading-location">
              {sourceDisplayText(citation.locator)}
            </p>
          )}
          {readable && citation.reference_id && onReadReference ? (
            <ReferenceText
              key={`${citation.reference_id}:${attempt}`}
              onRetry={() => setAttempt((value) => value + 1)}
              reader={onReadReference}
              referenceId={citation.reference_id}
              quote={citation.quote}
              imageContext={imageContext}
              onPreviewImage={setPreview}
            />
          ) : (
            <ReferenceContent
              quote={citation.quote}
              result={{ status: "unavailable" }}
              imageContext={imageContext}
              onPreviewImage={setPreview}
            />
          )}
        </div>
      </ReadingOverlay>
      {preview && (
        <ImagePreview
          src={preview.src}
          alt={preview.alt}
          onClose={() => setPreview(undefined)}
          returnFocusTo={preview.trigger}
          returnFocusFallback={returnToSource}
        />
      )}
    </>
  );
}
