import { useState, type FormEvent } from "react";

import {
  ApiError,
  api,
  type CredentialSummary,
  type ProviderConnection,
} from "../api/client";
import { ErrorPanel, Modal } from "../components/ui";
import { previewAliyunEndpoint } from "./aliyun-endpoint";

export function ConnectionEditor({
  connection,
  credential,
  onSaved,
  onCancel,
  onCredentialRotated,
}: {
  connection: ProviderConnection;
  credential?: CredentialSummary;
  onSaved: () => Promise<void>;
  onCancel: () => void;
  onCredentialRotated?: () => Promise<void>;
}) {
  const [name, setName] = useState(connection.display_name);
  const [workspace, setWorkspace] = useState(connection.workspace_id ?? "");
  const [mode, setMode] = useState<string>(connection.endpoint_mode ?? "");
  const [host, setHost] = useState(connection.api_host ?? "");
  const [baseUrl, setBaseUrl] = useState(connection.api_base_url ?? "");
  const [ocrBaseUrl, setOcrBaseUrl] = useState(
    connection.ocr_api_base_url ?? "",
  );
  const [rerankProtocol, setRerankProtocol] = useState<
    "tei" | "jina-compatible"
  >(connection.rerank_protocol ?? "tei");
  const [rerankPath, setRerankPath] = useState(connection.rerank_path ?? "");
  const [requestBudget, setRequestBudget] = useState(
    connection.request_budget ?? 5,
  );
  const [tokenBudget, setTokenBudget] = useState(
    connection.token_budget ?? 4096,
  );
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [enabled, setEnabled] = useState(connection.enabled ?? true);
  const [changingKey, setChangingKey] = useState(false);
  const [secret, setSecret] = useState("");
  const [rotating, setRotating] = useState(false);
  const [rotated, setRotated] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>();
  const aliyun = connection.provider_type === "aliyun-model-studio";
  const custom = connection.provider_type === "openai-compatible";
  const paddle = connection.provider_type === "paddleocr";
  const paddleSelfHosted = paddle && mode === "self_hosted";
  const endpointPreview = previewAliyunEndpoint(mode, host);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    if (aliyun && endpointPreview.error) {
      setError(new Error(endpointPreview.error));
      return;
    }
    if (connection.enabled !== false && !enabled && !confirmDisable) {
      setConfirmDisable(true);
      return;
    }
    setSaving(true);
    setError(undefined);
    try {
      await api.updateConnection(connection.connection_id, {
        expected_version: connection.configuration_version,
        display_name: name,
        enabled,
        request_budget: requestBudget,
        token_budget: tokenBudget,
        ...(aliyun
          ? {
              workspace_id: workspace,
              endpoint_mode: mode,
              api_host: endpointPreview.endpoint,
              region: "cn-beijing",
            }
          : {}),
        ...(custom
          ? {
              endpoint_mode: "custom",
              api_base_url: baseUrl,
              rerank_protocol: rerankProtocol,
              rerank_path: rerankPath.trim() || null,
            }
          : {}),
        ...(paddle
          ? {
              endpoint_mode: mode,
              api_base_url: paddleSelfHosted ? baseUrl : null,
              ocr_api_base_url:
                paddleSelfHosted && ocrBaseUrl.trim() ? ocrBaseUrl : null,
            }
          : {}),
      });
      await onSaved();
    } catch (reason) {
      setError(
        reason instanceof ApiError && reason.status === 409
          ? new Error(
              "另一窗口已修改此连接，请取消后重新读取。当前输入已保留，未覆盖对方修改。",
            )
          : reason,
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={
        aliyun
          ? "编辑百炼连接"
          : custom
            ? "编辑自定义模型连接"
            : paddle
              ? "编辑 PaddleOCR 连接"
              : "编辑 Jina 连接"
      }
      onClose={() => {
        if (!saving && !rotating) onCancel();
      }}
      drawer
    >
      <form className="form-grid" onSubmit={save}>
        <h3 className="sr-only">编辑连接</h3>
        <p className="span-two">
          密钥已保存，修改连接无需重新填写。保存仅修改配置；测试需单独发起。
        </p>
        <p className="span-two">
          {credential?.masked_hint ?? "••••"} · 保持现有密钥
        </p>
        {error !== undefined && <ErrorPanel error={error} />}
        <label>
          连接名称
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </label>
        {aliyun && (
          <>
            <label>
              工作空间标识
              <input
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                required
              />
            </label>
            <label>
              端点模式
              <select
                value={mode}
                onChange={(e) => {
                  setMode(e.target.value);
                  setHost(
                    e.target.value === "beijing_dashscope"
                      ? "https://dashscope.aliyuncs.com"
                      : "",
                  );
                }}
              >
                <option value="" disabled>
                  请选择端点模式
                </option>
                <option value="workspace_host">北京业务空间 API Host</option>
                <option value="beijing_dashscope">
                  北京 DashScope（显式选择）
                </option>
              </select>
            </label>
            {mode === "workspace_host" && (
              <label>
                API Host
                <input
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                  required={mode === "workspace_host"}
                  placeholder="请从当前北京业务空间复制API Host"
                />
              </label>
            )}
            <label>
              地域
              <select value="cn-beijing" disabled>
                <option value="cn-beijing">北京</option>
              </select>
            </label>
            <p role="status" className="span-two">
              {endpointPreview.endpoint
                ? `保存前规范结果：${endpointPreview.endpoint}`
                : endpointPreview.error}
            </p>
            <p className="span-two">
              Workspace ID 与 API Host 分别从控制台获取，不能互相推导。 系统不按
              llm- 或 ws- 前缀判断账号是否有效。
            </p>
            <p className="span-two">
              在 API Key 创建弹窗或业务空间管理的 API Host
              列复制，不要填写带密钥的链接。
            </p>
            {mode === "beijing_dashscope" && (
              <p className="span-two">
                使用官方北京兼容接入点。模型、密钥和业务空间权限仍需测试确认。
              </p>
            )}
          </>
        )}
        {custom && (
          <>
            <label className="span-two">
              API Base URL
              <input
                type="url"
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                required
              />
            </label>
            <label>
              Rerank 协议
              <select
                value={rerankProtocol}
                onChange={(event) =>
                  setRerankProtocol(
                    event.target.value as "tei" | "jina-compatible",
                  )
                }
              >
                <option value="tei">TEI</option>
                <option value="jina-compatible">Jina-compatible</option>
              </select>
            </label>
            <label>
              Rerank 路径（可选）
              <input
                value={rerankPath}
                onChange={(event) => setRerankPath(event.target.value)}
                placeholder={
                  rerankProtocol === "tei" ? "/rerank" : "/v1/rerank"
                }
              />
            </label>
            <p className="span-two">
              Base URL、Rerank
              协议或路径变化后，既有成功记录会失效，需重新测试对应能力。
            </p>
          </>
        )}
        {paddle && (
          <>
            <label>
              PaddleOCR 模式
              <select
                value={mode}
                onChange={(event) => setMode(event.target.value)}
              >
                <option value="self_hosted">本地/自托管完整产线</option>
                <option value="official_api">PaddleOCR 官方 API</option>
              </select>
            </label>
            {paddleSelfHosted && (
              <>
                <label className="span-two">
                  文档解析 Base URL
                  <input
                    type="url"
                    value={baseUrl}
                    onChange={(event) => setBaseUrl(event.target.value)}
                    placeholder="http://127.0.0.1:8080"
                    required
                  />
                </label>
                <label className="span-two">
                  PP-OCRv6 复核 Base URL（可选）
                  <input
                    type="url"
                    value={ocrBaseUrl}
                    onChange={(event) => setOcrBaseUrl(event.target.value)}
                    placeholder="http://127.0.0.1:8081"
                  />
                </label>
              </>
            )}
            <p className="span-two">
              {paddleSelfHosted
                ? "文档解析使用 /layout-parsing；仅在另配 PP-OCRv6 地址时对最终高风险原子调用 /ocr，未配置则标为未复核。"
                : "官方模式由 PaddleOCR SDK 使用当前 Access Token 调用文档解析与 PP-OCRv6。"}
              模式或地址变化后需要重新执行连接测试。
            </p>
          </>
        )}
        <label>
          请求预算
          <input
            type="number"
            min={1}
            max={500}
            value={requestBudget}
            onChange={(e) => setRequestBudget(Number(e.target.value))}
          />
        </label>
        <label>
          Token 预算
          <input
            type="number"
            min={1}
            max={1000000}
            value={tokenBudget}
            onChange={(e) => setTokenBudget(Number(e.target.value))}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          启用连接
        </label>
        {confirmDisable && !enabled && (
          <p role="alert" className="span-two">
            停用后此连接将无法继续调用。请确认停用并保存。
          </p>
        )}
        <button className="primary" disabled={saving || rotating}>
          {saving
            ? "正在保存…"
            : confirmDisable && !enabled
              ? "确认停用并保存"
              : "保存修改"}
        </button>
        <button type="button" onClick={onCancel} disabled={saving || rotating}>
          取消
        </button>
      </form>
      {credential?.source === "database_encrypted" && (
        <section className="stack">
          <button onClick={() => setChangingKey(!changingKey)}>更换密钥</button>
          {changingKey && (
            <form
              className="stack"
              onSubmit={async (event) => {
                event.preventDefault();
                if (rotating) return;
                setRotating(true);
                setError(undefined);
                try {
                  await api.rotateCredential(connection.credential_id, secret);
                  setSecret("");
                  setChangingKey(false);
                  setRotated(true);
                  await onCredentialRotated?.();
                } catch (reason) {
                  setSecret("");
                  setError(reason);
                } finally {
                  setRotating(false);
                }
              }}
            >
              <p>
                此操作会轮换当前连接引用的密钥，共用该密钥的连接都需要重新测试。
              </p>
              <label>
                新服务密钥
                <input
                  type="password"
                  value={secret}
                  onChange={(event) => setSecret(event.target.value)}
                  autoComplete="off"
                  required={!custom && !paddleSelfHosted}
                  placeholder={
                    custom || paddleSelfHosted ? "无鉴权服务可留空" : undefined
                  }
                />
              </label>
              <button disabled={rotating}>确认更换密钥</button>
            </form>
          )}
          {rotated && <p role="status">密钥已更换，请重新测试。</p>}
        </section>
      )}
    </Modal>
  );
}
