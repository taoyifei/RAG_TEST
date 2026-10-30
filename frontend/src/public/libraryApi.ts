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
}

export interface LibraryResponse {
  scope: LibraryScope;
  can_manage: boolean;
  items: LibraryDocument[];
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
): Promise<void> {
  const body = new FormData();
  body.set("scope", scope);
  body.set("file", file);
  await checkResponse(
    await fetch(withAppBase("/api/public/library"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
      body,
    }),
  );
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
