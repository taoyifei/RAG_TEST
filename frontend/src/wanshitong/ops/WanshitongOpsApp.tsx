import { useCallback, useEffect, useState } from "react";

import { withAppBase } from "../../app/basePath";

interface TraceSummary {
  trace_id: string;
  created_at: string;
  status: string;
  native_message_id: string | null;
  native_request_id: string | null;
  feedback_useful: number | null;
}

interface TraceDetail {
  bridge_trace_id: string;
  native_session_id: string;
  native_message_id: string | null;
  status: string;
  events: { sequence: number; event_type: string; created_at: string }[];
  references: { reference_id: string; native_knowledge_id: string | null }[];
  feedback: { useful: boolean; reason_detail: string | null } | null;
}

async function jsonRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(withAppBase(path), {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) throw new Error(`请求失败（${response.status}）`);
  return (await response.json()) as T;
}

export function WanshitongOpsApp() {
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [csrfToken, setCsrfToken] = useState("");
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [migrationCount, setMigrationCount] = useState<number | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const [traceResult, migrationResult] = await Promise.all([
      jsonRequest<{ items: TraceSummary[] }>("/api/admin/ops/traces"),
      jsonRequest<{ items: unknown[] }>("/api/admin/ops/migration"),
    ]);
    setTraces(traceResult.items);
    setMigrationCount(migrationResult.items.length);
  }, []);

  useEffect(() => {
    void jsonRequest<{ csrf_token: string }>("/api/v1/console/session")
      .then(async (session) => {
        setCsrfToken(session.csrf_token);
        await refresh();
      })
      .catch(() => {
        // 管理员尚未登录时显示湾事通登录表单。
      });
  }, [refresh]);

  const login = async () => {
    setError("");
    try {
      const session = await jsonRequest<{ csrf_token: string }>(
        "/api/v1/console/session",
        {
          method: "POST",
          body: JSON.stringify({ bootstrap_token: bootstrapToken }),
        },
      );
      setBootstrapToken("");
      setCsrfToken(session.csrf_token);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败");
    }
  };

  const selectTrace = async (traceId: string) => {
    setError("");
    try {
      setSelected(
        await jsonRequest<TraceDetail>(`/api/admin/ops/traces/${traceId}`),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 Trace 失败");
    }
  };

  const exportTrace = async (traceId: string, includeContent: boolean) => {
    if (
      includeContent &&
      !window.confirm("此文件包含原始问题、回答和引用正文。确认下载？")
    ) {
      return;
    }
    setError("");
    try {
      const response = await fetch(
        withAppBase(`/api/admin/ops/traces/${traceId}/export`),
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            include_content: includeContent,
            confirm_id: includeContent ? traceId : null,
          }),
        },
      );
      if (!response.ok) throw new Error(`导出失败（${response.status}）`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${traceId}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    }
  };

  return (
    <main className="wst-ops" id="main-content">
      <header className="wst-ops-header">
        <h1>湾事通运营记录</h1>
        <nav aria-label="管理导航">
          <a href={withAppBase("/admin/")}>知识库管理</a>
          <a href={withAppBase("/")}>普通问答</a>
        </nav>
      </header>
      {!csrfToken ? (
        <section className="wst-ops-login">
          <h2>管理员登录</h2>
          <label htmlFor="wst-ops-token">湾事通管理员口令</label>
          <input
            autoComplete="off"
            id="wst-ops-token"
            onChange={(event) => setBootstrapToken(event.target.value)}
            type="password"
            value={bootstrapToken}
          />
          <button disabled={!bootstrapToken} onClick={() => void login()}>
            登录
          </button>
        </section>
      ) : (
        <>
          <p>引擎：WeKnora · 已映射原件：{migrationCount ?? "读取中"}</p>
          <button onClick={() => void refresh()}>刷新记录</button>
          <section>
            <h2>最近问答与反馈</h2>
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>Trace</th>
                  <th>状态</th>
                  <th>反馈</th>
                </tr>
              </thead>
              <tbody>
                {traces.map((trace) => (
                  <tr key={trace.trace_id}>
                    <td>{trace.created_at}</td>
                    <td>
                      <button onClick={() => void selectTrace(trace.trace_id)}>
                        {trace.trace_id}
                      </button>
                    </td>
                    <td>{trace.status}</td>
                    <td>
                      {trace.feedback_useful === null
                        ? "未反馈"
                        : trace.feedback_useful
                          ? "有帮助"
                          : "待改进"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          {selected && (
            <section className="wst-ops-detail">
              <h2>Trace {selected.bridge_trace_id}</h2>
              <p>原生会话：{selected.native_session_id}</p>
              <p>原生消息：{selected.native_message_id ?? "未返回"}</p>
              <p>状态：{selected.status}</p>
              <p>
                实际流事件：{selected.events.length} · 引用：
                {selected.references.length}
              </p>
              <button
                onClick={() => void exportTrace(selected.bridge_trace_id, false)}
              >
                下载脱敏 Trace
              </button>
              <button
                onClick={() => void exportTrace(selected.bridge_trace_id, true)}
              >
                确认后下载完整 Trace
              </button>
            </section>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </main>
  );
}
