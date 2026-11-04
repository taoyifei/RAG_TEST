import { withAppBase } from "../app/basePath";
import { PublicApiError } from "./publicApi";

export type UserRole = "admin" | "hr" | "user";
export type LibraryScope = "personal" | "public" | "hr_public" | "hr_internal";

export interface LibraryDocument {
  id: string;
  title: string;
  status: string;
  created_at: string;
  size: number;
  shared: boolean;
  owner_id?: string;
  owner_name?: string;
  error?: string | null;
  can_manage?: boolean;
  version_state?: "current" | "staging" | "archived";
  replaces_id?: string;
  kind?: DocumentKind;
  group_name?: string;
  health?: DocumentHealth | null;
  assets?: DocumentAsset[];
}

export type DocumentKind = "policy" | "illustration" | "template";
export interface DocumentHealth {
  checked_at: string;
  original_available: boolean | null;
  check_error?: string;
  parse_status: string;
  ready: boolean;
  size_ok?: boolean;
  hash_ok?: boolean;
  image_check: "not_checked";
}
export interface DocumentAsset {
  id: string;
  filename: string;
  size: number;
  relation: "template" | "source_workbook" | "full_guide";
}
export interface DocumentDetails {
  id: string;
  kind: DocumentKind;
  version_state: string;
  group_name: string;
  assets: DocumentAsset[];
  health?: DocumentHealth | null;
}
export interface UploadReceipt {
  id: string;
  status: string;
  duplicate?: boolean;
  message?: string;
}

export interface LibraryResponse {
  scope: LibraryScope;
  can_manage: boolean;
  can_share?: boolean;
  quota?: PersonalLibraryQuota;
  items: LibraryDocument[];
}

export interface PersonalQuotaLimits {
  max_bytes: number;
  max_files: number;
}

export interface PersonalLibraryQuota extends PersonalQuotaLimits {
  used_bytes: number;
  used_files: number;
  max_file_bytes: number;
  warning: boolean;
  full: boolean;
  active_parses: number;
  queued_parses: number;
  max_active_parses: number;
  max_queued_parses: number;
  upload_allowed: boolean;
  upload_block_reason: string | null;
}

export interface ManagedUser {
  user_id: string;
  username: string;
  display_name: string;
  dept_id: string | null;
  role: UserRole;
  is_super_admin: boolean;
  editable_roles: UserRole[];
  role_edit_note: string | null;
  personal_quota?: PersonalQuotaLimits;
  can_edit_personal_quota?: boolean;
}

async function checkResponse(response: Response): Promise<Response> {
  if (response.ok) return response;
  let message = "资料服务请求失败，请稍后重试。";
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // 代理返回的 HTML 错误页不作为正文展示。
  }
  throw new PublicApiError(message, { status: response.status });
}

export async function listLibrary(
  scope: LibraryScope,
  signal?: AbortSignal,
): Promise<LibraryResponse> {
  const response = await fetch(
    withAppBase(`/api/public/library?scope=${scope}`),
    { credentials: "same-origin", signal },
  );
  return (await checkResponse(response)).json() as Promise<LibraryResponse>;
}

export async function uploadLibrary(
  scope: LibraryScope,
  file: File,
  csrfToken: string,
  options?: { replacesId?: string; kind?: DocumentKind },
): Promise<UploadReceipt> {
  const body = new FormData();
  body.set("scope", scope);
  body.set("file", file);
  if (options?.replacesId) body.set("replaces_id", options.replacesId);
  if (options?.kind) body.set("kind", options.kind);
  const response = await checkResponse(
    await fetch(withAppBase("/api/public/library"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
      body,
    }),
  );
  return response.json() as Promise<UploadReceipt>;
}

export async function getDocumentDetails(
  id: string,
  signal?: AbortSignal,
): Promise<DocumentDetails> {
  const response = await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}/details`),
      { credentials: "same-origin", signal },
    ),
  );
  return response.json() as Promise<DocumentDetails>;
}

export async function checkDocumentHealth(
  id: string,
  csrf: string,
): Promise<DocumentHealth> {
  const response = await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}/health`),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      },
    ),
  );
  return response.json() as Promise<DocumentHealth>;
}

export async function publishDocumentVersion(
  id: string,
  csrf: string,
): Promise<void> {
  await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}/publish`),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      },
    ),
  );
}

export async function uploadDocumentAsset(
  id: string,
  file: File,
  relation: DocumentAsset["relation"],
  csrf: string,
): Promise<void> {
  const body = new FormData();
  body.set("file", file);
  body.set("relation", relation);
  await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}/assets`),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
        body,
      },
    ),
  );
}

export async function downloadDocumentAsset(
  id: string,
  asset: DocumentAsset,
): Promise<void> {
  const response = await checkResponse(
    await fetch(
      withAppBase(
        `/api/public/library/${encodeURIComponent(id)}/assets/${encodeURIComponent(asset.id)}`,
      ),
      { credentials: "same-origin" },
    ),
  );
  saveDownload(await response.blob(), asset.filename);
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/[\\/:*?"<>|]/g, "_");
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export async function changeDocument(
  id: string,
  action: "delete" | "retry" | "share" | "unshare",
  csrfToken: string,
): Promise<void> {
  const suffix =
    action === "delete" ? "" : `/${action === "unshare" ? "share" : action}`;
  await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}${suffix}`),
      {
        method: action === "delete" || action === "unshare" ? "DELETE" : "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken },
      },
    ),
  );
}

export async function downloadDocument(
  id: string,
  title: string,
): Promise<void> {
  const response = await checkResponse(
    await fetch(
      withAppBase(`/api/public/library/${encodeURIComponent(id)}/download`),
      {
        credentials: "same-origin",
      },
    ),
  );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = title.replace(/[\\/:*?"<>|]/g, "_");
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export async function listUsers(signal?: AbortSignal): Promise<ManagedUser[]> {
  const response = await checkResponse(
    await fetch(withAppBase("/api/ops/users"), {
      credentials: "same-origin",
      signal,
    }),
  );
  const body = (await response.json()) as { items: ManagedUser[] };
  return body.items;
}

export async function updateUserRole(
  userId: string,
  role: UserRole,
  csrfToken: string,
): Promise<void> {
  await checkResponse(
    await fetch(withAppBase(`/api/ops/users/${encodeURIComponent(userId)}`), {
      method: "PATCH",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ role }),
    }),
  );
}

export async function updateUserPersonalQuota(
  userId: string,
  limits: PersonalQuotaLimits,
  csrfToken: string,
): Promise<PersonalQuotaLimits> {
  const response = await checkResponse(
    await fetch(
      withAppBase(
        `/api/ops/users/${encodeURIComponent(userId)}/personal-quota`,
      ),
      {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(limits),
      },
    ),
  );
  return response.json() as Promise<PersonalQuotaLimits>;
}
