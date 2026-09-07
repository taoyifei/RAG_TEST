import { Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  createIdempotencyKey,
  type Document,
  type DocumentVersion,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { useConsole } from "../state/console-context";

export function DocumentsPage({ go }: { go: (path: string) => void }) {
  const { tokens, scope, setRevision } = useConsole();
  const [items, setItems] = useState<Document[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState<unknown>();
  const [uploading, setUploading] = useState(false);
  const [detail, setDetail] = useState<{
    document: Document;
    versions: DocumentVersion[];
  }>();
  const load = useCallback(
    () =>
      api
        .listDocuments(tokens.admin, scope.projectId, scope.kbId, offset)
        .then((p) => {
          setItems(p.items);
          setNextOffset(p.next_offset ?? null);
        })
        .catch(setError),
    [scope, tokens.admin, offset],
  );
  useEffect(() => {
    void load();
  }, [load]);
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
            onChange={(e) =>
              e.target.files?.[0] && void upload(e.target.files[0])
            }
          />
        </label>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
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
        <table>
          <thead>
            <tr>
              <th>显示名</th>
              <th>文档标识</th>
              <th>当前版本</th>
              <th>状态</th>
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
                  <StatusBadge value={item.status} />
                </td>
                <td>
                  <DocumentActions
                    document={item}
                    reload={load}
                    go={go}
                    inspect={() => void inspect(item)}
                    onError={setError}
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
}: {
  document: Document;
  reload: () => void;
  go: (path: string) => void;
  inspect: () => void;
  onError: (error: unknown) => void;
}) {
  const { tokens, scope, setRevision } = useConsole();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(document.display_name);
  const [confirmDelete, setConfirmDelete] = useState(false);
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
    try {
      await api.deleteDocument(
        tokens.admin,
        scope.projectId,
        scope.kbId,
        document.document_id,
      );
      setConfirmDelete(false);
      reload();
    } catch (reason) {
      onError(reason);
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
          <button onClick={() => void rename()}>保存</button>
          <small>只改显示名，不创建新 dver 或重建索引。</small>
        </>
      ) : (
        <button onClick={() => setRenaming(true)}>重命名</button>
      )}
      <label className="button-link">
        创建新版本
        <input
          data-testid={`version-${document.document_id}`}
          type="file"
          accept=".doc,.docx"
          onChange={(e) =>
            e.target.files?.[0] && void version(e.target.files[0])
          }
        />
      </label>
      <button onClick={inspect}>详情</button>
      <button
        className={confirmDelete ? "danger" : ""}
        onClick={() => (confirmDelete ? void remove() : setConfirmDelete(true))}
      >
        {confirmDelete ? "确认删除" : "删除"}
      </button>
    </div>
  );
}
