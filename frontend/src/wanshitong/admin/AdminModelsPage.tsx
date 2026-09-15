import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ErrorPanel, StatusBadge } from "../../components/ui";
import {
  wanshitongAdminApi,
  type ProviderReadiness,
  type WanshitongModels,
} from "./adminApi";

function printable(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "已启用" : "未启用";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "已配置";
}

function ProviderPanel({
  title,
  value,
  reranker = false,
}: {
  title: string;
  value: ProviderReadiness;
  reranker?: boolean;
}) {
  return (
    <section className="panel readonly-service-card">
      <div className="section-heading">
        <h3>{title}</h3>
        <StatusBadge value={value.configured} />
      </div>
      <dl className="detail-grid">
        <dt>Connection</dt>
        <dd>{value.connection_name || value.connection_id || "—"}</dd>
        <dt>Model</dt>
        <dd>{value.model || "—"}</dd>
        {value.dimension != null && (
          <>
            <dt>Dimension</dt>
            <dd>{value.dimension}</dd>
          </>
        )}
        {reranker && (
          <>
            <dt>Protocol / Path</dt>
            <dd>{`${value.protocol || "—"} / ${value.path || "—"}`}</dd>
          </>
        )}
        <dt>Host</dt>
        <dd>{value.host || "—"}</dd>
        <dt>最近验证</dt>
        <dd>
          {value.validation_status || value.status || "尚未验证"}
          {value.validated_at ? ` · ${value.validated_at}` : ""}
        </dd>
      </dl>
    </section>
  );
}

export function AdminModelsPage() {
  const [models, setModels] = useState<WanshitongModels>();
  const [error, setError] = useState<unknown>();
  const load = useCallback(() => {
    setError(undefined);
    void wanshitongAdminApi.models().then(setModels).catch(setError);
  }, []);
  useEffect(() => {
    void wanshitongAdminApi.models().then(setModels).catch(setError);
  }, []);

  return (
    <section className="stack">
      <div className="section-heading">
        <div>
          <h2>模型与服务状态</h2>
          <p>湾事通模式只读展示部署配置，不在浏览器中变更或验证 Provider。</p>
        </div>
        <button className="secondary" onClick={load}>
          <RefreshCw aria-hidden="true" size={17} />
          刷新
        </button>
      </div>
      {error !== undefined && <ErrorPanel error={error} />}
      {!models && error === undefined && <p role="status">正在读取模型状态…</p>}
      {models && (
        <>
          <div className="admin-summary-grid">
            <ProviderPanel title="Embedding" value={models.embedding} />
            <ProviderPanel title="Reranker" value={models.reranker} reranker />
            <ProviderPanel title="LLM" value={models.llm} />
          </div>
          <div className="admin-summary-grid">
            <section className="panel">
              <h3>Retrieval Profile</h3>
              <dl className="detail-grid">
                <dt>当前 Profile</dt>
                <dd>{models.retrieval_profile?.profile_revision_id || "—"}</dd>
                <dt>状态</dt>
                <dd>{models.retrieval_profile?.status || "—"}</dd>
                <dt>Index fingerprint</dt>
                <dd>
                  {models.retrieval_profile?.index_semantic_fingerprint || "—"}
                </dd>
                <dt>Serving fingerprint</dt>
                <dd>{models.retrieval_profile?.serving_fingerprint || "—"}</dd>
              </dl>
            </section>
            <section className="panel">
              <h3>Knowledge Base Model Settings</h3>
              <dl className="detail-grid">
                <dt>Generation</dt>
                <dd>{printable(models.kb_model_settings.generation_model)}</dd>
                <dt>Rewrite</dt>
                <dd>{printable(models.kb_model_settings.rewrite_enabled)}</dd>
                <dt>OCR</dt>
                <dd>{printable(models.kb_model_settings.ocr_enabled)}</dd>
                <dt>PDF Parser</dt>
                <dd>{printable(models.kb_model_settings.pdf_parser_enabled)}</dd>
              </dl>
            </section>
            <section className="panel">
              <h3>图片 OCR</h3>
              <StatusBadge value={models.image_ocr.enabled ?? false} />
              <p>{models.image_ocr.model || "未配置"}</p>
              <small>{models.image_ocr.validation_status || "尚未验证"}</small>
            </section>
            <section className="panel">
              <h3>PDF Parser</h3>
              <StatusBadge value={models.pdf_parser.enabled ?? false} />
              <p>{models.pdf_parser.model || "未配置"}</p>
              <small>{models.pdf_parser.validation_status || "尚未验证"}</small>
            </section>
          </div>
          <p className="readonly-notice" role="note">
            模型配置已锁定。需要调整时请使用部署侧 CLI 或初始化流程。
          </p>
        </>
      )}
    </section>
  );
}
