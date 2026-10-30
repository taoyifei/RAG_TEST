import { useEffect, useState } from "react";

import { redirectToSso } from "./authNavigation";
import {
  listUsers,
  updateUserRole,
  type ManagedUser,
  type UserRole,
} from "./libraryApi";
import { workspaceErrorMessage } from "./LibraryWorkspace";

const ROLE_LABELS: Record<UserRole, string> = {
  user: "普通用户",
  hr: "人力角色",
  admin: "系统管理员",
};

export function UserRolesWorkspace({
  getCsrfToken,
  onBack,
}: {
  getCsrfToken: () => string | undefined;
  onBack: () => void;
}) {
  const [users, setUsers] = useState<ManagedUser[]>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void listUsers(controller.signal)
      .then(setUsers)
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setError(workspaceErrorMessage(failure));
      });
    return () => controller.abort();
  }, [refresh]);
  const save = async (user: ManagedUser, role: UserRole) => {
    const csrf = getCsrfToken();
    if (!csrf) {
      redirectToSso();
      return;
    }
    setBusy(user.user_id);
    setError(undefined);
    setNotice(undefined);
    try {
      await updateUserRole(user.user_id, role, csrf);
      setNotice(
        `已将 ${user.display_name || user.username} 设置为${ROLE_LABELS[role]}。`,
      );
      setRefresh((value) => value + 1);
    } catch (failure) {
      setError(workspaceErrorMessage(failure));
    } finally {
      setBusy(undefined);
    }
  };
  return (
    <main className="wst-workspace" id="main-content">
      <header className="wst-workspace-heading">
        <div>
          <button className="wst-text-action" type="button" onClick={onBack}>
            ← 返回问答
          </button>
          <h1>用户与角色</h1>
          <p>
            同事首次登录后会出现在这里。管理员可调整普通、人力角色；管理员任免由超级管理员处理。
          </p>
        </div>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => {
            setError(undefined);
            setRefresh((value) => value + 1);
          }}
        >
          刷新用户
        </button>
      </header>
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
      <section className="wst-library-panel" aria-label="用户角色管理">
        {!users && !error && (
          <p className="wst-workspace-empty" role="status">
            正在加载用户…
          </p>
        )}
        {users?.length === 0 && (
          <p className="wst-workspace-empty">暂时没有已登录的用户。</p>
        )}
        {users && users.length > 0 && (
          <div className="wst-workspace-table-scroll">
            <table className="wst-workspace-table">
              <thead>
                <tr>
                  <th scope="col">用户</th>
                  <th scope="col">所属部门</th>
                  <th scope="col">角色</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <UserRoleRow
                    key={`${user.user_id}:${user.role}`}
                    user={user}
                    busy={!!busy}
                    onSave={(role) => {
                      void save(user, role);
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

function UserRoleRow({
  user,
  busy,
  onSave,
}: {
  user: ManagedUser;
  busy: boolean;
  onSave: (role: UserRole) => void;
}) {
  const [role, setRole] = useState(user.role);
  return (
    <tr>
      <td>
        <strong>{user.display_name || user.username}</strong>
        <div className="wst-document-meta">
          {user.username} · {user.user_id}
        </div>
      </td>
      <td>{user.dept_id ? `部门 ID：${user.dept_id}` : "登录系统暂未提供"}</td>
      <td>
        {user.editable_roles.length === 0 ? (
          <div>
            <strong>
              {user.is_super_admin ? "超级管理员" : ROLE_LABELS[user.role]}
            </strong>
            <div className="wst-document-meta">{user.role_edit_note}</div>
          </div>
        ) : (
          <div className="wst-role-editor">
            <select
              aria-label={`${user.display_name || user.username}的角色`}
              value={role}
              disabled={busy}
              onChange={(event) =>
                setRole(event.currentTarget.value as UserRole)
              }
            >
              {user.editable_roles.map((value) => (
                <option key={value} value={value}>
                  {ROLE_LABELS[value]}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={busy || role === user.role}
              onClick={() => onSave(role)}
            >
              保存
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}
