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
  const [standby, setStandby] = useState("");
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
      const primaryModel = modelFor(catalog, "jina", "embedding.document");
      const standbyModel = standby
        ? modelFor(catalog, "aliyun-model-studio", "embedding.document")
        : null;
      const rerankerModel = modelFor(catalog, "jina", "reranking");
      const next = await api.createRetrievalProfile(scope.kbId, {
        primary_connection_id: primary,
        primary_embedding_model: primaryModel,
        primary_dimension: base?.primary_dimension ?? 1024,
        primary_document_policy: base?.primary_document_policy ?? {
          task: "retrieval.passage",
          normalized: true,
        },
        primary_query_policy: base?.primary_query_policy ?? {
          task: "retrieval.query",
          normalized: true,
        },
        standby_connection_id: standby || null,
        standby_embedding_model: standby ? standbyModel : null,
        standby_dimension: standby ? (base?.standby_dimension ?? 1024) : null,
        standby_document_policy: standby
          ? (base?.standby_document_policy ?? { text_type: "document" })
          : {},
        standby_query_policy: standby
          ? {
              ...base?.standby_query_policy,
              text_type: "query",
              query_instruct: instruction.trim() ? instruction : undefined,
            }
          : {},
        reranker_connection_id: base ? base.reranker_connection_id : primary,
        reranker_model: base ? base.reranker_model : rerankerModel,
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
      setDraft(next);
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
  const jina = connections.filter((item) => item.provider_type === "jina");
  const aliyun = connections.filter(
    (item) => item.provider_type === "aliyun-model-studio",
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
            onChange={(event) => setPrimary(event.target.value)}
            required
          >
            <option value="">选择 Jina 连接</option>
            {jina.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          备用向量连接
          <select
            value={standby}
            onChange={(event) => {
              setStandby(event.target.value);
              const connection = connections.find(
                (item) => item.connection_id === event.target.value,
              );
              setStandbyRequests(connection?.request_budget);
              setStandbyTokens(connection?.token_budget);
            }}
          >
            <option value="">不启用备用连接</option>
            {aliyun.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        {standby && (
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
          Jina 任务类型：文档 retrieval.passage，查询 retrieval.query（只读）。
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
                    ?.request_budget ?? 20
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
          </div>
          <button
            className="primary"
            disabled={busy}
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
                  setStandby(profile.standby_connection_id ?? "");
                  setInstruction(
                    String(profile.standby_query_policy.query_instruct ?? ""),
                  );
                  setStandbyRequests(profile.standby_budget?.requests);
                  setStandbyTokens(profile.standby_budget?.tokens);
                  setFailover(profile.failover_enabled);
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

function modelFor(
  catalog: ProviderCatalog | undefined,
  providerType: ProviderConnection["provider_type"],
  operation: string,
): string {
  const model = catalog?.providers.find(
    (item) => item.provider_type === providerType,
  )?.operation_models?.[operation]?.[0];
  if (!model) throw new Error("模型目录尚未就绪，请刷新后重试。");
  return model;
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
