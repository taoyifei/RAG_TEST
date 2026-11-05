import {
  Download,
  MessageCircle,
  RefreshCw,
  Search,
  Share2,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { redirectToSso } from "./authNavigation";
import {
  changeDocument,
  checkDocumentHealth,
  downloadDocument,
  listLibrary,
  uploadLibrary,
  publishDocumentVersion,
  uploadDocumentAsset,
  type DocumentAsset,
  type DocumentKind,
  type LibraryDocument,
  type LibraryResponse,
  type LibraryScope,
  type PersonalLibraryQuota,
  type UserRole,
} from "./libraryApi";
import { PublicApiError } from "./publicApi";
import {
  DOCUMENT_KIND_NAMES,
  DocumentAssetButtons,
} from "./DocumentAttachments";

const SCOPES: Record<LibraryScope, { title: string }> = {
  personal: {
    title: "个人资料",
  },
  public: {
    title: "公共资料",
  },
  hr_public: {
    title: "公开人力制度",
  },
  hr_internal: {
    title: "人力内部资料",
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
const MEBIBYTE = 1024 * 1024;

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

function formatCapacity(size: number): string {
  if (size > 0 && size < MEBIBYTE) {
    return size < 1024
      ? `${size} B`
      : `${(size / 1024).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} KB`;
  }
  return `${(size / MEBIBYTE).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} MB`;
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
  onAskDocuments,
}: {
  userRole: UserRole;
  isSuperAdmin?: boolean;
  getCsrfToken: () => string | undefined;
  onBack: () => void;
  onAskDocuments: (
    scope: LibraryScope,
    documents: { id: string; title: string }[],
  ) => void;
}) {
  const [scope, setScope] = useState<LibraryScope>("personal");
  const scopes: LibraryScope[] = [
    "personal",
    "public",
    "hr_public",
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
        onAskDocuments={onAskDocuments}
      />
    </main>
  );
}

function LibraryListing({
  scope,
  getCsrfToken,
  onAskDocuments,
}: {
  scope: LibraryScope;
  getCsrfToken: () => string | undefined;
  onAskDocuments: (
    scope: LibraryScope,
    documents: { id: string; title: string }[],
  ) => void;
}) {
  const [data, setData] = useState<LibraryResponse>();
  const [error, setError] = useState<string>();
  const [loadError, setLoadError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortOrder, setSortOrder] = useState("newest");
  const [versionFilter, setVersionFilter] = useState("current");
  const [kindFilter, setKindFilter] = useState<"all" | DocumentKind>("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [versionFor, setVersionFor] = useState<LibraryDocument>();
  const [assetFor, setAssetFor] = useState<LibraryDocument>();
  const [assetRelation, setAssetRelation] =
    useState<DocumentAsset["relation"]>("template");
  const assetInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const versionInput = useRef<HTMLInputElement>(null);
  const quota = scope === "personal" ? data?.quota : undefined;
  const uploadBlocked = quota?.upload_allowed === false;
  const filteredItems = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    return (data?.items ?? [])
      .filter((item) => {
        if (
          versionFilter === "archived"
            ? item.version_state !== "archived"
            : item.version_state === "archived"
        )
          return false;
        if (keyword && !item.title.toLocaleLowerCase().includes(keyword))
          return false;
        if (kindFilter !== "all" && (item.kind ?? "policy") !== kindFilter)
          return false;
        if (statusFilter === "ready") return READY_STATES.has(item.status);
        if (statusFilter === "failed") return FAILED_STATES.has(item.status);
        if (statusFilter === "parsing")
          return (
            !READY_STATES.has(item.status) && !FAILED_STATES.has(item.status)
          );
        return true;
      })
      .sort((left, right) => {
        if (sortOrder === "name")
          return left.title.localeCompare(right.title, "zh-CN", {
            numeric: true,
          });
        const difference =
          (Date.parse(right.created_at) || 0) -
          (Date.parse(left.created_at) || 0);
        return (
          (sortOrder === "oldest" ? -difference : difference) ||
          left.title.localeCompare(right.title, "zh-CN", { numeric: true })
        );
      });
  }, [data?.items, kindFilter, query, sortOrder, statusFilter, versionFilter]);
  const askable = (item: LibraryDocument) =>
    READY_STATES.has(item.status) &&
    (item.version_state ?? "current") === "current";
  const chosen = (data?.items ?? []).filter(
    (item) => selected.has(item.id) && askable(item),
  );
  const archivedCount = (data?.items ?? []).filter(
    (item) => item.version_state === "archived",
  ).length;
  const currentCount = (data?.items.length ?? 0) - archivedCount;
  const hasFilters =
    !!query ||
    kindFilter !== "all" ||
    statusFilter !== "all" ||
    sortOrder !== "newest" ||
    versionFilter !== "current";
  const clearFilters = () => {
    setQuery("");
    setKindFilter("all");
    setStatusFilter("all");
    setSortOrder("newest");
    setVersionFilter("current");
  };

  useEffect(() => {
    const controller = new AbortController();
    void listLibrary(scope, controller.signal)
      .then((result) => {
        setData(result);
        setLoadError(undefined);
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setLoadError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [scope, refresh]);

  const parsing =
    data?.items.some(
      (item) =>
        !READY_STATES.has(item.status) && !FAILED_STATES.has(item.status),
    ) ||
    (quota !== undefined && quota.active_parses + quota.queued_parses > 0);
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
    operation: (csrf: string) => Promise<void | string>,
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
      const result = await operation(csrf);
      setNotice(typeof result === "string" ? result : success);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setRefresh((value) => value + 1);
      setBusy(undefined);
    }
  };

  const upload = (file: File) => {
    setError(undefined);
    setNotice(undefined);
    if (scope === "personal") {
      const maxFileBytes = quota?.max_file_bytes ?? 50 * MEBIBYTE;
      if (!file.size) {
        setError("请上传非空文件。");
        return;
      }
      if (file.size > maxFileBytes) {
        setError(`单个文件不能超过 ${formatCapacity(maxFileBytes)}。`);
        return;
      }
      if (uploadBlocked) {
        setError(
          quota?.upload_block_reason || "个人资料暂时无法新增，请稍后重试。",
        );
        return;
      }
      if (quota && quota.used_files + 1 > quota.max_files) {
        setError("个人资料份数已达上限，请先删除不再需要的资料。");
        return;
      }
      if (quota && quota.used_bytes + file.size > quota.max_bytes) {
        setError(
          "个人空间不足，请先删除不再需要的资料，或联系超级管理员调整额度。",
        );
        return;
      }
    }
    void run(
      "upload",
      async (csrf) => {
        const result = await uploadLibrary(
          versionFor?.scope ?? scope,
          file,
          csrf,
          {
            replacesId: versionFor?.id,
            kind: versionFor?.kind ?? "policy",
          },
        );
        setVersionFor(undefined);
        return result.message;
      },
      "已上传，正在准备解析。解析完成后即可提问。",
    );
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
      <input
        ref={versionInput}
        type="file"
        hidden
        aria-label="选择资料新版本"
        accept=".pdf,.docx,.xlsx,.pptx,.txt,.md"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) upload(file);
        }}
      />
      <div className="wst-library-toolbar">
        <div className="wst-library-actions">
          <button
            type="button"
            onClick={() => {
              setError(undefined);
              setRefresh((value) => value + 1);
            }}
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
                  upload(file);
                }}
              />
              <button
                className="wst-workspace-primary"
                type="button"
                disabled={!!busy || uploadBlocked}
                onClick={() => {
                  setVersionFor(undefined);
                  fileInput.current?.click();
                }}
              >
                <Upload size={16} aria-hidden="true" />
                {busy === "upload" ? "上传中…" : "上传资料"}
              </button>
              <button
                type="button"
                disabled={!!busy || !data.items.length}
                onClick={() =>
                  void run(
                    "health",
                    async (csrf) => {
                      const candidates = data.items.filter(
                        (item) => item.version_state !== "archived",
                      );
                      let issues = 0;
                      for (
                        let offset = 0;
                        offset < candidates.length;
                        offset += 4
                      ) {
                        const results = await Promise.all(
                          candidates
                            .slice(offset, offset + 4)
                            .map((item) => checkDocumentHealth(item.id, csrf)),
                        );
                        issues += results.filter(
                          (result) => !result.ready,
                        ).length;
                      }
                      return `已检查 ${candidates.length} 份资料的原件和解析状态${issues ? `，${issues} 份需要处理，详见资料列表。` : "，均可下载且已解析。"} 图片显示效果请在引用片段中确认。`;
                    },
                    "原件检查完成。",
                  )
                }
              >
                {busy === "health" ? "检查中…" : "检查资料"}
              </button>
            </>
          )}
        </div>
      </div>
      {quota && <PersonalQuotaPanel quota={quota} />}
      {data?.can_manage && (
        <p className="wst-document-meta">
          支持 Word、Excel、PowerPoint、PDF 和文本文件；PDF
          建议使用可选中文字的版本。
        </p>
      )}
      {(error || loadError) && (
        <p className="wst-workspace-error" role="alert">
          {error || loadError}
        </p>
      )}
      {notice && (
        <p className="wst-workspace-notice" role="status">
          {notice}
        </p>
      )}
      {assetFor && (
        <div
          className="wst-asset-editor"
          role="region"
          aria-label="关联原始附件"
        >
          <p>
            <strong>给“{assetFor.title}”关联文件</strong>
          </p>
          <p className="wst-document-meta">
            请提供真正可编辑的原始附件或完整指引。截图文档请选择操作图示，不作为可填写模板。
          </p>
          <select
            aria-label="关联文件用途"
            value={assetRelation}
            onChange={(event) =>
              setAssetRelation(
                event.currentTarget.value as DocumentAsset["relation"],
              )
            }
          >
            <option value="template">可填写附件</option>
            <option value="full_guide">完整操作指引</option>
            <option value="source_workbook">原始Excel附件汇总</option>
          </select>
          <input
            ref={assetInput}
            type="file"
            aria-label="选择原始附件"
            accept=".xlsx,.xls,.docx,.doc,.pdf"
          />
          <button
            type="button"
            disabled={!!busy}
            onClick={() => {
              const file = assetInput.current?.files?.[0];
              if (!file) {
                setError("请先选择一个文件。");
                return;
              }
              void run(
                "asset",
                async (csrf) => {
                  await uploadDocumentAsset(
                    assetFor.id,
                    file,
                    assetRelation,
                    csrf,
                  );
                  setAssetFor(undefined);
                },
                "附件已关联，可在资料列表及回答引用旁下载。",
              );
            }}
          >
            关联文件
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() => setAssetFor(undefined)}
          >
            取消
          </button>
        </div>
      )}
      {data && data.items.length > 0 && (
        <>
          <div className="wst-library-filters" aria-label="查找资料">
            <label className="wst-library-search">
              <Search size={16} aria-hidden="true" />
              <span className="sr-only">搜索资料名称</span>
              <input
                type="search"
                placeholder="输入资料名称"
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
              />
            </label>
            <label>
              <span className="sr-only">版本范围</span>
              <select
                aria-label="版本范围"
                value={versionFilter}
                onChange={(event) =>
                  setVersionFilter(event.currentTarget.value)
                }
              >
                <option value="current">当前资料</option>
                <option value="archived">已归档旧版</option>
              </select>
            </label>
            <label>
              <span className="sr-only">资料用途</span>
              <select
                aria-label="资料用途"
                value={kindFilter}
                onChange={(event) =>
                  setKindFilter(
                    event.currentTarget.value as "all" | DocumentKind,
                  )
                }
              >
                <option value="all">全部用途</option>
                {Object.entries(DOCUMENT_KIND_NAMES).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="sr-only">资料状态</span>
              <select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.currentTarget.value)}
              >
                <option value="all">全部状态</option>
                <option value="ready">可问答</option>
                <option value="parsing">正在准备</option>
                <option value="failed">需要处理</option>
              </select>
            </label>
            <label>
              <span className="sr-only">资料排序</span>
              <select
                value={sortOrder}
                onChange={(event) => setSortOrder(event.currentTarget.value)}
              >
                <option value="newest">最近上传</option>
                <option value="oldest">最早上传</option>
                <option value="name">按名称排序</option>
              </select>
            </label>
            {hasFilters && (
              <button type="button" onClick={clearFilters}>
                清除筛选
              </button>
            )}
          </div>
          <div className="wst-document-selection">
            <button
              type="button"
              disabled={!!busy || !chosen.length}
              onClick={() =>
                onAskDocuments(
                  scope,
                  chosen.map(({ id, title }) => ({ id, title })),
                )
              }
            >
              <MessageCircle size={15} aria-hidden="true" />
              问选中资料{chosen.length ? `（${chosen.length}）` : ""}
            </button>
            {chosen.length > 0 && (
              <button type="button" onClick={() => setSelected(new Set())}>
                取消选择
              </button>
            )}
            <span className="wst-document-meta">
              可选1—20份，只在选中的资料中查找。
            </span>
          </div>
          <p className="wst-library-result-count" role="status">
            {hasFilters
              ? `找到 ${filteredItems.length} 份资料，共 ${versionFilter === "archived" ? archivedCount : currentCount} 份`
              : `共 ${currentCount} 份资料${archivedCount ? `，另有 ${archivedCount} 份已归档旧版` : ""}`}
          </p>
        </>
      )}
      {!data && !error && !loadError && (
        <p className="wst-workspace-empty" role="status">
          正在加载资料…
        </p>
      )}
      {data?.items.length === 0 && (
        <p className="wst-workspace-empty">
          这里还没有资料。
          {data.can_manage
            ? uploadBlocked
              ? "额度或解析名额恢复后即可上传。"
              : "点击“上传资料”开始。"
            : "资料发布后会显示在这里。"}
        </p>
      )}
      {data && data.items.length > 0 && filteredItems.length === 0 && (
        <p className="wst-workspace-empty">
          没有找到相关资料，换个名称或清除筛选试试。
        </p>
      )}
      {data && filteredItems.length > 0 && (
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
              {filteredItems.map((item) => {
                const canManage = item.can_manage ?? data.can_manage;
                const failed = FAILED_STATES.has(item.status);
                return (
                  <tr key={item.id}>
                    <td>
                      <div className="wst-document-choice">
                        <input
                          type="checkbox"
                          aria-label={`选择${item.title}`}
                          disabled={
                            !askable(item) ||
                            (!selected.has(item.id) && chosen.length >= 20)
                          }
                          checked={selected.has(item.id) && askable(item)}
                          onChange={(event) => {
                            const checked = event.currentTarget.checked;
                            setSelected((previous) => {
                              const next = new Set(previous);
                              if (checked) next.add(item.id);
                              else next.delete(item.id);
                              return next;
                            });
                          }}
                        />
                        <strong>{item.title}</strong>
                      </div>
                      <span className="wst-document-kind">
                        {DOCUMENT_KIND_NAMES[item.kind ?? "policy"]}
                      </span>
                      {item.version_state === "staging" && (
                        <span className="wst-document-meta">
                          新版本待发布，现有版本继续使用
                        </span>
                      )}
                      {item.version_state === "archived" && (
                        <span className="wst-document-meta">
                          已归档，不参与新问答
                        </span>
                      )}
                      <div className="wst-document-meta">
                        {formatSize(item.size)}
                        {item.created_at &&
                          ` · ${new Date(item.created_at).toLocaleString("zh-CN")}`}
                        {item.owner_name && ` · ${item.owner_name}`}
                      </div>
                      {item.error && (
                        <p className="wst-document-error">{item.error}</p>
                      )}
                      {item.health && (
                        <span
                          className={`wst-document-health ${item.health.ready ? "" : "issue"}`}
                        >
                          {item.health.ready
                            ? "原件及解析已检查"
                            : item.health.original_available === null
                              ? "原件下载暂时失败，请稍后重试"
                              : !item.health.original_available
                                ? "原件暂不可用，请联系资料维护者"
                                : "原件或解析状态需要处理"}{" "}
                          ·{" "}
                          {new Date(item.health.checked_at).toLocaleDateString(
                            "zh-CN",
                          )}
                        </span>
                      )}
                      <DocumentAssetButtons
                        documentId={item.id}
                        assets={item.assets ?? []}
                      />
                    </td>
                    <td>
                      <span
                        className={`wst-document-status ${failed ? "failed" : READY_STATES.has(item.status) ? "ready" : "parsing"}`}
                      >
                        {statusLabel(item.status)}
                      </span>
                      {scope === "personal" &&
                        data.can_share === true &&
                        item.shared && (
                          <span className="wst-shared-label">已共享</span>
                        )}
                    </td>
                    <td>
                      <div className="wst-document-actions">
                        {askable(item) && (
                          <button
                            type="button"
                            disabled={!!busy}
                            onClick={() =>
                              onAskDocuments(scope, [
                                { id: item.id, title: item.title },
                              ])
                            }
                          >
                            <MessageCircle size={15} aria-hidden="true" />
                            问这份资料
                          </button>
                        )}
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
                        {canManage &&
                          item.version_state !== "archived" &&
                          item.version_state !== "staging" && (
                            <button
                              type="button"
                              disabled={!!busy}
                              onClick={() => {
                                setVersionFor(item);
                                versionInput.current?.click();
                              }}
                            >
                              更新版本
                            </button>
                          )}
                        {canManage &&
                          item.version_state === "staging" &&
                          READY_STATES.has(item.status) && (
                            <button
                              type="button"
                              disabled={!!busy}
                              onClick={() => {
                                if (
                                  !window.confirm(
                                    "发布此版本？检查通过后，新问答将使用此版本，旧版本保留在“已归档旧版”。",
                                  )
                                )
                                  return;
                                void run(
                                  item.id,
                                  (csrf) =>
                                    publishDocumentVersion(item.id, csrf),
                                  "新版本已发布，旧版已归档，不再参与新问答。",
                                );
                              }}
                            >
                              发布此版本
                            </button>
                          )}
                        {canManage && scope !== "personal" && (
                          <button
                            type="button"
                            disabled={!!busy}
                            onClick={() => {
                              setAssetFor(item);
                              setError(undefined);
                            }}
                          >
                            关联附件
                          </button>
                        )}
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
                          data.can_share === true &&
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

function PersonalQuotaPanel({ quota }: { quota: PersonalLibraryQuota }) {
  const capacity = `${formatCapacity(quota.used_bytes)} / ${formatCapacity(quota.max_bytes)}`;
  const files = `${quota.used_files} / ${quota.max_files} 份`;
  return (
    <section
      className={`wst-personal-quota ${quota.full || quota.warning ? "warning" : ""}`}
      aria-label="个人资料额度"
    >
      <div className="wst-quota-meters">
        <div>
          <div className="wst-quota-heading">
            <span>个人空间</span>
            <strong>{capacity}</strong>
          </div>
          <progress
            aria-label="个人空间使用量"
            aria-valuetext={capacity}
            max={Math.max(1, quota.max_bytes)}
            value={Math.min(quota.used_bytes, Math.max(1, quota.max_bytes))}
          />
        </div>
        <div>
          <div className="wst-quota-heading">
            <span>资料份数</span>
            <strong>{files}</strong>
          </div>
          <progress
            aria-label="个人资料份数"
            aria-valuetext={files}
            max={Math.max(1, quota.max_files)}
            value={Math.min(quota.used_files, Math.max(1, quota.max_files))}
          />
        </div>
      </div>
      <p className="wst-quota-parsing" role="status">
        {`正在解析 ${quota.active_parses} 份 · 等待 ${quota.queued_parses} / ${quota.max_queued_parses} 份`}
        <span>{`每人同时解析 ${quota.max_active_parses} 份，单文件最多 ${formatCapacity(quota.max_file_bytes)}。`}</span>
      </p>
      {(quota.warning || !quota.upload_allowed) && (
        <p className="wst-quota-message" role="status">
          {!quota.upload_allowed
            ? quota.upload_block_reason ||
              "个人资料已达额度上限，暂时无法新增。"
            : "个人空间或资料份数已使用80%以上，请留意剩余额度。"}
          <span>已有资料仍可问答、下载和删除。</span>
        </p>
      )}
    </section>
  );
}

export { errorMessage as workspaceErrorMessage };
