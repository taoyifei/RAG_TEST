import { useEffect, useState } from "react";

import { api, type ProductFeedback } from "../api/client";
import { ErrorPanel } from "./ui";

const REASONS = [
  ["", "不填写原因"],
  ["INCORRECT", "内容不正确"],
  ["INCOMPLETE", "回答不完整"],
  ["WRONG_SOURCE", "来源不合适"],
  ["OUTDATED", "内容已过期"],
  ["TOO_SLOW", "响应太慢"],
  ["OTHER", "其他"],
] as const;

export function QueryFeedback({
  projectId,
  kbId,
  traceId,
}: {
  projectId: string;
  kbId: string;
  traceId: string;
}) {
  const [feedback, setFeedback] = useState<ProductFeedback | null>();
  const [reasonCode, setReasonCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  useEffect(() => {
    const controller = new AbortController();
    void api
      .getFeedback(projectId, kbId, traceId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setFeedback(value.feedback);
          setReasonCode(value.feedback?.reason_code ?? "");
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason);
      });
    return () => controller.abort();
  }, [projectId, kbId, traceId]);

  async function submit(useful: boolean) {
    setBusy(true);
    setError(undefined);
    try {
      const value = await api.putFeedback(
        projectId,
        kbId,
        traceId,
        useful,
        useful ? undefined : reasonCode || undefined,
      );
      setFeedback(value);
      setReasonCode(value.reason_code ?? "");
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="query-feedback" aria-label="回答反馈">
      <div>
        <strong>这个结果有帮助吗？</strong>
        <small>
          只保存选择和有限原因码，不保存新的问题、答案或自由文本。
        </small>
      </div>
      <div className="row-actions">
        <button
          type="button"
          aria-pressed={feedback?.useful === true}
          disabled={busy}
          onClick={() => void submit(true)}
        >
          有用
        </button>
        <label>
          无用原因
          <select
            value={reasonCode}
            disabled={busy}
            onChange={(event) => setReasonCode(event.target.value)}
          >
            {REASONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          aria-pressed={feedback?.useful === false}
          disabled={busy}
          onClick={() => void submit(false)}
        >
          无用
        </button>
      </div>
      {feedback && (
        <p role="status">
          当前反馈：{feedback.useful ? "有用" : "无用"}
          {feedback.reason_code ? ` · ${feedback.reason_code}` : ""}
          {feedback.projection_state === "PENDING"
            ? " · 技术 Trace 投影待恢复"
            : ""}
        </p>
      )}
      {feedback === undefined && error === undefined && (
        <small role="status">正在读取反馈…</small>
      )}
      {error !== undefined && <ErrorPanel error={error} />}
    </section>
  );
}
