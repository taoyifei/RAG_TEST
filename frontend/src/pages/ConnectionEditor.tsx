import { useState, type FormEvent } from "react";

import { api, type ProviderConnection } from "../api/client";
import { ErrorPanel } from "../components/ui";

export function ConnectionEditor({
  connection,
  onSaved,
  onCancel,
}: {
  connection: ProviderConnection;
  onSaved: () => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(connection.display_name);
  const [workspace, setWorkspace] = useState(connection.workspace_id ?? "");
  const [mode, setMode] = useState(
    connection.endpoint_mode ?? "workspace_host",
  );
  const [host, setHost] = useState(connection.api_host ?? "");
  const [requestBudget, setRequestBudget] = useState(
    connection.request_budget ?? 5,
  );
  const [tokenBudget, setTokenBudget] = useState(
    connection.token_budget ?? 4096,
  );
  const [enabled, setEnabled] = useState(connection.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>();
  const aliyun = connection.provider_type === "aliyun-model-studio";

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(undefined);
    try {
      await api.updateConnection(connection.connection_id, {
        expected_version: connection.configuration_version,
        display_name: name,
        enabled,
        request_budget: requestBudget,
        token_budget: tokenBudget,
        ...(aliyun
          ? {
              workspace_id: workspace,
              endpoint_mode: mode,
              api_host: host || null,
              region: "cn-beijing",
            }
          : {}),
      });
      await onSaved();
    } catch (reason) {
      setError(reason);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="panel form-grid" onSubmit={save}>
      <h3>编辑连接</h3>
      <p>密钥已保存，修改连接无需重新填写。保存仅修改配置；测试需单独发起。</p>
      {error !== undefined && <ErrorPanel error={error} />}
      <label>
        连接名称
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
      </label>
      {aliyun && (
        <>
          <label>
            工作空间标识
            <input
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
              required
            />
          </label>
          <label>
            端点模式
            <select
              value={mode}
              onChange={(e) => {
                setMode(e.target.value as typeof mode);
                setHost(
                  e.target.value === "beijing_dashscope"
                    ? "https://dashscope.aliyuncs.com"
                    : "",
                );
              }}
            >
              <option value="workspace_host">北京业务空间 API Host</option>
              <option value="beijing_dashscope">
                北京 DashScope（显式选择）
              </option>
            </select>
          </label>
          <label>
            API Host
            <input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              required={mode === "workspace_host"}
              placeholder="请从当前北京业务空间复制API Host"
            />
          </label>
          <p>
            地域：北京 cn-beijing。Workspace ID 仅为管理标识；北京 DashScope
            模式仍需验证 Key 与目标资源权限。
          </p>
        </>
      )}
      <label>
        请求预算
        <input
          type="number"
          min={1}
          max={20}
          value={requestBudget}
          onChange={(e) => setRequestBudget(Number(e.target.value))}
        />
      </label>
      <label>
        Token 预算
        <input
          type="number"
          min={1}
          max={1000000}
          value={tokenBudget}
          onChange={(e) => setTokenBudget(Number(e.target.value))}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        启用连接
      </label>
      <button disabled={saving}>{saving ? "正在保存…" : "保存修改"}</button>
      <button type="button" onClick={onCancel} disabled={saving}>
        取消
      </button>
    </form>
  );
}
