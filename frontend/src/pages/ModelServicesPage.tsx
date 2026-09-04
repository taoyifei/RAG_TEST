import { KeyRound, PlugZap, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  api,
  type CredentialSummary,
  type ProviderCatalog,
  type ProviderConnection,
  type ProviderValidation,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";

type ProviderType = "jina" | "aliyun-model-studio";
type CredentialSource = "database_encrypted" | "environment_managed";

const operationLabels: Record<string, string> = {
  "embedding.document": "文档向量",
  "embedding.query": "查询向量",
  reranking: "结果重排",
};

function catalogOperations(
  catalog: ProviderCatalog | undefined,
  providerType: ProviderType,
): [string, string, string][] {
  const provider = catalog?.providers.find(
    (item) => item.provider_type === providerType,
  );
  if (!provider) return [];
  return provider.operations.flatMap((operation) => {
    const reranking = operation === "reranking";
    const model = provider.models.find(
      (item) => item.includes("reranker") === reranking,
    );
    return model ? [[operation, model, operationLabels[operation]]] : [];
  });
}

export function ModelServicesPage() {
  const [credentials, setCredentials] = useState<CredentialSummary[]>([]);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog>();
  const [validations, setValidations] = useState<ProviderValidation[]>([]);
  const [provider, setProvider] = useState<ProviderType>("jina");
  const [source, setSource] =
    useState<CredentialSource>("database_encrypted");
  const [displayName, setDisplayName] = useState("Jina 主连接");
  const [credentialValue, setCredentialValue] = useState("");
  const [environmentName, setEnvironmentName] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [rotationId, setRotationId] = useState("");
  const [rotationValue, setRotationValue] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const [providerCatalog, credentialPage, connectionPage] = await Promise.all([
      api.providerCatalog(),
      api.listCredentials(),
      api.listConnections(),
    ]);
    setCatalog(providerCatalog);
    setCredentials(credentialPage.items);
    setConnections(connectionPage.items);
  }, []);
  useEffect(() => {
    let active = true;
    void Promise.all([
      api.providerCatalog(),
      api.listCredentials(),
      api.listConnections(),
    ])
      .then(([providerCatalog, credentialPage, connectionPage]) => {
        if (!active) return;
        setCatalog(providerCatalog);
        setCredentials(credentialPage.items);
        setConnections(connectionPage.items);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [load]);

  async function createConnection(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const credential = await api.createCredential({
        provider_type: provider,
        source,
        environment_name:
          source === "environment_managed" ? environmentName : undefined,
        secret_value:
          source === "database_encrypted" ? credentialValue : undefined,
      });
      await api.createConnection({
        display_name: displayName,
        provider_type: provider,
        credential_id: credential.credential_id,
        workspace_id:
          provider === "aliyun-model-studio" ? workspaceId : undefined,
        region:
          provider === "aliyun-model-studio" ? "cn-beijing" : undefined,
      });
      setCredentialValue("");
      setEnvironmentName("");
      await load();
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function validate(
    connection: ProviderConnection,
    operation: string,
    model: string,
  ) {
    setError(undefined);
    try {
      await api.validateConnection(connection.connection_id, {
        operation,
        model,
        expected_dimension: operation === "reranking" ? null : 1024,
      });
      const history = await api.listValidations(connection.connection_id);
      setValidations(history.items);
      await load();
    } catch (reason) {
      setError(reason);
    }
  }

  async function rotate(event: FormEvent) {
    event.preventDefault();
    try {
      await api.rotateCredential(rotationId, rotationValue);
      setRotationValue("");
      await load();
    } catch (reason) {
      setError(reason);
    }
  }

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <span className="eyebrow">全局连接</span>
          <h2>模型服务</h2>
          <p>凭据与知识库方案分离。连接测试只发送公开合成文本。</p>
        </div>
        <button className="secondary" onClick={() => void load()}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <form className="panel form-grid" onSubmit={createConnection}>
        <h3>新增模型连接</h3>
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
          <label>
            工作空间标识
            <input
              value={workspaceId}
              onChange={(event) => setWorkspaceId(event.target.value)}
              required
            />
          </label>
        )}
        <button className="primary" disabled={busy}>
          <PlugZap aria-hidden="true" size={17} />
          {busy ? "正在保存…" : "保存连接"}
        </button>
      </form>
      <div className="card-list">
        {connections.map((connection) => (
          <article key={connection.connection_id} className="provider-card">
            <div>
              <h3>{connection.display_name}</h3>
              <p>
                {connection.provider_type === "jina" ? "Jina" : "阿里云百炼"}
              </p>
              <code>{connection.connection_id}</code>
            </div>
            <StatusBadge value={connection.status} />
            <div className="row-actions">
              {catalogOperations(catalog, connection.provider_type).map(
                ([operation, model, label]) => (
                  <button
                    key={operation}
                    onClick={() => void validate(connection, operation, model)}
                  >
                    测试{label}
                  </button>
                ),
              )}
              <button
                onClick={() =>
                  void api
                    .listValidations(connection.connection_id)
                    .then((page) => setValidations(page.items))
                    .catch(setError)
                }
              >
                验证记录
              </button>
            </div>
          </article>
        ))}
      </div>
      {!connections.length && (
        <EmptyState title="尚未配置模型服务">
          先建立 Jina 主连接，再建立阿里云百炼备用连接。
        </EmptyState>
      )}
      <form className="panel inline-form" onSubmit={rotate}>
        <KeyRound aria-hidden="true" size={18} />
        <label className="sr-only" htmlFor="rotation-credential">
          待轮换凭据
        </label>
        <select
          id="rotation-credential"
          value={rotationId}
          onChange={(event) => setRotationId(event.target.value)}
          required
        >
          <option value="">选择页面托管凭据</option>
          {credentials
            .filter((item) => item.source === "database_encrypted")
            .map((item) => (
              <option key={item.credential_id} value={item.credential_id}>
                {item.provider_type} · {item.masked_hint} · 第{item.key_version}版
              </option>
            ))}
        </select>
        <label className="sr-only" htmlFor="rotation-value">
          新服务密钥
        </label>
        <input
          id="rotation-value"
          type="password"
          value={rotationValue}
          onChange={(event) => setRotationValue(event.target.value)}
          placeholder="新服务密钥"
          autoComplete="off"
          required
        />
        <button>轮换密钥</button>
      </form>
      {!!validations.length && (
        <section className="panel">
          <h3>最近验证记录</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>能力</th>
                  <th>模型</th>
                  <th>状态</th>
                  <th>响应类别</th>
                  <th>完成时间</th>
                </tr>
              </thead>
              <tbody>
                {validations.map((item) => (
                  <tr key={item.validation_id}>
                    <td>{item.operation}</td>
                    <td>{item.provider_model}</td>
                    <td>
                      <StatusBadge value={item.status} />
                    </td>
                    <td>{item.http_category}</td>
                    <td>{new Date(item.finished_at).toLocaleString("zh-CN")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  );
}
