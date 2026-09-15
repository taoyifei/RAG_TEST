import { FolderUp, RefreshCw, Upload } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";

import { createIdempotencyKey, type Job } from "../../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../../components/ui";
import { useJobPolling } from "../../hooks/use-job-polling";
import { stageLabel } from "../../pages/JobsPage";
import {
  DOCX_MEDIA_TYPE,
  wanshitongAdminApi,
  type WanshitongDocument,
} from "./adminApi";

export const DOCX_ONLY_MESSAGE =
  "当前湾事通 Demo 仅开放 DOCX 文档。PDF、旧 DOC、Excel 和 ZIP 将在后续版本接入。";

type UploadState =
  | "rejected"
  | "uploading"
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

interface UploadItem {
  id: string;
  file: File;
  relativePath: string;
  documentId?: string;
  idempotencyKey: string;
  state: UploadState;
  job?: Job;
  message?: string;
}

function relativePathOf(file: File): string {
  return (
    (file as File & { webkitRelativePath?: string }).webkitRelativePath ||
    file.name
  );
}

function containsControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const code = character.charCodeAt(0);
    return code < 32 || code === 127;
  });
}

export function validateDocx(file: File, relativePath: string): string | null {
  if (!file.name.toLowerCase().endsWith(".docx")) return DOCX_ONLY_MESSAGE;
  if (file.type && file.type !== DOCX_MEDIA_TYPE) return DOCX_ONLY_MESSAGE;
  if (!file.size) return "DOCX 文件不能为空。";
  if (
    !relativePath ||
    relativePath.startsWith("/") ||
    /^[A-Za-z]:[\\/]/.test(relativePath) ||
    relativePath.includes("\\") ||
    containsControlCharacter(relativePath)
  ) {
    return "相对路径不安全，请移除绝对路径、盘符、反斜杠或控制字符。";
  }
  const segments = relativePath.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
    return "相对路径不能包含空路径段、. 或 ..。";
  }
  return null;
}

function uploadState(job: Job): UploadState {
  if (job.state === "succeeded") return "succeeded";
  if (["queued", "pending"].includes(job.state)) return "queued";
  if (job.state === "running") return "running";
  return "failed";
}

function activeUpload(item: UploadItem): boolean {
  return ["queued", "running"].includes(item.state) && !!item.job;
}

