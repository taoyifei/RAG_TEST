import { useState } from "react";

import {
  getLegacyHistory,
  getLegacyHistoryDetail,
  type LegacyHistoryDetail,
  type LegacyHistoryItem,
} from "./publicApi";

export function LegacyHistory() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<LegacyHistoryItem[]>([]);
  const [detail, setDetail] = useState<LegacyHistoryDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const show = async () => {
    setOpen(true);
    setBusy(true);
    setError("");
    try {
      setItems(await getLegacyHistory());
    } catch {
      setError("旧版记录暂时无法读取。");
    } finally {
      setBusy(false);
    }
  };

  const select = async (traceId: string) => {
    setBusy(true);
    setError("");
    try {
      setDetail(await getLegacyHistoryDetail(traceId));
    } catch {
      setError("这条旧版记录暂时无法读取。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button onClick={() => void show()} type="button">
        旧版记录
      </button>
      {open && (
        <div className="wst-legacy-overlay">
          <section
            aria-label="旧版问答记录"
            aria-modal="true"
            className="wst-legacy-panel"
            role="dialog"
          >
            <header>
              <div>
                <h2>旧版问答记录</h2>
                <p>切换前的记录仅供查看，不能接着旧会话提问。</p>
              </div>
              <button
                aria-label="关闭旧版记录"
                onClick={() => {
                  setOpen(false);
                  setDetail(null);
                }}
                type="button"
              >
                关闭
              </button>
            </header>
            {error && <p role="alert">{error}</p>}
            {busy && <p role="status">正在读取…</p>}
            <div className="wst-legacy-content">
              <nav aria-label="旧版记录列表">
                {items.length === 0 && !busy && !error && (
                  <p>没有可查看的旧版记录。</p>
                )}
                {items.map((item) => (
                  <button
                    aria-current={detail?.trace_id === item.trace_id}
                    key={item.trace_id}
                    onClick={() => void select(item.trace_id)}
                    type="button"
                  >
                    <time dateTime={item.created_at}>
                      {new Date(item.created_at).toLocaleString("zh-CN")}
                    </time>
                    <span>{item.status}</span>
                  </button>
                ))}
              </nav>
              <article>
                {detail ? (
                  <>
                    <p>旧引擎 · 只读 · {detail.trace_id}</p>
                    {detail.body_available ? (
                      <>
                        <h3>问题</h3>
                        <p className="wst-legacy-body">{detail.question}</p>
                        <h3>回答</h3>
                        <p className="wst-legacy-body">{detail.answer}</p>
                      </>
                    ) : (
                      <p>正文不可用：{detail.body_unavailable_reason}</p>
                    )}
                    <details>
                      <summary>旧版事件（{detail.events.length}）</summary>
                      <ol>
                        {detail.events.map((event, index) => (
                          <li key={`${event.occurred_at}-${index}`}>
                            {event.occurred_at} · {event.event_name}
                          </li>
                        ))}
                      </ol>
                    </details>
                  </>
                ) : (
                  <p>选择一条记录查看。</p>
                )}
              </article>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
