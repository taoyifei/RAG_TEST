import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, Plus } from "lucide-react";
import {
  api,
  type CredentialSummary,
  type ProviderCatalog,
  type ProviderConnection,
  type ProviderValidation,
  type ProviderUsageDaily,
} from "../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../components/ui";
import { operationLabel, validationMessage } from "../copy/zh-CN";
import { ConnectionCreator } from "./ConnectionCreator";
import { ConnectionEditor } from "./ConnectionEditor";

type Probe = {
  connection: ProviderConnection;
  operation: string;
  model: string;
};

export function ModelServicesPage() {
  const [credentials, setCredentials] = useState<CredentialSummary[]>([]);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog>();
  const [usage, setUsage] = useState<ProviderUsageDaily[]>([]);
  const [history, setHistory] = useState<Record<string, ProviderValidation[]>>(
    {},
  );
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ProviderConnection>();
  const [records, setRecords] = useState<ProviderConnection>();
  const [confirmation, setConfirmation] = useState<Probe>();
  const [error, setError] = useState<unknown>();
  const pendingRef = useRef(new Set<string>());
  const [pending, setPending] = useState<string[]>([]);
  const apply = useCallback(
    (data: Awaited<ReturnType<typeof readConnections>>) => {
      setCatalog(data.catalog);
      setCredentials(data.credentials);
      setConnections(data.connections);
      setHistory(data.history);
      setUsage(data.usage);
    },
    [],
  );
  const load = useCallback(() => readConnections().then(apply), [apply]);
  useEffect(() => {
    let active = true;
    void readConnections()
      .then((data) => {
        if (active) apply(data);
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [apply]);
  async function validate(probe: Probe) {
    const key = `${probe.connection.connection_id}:${probe.operation}`;
    if (pendingRef.current.has(key)) return;
    pendingRef.current.add(key);
    setPending([...pendingRef.current]);
    setConfirmation(undefined);
    setError(undefined);
    try {
      await api.validateConnection(probe.connection.connection_id, {
        operation: probe.operation,
        model: probe.model,
        expected_dimension: probe.operation === "reranking" ? null : 1024,
      });
      await load();
    } catch (reason) {
      setError(reason);
    } finally {
      pendingRef.current.delete(key);
      setPending([...pendingRef.current]);
    }
  }
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>模型服务</h2>
          <p>一处配置，供知识库检索使用。</p>
        </div>
        <div className="row-actions">
          <button
            className="secondary"
            onClick={() => void load().catch(setError)}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新
          </button>
          <button className="primary" onClick={() => setCreating(true)}>
            <Plus size={16} aria-hidden="true" />
            新增连接
          </button>
        </div>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <div className="connection-list">
        {connections.map((connection) => {
          const credential = credentials.find(
            (item) => item.credential_id === connection.credential_id,
          );
          const provider = catalog?.providers.find(
            (item) => item.provider_type === connection.provider_type,
          );
          const runs = history[connection.connection_id] ?? [];
          return (
            <article className="connection-row" key={connection.connection_id}>
              <div className="section-heading">
                <div>
                  <h3>{connection.display_name}</h3>
                  <p>
                    {provider?.display_name ?? connection.provider_type} ·{" "}
                    {connection.region === "cn-beijing" ? "北京" : "默认地域"} ·{" "}
                    {connection.enabled === false ? "已停用" : "已保存"}
                  </p>
                  <small>
                    密钥：{credential?.masked_hint ?? "未配置"} ·{" "}
                    {credential?.configured ? "已保存" : "本地配置待完善"}
                  </small>
                </div>
                <div className="row-actions">
                  <button onClick={() => setEditing(connection)}>
                    编辑连接
                  </button>
                  <details>
                    <summary>更多</summary>
                    <div className="stack">
                      <button onClick={() => setRecords(connection)}>
                        验证记录
                      </button>
                      <button onClick={() => setEditing(connection)}>
                        轮换密钥 / 停用
                      </button>
                      <code>{connection.connection_id}</code>
                    </div>
                  </details>
                </div>
              </div>
              <div className="capability-list">
                {(provider?.operations ?? []).map((operation) => {
                  const model = provider?.operation_models?.[operation]?.[0];
                  const run = runs.find(
                    (item) =>
                      item.operation === operation &&
                      item.provider_model === model,
                  );
                  const stale =
                    run &&
                    (run.is_current === false ||
                      run.configuration_version !==
                        connection.configuration_version ||
                      run.credential_key_version !== credential?.key_version ||
                      run.catalog_version !== catalog?.catalog_version);
                  const incomplete =
                    connection.provider_type === "aliyun-model-studio" &&
                    connection.endpoint_mode !== "beijing_dashscope" &&
                    !connection.api_host;
                  const status = incomplete
                    ? "configuration_incomplete"
                    : stale
                      ? "needs_retest"
                      : run
                        ? run.status === "succeeded"
                          ? run.validation_mode === "mock"
                            ? "mock_validated"
                            : run.validation_mode === "live"
                              ? "live_validated"
                              : "not_verified"
                          : "failed"
                        : "not_verified";
                  const key = `${connection.connection_id}:${operation}`;
                  return (
                    <div className="capability" key={operation}>
                      <strong>{operationLabel(operation)}</strong>
                      <StatusBadge value={status} />
                      <small>
                        {run
                          ? `最近测试：${new Date(run.finished_at).toLocaleString("zh-CN")}`
                          : "尚未测试"}
                      </small>
                      {incomplete && (
                        <p className="failure-message">
                          配置尚未完整，请补充 API Host。本次未发送请求。
                        </p>
                      )}
                      {run?.status === "failed" && !stale && (
                        <p className="failure-message">
                          {validationMessage(run)}
                        </p>
                      )}
                      <button
                        disabled={
                          !model ||
                          connection.enabled === false ||
                          pending.includes(key)
                        }
                        onClick={() =>
                          model &&
                          setConfirmation({ connection, operation, model })
                        }
                      >
                        {pending.includes(key)
                          ? "测试中…"
                          : `测试${operationLabel(operation)}`}
                      </button>
                    </div>
                  );
                })}
              </div>
            </article>
          );
        })}
      </div>
      {!connections.length && (
        <EmptyState title="尚未配置模型服务">
          点击“新增连接”配置检索所需的服务。
        </EmptyState>
      )}
      <details className="panel">
        <summary>每日调用与费用边界</summary>
        <p>按 UTC 日聚合脱敏计数，不记录文档或查询正文。</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>日期</th>
                <th>连接</th>
                <th>用途</th>
                <th>请求</th>
                <th>Token（估算 / 实测）</th>
              </tr>
            </thead>
            <tbody>
              {usage.map((item) => (
                <tr
                  key={`${item.usage_date}:${item.connection_id}:${item.operation}`}
                >
                  <td>{item.usage_date}</td>
                  <td>
                    {connections.find(
                      (connection) =>
                        connection.connection_id === item.connection_id,
                    )?.display_name ?? "未知连接"}
                  </td>
                  <td>{operationLabel(item.operation)}</td>
                  <td>
                    {item.request_count}（成功 {item.successful_requests} / 失败{" "}
                    {item.failed_requests}）
                  </td>
                  <td>
                    {item.estimated_tokens} / {item.observed_tokens}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!usage.length && <EmptyState title="尚无调用记录" />}
      </details>
      {creating && (
        <ConnectionCreator
          catalog={catalog}
          credentials={credentials}
          onCancel={() => setCreating(false)}
          onSaved={async () => {
            await load();
            setCreating(false);
          }}
        />
      )}
      {editing && (
        <ConnectionEditor
          key={editing.connection_id}
          connection={editing}
          credential={credentials.find(
            (item) => item.credential_id === editing.credential_id,
          )}
          onCancel={() => {
            setEditing(undefined);
          }}
          onCredentialRotated={load}
          onSaved={async () => {
            await load();
            setEditing(undefined);
          }}
        />
      )}
      {confirmation && (
        <Modal title="确认连接测试" onClose={() => setConfirmation(undefined)}>
          <p>
            只发送公开短文本，不会发送知识库文档。本次预计操作数：1，可能消耗服务额度。
          </p>
          <p>累计预算以服务端为准，保存配置不会重置验收预算。</p>
          <button
            className="primary"
            onClick={() => void validate(confirmation)}
          >
            开始测试
          </button>
          <button onClick={() => setConfirmation(undefined)}>取消</button>
        </Modal>
      )}
      {records && (
        <Modal
          title={`${records.display_name} · 验证记录`}
          onClose={() => setRecords(undefined)}
          drawer
        >
          {(history[records.connection_id] ?? []).map((run) => (
            <section className="panel" key={run.validation_id}>
              <h3>
                {operationLabel(run.operation)} · {run.provider_model}
              </h3>
              <p>{validationMessage(run)}</p>
              <small>{new Date(run.finished_at).toLocaleString("zh-CN")}</small>
              <details>
                <summary>技术详情</summary>
                <pre>
                  {JSON.stringify(
                    {
                      http_status: run.http_status,
                      provider_code: run.provider_code,
                      request_id: run.provider_request_id,
                      operation: run.operation,
                      time: run.finished_at,
                      request_dispatched: run.request_dispatched,
                      validation_mode: run.validation_mode,
                      request_policy_identity: run.request_policy_identity,
                    },
                    null,
                    2,
                  )}
                </pre>
                <p>可选中文本复制；Mock 记录表示离线模拟。</p>
              </details>
            </section>
          ))}
          {!history[records.connection_id]?.length && (
            <EmptyState title="尚无验证记录" />
          )}
        </Modal>
      )}
    </section>
  );
}

async function readConnections() {
  const [catalog, credentials, connections, usage] = await Promise.all([
    api.providerCatalog(),
    api.listCredentials(),
    api.listConnections(),
    api.listDailyProviderUsage(),
  ]);
  const history = await Promise.all(
    connections.items.map(
      async (connection) =>
        [
          connection.connection_id,
          (await api.listValidations(connection.connection_id)).items,
        ] as const,
    ),
  );
  return {
    catalog,
    credentials: credentials.items,
    connections: connections.items,
    usage: usage.items,
    history: Object.fromEntries(history),
  };
}
