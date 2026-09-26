import { useCallback, useEffect, useState } from "react";

import { withAppBase } from "../../app/basePath";

interface Recommendation {
  recommendation_id: string;
  question: string;
  topic_key: string;
  source_knowledge_ids: string[];
  review_note: string;
  state: "DRAFT" | "APPROVED" | "DISABLED";
  disabled_reason: string | null;
  version: number;
  updated_at: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(withAppBase(path), {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      ...(options?.body ? { "Content-Type": "application/json" } : {}),
      ...options?.headers,
    },
  });
  if (!response.ok) throw new Error(`推荐问题保存失败（${response.status}）`);
  return (await response.json()) as T;
}

export function RecommendationManager({
  csrfToken,
  seedQuestion,
}: {
  csrfToken: string;
  seedQuestion: string;
}) {
  const [items, setItems] = useState<Recommendation[]>([]);
  const [selected, setSelected] = useState<Recommendation | null>(null);
  const [question, setQuestion] = useState("");
  const [topic, setTopic] = useState("");
  const [sourceIds, setSourceIds] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [disabledReason, setDisabledReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const result = await request<{ items: Recommendation[] }>(
      "/api/admin/ops/recommendations",
    );
    setItems(result.items);
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load).catch((cause: unknown) =>
      setError(cause instanceof Error ? cause.message : "无法读取推荐问题"),
    );
  }, [load]);

  useEffect(() => {
    if (seedQuestion) {
      void Promise.resolve().then(() => {
        setSelected(null);
        setQuestion(seedQuestion);
        setTopic("");
        setSourceIds("");
        setReviewNote("");
        setConfirmed(false);
      });
    }
  }, [seedQuestion]);

  const select = (item: Recommendation) => {
    setSelected(item);
    setQuestion(item.question);
    setTopic(item.topic_key);
    setSourceIds(item.source_knowledge_ids.join("\n"));
    setReviewNote(item.review_note);
    setDisabledReason(item.disabled_reason ?? "");
    setConfirmed(false);
  };

  const save = async (state: Recommendation["state"]) => {
    setBusy(true);
    setError("");
    try {
      const path = selected
        ? `/api/admin/ops/recommendations/${encodeURIComponent(selected.recommendation_id)}`
        : "/api/admin/ops/recommendations";
      const result = await request<Recommendation>(path, {
        method: selected ? "PUT" : "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          expected_version: selected?.version ?? null,
          question: question.trim(),
          topic_key: topic.trim(),
          source_knowledge_ids: sourceIds.split(/[,\n]/u).map((id) => id.trim()).filter(Boolean),
          review_note: reviewNote.trim(),
          state,
          review_confirmed: state === "APPROVED" && confirmed,
          disabled_reason: state === "DISABLED" ? disabledReason.trim() : null,
        }),
      });
      await load();
      select(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "推荐问题保存失败");
    } finally {
      setBusy(false);
    }
  };

  const ready = Boolean(question.trim() && topic.trim());
  const hasSources = Boolean(sourceIds.split(/[,\n]/u).some((id) => id.trim()));

  return (
    <section className="wst-ops-recommendations" aria-label="推荐问题审核目录">
      <h3>推荐问题审核目录</h3>
      <p>从真实高频问题创建草稿。公开题面由人工编辑并核对知识库资料；这只决定展示入口，不改变问答、检索或模型输出。</p>
      <div className="wst-ops-recommendation-list">
        <button onClick={() => { setSelected(null); setQuestion(""); setTopic(""); setSourceIds(""); setReviewNote(""); setConfirmed(false); }} type="button">新建空白题目</button>
        {items.map((item) => (
          <button aria-pressed={selected?.recommendation_id === item.recommendation_id} key={item.recommendation_id} onClick={() => select(item)} type="button">
            {item.question} · {item.state} · v{item.version}
          </button>
        ))}
      </div>
      <div className="wst-ops-recommendation-editor">
        <label htmlFor="wst-recommendation-question">可公开题面</label>
        <textarea id="wst-recommendation-question" maxLength={500} onChange={(event) => { setQuestion(event.target.value); setConfirmed(false); }} value={question} />
        <label htmlFor="wst-recommendation-topic">主题</label>
        <input id="wst-recommendation-topic" maxLength={100} onChange={(event) => setTopic(event.target.value)} value={topic} />
        <label htmlFor="wst-recommendation-sources">已核对的知识库文件 ID（每行一个）</label>
        <textarea id="wst-recommendation-sources" onChange={(event) => { setSourceIds(event.target.value); setConfirmed(false); }} value={sourceIds} />
        <label htmlFor="wst-recommendation-note">审核记录</label>
        <textarea id="wst-recommendation-note" maxLength={1000} onChange={(event) => { setReviewNote(event.target.value); setConfirmed(false); }} value={reviewNote} />
        <label><input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" /> 我已核对题面和对应资料，确认可以公开</label>
        <label htmlFor="wst-recommendation-disable">下架原因</label>
        <input id="wst-recommendation-disable" maxLength={500} onChange={(event) => setDisabledReason(event.target.value)} value={disabledReason} />
        <div className="wst-ops-actions">
          <button disabled={busy || !ready} onClick={() => void save("DRAFT")} type="button">保存草稿</button>
          <button disabled={busy || !ready || !hasSources || !reviewNote.trim() || !confirmed} onClick={() => void save("APPROVED")} type="button">审核发布</button>
          <button disabled={busy || !selected || !disabledReason.trim()} onClick={() => void save("DISABLED")} type="button">立即下架</button>
        </div>
        {error && <p className="wst-ops-error" role="alert">{error}</p>}
      </div>
    </section>
  );
}
