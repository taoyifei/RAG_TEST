import { useState, type FormEvent } from "react";
import { PlugZap } from "lucide-react";
import {
  api,
  type ProviderCatalog,
  type CredentialSummary,
} from "../api/client";
import { ErrorPanel, Modal } from "../components/ui";
type ProviderType = "jina" | "aliyun-model-studio";
type CredentialSource =
  | "database_encrypted"
  | "environment_managed"
  | "existing";
export function ConnectionCreator({
  catalog,
  credentials,
  onSaved,
  onCancel,
}: {
  catalog?: ProviderCatalog;
  credentials: CredentialSummary[];
  onSaved: () => Promise<void>;
  onCancel: () => void;
}) {
  const [provider, setProvider] = useState<ProviderType>("jina");
  const [source, setSource] = useState<CredentialSource>("database_encrypted");
  const [displayName, setDisplayName] = useState("Jina 主连接");
  const [credentialValue, setCredentialValue] = useState("");
  const [environmentName, setEnvironmentName] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [endpointMode, setEndpointMode] = useState("workspace_host");
  const [apiHost, setApiHost] = useState("");
  const [existingCredential, setExistingCredential] = useState("");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  async function createConnection(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api.createConnection({
        display_name: displayName,
        provider_type: provider,
        credential_id: source === "existing" ? existingCredential : undefined,
        credential:
          source === "existing"
            ? undefined
            : {
                provider_type: provider,
                source,
                environment_name:
                  source === "environment_managed"
                    ? environmentName
                    : undefined,
                secret_value:
                  source === "database_encrypted" ? credentialValue : undefined,
              },
        endpoint_mode:
          provider === "aliyun-model-studio" ? endpointMode : undefined,
        api_host:
          provider === "aliyun-model-studio" ? apiHost || null : undefined,
        workspace_id:
          provider === "aliyun-model-studio" ? workspaceId : undefined,
        region: provider === "aliyun-model-studio" ? "cn-beijing" : undefined,
      });
      setCredentialValue("");
      setEnvironmentName("");
      await onSaved();
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="新增连接"
      onClose={() => {
        if (!busy) onCancel();
      }}
      drawer
    >
      {error !== undefined && <ErrorPanel error={error} />}
      <form className="panel form-grid" onSubmit={createConnection}>
        <label>
          服务商
          <select
            value={provider}
            onChange={(event) => {
              const next = event.target.value as ProviderType;
              setProvider(next);
              setDisplayName(next === "jina" ? "Jina 主连接" : "百炼备用连接");
            }}
          >
            {catalog?.providers.map((item) => (
              <option key={item.provider_type} value={item.provider_type}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          连接名称
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            required
          />
        </label>
        <label>
          凭据来源
          <select
            value={source}
            onChange={(event) =>
              setSource(event.target.value as CredentialSource)
            }
          >
            <option value="database_encrypted">页面加密托管</option>
            <option value="existing">选择已有凭据</option>
            <option value="environment_managed">部署环境托管</option>
          </select>
        </label>
        {source === "database_encrypted" ? (
          <label>
            服务密钥
            <input
              type="password"
              value={credentialValue}
              onChange={(event) => setCredentialValue(event.target.value)}
              autoComplete="off"
              required
            />
          </label>
        ) : source === "existing" ? (
          <label>
            已有凭据
            <select
              value={existingCredential}
              onChange={(e) => setExistingCredential(e.target.value)}
              required
            >
              <option value="">请选择匹配服务商的凭据</option>
              {credentials
                .filter((item) => item.provider_type === provider)
                .map((item) => (
                  <option key={item.credential_id} value={item.credential_id}>
                    {item.masked_hint}
                  </option>
                ))}
            </select>
          </label>
        ) : (
          <label>
            环境变量名
            <input
              value={environmentName}
              onChange={(event) => setEnvironmentName(event.target.value)}
              placeholder="JINA_API_KEY"
              pattern="[A-Z][A-Z0-9_]+"
              required
            />
          </label>
        )}
        {provider === "aliyun-model-studio" && (
          <>
            <label>
              工作空间标识
              <input
                value={workspaceId}
                onChange={(event) => setWorkspaceId(event.target.value)}
                required
              />
            </label>
          </>
        )}
        {provider === "aliyun-model-studio" && (
          <>
            <label>
              端点模式
              <select
                value={endpointMode}
                onChange={(e) => {
                  setEndpointMode(e.target.value);
                  setApiHost(
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
                value={apiHost}
                onChange={(e) => setApiHost(e.target.value)}
                required={endpointMode === "workspace_host"}
                placeholder="请从当前北京业务空间复制API Host"
              />
            </label>
            <p>Key 非空仅代表本地形状检查；端点选择不代表鉴权成功。</p>
          </>
        )}
        <button className="primary" disabled={busy}>
          <PlugZap aria-hidden="true" size={17} />
          {busy ? "正在保存…" : "保存连接"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          取消
        </button>
      </form>
    </Modal>
  );
}
