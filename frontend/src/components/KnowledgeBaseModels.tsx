import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  type KnowledgeBaseModelSettings,
  type ProviderCatalog,
  type ProviderConnection,
} from "../api/client";
import { ErrorPanel, Modal } from "./ui";

export function KnowledgeBaseModels({ kbId }: { kbId: string }) {
  const [settings, setSettings] = useState<KnowledgeBaseModelSettings>();
  const [editing, setEditing] = useState(false);
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
          </div>
          <button disabled={!settings} onClick={() => setEditing(true)}>
            设置回答与图片识别
          </button>
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
    </>
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
        <details>
          <summary>调用授权</summary>
          <label>
            已有预算批次标识
            <input
              value={value.budget_campaign_id ?? ""}
              disabled={pending}
              pattern="[A-Za-z0-9_.:\-]{1,128}"
              onChange={(event) =>
                setValue({
                  ...value,
                  budget_campaign_id: event.target.value || null,
                })
              }
            />
          </label>
          <small>填写服务端已授权的批次；保存不会创建或重置预算。</small>
        </details>
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
