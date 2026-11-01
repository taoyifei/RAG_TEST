import type { Nodes, Root } from "mdast";
import { useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  publicNaturalImageUrl,
  type PublicNaturalImageContext,
} from "./publicApi";

export interface SourceImagePreview {
  src: string;
  alt: string;
  trigger: HTMLElement;
}

const IMAGE_HINT = "图片请在原回答或原件中查看";
const RESOURCE_HANDLE =
  /(?:resource|privatefile|file|blob|data):\/\/[^\s<>\])（），。；、]+/gi;

/** 来源字符串只用于阅读，不将内部资源句柄作为内容或链接暴露。 */
export function sourceDisplayText(value: string): string {
  return value.replace(RESOURCE_HANDLE, IMAGE_HINT);
}

function sourceLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (!["http:", "https:", "mailto:"].includes(url.protocol))
      return undefined;
    if (/\/(?:kb\/)?(?:api|files|uploads|storage)\//i.test(url.pathname))
      return undefined;
    if (
      [...url.searchParams.keys()].some((key) =>
        /token|secret|credential|signature|file_path|trace_id|reference_id/i.test(
          key,
        ),
      )
    )
      return undefined;
    if (sourceDisplayText(value) !== value) return undefined;
    return value;
  } catch {
    return undefined;
  }
}

function readableResources() {
  return (tree: Root) => {
    function visit(node: Nodes): void {
      if (
        node.type === "text" ||
        node.type === "code" ||
        node.type === "inlineCode"
      ) {
        node.value = sourceDisplayText(node.value);
        if (node.type === "text") {
          // 分块边界残留的未闭合加粗标记不作为正文显示，代码示例保留其原有符号。
          node.value = node.value.replace(/^\*\*|\*\*$/g, "");
        }
      }
      if ("children" in node) node.children.forEach(visit);
    }
    visit(tree);
  };
}

const TEXT_COMPONENTS: Components = {
  a: ({ children, href, node }) =>
    href && !containsImage(node) ? (
      <a href={href} rel="noopener noreferrer" target="_blank">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  /* eslint-disable jsx-a11y/no-noninteractive-tabindex -- 横向滚动的命名区域需要键盘聚焦，才能用方向键读完整表格。 */
  table: ({ children }) => (
    <div
      className="wst-source-table-scroll"
      role="region"
      aria-label="原文表格"
      tabIndex={0}
    >
      <table>{children}</table>
    </div>
  ),
  /* eslint-enable jsx-a11y/no-noninteractive-tabindex */
};

interface ImageNode {
  type: string;
  tagName?: string;
  children?: readonly ImageNode[];
}

function containsImage(node: ImageNode | undefined): boolean {
  return Boolean(
    node && (node.tagName === "img" || node.children?.some(containsImage)),
  );
}

function SourceImage({
  src,
  alt,
  onPreview,
}: {
  src: string;
  alt: string;
  onPreview: (image: SourceImagePreview) => void;
}) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  return (
    <span className="wst-source-image">
      {failed ? (
        <span className="wst-source-image-error" role="status">
          <span>{alt}暂时无法显示。</span>
          <button
            type="button"
            onClick={() => {
              setFailed(false);
              setAttempt((value) => value + 1);
            }}
          >
            重新加载图片
          </button>
        </span>
      ) : (
        <>
          <button
            aria-label={`放大图片：${alt}`}
            className="wst-source-image-button"
            type="button"
            title="点击放大图片"
            onClick={(event) =>
              onPreview({ src, alt, trigger: event.currentTarget })
            }
          >
            <img
              key={attempt}
              src={src}
              alt={alt}
              loading="lazy"
              referrerPolicy="no-referrer"
              onError={() => setFailed(true)}
            />
          </button>
          <span className="wst-source-image-hint">点击图片可放大</span>
        </>
      )}
    </span>
  );
}

/** 格式化引用图文；配图仅使用当前消息的受保护资源，不直连外部图片。 */
export function SourceText({
  text,
  imageContext,
  onPreviewImage,
}: {
  text: string;
  imageContext?: PublicNaturalImageContext;
  onPreviewImage?: (image: SourceImagePreview) => void;
}) {
  const conversationId = imageContext?.conversationId;
  const traceId = imageContext?.traceId;
  const components = useMemo<Components>(
    () => ({
      ...TEXT_COMPONENTS,
      img: ({ alt, src }) => {
        if (
          !src?.startsWith("resource://") ||
          !conversationId ||
          !traceId ||
          !onPreviewImage
        )
          return (
            <span className="wst-source-image-hint">
              {alt ? `${sourceDisplayText(alt)}：` : ""}
              {IMAGE_HINT}
            </span>
          );
        const url = publicNaturalImageUrl({ conversationId, traceId }, src);
        return (
          <SourceImage
            key={url}
            src={url}
            alt={sourceDisplayText(alt || "引用中的图片")}
            onPreview={onPreviewImage}
          />
        );
      },
    }),
    [conversationId, traceId, onPreviewImage],
  );
  return (
    <div className="wst-source-text">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm, readableResources]}
        components={components}
        urlTransform={(url, key, node) =>
          key === "src"
            ? node.tagName === "img" &&
              url.startsWith("resource://") &&
              conversationId &&
              traceId &&
              onPreviewImage
              ? url
              : ""
            : (sourceLink(url) ?? "")
        }
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
