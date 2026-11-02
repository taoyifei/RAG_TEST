import { X } from "lucide-react";
import { useState } from "react";

import { ReadingOverlay } from "./ReadingOverlay";

/** 复用答案中已存在的图片地址，不另行获取图片或代理外部资源。 */
export function ImagePreview({
  src,
  alt,
  onClose,
  returnFocusTo,
  returnFocusFallback,
}: {
  src: string;
  alt: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  returnFocusFallback?: () => HTMLElement | null;
}) {
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  return (
    <ReadingOverlay
      kind="image"
      label="图片预览"
      modal
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      returnFocusFallback={returnFocusFallback}
    >
      <header className="wst-reading-header">
        <div>
          <p className="wst-reading-eyebrow">图片预览</p>
          <h2>{alt || "回答中的图片"}</h2>
        </div>
        <button
          aria-label="关闭图片预览"
          className="wst-reading-close"
          onClick={onClose}
          type="button"
        >
          <X aria-hidden="true" size={20} />
        </button>
      </header>
      <div className="wst-image-canvas">
        {status === "loading" && <p role="status">正在显示图片…</p>}
        {status === "error" && (
          <div className="wst-reading-error" role="alert">
            <p>
              图片暂时无法显示，可以重新加载；若资料或权限已变化，请刷新页面。
            </p>
            <button
              onClick={() => {
                setStatus("loading");
                setAttempt((value) => value + 1);
              }}
              type="button"
            >
              重新加载图片
            </button>
          </div>
        )}
        <img
          alt={alt}
          className={status === "error" ? "is-unavailable" : undefined}
          key={`${src}:${attempt}`}
          onError={() => setStatus("error")}
          onLoad={() => setStatus("ready")}
          referrerPolicy="no-referrer"
          src={src}
        />
      </div>
    </ReadingOverlay>
  );
}
