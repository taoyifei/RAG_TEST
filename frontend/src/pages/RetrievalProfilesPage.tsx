import { useJobPolling } from "../hooks/use-job-polling";
import { AlertTriangle, CheckCircle2, SlidersHorizontal } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import {
  api,
  type ImpactPreview,
  type ProviderCatalog,
  type ProviderConnection,
  type RetrievalAuthorizationStatus,
  type RetrievalProfile,
} from "../api/client";
import { EmptyState, ErrorPanel, Modal, StatusBadge } from "../components/ui";
import { zhCN } from "../copy/zh-CN";
import { useConsole } from "../state/console-context";

export function RetrievalProfilesPage() {
  const { scope } = useConsole();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog>();
  const [profiles, setProfiles] = useState<RetrievalProfile[]>([]);
  const [primary, setPrimary] = useState("");
  const [primaryModel, setPrimaryModel] = useState("");
  const [primaryDimension, setPrimaryDimension] = useState(1024);
  const [standby, setStandby] = useState("");
  const [standbyModel, setStandbyModel] = useState("");
  const [standbyDimension, setStandbyDimension] = useState(1024);
  const [reranker, setReranker] = useState("");
  const [rerankerModel, setRerankerModel] = useState("");
  const [instruction, setInstruction] = useState("");
  const [minimumSupport, setMinimumSupport] = useState(1);
  const [maxEvidence, setMaxEvidence] = useState(8);
  const [tokenBudget, setTokenBudget] = useState(1024);
  const [base, setBase] = useState<RetrievalProfile>();
  const [standbyRequests, setStandbyRequests] = useState<number>();
  const [standbyTokens, setStandbyTokens] = useState<number>();
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [activationJob, setActivationJob] = useState<string>();
  const [confirmTest, setConfirmTest] = useState(false);
  const [confirmAuthorization, setConfirmAuthorization] = useState(false);
  const [retrievalAuthorization, setRetrievalAuthorization] =
    useState<RetrievalAuthorizationStatus>();
  const [failover, setFailover] = useState(true);
  const [validationMessage, setValidationMessage] = useState("");
  const [draft, setDraft] = useState<RetrievalProfile>();
  const [preview, setPreview] = useState<ImpactPreview>();
  const [error, setError] = useState<unknown>();
  const load = useCallback(async () => {
    const [providerCatalog, connectionPage] = await Promise.all([
      api.providerCatalog(),
      api.listConnections(),
    ]);
    setCatalog(providerCatalog);
    setConnections(connectionPage.items);
    if (scope.kbId) {
      const profilePage = await api.listRetrievalProfiles(scope.kbId);
      setProfiles(profilePage.items);
    }
  }, [scope.kbId]);
  useEffect(() => {
    let active = true;
    void Promise.all([api.providerCatalog(), api.listConnections()])
      .then(([providerCatalog, connectionPage]) => {
        if (active) {
          setCatalog(providerCatalog);
          setConnections(connectionPage.items);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason);
      });
    if (scope.kbId) {
      void api
        .listRetrievalProfiles(scope.kbId)
        .then((page) => {
          if (active) setProfiles(page.items);
        })
        .catch((reason: unknown) => {
          if (active) setError(reason);
        });
    }
    return () => {
      active = false;
    };
  }, [scope.kbId]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    try {
      const primaryConnection = requireConnection(connections, primary);
      const standbyConnection = standby
        ? requireConnection(connections, standby)
        : undefined;
      const rerankerConnection = reranker
        ? requireConnection(connections, reranker)
        : undefined;
      const resolvedPrimaryModel = selectedModel(
        catalog,
        primaryConnection,
        "embedding.document",
        primaryModel,
      );
      const resolvedStandbyModel = standbyConnection
        ? selectedModel(
            catalog,
            standbyConnection,
            "embedding.document",
            standbyModel,
          )
        : null;
      const resolvedRerankerModel = rerankerConnection
        ? selectedModel(catalog, rerankerConnection, "reranking", rerankerModel)
        : null;
      const primaryPolicies = embeddingPolicies(
        primaryConnection,
        base?.primary_connection_id === primary
          ? {
              document: base.primary_document_policy,
              query: base.primary_query_policy,
            }
          : undefined,
      );
      const standbyPolicies = standbyConnection
        ? embeddingPolicies(
            standbyConnection,
            base?.standby_connection_id === standby
              ? {
                  document: base.standby_document_policy,
                  query: base.standby_query_policy,
                }
              : undefined,
            instruction,
          )
        : { document: {}, query: {} };
      const next = await api.createRetrievalProfile(scope.kbId, {
        primary_connection_id: primary,
        primary_embedding_model: resolvedPrimaryModel,
        primary_dimension: primaryDimension,
        primary_document_policy: primaryPolicies.document,
        primary_query_policy: primaryPolicies.query,
        standby_connection_id: standby || null,
        standby_embedding_model: resolvedStandbyModel,
        standby_dimension: standby ? standbyDimension : null,
        standby_document_policy: standbyPolicies.document,
        standby_query_policy: standbyPolicies.query,
        reranker_connection_id: reranker || null,
        reranker_model: resolvedRerankerModel,
        failover_enabled: Boolean(standby) && failover,
        standby_budget: standby
          ? {
              requests:
                standbyRequests ??
                connections.find((item) => item.connection_id === standby)
                  ?.request_budget,
              tokens:
                standbyTokens ??
                connections.find((item) => item.connection_id === standby)
                  ?.token_budget,
            }
          : {},
        evidence_policy: base?.evidence_policy ?? {},
        retrieval_policy: {
          ...base?.retrieval_policy,
          rrf_k: base?.retrieval_policy.rrf_k ?? 60,
          minimum_support_items: minimumSupport,
          max_evidence_items: maxEvidence,
          evidence_token_budget: tokenBudget,
        },
      });
      const impact = await api.previewRetrievalProfile(
        next.profile_revision_id,
      );
      const authorization = await api.retrievalAuthorization(
        next.profile_revision_id,
      );
      setDraft(next);
      setRetrievalAuthorization(authorization);
      setValidationMessage("");
      setPreview(impact);
      await load();
    } catch (reason) {
      setError(reason);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  async function activate() {
    if (!draft || !preview || lock.current) return;
    if (!retrievalAuthorizationReady(retrievalAuthorization)) {
      setError(new Error("请先完成检索授权、累计预算和连接预算检查。"));
      return;
    }
    lock.current = true;
    setBusy(true);
    try {
      const activated = await api.activateRetrievalProfile(
        draft.profile_revision_id,
        preview.impact,
      );
      setActivationJob(activated.activation_job_id ?? undefined);
      setDraft(undefined);
      setPreview(undefined);
      setRetrievalAuthorization(undefined);
      await load();
    } catch (reason) {
      setError(reason);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  async function validateDraft() {
    if (!draft || lock.current) return;
    lock.current = true;
    setBusy(true);
    setConfirmTest(false);
    try {
      for (const role of ["primary", "standby"] as const) {
        const connection = draft[`${role}_connection_id`];
        if (!connection) continue;
        for (const operation of ["document", "query"] as const) {
          const result = await api.validateConnection(connection, {
            operation: `embedding.${operation}`,
            model: draft[`${role}_embedding_model`],
            expected_dimension: draft[`${role}_dimension`],
            request_policy: draft[`${role}_${operation}_policy`],
          });
          if (result.status !== "succeeded")
            throw new Error("方案参数验证未通过，请查看模型服务中的记录。");
        }
      }
      if (draft.reranker_connection_id) {
        const result = await api.validateConnection(
          draft.reranker_connection_id,
          {
            operation: "reranking",
            model: draft.reranker_model,
          },
        );
        if (result.status !== "succeeded") throw new Error("重排验证未通过。");
      }
      setValidationMessage("方案参数连接验证通过；检索质量仍需独立验证。");
      setRetrievalAuthorization(
        await api.retrievalAuthorization(draft.profile_revision_id),
      );
    } catch (reason) {
      setError(reason);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  async function approveRetrieval() {
    if (!draft || !retrievalAuthorization || lock.current) return;
    lock.current = true;
    setBusy(true);
    setConfirmAuthorization(false);
    setError(undefined);
    try {
      const documentRequests =
        retrievalAuthorization.estimated_document_requests_per_slot *
        retrievalAuthorization.embedding_slot_count;
      const operationRequestLimits = Object.fromEntries(
        retrievalAuthorization.required_operations.map((operation) => [
          operation,
          operation === "embedding.document"
            ? Math.max(1, documentRequests + 10)
            : 200,
        ]),
      );
      const requestLimit = Object.values(operationRequestLimits).reduce(
        (total, value) => total + value,
        0,
      );
      const estimatedTokenLimit = Math.min(
        100_000_000,
        retrievalProviderTokenLimit(draft, connections),
      );
      const expires = new Date();
      expires.setUTCDate(expires.getUTCDate() + 30);
      const approved = await api.approveRetrievalAuthorization(
        draft.profile_revision_id,
        {
          expires_at: expires.toISOString(),
          request_limit: requestLimit,
          estimated_token_limit: estimatedTokenLimit,
          operation_request_limits: operationRequestLimits,
        },
      );
      setRetrievalAuthorization(approved);
      await load();
    } catch (reason) {
      setError(reason);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  if (!scope.kbId) {
    return (
      <EmptyState title="请先选择知识库">
        检索方案按知识库保存，不会影响其他工作范围。
      </EmptyState>
    );
  }
  const primaryOptions = connections.filter(
    (item) =>
      item.enabled !== false &&
      ["jina", "openai-compatible"].includes(item.provider_type),
  );
  const standbyOptions = connections.filter(
    (item) =>
      item.enabled !== false &&
      ["aliyun-model-studio", "openai-compatible"].includes(item.provider_type),
  );
  const rerankerOptions = connections.filter(
    (item) =>
      item.enabled !== false &&
      ["jina", "openai-compatible"].includes(item.provider_type),
  );
  const primaryConnection = connections.find(
    (item) => item.connection_id === primary,
  );
  const standbyConnection = connections.find(
    (item) => item.connection_id === standby,
  );
  const rerankerConnection = connections.find(
    (item) => item.connection_id === reranker,
  );
  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <span className="eyebrow">知识库级配置</span>
          <h2>检索方案</h2>
          <p>先创建草稿，再预览索引影响并确认应用。</p>
        </div>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      <form className="panel form-grid" onSubmit={create}>
        <h3>{base ? "编辑检索方案" : "新建方案草稿"}</h3>
        <label>
          主向量连接
          <select
            value={primary}
            onChange={(event) => {
              const previous = primary;
              const nextId = event.target.value;
              const next = connections.find(
                (item) => item.connection_id === nextId,
              );
              setPrimary(nextId);
              setPrimaryModel(
                defaultModel(catalog, next, "embedding.document"),
              );
              if (next?.provider_type !== "openai-compatible") {
                setPrimaryDimension(1024);
              }
              if (!base && (!reranker || reranker === previous)) {
                setReranker(nextId);
                setRerankerModel(defaultModel(catalog, next, "reranking"));
              }
            }}
            required
          >
            <option value="">选择主向量连接</option>
            {primaryOptions.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        {primaryConnection?.provider_type === "openai-compatible" ? (
          <>
            <label>
              主向量模型 ID
              <input
                value={primaryModel}
                onChange={(event) => setPrimaryModel(event.target.value)}
                required
              />
            </label>
            <label>
              主向量维度
              <input
                type="number"
                min={1}
                value={primaryDimension}
                onChange={(event) =>
                  setPrimaryDimension(Number(event.target.value))
                }
                required
              />
            </label>
          </>
        ) : (
          primaryConnection && (
            <p>
              主模型：{primaryModel || "目录加载中"} · {primaryDimension} 维
            </p>
          )
        )}
        <label>
          备用向量连接
          <select
            value={standby}
            onChange={(event) => {
              setStandby(event.target.value);
              const connection = connections.find(
                (item) => item.connection_id === event.target.value,
              );
              setStandbyModel(
                defaultModel(catalog, connection, "embedding.document"),
              );
              setStandbyDimension(
                connection?.provider_type === "openai-compatible"
                  ? primaryDimension
                  : 1024,
              );
              setStandbyRequests(connection?.request_budget);
              setStandbyTokens(connection?.token_budget);
            }}
          >
            <option value="">不启用备用连接</option>
            {standbyOptions.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        {standbyConnection?.provider_type === "openai-compatible" && (
          <>
            <label>
              备用向量模型 ID
              <input
                value={standbyModel}
                onChange={(event) => setStandbyModel(event.target.value)}
                required
              />
            </label>
            <label>
              备用向量维度
              <input
                type="number"
                min={1}
                value={standbyDimension}
                onChange={(event) =>
                  setStandbyDimension(Number(event.target.value))
                }
                required
              />
            </label>
          </>
        )}
        {standbyConnection?.provider_type === "aliyun-model-studio" && (
          <details className="span-two">
            <summary>高级设置</summary>
            <label>
              Qwen 查询指令
              <textarea
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                rows={3}
                placeholder="留空使用目录默认值，保存后可查看已解析指令"
              />
            </label>
          </details>
        )}
        <label>
          重排连接
          <select
            value={reranker}
            onChange={(event) => {
              const connection = connections.find(
                (item) => item.connection_id === event.target.value,
              );
              setReranker(event.target.value);
              setRerankerModel(defaultModel(catalog, connection, "reranking"));
            }}
          >
            <option value="">不启用重排</option>
            {rerankerOptions.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name} · 重排
              </option>
            ))}
          </select>
        </label>
        {rerankerConnection?.provider_type === "openai-compatible" ? (
          <label>
            重排模型 ID
            <input
              value={rerankerModel}
              onChange={(event) => setRerankerModel(event.target.value)}
              required
            />
          </label>
        ) : (
          rerankerConnection && <p>重排模型：{rerankerModel}</p>
        )}
        {standby && (
          <label>
            <input
              type="checkbox"
              checked={failover}
              onChange={(event) => setFailover(event.target.checked)}
            />
            主槽不可用时允许切换备用槽
          </label>
        )}
        <label>
          最少独立支持数
          <input
            type="number"
            min={1}
            max={8}
            value={minimumSupport}
            onChange={(event) => setMinimumSupport(Number(event.target.value))}
          />
        </label>
        <label>
          最多证据条数
          <input
            type="number"
            min={minimumSupport}
            max={50}
            value={maxEvidence}
            onChange={(event) => setMaxEvidence(Number(event.target.value))}
          />
        </label>
        <label>
          证据 Token 预算
          <input
            type="number"
            min={1}
            value={tokenBudget}
            onChange={(event) => setTokenBudget(Number(event.target.value))}
          />
        </label>
        <p className="span-two">
          内置连接沿用其目录请求参数；自定义连接显式绑定 document/query
          角色。所有模型 ID 与维度以当前表单值参与验证和指纹计算。
        </p>
        {standby && (
          <>
            <label>
              备用请求预算
              <input
                type="number"
                min={1}
                max={
                  connections.find((item) => item.connection_id === standby)
                    ?.request_budget ?? 500
                }
                value={standbyRequests ?? ""}
                onChange={(event) =>
                  setStandbyRequests(Number(event.target.value))
                }
                required
              />
            </label>
            <label>
              备用 Token 预算
              <input
                type="number"
                min={1}
                max={
                  connections.find((item) => item.connection_id === standby)
                    ?.token_budget ?? 1000000
                }
                value={standbyTokens ?? ""}
                onChange={(event) =>
                  setStandbyTokens(Number(event.target.value))
                }
                required
              />
            </label>
          </>
        )}
        <button className={preview ? "secondary" : "primary"} disabled={busy}>
          <SlidersHorizontal aria-hidden="true" size={17} />
          创建并预览影响
        </button>
      </form>
      {preview && draft && (
        <section className="impact-panel" role="alert">
          <AlertTriangle aria-hidden="true" size={24} />
          <div>
            <span className="eyebrow">应用前确认</span>
            <h3>{zhCN.impact[preview.impact]}</h3>
            <p>
              {preview.index_fingerprint_changed
                ? "本次修改需要重新建立索引"
                : "本次修改无需重新建立索引"}
              ，查询配置
              {preview.serving_fingerprint_changed ? "已变化" : "未变化"}。
            </p>
            <details>
              <summary>技术详情</summary>
              <code>{draft.profile_revision_id}</code>
            </details>
            {draft.standby_query_policy.query_instruct !== undefined && (
              <p>
                已解析 Qwen 指令：
                {String(draft.standby_query_policy.query_instruct)}
              </p>
            )}
            <p>验证将向所选模型服务发送公开合成文本，并可能消耗调用额度。</p>
            <button disabled={busy} onClick={() => setConfirmTest(true)}>
              验证方案所用参数
            </button>
            {validationMessage && <p role="status">{validationMessage}</p>}
            {retrievalAuthorization && (
              <div className="stack">
                <p>
                  当前文档 {retrievalAuthorization.estimated_document_chunks}
                  个检索片段；每个向量槽预计
                  {retrievalAuthorization.estimated_document_requests_per_slot}
                  次请求、
                  {retrievalAuthorization.estimated_document_tokens_per_slot}个
                  Token。
                </p>
                {retrievalAuthorization.connection_budget_state !== "READY" && (
                  <p role="alert">
                    连接预算未就绪，需先在模型服务中把请求预算和 Token
                    预算提高到上述每槽需求，并重新验证方案。
                  </p>
                )}
                {retrievalAuthorization.authorization_state === "APPROVED" &&
                  retrievalAuthorization.budget_state === "AVAILABLE" && (
                    <p role="status">
                      当前真实文档的检索出网与累计预算已批准。
                    </p>
                  )}
                {retrievalAuthorization.authorization_state === "APPROVED" &&
                  retrievalAuthorization.budget_state !== "AVAILABLE" && (
                    <p role="alert">
                      当前检索累计预算已不可用，需要重新验证并批准新预算。
                    </p>
                  )}
                {retrievalAuthorization.authorization_state ===
                  "NOT_REQUIRED" && (
                  <p>当前连接模式或资料状态不需要 Campaign 批准。</p>
                )}
                {!retrievalAuthorizationReady(retrievalAuthorization) &&
                  retrievalAuthorization.authorization_state !==
                    "NOT_REQUIRED" && (
                    <button
                      disabled={
                        busy ||
                        !validationMessage ||
                        retrievalAuthorization.connection_budget_state !==
                          "READY"
                      }
                      onClick={() => setConfirmAuthorization(true)}
                    >
                      批准当前活动文档用于真实检索
                    </button>
                  )}
              </div>
            )}
          </div>
          <button
            className="primary"
            disabled={
              busy || !retrievalAuthorizationReady(retrievalAuthorization)
            }
            onClick={() => void activate()}
          >
            {
              {
                NO_REINDEX: "保存设置",
                SERVING_RELOAD: "更新检索设置",
                NEW_INDEX_REVISION_REQUIRED: "建立新索引并切换",
              }[preview.impact]
            }
          </button>
        </section>
      )}
      {confirmTest && draft && (
        <Modal title="确认方案测试" onClose={() => setConfirmTest(false)}>
          <p>
            只发送公开短文本，不会发送知识库文档。预计操作数：
            {2 +
              (draft.standby_connection_id ? 2 : 0) +
              (draft.reranker_connection_id ? 1 : 0)}
            。可能消耗服务额度，累计预算以服务端为准。
          </p>
          <button onClick={() => void validateDraft()}>开始测试</button>
        </Modal>
      )}
      {confirmAuthorization && retrievalAuthorization && (
        <Modal
          title="批准真实检索出网"
          onClose={() => setConfirmAuthorization(false)}
        >
          <p>
            将把当前活动文档发送给所选 Embedding
            服务，并允许后续查询文本与候选片段用于 Embedding
            和重排。授权绑定当前文档、方案、连接版本和模型，30
            天后到期；文档或连接变化后自动失效。
          </p>
          <p>
            当前估算：{retrievalAuthorization.estimated_document_chunks}
            个片段，
            {retrievalAuthorization.estimated_document_requests_per_slot}
            次/向量槽，
            {retrievalAuthorization.estimated_document_tokens_per_slot}
            Token/向量槽。
          </p>
          <button className="primary" onClick={() => void approveRetrieval()}>
            确认批准并建立累计预算
          </button>
        </Modal>
      )}
      {activationJob && (
        <ProfileJob
          key={activationJob}
          jobId={activationJob}
          onComplete={load}
        />
      )}
      <div className="card-list">
        {profiles.map((profile) => (
          <article key={profile.profile_revision_id}>
            <div className="grow">
              <h3>{profile.status === "active" ? "当前方案" : "方案草稿"}</h3>
              <details>
                <summary>技术详情</summary>
                <code>{profile.profile_revision_id}</code>
                <code>{profile.effective_serving_fingerprint}</code>
              </details>
              {profile.status === "active" && !scope.revisionId && (
                <p>方案已绑定，上传文档后建立索引。</p>
              )}
              <button
                onClick={() => {
                  setBase(profile);
                  setPrimary(profile.primary_connection_id);
                  setPrimaryModel(profile.primary_embedding_model);
                  setPrimaryDimension(profile.primary_dimension);
                  setStandby(profile.standby_connection_id ?? "");
                  setStandbyModel(profile.standby_embedding_model ?? "");
                  setStandbyDimension(profile.standby_dimension ?? 1024);
                  setReranker(profile.reranker_connection_id ?? "");
                  setRerankerModel(profile.reranker_model ?? "");
                  setInstruction(
                    String(profile.standby_query_policy.query_instruct ?? ""),
                  );
                  setStandbyRequests(profile.standby_budget?.requests);
                  setStandbyTokens(profile.standby_budget?.tokens);
                  setFailover(profile.failover_enabled);
                  setRetrievalAuthorization(profile.retrieval_authorization);
                  setMinimumSupport(
                    Number(profile.retrieval_policy.minimum_support_items ?? 1),
                  );
                  setMaxEvidence(
                    Number(profile.retrieval_policy.max_evidence_items ?? 8),
                  );
                  setTokenBudget(
                    Number(
                      profile.retrieval_policy.evidence_token_budget ?? 1024,
                    ),
                  );
                }}
              >
                编辑方案
              </button>
              {profile.activation_job_id && profile.status !== "active" && (
                <small>
                  索引构建任务：{profile.activation_job_id}
                  。完成前继续使用当前方案，可在任务页查看或重试。
                </small>
              )}
              {profile.standby_query_policy?.query_instruct !== undefined && (
                <small>
                  Qwen 查询指令：
                  {String(profile.standby_query_policy.query_instruct)}
                </small>
              )}
              <small>主模型：{profile.primary_embedding_model}</small>

              {profile.standby_embedding_model && (
                <small>备用模型：{profile.standby_embedding_model}</small>
              )}
            </div>
            <StatusBadge value={profile.status} />
            {profile.status === "active" && (
              <CheckCircle2 aria-label="当前正在使用" size={20} />
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

function defaultModel(
  catalog: ProviderCatalog | undefined,
  connection: ProviderConnection | undefined,
  operation: string,
): string {
  if (!connection) return "";
  return (
    catalog?.providers.find(
      (item) => item.provider_type === connection.provider_type,
    )?.operation_models?.[operation]?.[0] ?? ""
  );
}

function selectedModel(
  catalog: ProviderCatalog | undefined,
  connection: ProviderConnection,
  operation: string,
  input: string,
): string {
  const model = input.trim() || defaultModel(catalog, connection, operation);
  if (!model) throw new Error("请填写所选连接实际暴露的模型 ID。");
  return model;
}

function requireConnection(
  connections: ProviderConnection[],
  connectionId: string,
): ProviderConnection {
  const connection = connections.find(
    (item) => item.connection_id === connectionId,
  );
  if (!connection) throw new Error("所选连接不存在或已不可用，请刷新后重试。");
  return connection;
}

type EmbeddingPolicies = {
  document: Record<string, unknown>;
  query: Record<string, unknown>;
};

function embeddingPolicies(
  connection: ProviderConnection,
  preserved?: EmbeddingPolicies,
  queryInstruction = "",
): EmbeddingPolicies {
  if (connection.provider_type === "openai-compatible") {
    return {
      document: {
        ...preserved?.document,
        role: "document",
        encoding_format: "float",
        normalized: true,
      },
      query: {
        ...preserved?.query,
        role: "query",
        encoding_format: "float",
        normalized: true,
      },
    };
  }
  if (connection.provider_type === "jina") {
    return {
      document: {
        ...preserved?.document,
        task: "retrieval.passage",
        normalized: true,
      },
      query: {
        ...preserved?.query,
        task: "retrieval.query",
        normalized: true,
      },
    };
  }
  return {
    document: {
      ...preserved?.document,
      text_type: "document",
    },
    query: {
      ...preserved?.query,
      text_type: "query",
      query_instruct: queryInstruction.trim() || undefined,
    },
  };
}

function retrievalAuthorizationReady(
  status: RetrievalAuthorizationStatus | undefined,
): boolean {
  return Boolean(
    status &&
    ["APPROVED", "NOT_REQUIRED"].includes(status.authorization_state) &&
    status.budget_state === "AVAILABLE" &&
    status.connection_budget_state === "READY",
  );
}

function retrievalProviderTokenLimit(
  profile: RetrievalProfile,
  connections: ProviderConnection[],
): number {
  const connectionIds = [
    profile.primary_connection_id,
    profile.standby_connection_id,
    profile.reranker_connection_id,
  ].filter((value): value is string => Boolean(value));
  const limits = new Map<string, number>();
  for (const connectionId of new Set(connectionIds)) {
    const connection = connections.find(
      (item) => item.connection_id === connectionId,
    );
    if (!connection) continue;
    const tokenBudget = connection.token_budget ?? 4096;
    const existing = limits.get(connection.provider_type);
    limits.set(
      connection.provider_type,
      existing === undefined ? tokenBudget : Math.min(existing, tokenBudget),
    );
  }
  return [...limits.values()].reduce((total, value) => total + value, 0);
}

function ProfileJob({
  jobId,
  onComplete,
}: {
  jobId: string;
  onComplete: () => Promise<void>;
}) {
  const [state, setState] = useState("pending");
  const [error, setError] = useState<unknown>();
  const poll = useCallback(async () => {
    const job = await api.getJob("", jobId);
    setState(job.state);
    if (["queued", "running", "failed_retryable"].includes(job.state))
      return true;
    await onComplete();
    return false;
  }, [jobId, onComplete]);
  useJobPolling(poll, setError);
  return (
    <section className="panel">
      <h3>索引构建任务</h3>
      <StatusBadge value={state} />
      <p>
        {state === "succeeded"
          ? "索引构建已完成，当前方案以服务端状态为准。"
          : "完成前继续使用当前索引。"}
      </p>
      {error !== undefined && <ErrorPanel error={error} />}
      <details>
        <summary>技术详情</summary>
        <code>{jobId}</code>
      </details>
    </section>
  );
}
