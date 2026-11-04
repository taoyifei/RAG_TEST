import { useEffect, useState } from "react";

import { redirectToSso } from "./authNavigation";
import {
  listUsers,
  updateUserPersonalQuota,
  updateUserRole,
  type ManagedUser,
  type PersonalQuotaLimits,
  type UserRole,
} from "./libraryApi";
import { workspaceErrorMessage } from "./LibraryWorkspace";

const ROLE_LABELS: Record<UserRole, string> = {
  user: "普通用户",
  hr: "人力角色",
  admin: "系统管理员",
};
const MEBIBYTE = 1024 * 1024;

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
  const saveQuota = async (user: ManagedUser, limits: PersonalQuotaLimits) => {
    const csrf = getCsrfToken();
    if (!csrf) {
      redirectToSso();
      return;
    }
    setBusy(`quota:${user.user_id}`);
    setError(undefined);
    setNotice(undefined);
    try {
      const saved = await updateUserPersonalQuota(user.user_id, limits, csrf);
      setUsers((current) =>
        current?.map((item) =>
          item.user_id === user.user_id
            ? { ...item, personal_quota: saved }
            : item,
        ),
      );
      setNotice(
        `已更新 ${user.display_name || user.username} 的个人资料额度。`,
      );
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
            包含已登录用户及旧版问答历史中的用户。管理员可调整普通、人力角色；管理员任免由超级管理员处理。
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
        {users?.some((user) => user.personal_quota) && (
          <p className="wst-document-meta">
            个人资料额度仅限超级管理员调整，调整额度不会开放个人文件的查看权限。
          </p>
        )}
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
            <table className="wst-workspace-table wst-user-table">
              <thead>
                <tr>
                  <th scope="col">用户</th>
                  <th scope="col">所属部门</th>
                  <th scope="col">角色</th>
                  <th scope="col">个人资料额度</th>
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
                    onSaveQuota={(limits) => {
                      void saveQuota(user, limits);
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
  onSaveQuota,
}: {
  user: ManagedUser;
  busy: boolean;
  onSave: (role: UserRole) => void;
  onSaveQuota: (limits: PersonalQuotaLimits) => void;
}) {
  const [role, setRole] = useState(user.role);
  return (
    <tr>
      <td>
        <strong>{user.display_name || user.username}</strong>
        <div className="wst-document-meta">
          {user.username !== user.user_id ? `${user.username} · ` : ""}
          用户 ID：{user.user_id}
        </div>
        {user.username === user.user_id && (
          <div className="wst-document-meta">登录账号待本人下次登录同步</div>
        )}
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
      <td>
        {user.personal_quota ? (
          user.can_edit_personal_quota === true ? (
            <PersonalQuotaEditor
              key={`${user.personal_quota.max_bytes}:${user.personal_quota.max_files}`}
              user={user}
              limits={user.personal_quota}
              busy={busy}
              onSave={onSaveQuota}
            />
          ) : (
            <span>
              {`${(user.personal_quota.max_bytes / MEBIBYTE).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} MB · ${user.personal_quota.max_files} 份`}
            </span>
          )
        ) : (
          <span className="wst-document-meta">尚未同步</span>
        )}
      </td>
    </tr>
  );
}

function PersonalQuotaEditor({
  user,
  limits,
  busy,
  onSave,
}: {
  user: ManagedUser;
  limits: PersonalQuotaLimits;
  busy: boolean;
  onSave: (limits: PersonalQuotaLimits) => void;
}) {
  const [capacityMb, setCapacityMb] = useState(
    String(limits.max_bytes / MEBIBYTE),
  );
  const [files, setFiles] = useState(String(limits.max_files));
  const [error, setError] = useState<string>();
  const capacity = Number(capacityMb);
  const fileCount = Number(files);
  const changed =
    capacity * MEBIBYTE !== limits.max_bytes || fileCount !== limits.max_files;
  const name = user.display_name || user.username;
  return (
    <form
      className="wst-quota-limit-editor"
      aria-label={`${name}的个人资料额度`}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (
          !Number.isInteger(capacity) ||
          capacity <= 0 ||
          !Number.isSafeInteger(capacity * MEBIBYTE) ||
          !Number.isSafeInteger(fileCount) ||
          fileCount <= 0
        ) {
          setError("容量和份数请填写大于0的整数。");
          return;
        }
        setError(undefined);
        onSave({ max_bytes: capacity * MEBIBYTE, max_files: fileCount });
      }}
    >
      <div className="wst-quota-limit-fields">
        <label>
          <span>容量（MB）</span>
          <input
            type="number"
            inputMode="numeric"
            min="1"
            step="1"
            aria-label={`${name}的个人空间（MB）`}
            value={capacityMb}
            disabled={busy}
            onChange={(event) => setCapacityMb(event.currentTarget.value)}
          />
        </label>
        <label>
          <span>最多份数</span>
          <input
            type="number"
            inputMode="numeric"
            min="1"
            step="1"
            aria-label={`${name}的个人资料份数`}
            value={files}
            disabled={busy}
            onChange={(event) => setFiles(event.currentTarget.value)}
          />
        </label>
      </div>
      <button type="submit" disabled={busy || !changed}>
        保存额度
      </button>
      {error && (
        <p className="wst-document-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
