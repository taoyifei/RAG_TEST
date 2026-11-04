import { Download, FileText } from "lucide-react";
import { useEffect, useState } from "react";

import {
  downloadDocumentAsset,
  getDocumentDetails,
  type DocumentAsset,
  type DocumentDetails,
} from "./libraryApi";
import "./documentWorkflows.css";

export const DOCUMENT_KIND_NAMES = {
  policy: "制度说明",
  illustration: "操作图示",
  template: "可填写模板",
};

export function DocumentAssetButtons({
  documentId,
  assets,
}: {
  documentId: string;
  assets: DocumentAsset[];
}) {
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState<string>();
  if (!assets.length) return null;
  return (
    <div className="wst-document-assets">
      {assets.map((asset) => (
        <button
          type="button"
          key={asset.id}
          disabled={!!busy}
          title={asset.filename}
          onClick={() => {
            setBusy(asset.id);
            setError(undefined);
            void downloadDocumentAsset(documentId, asset)
              .catch((failure: unknown) =>
                setError(
                  failure instanceof Error
                    ? failure.message
                    : "附件下载失败，请重试。",
                ),
              )
              .finally(() => setBusy(undefined));
          }}
        >
          {asset.relation === "full_guide" ? (
            <FileText size={14} aria-hidden="true" />
          ) : (
            <Download size={14} aria-hidden="true" />
          )}
          {asset.relation === "full_guide"
            ? "查看完整指引"
            : asset.relation === "source_workbook"
              ? "原始Excel附件汇总"
              : "下载可填写附件"}
          {assets.filter((item) => item.relation === asset.relation).length >
            1 && <span> · {asset.filename.replace(/\.[^.]+$/, "")}</span>}
        </button>
      ))}
      {assets.some((asset) => asset.relation === "template") && (
        <small>附件可能含填写示例，使用时请替换为实际信息。</small>
      )}
      {error && (
        <p role="alert" className="wst-document-error">
          {error}
        </p>
      )}
    </div>
  );
}

export function DocumentAttachments({ documentId }: { documentId: string }) {
  const [result, setResult] = useState<{
    id: string;
    data?: DocumentDetails;
    error?: string;
  }>();
  useEffect(() => {
    const controller = new AbortController();
    void getDocumentDetails(documentId, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setResult({ id: documentId, data });
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setResult({
            id: documentId,
            error:
              failure instanceof Error ? failure.message : "附件信息暂不可用。",
          });
      });
    return () => controller.abort();
  }, [documentId]);
  if (result?.id !== documentId) return null;
  const { data, error } = result;
  if (error) return <p className="wst-document-meta">{error}</p>;
  if (!data) return null;
  return (
    <div className="wst-document-related">
      <span className="wst-document-kind">
        {DOCUMENT_KIND_NAMES[data.kind]}
      </span>
      {data.version_state === "archived" && (
        <span className="wst-document-meta">
          此引用来自旧版本，请以当前发布的资料为准。
        </span>
      )}
      <DocumentAssetButtons documentId={documentId} assets={data.assets} />
    </div>
  );
}
