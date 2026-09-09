import { Download } from "lucide-react";
import { useState, type ReactNode } from "react";

import { api } from "../api/client";
import { downloadFile } from "../utils/download";
import { ErrorPanel, Modal } from "./ui";

export function HistorySupportDownload({
  traceIds,
  children,
  disabled = false,
}: {
  traceIds: string[];
  children: ReactNode;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [includeBody, setIncludeBody] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  async function download() {
    if (!traceIds.length) return;
    setBusy(true);
    setError(undefined);
    try {
      const file =
        traceIds.length === 1
          ? await api.exportHistoryTrace(traceIds[0], includeBody)
          : await api.exportHistoryTraces(traceIds, includeBody);
      downloadFile(file);
      setOpen(false);
      setIncludeBody(false);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={disabled || !traceIds.length || busy}
        onClick={() => {
          setError(undefined);
          setOpen(true);
        }}
      >
        <Download aria-hidden="true" size={16} />
        {children}
      </button>
      {open && (
        <Modal
          title="下载问答与技术 Trace 支持包"
          onClose={() => !busy && setOpen(false)}
        >
          <p>
            将导出 {traceIds.length} 条记录。默认只含安全元数据；正文不可用时，
            支持包仍会生成并在清单中写明原因。
          </p>
          <label className="sensitive-export-choice">
            <input
              type="checkbox"
              checked={includeBody}
              disabled={busy}
              onChange={(event) => setIncludeBody(event.target.checked)}
            />
            包含当前仍获授权的敏感问题、答案与引用正文
          </label>
          {includeBody && (
            <p role="alert">
              该文件可能含敏感问答和引用。请只保存到受控位置，并确认接收者有权查看。
            </p>
          )}
          {error !== undefined && <ErrorPanel error={error} />}
          <div className="row-actions">
            <button
              className="primary"
              type="button"
              disabled={busy}
              onClick={() => void download()}
            >
              {busy ? "正在生成…" : "确认下载支持包"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              取消
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
