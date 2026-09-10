import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  type CorpusAuthorizationStatus,
  type KnowledgeBaseModelSettings,
  type ProviderCatalog,
  type ProviderConnection,
} from "../api/client";
import { ErrorPanel, Modal } from "./ui";

export function KnowledgeBaseModels({ kbId }: { kbId: string }) {
  const [settings, setSettings] = useState<KnowledgeBaseModelSettings>();
  const [editing, setEditing] = useState(false);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    let active = true;
    void api
      .modelSettings(kbId)
      .then((value) => {
        if (active) setSettings(value);
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [kbId]);
  return (
    <>
      <div className="panel">
        <div className="section-heading">
          <div>
            <h3>知识库模型</h3>
            <p>
              {settings ? (
                <>
                  回答：
                  {settings.generation_model ?? "未配置，使用证据摘录回答"} ·
                  问题改写：{settings.rewrite_enabled ? "已开启" : "已关闭"} ·
                  图片识别：
                  {settings.ocr_enabled ? settings.ocr_model : "已关闭"}
                </>
              ) : error ? (
                "模型设置读取失败"
              ) : (
                "正在读取模型设置…"
              )}
            </p>
            {settings?.retrieval_data_plane && (
              <p role="status">
                当前检索：
                {retrievalLabel(settings.retrieval_data_plane)} · Embedding：
                {settings.retrieval_data_plane.embedding_provider_id ?? "未就绪"}
                {settings.retrieval_data_plane.embedding_model
                  ? ` / ${settings.retrieval_data_plane.embedding_model}`
                  : ""}
                {" · "}Reranker：
                {settings.retrieval_data_plane.reranker_provider_id ?? "未配置"}
                {settings.retrieval_data_plane.reranker_model
                  ? ` / ${settings.retrieval_data_plane.reranker_model}`
                  : ""}
              </p>
            )}
            {settings?.corpus_authorization && (
              <p role="status">
                资料授权：
                {authorizationLabel(settings.corpus_authorization)}
              </p>
            )}
          </div>
          <div className="row-actions">
            {settings?.retrieval_data_plane?.retrieval_data_plane ===
              "default_local_fallback" && (
              <a href={settings.retrieval_data_plane.remediation_path}>
                配置真实检索方案
              </a>
            )}
            {settings?.corpus_authorization &&
              settings.corpus_authorization.required_operations.length > 0 &&
              !authorizationReady(settings.corpus_authorization) && (
                <button onClick={() => setApproving(true)}>
                  批准当前活动资料
                </button>
              )}
            <button disabled={!settings} onClick={() => setEditing(true)}>
              设置回答与图片识别
            </button>
          </div>
        </div>
        {error !== undefined && <ErrorPanel error={error} />}
      </div>
      {editing && settings && (
        <ModelEditor
          kbId={kbId}
          initial={settings}
          onClose={() => setEditing(false)}
          onSaved={(value) => {
            setSettings(value);
            setEditing(false);
          }}
        />
      )}
      {approving && settings?.corpus_authorization && (
        <AuthorizationEditor
          kbId={kbId}
          status={settings.corpus_authorization}
          onClose={() => setApproving(false)}
          onApproved={(authorization) => {
            setSettings({
              ...settings,
              budget_campaign_id:
                authorization.manifest?.budget_campaign_id ??
                settings.budget_campaign_id,
              corpus_authorization: authorization,
            });
            setApproving(false);
          }}
        />
      )}
    </>
  );
}

function retrievalLabel(
  status: NonNullable<KnowledgeBaseModelSettings["retrieval_data_plane"]>,
): string {
  if (status.retrieval_data_plane === "default_local_fallback") {
    return "本地确定性检索";
  }
  if (status.profile_state === "PROFILE_INDEX_MISMATCH") {
    return "真实检索方案与活动索引不一致（查询会拒绝静默回退）";
  }
  if (!status.vector_coverage_complete) return "真实检索索引覆盖未完成";
  return "活动真实检索方案";
}

function authorizationReady(status: CorpusAuthorizationStatus): boolean {
  return (
    status.corpus_authorization_state === "APPROVED" &&
    status.model_authorization_state === "APPROVED" &&
    status.budget_state === "AVAILABLE"
  );
}

function authorizationLabel(status: CorpusAuthorizationStatus): string {
  if (status.required_operations.length === 0) return "当前配置不需要出网批准";
  if (authorizationReady(status)) {
    return `已批准当前版本，有效至 ${new Date(
      status.manifest?.expires_at ?? "",
    ).toLocaleString()}`;
  }
  const labels: Record<string, string> = {
    MISSING: "尚未批准当前活动语料",
    STALE_REVISION: "活动索引已变化，需要重新批准",
    STALE_CORPUS: "活动文档已变化，需要重新批准",
    STALE_MODEL: "模型或连接已变化，需要重新批准",
    PARTIAL: "当前模型用途未全部批准",
    EXPIRED: "批准已过期",
    EXHAUSTED: "累计预算已用尽",
    BLOCKED: "模型配置或预算当前不可用",
  };
  return (
    labels[status.corpus_authorization_state] ??
    labels[status.model_authorization_state] ??
    labels[status.budget_state] ??
    "当前不可用"
  );
}

function AuthorizationEditor({
  kbId,
  status,
  onClose,
  onApproved,
}: {
  kbId: string;
  status: CorpusAuthorizationStatus;
  onClose: () => void;
  onApproved: (status: CorpusAuthorizationStatus) => void;
}) {
  const [validDays, setValidDays] = useState(30);
  const [requestLimit, setRequestLimit] = useState(100);
  const [tokenLimit, setTokenLimit] = useState(500_000);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  async function approve(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError(undefined);
    const operations = status.required_operations;
    const eachLimit = Math.max(1, Math.floor(requestLimit / operations.length));
    try {
      onApproved(
        await api.approveCorpusAuthorization(kbId, {
          operations,
          expires_at: new Date(
            Date.now() + validDays * 24 * 60 * 60 * 1000,
          ).toISOString(),
          request_limit: requestLimit,
          estimated_token_limit: tokenLimit,
          operation_request_limits: Object.fromEntries(
            operations.map((operation) => [operation, eachLimit]),
          ),
        }),
      );
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }
  return (
    <Modal title="批准当前活动知识库资料" onClose={onClose}>
      <form className="stack model-settings-form" onSubmit={approve}>
        <p>
          服务端会在确认瞬间冻结当前活动 Revision、文档版本整体摘要、所选模型与用途；文档、模型或活动
          Revision 变化后不会自动跟随。
        </p>
        <p role="status">
          本次用途：{status.required_operations.map(operationLabel).join("、")}
        </p>
        <label>
          有效天数
          <input
            type="number"
            min={1}
            max={365}
            value={validDays}
            disabled={pending}
            onChange={(event) => setValidDays(Number(event.target.value))}
          />
        </label>
        <label>
          累计请求上限
          <input
            type="number"
            min={status.required_operations.length}
            max={10_000}
            value={requestLimit}
            disabled={pending}
            onChange={(event) => setRequestLimit(Number(event.target.value))}
          />
        </label>
        <label>
          累计估算 Token 上限
          <input
            type="number"
            min={1}
            max={100_000_000}
            value={tokenLimit}
            disabled={pending}
            onChange={(event) => setTokenLimit(Number(event.target.value))}
          />
        </label>
        <small>
          确认只创建这一份有界批准，不会立即调用 Provider，也不会由系统自动增加预算。
        </small>
        {error !== undefined && <ErrorPanel error={error} />}
        <div className="row-actions">
          <button className="primary" disabled={pending}>
            {pending ? "批准中…" : "确认批准当前版本"}
          </button>
          <button type="button" disabled={pending} onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    </Modal>
  );
}

function operationLabel(operation: string): string {
  return (
    {
      generation: "回答生成",
      "query.rewrite": "问题改写",
      "image.ocr": "图片识别",
    }[operation] ?? operation
  );
}

function ModelEditor({
  kbId,
  initial,
  onClose,
  onSaved,
}: {
  kbId: string;
  initial: KnowledgeBaseModelSettings;
  onClose: () => void;
  onSaved: (value: KnowledgeBaseModelSettings) => void;
}) {
  const [value, setValue] = useState(initial);
  const [catalog, setCatalog] = useState<ProviderCatalog>();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    let active = true;
    void Promise.all([api.providerCatalog(), api.listConnections()])
      .then(([models, page]) => {
        if (active) {
          setCatalog(models);
          setConnections(page.items);
        }
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, []);
  function choices(operation: string) {
    return connections
      .filter((connection) => connection.enabled !== false)
      .flatMap((connection) => {
        const provider = catalog?.providers.find(
          (entry) => entry.provider_type === connection.provider_type,
        );
        return (provider?.operation_models[operation] ?? []).map((model) => ({
          value: JSON.stringify([connection.connection_id, model]),
          label: `${connection.display_name} · ${model}`,
        }));
      });
  }
  function selected(connection: string | null, model: string | null) {
    return connection && model ? JSON.stringify([connection, model]) : "";
  }
  const generationChoices = choices("generation");
  const ocrChoices = choices("image.ocr");
  const generationSelection = selected(
    value.generation_connection_id,
    value.generation_model,
  );
  const ocrSelection = selected(value.ocr_connection_id, value.ocr_model);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError(undefined);
    try {
      onSaved(await api.saveModelSettings(kbId, value));
    } catch (reason) {
      setError(reason);
    } finally {
      setPending(false);
    }
  }
  return (
    <Modal title="知识库模型设置" onClose={onClose}>
      <form
        className="stack model-settings-form"
        onSubmit={(event) => void save(event)}
      >
        <p>
          保存只更新配置。启用后的问答与图片识别会使用对应服务，并受已有调用授权和预算限制。
        </p>
        <p role="status">
          已选择模型不等于连接已验证、资料已授权或本次已调用；请以查询结果与历史中的调用状态为准。
        </p>
        <label>
          回答模型
          <select
            value={generationSelection}
            disabled={pending || !catalog}
            onChange={(event) => {
              const [connection, model] = event.target.value
                ? (JSON.parse(event.target.value) as string[])
                : [null, null];
              setValue({
                ...value,
                generation_connection_id: connection ?? null,
                generation_model: model ?? null,
                rewrite_enabled: connection ? value.rewrite_enabled : false,
              });
            }}
          >
            <option value="">未配置 · 使用证据摘录回答</option>
            {generationSelection &&
              !generationChoices.some(
                (item) => item.value === generationSelection,
              ) && (
                <option value={generationSelection}>
                  当前配置不可用 · {value.generation_model}
                </option>
              )}
            {generationChoices.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={value.rewrite_enabled}
            disabled={pending || !value.generation_connection_id}
            onChange={(event) =>
              setValue({ ...value, rewrite_enabled: event.target.checked })
            }
          />
          启用问题改写
        </label>
        <label>
          图片识别模型
          <select
            value={ocrSelection}
            disabled={pending || !catalog}
            onChange={(event) => {
              const [connection, model] = event.target.value
                ? (JSON.parse(event.target.value) as string[])
                : [null, null];
              setValue({
                ...value,
                ocr_connection_id: connection ?? null,
                ocr_model: model ?? null,
                ocr_enabled: connection ? value.ocr_enabled : false,
              });
            }}
          >
            <option value="">未配置</option>
            {ocrSelection &&
              !ocrChoices.some((item) => item.value === ocrSelection) && (
                <option value={ocrSelection}>
                  当前配置不可用 · {value.ocr_model}
                </option>
              )}
            {ocrChoices.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={value.ocr_enabled}
            disabled={pending || !value.ocr_connection_id}
            onChange={(event) =>
              setValue({ ...value, ocr_enabled: event.target.checked })
            }
          />
          启用文档图片识别
        </label>
        <p>
          模型设置保存后，请回到知识库卡片明确批准当前活动资料；浏览器不需要管理 source hash。
        </p>
        {error !== undefined && <ErrorPanel error={error} />}
        <div className="row-actions">
          <button
            type="submit"
            className="primary"
            disabled={pending || !catalog}
          >
            {pending ? "保存中…" : "保存设置"}
          </button>
          <button type="button" disabled={pending} onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    </Modal>
  );
}
