import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { withAppBase } from "../app/basePath";
import {
  bootstrapPublicWorkbench,
  PublicApiError,
  type PublicSearchScope,
  type PublicSearchScopeOption,
  type PublicWorkbench,
} from "./publicApi";
import "./workspace.css";

interface Props {
  getCsrfToken: () => string | undefined;
  scopeOptions: readonly PublicSearchScopeOption[];
  theme: "dark" | "light";
}

export function WorkbenchWorkspace({
  getCsrfToken,
  scopeOptions,
  theme,
}: Props) {
  const [selected, setSelected] = useState<PublicSearchScope[]>(["public"]);
  const [requested, setRequested] = useState<PublicSearchScope[]>(["public"]);
  const [attempt, setAttempt] = useState(0);
  const [workspace, setWorkspace] = useState<PublicWorkbench>();
  const [error, setError] = useState<string>();
  const frame = useRef<HTMLIFrameElement>(null);
  const allowedKey = scopeOptions.map((option) => option.id).join(",");
  const scopeValid =
    requested.length > 0 &&
    requested.every((scope) =>
      scopeOptions.some((option) => option.id === scope),
    );
  const permissionError =
    !scopeValid || !getCsrfToken()
      ? "当前资料范围或登录状态已变化，请返回知识问答重新连接。"
      : undefined;

  useEffect(() => {
    const controller = new AbortController();
    const token = getCsrfToken();
    const scopes = requested.filter((scope) =>
      allowedKey.split(",").includes(scope),
    );
    if (!token || scopes.length !== requested.length || scopes.length === 0) {
      return;
    }
    void bootstrapPublicWorkbench(scopes, token, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setWorkspace(result);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(
          cause instanceof PublicApiError && cause.status === 403
            ? "当前账号无权打开这个资料范围，请重新选择。"
            : cause instanceof PublicApiError && cause.status === 401
              ? "登录已过期，请返回知识问答重新登录。"
              : "工作助手暂时无法连接，可以稍后重试。",
        );
      });
    return () => controller.abort();
  }, [allowedKey, attempt, getCsrfToken, requested]);

  const syncTheme = () => {
    frame.current?.contentWindow?.postMessage(
      { type: "wanshitong-workbench-theme", theme },
      window.location.origin,
    );
  };
  useEffect(syncTheme, [theme, workspace?.workspace_id]);

  const openWorkspace = () => {
    // 先卸载旧工作区，再请求新范围；晚到的旧响应由 AbortController 丢弃。
    setWorkspace(undefined);
    setError(undefined);
    const next = scopeOptions
      .filter((option) => selected.includes(option.id))
      .map((option) => option.id);
    setSelected(next);
    setRequested(next);
    setAttempt((value) => value + 1);
  };
  const selectedScopes = scopeOptions
    .filter((option) => selected.includes(option.id))
    .map((option) => option.id);
  const changed = selectedScopes.join(",") !== requested.join(",");

  return (
    <section className="wst-workbench" aria-label="工作助手">
      <div className="wst-workbench-toolbar">
        <div>
          <h1>工作助手</h1>
          <p>整理、对比所选资料。助手只读资料，自动化任务由你手工创建。</p>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            openWorkspace();
          }}
        >
          <fieldset>
            <legend>资料范围</legend>
            {scopeOptions.map((option) => (
              <label key={option.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(option.id)}
                  onChange={() =>
                    setSelected((scopes) =>
                      scopes.includes(option.id)
                        ? scopes.filter((scope) => scope !== option.id)
                        : scopeOptions
                            .filter(
                              (item) =>
                                item.id === option.id ||
                                scopes.includes(item.id),
                            )
                            .map((item) => item.id),
                    )
                  }
                />
                {option.label}
              </label>
            ))}
          </fieldset>
          <button
            type="submit"
            disabled={!selectedScopes.length || (!changed && !error)}
          >
            {changed ? "切换范围" : "重新连接"}
          </button>
        </form>
      </div>
      {changed ? (
        <p className="wst-workbench-notice" role="status">
          点击“切换范围”后，将打开对应的独立工作区。
        </p>
      ) : null}
      {workspace && !permissionError ? (
        <iframe
          key={workspace.workspace_id}
          ref={frame}
          title={`湾小度工作助手 · ${workspace.scope_label}`}
          src={withAppBase(`/workbench/${workspace.workspace_id}/`)}
          onLoad={syncTheme}
          className="wst-workbench-frame"
        />
      ) : (
        <div className="wst-workbench-state">
          {permissionError || error ? (
            <>
              <p role="alert">{permissionError ?? error}</p>
              <button
                type="button"
                onClick={openWorkspace}
                disabled={!selectedScopes.length}
              >
                <RefreshCw size={16} aria-hidden="true" />
                重试
              </button>
            </>
          ) : (
            <p role="status">正在连接你的工作区…</p>
          )}
        </div>
      )}
    </section>
  );
}
