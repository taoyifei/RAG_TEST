import { KeyRound, PlugZap, RefreshCw } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import {
  api,
  type CredentialSummary,
  type ProviderCatalog,
  type ProviderConnection,
  type ProviderUsageDaily,
  type ProviderValidation,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { ConnectionEditor } from "./ConnectionEditor";

type ProviderType = "jina" | "aliyun-model-studio";
type CredentialSource =
  | "database_encrypted"
  | "environment_managed"
  | "existing";

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
  const [dailyUsage, setDailyUsage] = useState<ProviderUsageDaily[]>([]);
  const [provider, setProvider] = useState<ProviderType>("jina");
  const [source, setSource] = useState<CredentialSource>("database_encrypted");
  const [displayName, setDisplayName] = useState("Jina 主连接");
  const [credentialValue, setCredentialValue] = useState("");
  const [environmentName, setEnvironmentName] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [endpointMode, setEndpointMode] = useState("workspace_host");
  const [apiHost, setApiHost] = useState("");
  const [existingCredential, setExistingCredential] = useState("");
  const [editing, setEditing] = useState<ProviderConnection>();
  const pendingRef = useRef(new Set<string>());
  const [pending, setPending] = useState<string[]>([]);
  const [rotationId, setRotationId] = useState("");
  const [rotationValue, setRotationValue] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const [providerCatalog, credentialPage, connectionPage, usagePage] =
      await Promise.all([
        api.providerCatalog(),
        api.listCredentials(),
        api.listConnections(),
        api.listDailyProviderUsage(),
      ]);
    setCatalog(providerCatalog);
    setCredentials(credentialPage.items);
    setConnections(connectionPage.items);
    setDailyUsage(usagePage.items);
  }, []);
  useEffect(() => {
    let active = true;
    void Promise.all([
      api.providerCatalog(),
      api.listCredentials(),
      api.listConnections(),
      api.listDailyProviderUsage(),
    ])
      .then(([providerCatalog, credentialPage, connectionPage, usagePage]) => {
        if (!active) return;
        setCatalog(providerCatalog);
        setCredentials(credentialPage.items);
        setConnections(connectionPage.items);
        setDailyUsage(usagePage.items);
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
    const key = `${connection.connection_id}:${operation}`;
    if (pendingRef.current.has(key)) return;
    pendingRef.current.add(key);
    setPending([...pendingRef.current]);
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
    } finally {
      pendingRef.current.delete(key);
      setPending([...pendingRef.current]);
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
      </form>
      {editing && (
        <ConnectionEditor
          key={editing.connection_id}
          connection={editing}
          onSaved={async () => {
            await load();
            setEditing(undefined);
          }}
          onCancel={() => setEditing(undefined)}
        />
      )}
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
              <button onClick={() => setEditing(connection)}>编辑连接</button>
              {catalogOperations(catalog, connection.provider_type).map(
                ([operation, model, label]) => (
                  <button
                    key={operation}
                    disabled={
                      connection.enabled === false ||
                      pending.includes(
                        `${connection.connection_id}:${operation}`,
                      )
                    }
                    onClick={() => void validate(connection, operation, model)}
                  >
                    {pending.includes(
                      `${connection.connection_id}:${operation}`,
                    )
                      ? "测试中…"
                      : `测试${label}`}
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
                {item.provider_type} · {item.masked_hint} · 第{item.key_version}
                版
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
                    <td>
                      {validationMessage(item)}
                      <br />
                      {item.http_status ? `HTTP ${item.http_status}` : ""}{" "}
                      {item.provider_code}
                      <br />
                      {item.provider_request_id}
                    </td>
                    <td>
                      {new Date(item.finished_at).toLocaleString("zh-CN")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <section className="panel">
        <h3>每日调用与费用边界</h3>
        <p>仅显示脱敏计数；不记录查询、文档、向量、密钥或响应正文。</p>
        {!dailyUsage.length ? (
          <EmptyState title="尚无 Provider 调用记录">
            连接验证、索引或查询发生后会按 UTC 日聚合。
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>UTC 日期</th>
                  <th>连接 / 操作</th>
                  <th>请求</th>
                  <th>Token</th>
                  <th>平均耗时</th>
                  <th>重试 / 限流 / 切换</th>
                </tr>
              </thead>
              <tbody>
                {dailyUsage.map((item) => (
                  <tr
                    key={`${item.usage_date}:${item.connection_id}:${item.operation}`}
                  >
                    <td>{item.usage_date}</td>
                    <td>
                      <code>{item.connection_id}</code>
                      <br />
                      {item.operation}
                    </td>
                    <td>
                      {item.request_count}（成功 {item.successful_requests} /
                      失败 {item.failed_requests}）
                    </td>
                    <td>
                      估算 {item.estimated_tokens} / 实测 {item.observed_tokens}
                    </td>
                    <td>{item.average_latency_ms} ms</td>
                    <td>
                      {item.retry_count} / {item.rate_limit_count} /{" "}
                      {item.failover_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  );
}

function validationMessage(item: ProviderValidation): string {
  if (item.request_dispatched === false) return "本地配置未通过，请求未发出";
  if (item.http_category === "connect_error") return "DNS/TLS 连接失败";
  if (item.http_status === 401) return "HTTP 鉴权失败";
  if (item.http_status === 403) return "权限或模型访问被拒绝，请核对资源授权";
  if (item.status === "failed" && item.stage === "response_contract")
    return "响应不兼容";
  return item.safe_error_code ?? item.http_category;
}
