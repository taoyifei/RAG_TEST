import { Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  createIdempotencyKey,
  type Document,
  type DocumentVersion,
  type Job,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";
import { KnowledgeBaseModels } from "../components/KnowledgeBaseModels";
import { DocumentImages } from "../components/DocumentImages";

export function DocumentsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setRevision } = useConsole();
  const [items, setItems] = useState<Document[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState<unknown>();
  const [uploading, setUploading] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [detail, setDetail] = useState<{
    document: Document;
    versions: DocumentVersion[];
  }>();
  const load = useCallback(
    () =>
      api
        .listDocuments(tokens.admin, scope.projectId, scope.kbId, offset)
        .then((p) => {
          if (!p.items.length && offset > 0) {
            setOffset(Math.max(0, offset - (p.page_size || 50)));
            return;
          }
          setItems(p.items);
          setNextOffset(p.next_offset ?? null);
          setError(undefined);
        })
        .catch(setError),
    [scope, tokens.admin, offset],
  );
  useEffect(() => {
    void load();
    let active = true;
    void api
      .listJobs(tokens.admin, scope.projectId, scope.kbId)
      .then((page) => {
        if (active) setJobs(page.items);
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [load, tokens.admin, scope.projectId, scope.kbId]);
  async function upload(file: File) {
    setUploading(true);
    setError(undefined);
    try {
      const job = await api.uploadDocument(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        file,
        createIdempotencyKey("document"),
      );
      setRevision(job.revision_id);
      go("/jobs");
    } catch (reason) {
      setError(reason);
    } finally {
      setUploading(false);
    }
  }
  async function inspect(document: Document) {
    setError(undefined);
    try {
      const [fresh, versions] = await Promise.all([
        api.getDocument(
          tokens.admin,
          scope.projectId,
          scope.kbId,
          document.document_id,
        ),
        api.listVersions(
          tokens.admin,
          scope.projectId,
          scope.kbId,
          document.document_id,
        ),
      ]);
      setDetail({ document: fresh, versions: versions.items });
    } catch (reason) {
      setError(reason);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>文档</h2>
          <p>上传文档建立索引，也可为已有文档上传新版本。</p>
        </div>
        <label className="primary file-button">
          <Upload aria-hidden="true" size={18} />
          {uploading ? "上传中…" : "新建文档"}
          <input
            data-testid="new-document-file"
            type="file"
            accept=".doc,.docx"
            disabled={uploading}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void upload(file);
            }}
          />
        </label>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <KnowledgeBaseModels key={scope.kbId} kbId={scope.kbId} />
      <div className="row-actions" aria-label="分页">
        <button disabled={offset === 0} onClick={() => setOffset(0)}>
          返回首页
        </button>
        <span>从第 {offset + 1} 项开始</span>
        <button
          disabled={nextOffset === null}
          onClick={() => nextOffset !== null && setOffset(nextOffset)}
        >
          下一页
        </button>
      </div>
      <div className="table-wrap">
        <table className="document-table">
          <thead>
            <tr>
              <th>显示名</th>
              <th>文档标识</th>
              <th>当前版本</th>
              <th>登记状态</th>
              <th>当前索引可检索</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.document_id}>
                <td>{item.display_name}</td>
                <td>
                  <code>{item.document_id}</code>
                </td>
                <td>
                  <code>{item.current_version_id ?? "—"}</code>
                </td>
                <td>
                  <StatusBadge
                    value={
                      item.status === "active" ? "registered" : item.status
                    }
                  />
                </td>
                <td>
                  {item.current_version_id && item.active_index_revision_id
                    ? "已进入当前索引"
                    : item.current_version_id
                      ? "未被当前索引收录"
                      : "无当前版本，尚不可检索"}
                  {jobs.find((job) => job.document_id === item.document_id)
                    ?.safe_error && (
                    <small className="error-text">
                      {
                        jobs.find((job) => job.document_id === item.document_id)
                          ?.safe_error
                      }
                    </small>
                  )}
                </td>
                <td>
                  <DocumentActions
                    document={item}
                    reload={load}
                    go={go}
                    inspect={() => void inspect(item)}
                    onError={setError}
                    onDeleted={() => {
                      setItems((current) =>
                        current.filter(
                          (doc) => doc.document_id !== item.document_id,
                        ),
                      );
                      if (detail?.document.document_id === item.document_id)
                        setDetail(undefined);
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <section className="panel" aria-label="文档详情">
          <div className="section-heading">
            <div>
              <span className="eyebrow">逻辑文档</span>
              <h3>{detail.document.display_name}</h3>
              <code>{detail.document.document_id}</code>
            </div>
            <StatusBadge value={detail.document.status} />
          </div>
          <h4>不可变版本时间线</h4>
          {!detail.versions.length && (
            <p>尚无版本。可为此文档重新上传原件，或删除整个逻辑文档。</p>
          )}
          <div className="version-list">
            {detail.versions.map((version) => (
              <article key={version.document_version_id}>
                <div>
                  <strong>{version.document_version_id}</strong>
                  <small>{version.created_at}</small>
                </div>
                <dl className="detail-grid">
                  <dt>内容指纹</dt>
                  <dd>{version.content_sha256}</dd>
                  <dt>来源文件指纹</dt>
                  <dd>{version.source_artifact_id}</dd>
                  <dt>大小</dt>
                  <dd>{version.size_bytes} bytes</dd>
                </dl>
              </article>
            ))}
          </div>
        </section>
      )}
      {!items.length && (
        <EmptyState title="暂无文档">
          上传 DOC 或 DOCX 后，系统会创建不可变版本与新的索引版本。
        </EmptyState>
      )}
    </section>
  );
}

function DocumentActions({
  document,
  reload,
  go,
  inspect,
  onError,
  onDeleted,
}: {
  document: Document;
  reload: () => void;
  go: (path: string) => void;
  inspect: () => void;
  onError: (error: unknown) => void;
  onDeleted: () => void;
}) {
  const { tokens, scope, setRevision } = useConsole();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(document.display_name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deleteMessage, setDeleteMessage] = useState("");
  const [images, setImages] = useState(false);
  async function rename() {
    await api.renameDocument(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
      name,
    );
    await api.getDocument(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
    );
    setRenaming(false);
    reload();
  }
  async function version(file: File) {
    const job = await api.uploadVersion(
      tokens.admin,
      scope.projectId,
      scope.kbId,
      document.document_id,
      file,
      createIdempotencyKey("version"),
    );
    setRevision(job.revision_id);
    go("/jobs");
  }
  async function remove() {
    setBusy(true);
    try {
      const removed = await api.deleteDocument(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        document.document_id,
      );
      setConfirmDelete(false);
      if (
        removed.statusCode === 202 &&
        removed.document?.status !== "deleted"
      ) {
        setDeleteMessage("删除请求已接收，后台处理中；可刷新列表核对。");
      } else {
        onDeleted();
      }
      reload();
    } catch (reason) {
      onError(reason);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="row-actions">
      {renaming ? (
        <>
          <input
            aria-label="新显示名"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button onClick={() => void rename().catch(onError)}>保存</button>
          <small>只改显示名，不创建新 dver 或重建索引。</small>
        </>
      ) : (
        <button onClick={() => setRenaming(true)}>重命名</button>
      )}
      <label className="button-link">
        {document.current_version_id ? "创建新版本" : "重新上传此文档"}
        <input
          data-testid={`version-${document.document_id}`}
          type="file"
          accept=".doc,.docx"
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (file) void version(file).catch(onError);
          }}
        />
      </label>
      <button onClick={inspect}>详情</button>
      <button
        onClick={() => {
          const url = new URL(window.location.href);
          url.searchParams.set("document_id", document.document_id);
          url.searchParams.delete("trace_id");
          url.searchParams.delete("job_id");
          url.searchParams.delete("revision_id");
          window.history.replaceState({}, "", url.pathname + url.search);
          go("/operational-traces");
        }}
      >
        技术 Trace
      </button>
      <button onClick={() => setImages(true)}>图片识别</button>
      {images && (
        <DocumentImages
          projectId={scope.projectId}
          kbId={scope.kbId}
          documentId={document.document_id}
          onClose={() => setImages(false)}
          onSubmitted={(job) => {
            setImages(false);
            setRevision(job.revision_id);
            go("/jobs");
          }}
        />
      )}
      {!document.active_index_revision_id && (
        <button onClick={() => go("/jobs")}>查看处理任务</button>
      )}
      <button
        className={confirmDelete ? "danger" : ""}
        disabled={busy}
        onClick={() => (confirmDelete ? void remove() : setConfirmDelete(true))}
      >
        {busy ? "删除中…" : confirmDelete ? "确认删除整个文档" : "删除"}
      </button>
      {confirmDelete && (
        <>
          <small>
            删除“{document.display_name}
            ”的全部版本；新查询和来源访问会立即排除它。
          </small>
          <button disabled={busy} onClick={() => setConfirmDelete(false)}>
            取消删除
          </button>
        </>
      )}
      {deleteMessage && <small role="status">{deleteMessage}</small>}
    </div>
  );
}
