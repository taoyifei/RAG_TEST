import { Download, RefreshCw, Share2, Trash2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { redirectToSso } from "./authNavigation";
import {
  changeDocument,
  downloadDocument,
  listLibrary,
  uploadLibrary,
  type LibraryDocument,
  type LibraryResponse,
  type LibraryScope,
  type UserRole,
} from "./libraryApi";
import { PublicApiError } from "./publicApi";

const SCOPES: Record<LibraryScope, { title: string; description: string }> = {
  personal: {
    title: "个人资料",
    description: "默认仅本人可检索；共享后全员可检索。系统管理员可后台管理。",
  },
  public: {
    title: "公共资料",
    description: "全员可检索的公共制度、操作指引和已共享资料。",
  },
  hr_public: {
    title: "公开人力制度",
    description: "由人力维护，所有同事均可检索的人力制度。",
  },
  hr_internal: {
    title: "人力内部资料",
    description: "仅人力角色和超级管理员可查看、检索和维护。",
  },
};

const READY_STATES = new Set([
  "completed",
  "ready",
  "success",
  "succeeded",
  "indexed",
]);
const FAILED_STATES = new Set(["failed", "error"]);

function statusLabel(status: string): string {
  if (READY_STATES.has(status)) return "可问答";
  if (FAILED_STATES.has(status)) return "解析失败";
  if (status === "pending" || status === "queued") return "等待解析";
  return "解析中";
}

function formatSize(size: number): string {
  if (!size) return "—";
  return size >= 1024 * 1024
    ? `${(size / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(size / 1024))} KB`;
}

function errorMessage(error: unknown): string {
  if (error instanceof PublicApiError && error.status === 401) {
    redirectToSso();
    return "登录已过期，正在重新登录…";
  }
  if (error instanceof PublicApiError && error.status === 403) {
    return "当前角色没有这项操作的权限，请刷新页面或联系系统管理员。";
  }
  return error instanceof Error ? error.message : "操作失败，请稍后重试。";
}

export function LibraryWorkspace({
  userRole,
  isSuperAdmin = false,
  getCsrfToken,
  onBack,
}: {
  userRole: UserRole;
  isSuperAdmin?: boolean;
  getCsrfToken: () => string | undefined;
  onBack: () => void;
}) {
  const [scope, setScope] = useState<LibraryScope>("personal");
  const scopes: LibraryScope[] = [
    "personal",
    "public",
    ...(userRole !== "user" ? ["hr_public" as const] : []),
    ...(userRole === "hr" || isSuperAdmin ? ["hr_internal" as const] : []),
  ];
  const visibleScope = scopes.includes(scope) ? scope : "personal";
  return (
    <main className="wst-workspace" id="main-content">
      <header className="wst-workspace-heading">
        <div>
          <button className="wst-text-action" type="button" onClick={onBack}>
            ← 返回问答
          </button>
          <h1>我的资料</h1>
          <p>上传资料，解析完成后就可以向湾小度提问。</p>
        </div>
      </header>
      <nav className="wst-workspace-tabs" aria-label="资料范围">
        {scopes.map((item) => (
          <button
            key={item}
            type="button"
            aria-current={visibleScope === item ? "page" : undefined}
            onClick={() => setScope(item)}
          >
            {SCOPES[item].title}
          </button>
        ))}
      </nav>
      <LibraryListing
        key={visibleScope}
        scope={visibleScope}
        getCsrfToken={getCsrfToken}
      />
    </main>
  );
}

function LibraryListing({
  scope,
  getCsrfToken,
}: {
  scope: LibraryScope;
  getCsrfToken: () => string | undefined;
}) {
  const [data, setData] = useState<LibraryResponse>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void listLibrary(scope, controller.signal)
      .then((result) => {
        setData(result);
        setError(undefined);
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [scope, refresh]);

  const parsing = data?.items.some(
    (item) => !READY_STATES.has(item.status) && !FAILED_STATES.has(item.status),
  );
  useEffect(() => {
    if (!parsing) return;
    const timer = window.setInterval(
      () => setRefresh((value) => value + 1),
      5000,
    );
    return () => window.clearInterval(timer);
  }, [parsing]);

  const run = async (
    key: string,
    operation: (csrf: string) => Promise<void>,
    success: string,
  ) => {
    const csrf = getCsrfToken();
    if (!csrf) {
      redirectToSso();
      return;
    }
    setBusy(key);
    setError(undefined);
    setNotice(undefined);
    try {
      await operation(csrf);
      setNotice(success);
      setRefresh((value) => value + 1);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(undefined);
    }
  };

  const mutate = (
    document: LibraryDocument,
    action: "delete" | "retry" | "share" | "unshare",
  ) => {
    if (
      action === "delete" &&
      !window.confirm(`删除“${document.title}”？删除后将无法再检索这份资料。`)
    )
      return;
    const messages = {
      delete: "资料已删除。",
      retry: "已重新提交解析，请等待状态更新。",
      share: "已提交共享。共享资料解析完成后，全员可检索。",
      unshare: "已取消共享，个人原件仍保留。",
    };
    void run(
      document.id,
      (csrf) => changeDocument(document.id, action, csrf),
      messages[action],
    );
  };

  return (
    <section className="wst-library-panel" aria-label={SCOPES[scope].title}>
      <div className="wst-library-toolbar">
        <p>{SCOPES[scope].description}</p>
        <div className="wst-library-actions">
          <button
            type="button"
            onClick={() => setRefresh((value) => value + 1)}
            disabled={!!busy}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
          </button>
          {data?.can_manage && (
            <>
              <input
                ref={fileInput}
                type="file"
                hidden
                aria-label="选择上传资料"
                accept=".pdf,.docx,.xlsx,.pptx,.txt,.md"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  event.currentTarget.value = "";
                  if (!file) return;
                  void run(
                    "upload",
                    (csrf) => uploadLibrary(scope, file, csrf),
                    "已上传，正在准备解析。解析完成后即可提问。",
                  );
                }}
              />
              <button
                className="wst-workspace-primary"
                type="button"
                disabled={!!busy}
                onClick={() => fileInput.current?.click()}
              >
                <Upload size={16} aria-hidden="true" />
                {busy === "upload" ? "上传中…" : "上传资料"}
              </button>
            </>
          )}
        </div>
      </div>
      {data?.can_manage && (
        <p className="wst-document-meta">
          支持 DOCX、PDF、XLSX、PPTX、TXT 和 MD。PDF 支持文字版；扫描件需 OCR
          识别。
        </p>
      )}
      {error && (
        <p className="wst-workspace-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="wst-workspace-notice" role="status">
          {notice}
        </p>
      )}
      {!data && !error && (
        <p className="wst-workspace-empty" role="status">
          正在加载资料…
        </p>
      )}
      {data?.items.length === 0 && (
        <p className="wst-workspace-empty">
          这里还没有资料。
          {data.can_manage
            ? "点击“上传资料”开始。"
            : "资料发布后会显示在这里。"}
        </p>
      )}
      {data && data.items.length > 0 && (
        <div className="wst-workspace-table-scroll">
          <table className="wst-workspace-table">
            <thead>
              <tr>
                <th scope="col">文档</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => {
                const canManage = item.can_manage ?? data.can_manage;
                const failed = FAILED_STATES.has(item.status);
                return (
                  <tr key={item.id}>
                    <td>
                      <strong>{item.title}</strong>
                      <div className="wst-document-meta">
                        {formatSize(item.size)}
                        {item.created_at &&
                          ` · ${new Date(item.created_at).toLocaleString("zh-CN")}`}
                        {item.owner_name && ` · ${item.owner_name}`}
                      </div>
                      {item.error && (
                        <p className="wst-document-error">{item.error}</p>
                      )}
                    </td>
                    <td>
                      <span
                        className={`wst-document-status ${failed ? "failed" : READY_STATES.has(item.status) ? "ready" : "parsing"}`}
                      >
                        {statusLabel(item.status)}
                      </span>
                      {scope === "personal" && item.shared && (
                        <span className="wst-shared-label">已共享</span>
                      )}
                    </td>
                    <td>
                      <div className="wst-document-actions">
                        <button
                          type="button"
                          disabled={!!busy}
                          onClick={() => {
                            setBusy(item.id);
                            void downloadDocument(item.id, item.title)
                              .catch((failure: unknown) =>
                                setError(errorMessage(failure)),
                              )
                              .finally(() => setBusy(undefined));
                          }}
                        >
                          <Download size={15} aria-hidden="true" />
                          下载
                        </button>
                        {canManage && failed && (
                          <button
                            type="button"
                            disabled={!!busy}
                            onClick={() => mutate(item, "retry")}
                          >
                            <RefreshCw size={15} aria-hidden="true" />
                            重试
                          </button>
                        )}
                        {canManage &&
                          scope === "personal" &&
                          (item.shared || READY_STATES.has(item.status)) && (
                            <button
                              type="button"
                              disabled={!!busy}
                              onClick={() =>
                                mutate(item, item.shared ? "unshare" : "share")
                              }
                            >
                              {item.shared ? (
                                <X size={15} aria-hidden="true" />
                              ) : (
                                <Share2 size={15} aria-hidden="true" />
                              )}
                              {item.shared ? "取消共享" : "共享给全员"}
                            </button>
                          )}
                        {canManage && (
                          <button
                            className="wst-document-delete"
                            type="button"
                            disabled={!!busy}
                            onClick={() => mutate(item, "delete")}
                          >
                            <Trash2 size={15} aria-hidden="true" />
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export { errorMessage as workspaceErrorMessage };
