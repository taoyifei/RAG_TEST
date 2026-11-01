import type { Nodes, Root } from "mdast";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

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

const COMPONENTS: Components = {
  a: ({ children, href }) =>
    href ? (
      <a href={href} rel="noopener noreferrer" target="_blank">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  img: ({ alt }) => (
    <span className="wst-source-image-hint">
      {alt ? `${sourceDisplayText(alt)}：` : ""}
      {IMAGE_HINT}
    </span>
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

/** 格式化已获授权的引用文本；禁止HTML、图片请求和内部资源链接。 */
export function SourceText({ text }: { text: string }) {
  return (
    <div className="wst-source-text">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm, readableResources]}
        components={COMPONENTS}
        urlTransform={(url, key) =>
          key === "src" ? "" : (sourceLink(url) ?? "")
        }
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