export function AdminDocumentsPage() {
  const [documents, setDocuments] = useState<WanshitongDocument[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>();
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const uploadsRef = useRef<UploadItem[]>([]);
  const directoryInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<unknown>();
  const [dragging, setDragging] = useState(false);
  const [pollKey, setPollKey] = useState(0);

  useEffect(() => {
    uploadsRef.current = uploads;
  }, [uploads]);
  useEffect(() => {
    directoryInput.current?.setAttribute("webkitdirectory", "");
  }, []);

  const loadDocuments = useCallback(async () => {
    try {
      const page = await wanshitongAdminApi.listDocuments(cursor);
      setDocuments(page.items);
      setNextCursor(page.next_cursor);
      setError(undefined);
    } catch (reason) {
      setError(reason);
    }
  }, [cursor]);
  useEffect(() => {
    const controller = new AbortController();
    void wanshitongAdminApi
      .listDocuments(cursor, controller.signal)
      .then((page) => {
        setDocuments(page.items);
        setNextCursor(page.next_cursor);
        setError(undefined);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      });
    return () => controller.abort();
  }, [cursor]);

  const pollUploads = useCallback(async () => {
    const active = uploadsRef.current.filter(activeUpload);
    if (!active.length) return false;
    const results = await Promise.allSettled(
      active.map((item) => wanshitongAdminApi.job(item.job!.job_id)),
    );
    const changed = new Map<string, Job>();
    const failures = new Map<string, unknown>();
    results.forEach((result, index) => {
      const id = active[index].id;
      if (result.status === "fulfilled") changed.set(id, result.value);
      else failures.set(id, result.reason);
    });
    setUploads((current) =>
      current.map((item) => {
        const job = changed.get(item.id);
        const failure = failures.get(item.id);
        if (job) {
          return {
            ...item,
            job,
            state: uploadState(job),
            message: job.safe_error || stageLabel(job.stage),
          };
        }
        if (failure) {
          return {
            ...item,
            state: "failed",
            message: failure instanceof Error ? failure.message : "任务状态读取失败。",
          };
        }
        return item;
      }),
    );
    const stillActive = [...changed.values()].some((job) =>
      ["queued", "pending", "running"].includes(job.state),
    );
    if (!stillActive) await loadDocuments();
    return stillActive;
  }, [loadDocuments]);
  useJobPolling(pollUploads, setError, pollKey);

  function updateUpload(id: string, patch: Partial<UploadItem>) {
    setUploads((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  async function startUpload(item: UploadItem) {
    updateUpload(item.id, { state: "uploading", message: "正在上传…" });
    try {
      const result = item.documentId
        ? await wanshitongAdminApi.uploadVersion(
            item.documentId,
            item.file,
            item.relativePath,
            item.idempotencyKey,
          )
        : await wanshitongAdminApi.uploadDocument(
            item.file,
            item.relativePath,
            item.idempotencyKey,
          );
      updateUpload(item.id, {
        job: result.job,
        state: uploadState(result.job),
        message: stageLabel(result.job.stage),
      });
      setPollKey((value) => value + 1);
      await loadDocuments();
    } catch (reason) {
      updateUpload(item.id, {
        state: "failed",
        message: reason instanceof Error ? reason.message : "上传失败。",
      });
    }
  }

  function enqueue(files: File[], document?: WanshitongDocument) {
    const items = files.map((file): UploadItem => {
      // 新版本沿用逻辑文档登记的相对路径，不能被本次选择的 basename 覆盖。
      const relativePath = document?.relative_path || relativePathOf(file);
      const validation = validateDocx(file, relativePath);
      return {
        id: createIdempotencyKey("upload-row"),
        file,
        relativePath,
        documentId: document?.document_id,
        // 网络结果不确定时必须复用同一 key，服务端才能返回同一请求结果。
        idempotencyKey: createIdempotencyKey(
          document ? "wst-version" : "wst-document",
        ),
        state: validation ? "rejected" : "uploading",
        message: validation || "等待上传…",
      };
    });
    setUploads((current) => [...items, ...current]);
    void Promise.allSettled(
      items
        .filter((item) => item.state !== "rejected")
        .map((item) => startUpload(item)),
    );
  }

  function pick(
    event: ChangeEvent<HTMLInputElement>,
    document?: WanshitongDocument,
  ) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length) enqueue(files, document);
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length) enqueue(files);
  }

  function openPicker(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    fileInput.current?.click();
  }

  async function retry(item: UploadItem) {
    if (item.job?.retryable) {
      try {
        const job = await wanshitongAdminApi.retryJob(item.job.job_id);
        updateUpload(item.id, {
          job,
          state: uploadState(job),
          message: stageLabel(job.stage),
        });
        setPollKey((value) => value + 1);
        return;
      } catch (reason) {
        updateUpload(item.id, {
          state: "failed",
          message: reason instanceof Error ? reason.message : "重试失败。",
        });
        return;
      }
    }
    await startUpload(item);
  }

  async function remove(document: WanshitongDocument) {
    if (!window.confirm(`确认删除“${document.display_name}”吗？`)) return;
    try {
      await wanshitongAdminApi.deleteDocument(document.document_id);
      await loadDocuments();
    } catch (reason) {
      setError(reason);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>文档管理</h2>
          <p>{DOCX_ONLY_MESSAGE}</p>
        </div>
        <button className="secondary" onClick={() => void loadDocuments()}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <section className="docx-upload-panel" aria-label="DOCX 上传">
        <div
          className={`docx-drop-zone ${dragging ? "dragging" : ""}`}
          role="button"
          tabIndex={0}
          aria-label="拖拽 DOCX 到此处，或按回车选择文件"
          onKeyDown={openPicker}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setDragging(false);
            }
          }}
          onDrop={drop}
        >
          <Upload aria-hidden="true" size={34} />
          <strong>拖拽 DOCX 到这里</strong>
          <span>每个文件会独立上传并创建处理任务。</span>
        </div>
        <div className="row-actions">
          <label className="primary file-button">
            <Upload aria-hidden="true" size={17} />
            选择文件（可多选）
            <input
              ref={fileInput}
              data-testid="wst-document-files"
              type="file"
              multiple
              accept={`.docx,${DOCX_MEDIA_TYPE}`}
              onChange={(event) => pick(event)}
            />
          </label>
          <label className="secondary file-button">
            <FolderUp aria-hidden="true" size={17} />
            选择目录
            <input
              ref={directoryInput}
              data-testid="wst-document-directory"
              type="file"
              multiple
              accept={`.docx,${DOCX_MEDIA_TYPE}`}
              onChange={(event) => pick(event)}
            />
          </label>
        </div>
      </section>
      {!!uploads.length && (
        <section className="panel" aria-label="上传与处理状态">
          <div className="section-heading">
            <h3>上传与处理状态</h3>
            <button className="secondary" onClick={() => setUploads([])}>
              清除已显示记录
            </button>
          </div>
          <div className="upload-queue" aria-live="polite">
            {uploads.map((item) => (
              <article key={item.id}>
                <div className="grow">
                  <strong>{item.file.name}</strong>
                  <code>{item.relativePath}</code>
                  <small>{item.message}</small>
                  {item.job && <small>Job：{item.job.job_id}</small>}
                </div>
                <StatusBadge value={item.job?.state || item.state} />
                {item.state === "failed" && (
                  <button onClick={() => void retry(item)}>重试此文件</button>
                )}
              </article>
            ))}
          </div>
        </section>
      )}
      <div className="row-actions" aria-label="文档分页">
        <button
          disabled={!cursorHistory.length}
          onClick={() => {
            const previous = [...cursorHistory];
            const target = previous.pop();
            setCursorHistory(previous);
            setCursor(target || undefined);
          }}
        >
          上一页
        </button>
        <button
          disabled={!nextCursor}
          onClick={() => {
            if (!nextCursor) return;
            setCursorHistory((current) => [...current, cursor || ""]);
            setCursor(nextCursor);
          }}
        >
          下一页
        </button>
      </div>
      <div className="table-wrap">
        <table className="document-table">
          <thead>
            <tr>
              <th>文档名称</th>
              <th>相对路径</th>
              <th>登记状态</th>
              <th>当前版本</th>
              <th>索引状态</th>
              <th>最近 Job</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.document_id}>
                <td>{document.display_name}</td>
                <td><code>{document.relative_path || document.display_name}</code></td>
                <td><StatusBadge value={document.status} /></td>
                <td>
                  <code>{document.current_version_id || "—"}</code>
                  <small>{document.current_version_status || "尚无版本"}</small>
                </td>
                <td>
                  <StatusBadge value={document.retrievable} />
                  <small>{document.retrievable ? "可检索" : "尚不可检索"}</small>
                </td>
                <td>
                  {document.latest_job ? (
                    <>
                      <code>{document.latest_job.job_id}</code>
                      <StatusBadge value={document.latest_job.state} />
                    </>
                  ) : "—"}
                </td>
                <td><time dateTime={document.updated_at}>{document.updated_at}</time></td>
                <td>
                  <div className="row-actions">
                    <label className="secondary file-button compact-file-button">
                      上传新版本
                      <input
                        type="file"
                        accept={`.docx,${DOCX_MEDIA_TYPE}`}
                        onChange={(event) => pick(event, document)}
                      />
                    </label>
                    {document.latest_job?.retryable &&
                      document.latest_job.state === "failed_retryable" && (
                        <button
                          onClick={() =>
                            void wanshitongAdminApi
                              .retryJob(document.latest_job!.job_id)
                              .then(() => setPollKey((value) => value + 1))
                              .catch(setError)
                          }
                        >
                          重试 Job
                        </button>
                      )}
                    <button className="danger" onClick={() => void remove(document)}>
                      删除
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!documents.length && (
        <EmptyState title="当前还没有 DOCX 文档。">
          最终业务资料将在 WB-07 阶段统一导入。
        </EmptyState>
      )}
    </section>
  );
}
