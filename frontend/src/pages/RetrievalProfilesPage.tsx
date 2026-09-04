import { AlertTriangle, CheckCircle2, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  api,
  type ImpactPreview,
  type ProviderCatalog,
  type ProviderConnection,
  type RetrievalProfile,
} from "../api/client";
import { EmptyState, ErrorPanel, StatusBadge } from "../components/ui";
import { zhCN } from "../copy/zh-CN";
import { useConsole } from "../state/console-context";

export function RetrievalProfilesPage() {
  const { scope } = useConsole();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog>();
  const [profiles, setProfiles] = useState<RetrievalProfile[]>([]);
  const [primary, setPrimary] = useState("");
  const [standby, setStandby] = useState("");
  const [instruction, setInstruction] = useState("为检索查询生成准确表示");
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
    setError(undefined);
    try {
      const primaryModel = modelFor(catalog, "jina", false);
      const standbyModel = modelFor(catalog, "aliyun-model-studio", false);
      const rerankerModel = modelFor(catalog, "jina", true);
      const next = await api.createRetrievalProfile(scope.kbId, {
        primary_connection_id: primary,
        primary_embedding_model: primaryModel,
        primary_dimension: 1024,
        primary_document_policy: { task: "retrieval.passage", normalize: true },
        primary_query_policy: {
          task: "retrieval.query",
          normalize: true,
          instruction,
        },
        standby_connection_id: standby || null,
        standby_embedding_model: standby ? standbyModel : null,
        standby_dimension: standby ? 1024 : null,
        standby_document_policy: standby ? { text_type: "document" } : {},
        standby_query_policy: standby ? { text_type: "query", instruction } : {},
        reranker_connection_id: primary,
        reranker_model: rerankerModel,
        failover_enabled: Boolean(standby),
        standby_budget: { requests: 2, tokens: 4096 },
        retrieval_policy: { rrf_k: 60, cache: "revision-bound" },
        evidence_policy: { minimum_units: 1 },
      });
      const impact = await api.previewRetrievalProfile(next.profile_revision_id);
      setDraft(next);
      setPreview(impact);
      await load();
    } catch (reason) {
      setError(reason);
    }
  }

  async function activate() {
    if (!draft || !preview) return;
    try {
      await api.activateRetrievalProfile(
        draft.profile_revision_id,
        preview.impact,
      );
      setDraft(undefined);
      setPreview(undefined);
      await load();
    } catch (reason) {
      setError(reason);
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
        <h3>新建方案草稿</h3>
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
            onChange={(event) => setStandby(event.target.value)}
          >
            <option value="">不启用备用连接</option>
            {aliyun.map((item) => (
              <option key={item.connection_id} value={item.connection_id}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label className="span-two">
          查询指令
          <textarea
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            rows={3}
          />
        </label>
        <button className="primary">
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
              索引语义{preview.index_fingerprint_changed ? "已变化" : "未变化"}
              ，查询配置{preview.serving_fingerprint_changed ? "已变化" : "未变化"}。
            </p>
            <code>{draft.profile_revision_id}</code>
          </div>
          <button className="primary" onClick={() => void activate()}>
            确认应用
          </button>
        </section>
      )}
      <div className="card-list">
        {profiles.map((profile) => (
          <article key={profile.profile_revision_id}>
            <div className="grow">
              <h3>
                {profile.status === "active" ? "当前方案" : "方案草稿"}
              </h3>
              <code>{profile.profile_revision_id}</code>
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
  reranker: boolean,
): string {
  const model = catalog?.providers
    .find((item) => item.provider_type === providerType)
    ?.models.find((item) => item.includes("reranker") === reranker);
  if (!model) throw new Error("模型目录尚未就绪，请刷新后重试。");
  return model;
}
