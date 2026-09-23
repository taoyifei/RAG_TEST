import { useEffect, useState } from "react";

import { ErrorPanel } from "../../components/ui";
import {
  wanshitongAdminApi,
  type QuestionAnalyticsItem,
  type Recommendation,
  type RecommendationSource,
  type RecommendationState,
  type WanshitongDocument,
} from "./adminApi";

function sourceKey(source: RecommendationSource): string {
  return `${source.document_id}:${source.version_id}`;
}

export function RecommendationCatalogSection({
  availableGroups,
  selectedId,
}: {
  availableGroups: readonly QuestionAnalyticsItem[];
  selectedId?: string;
}) {
  const [items, setItems] = useState<Recommendation[]>([]);
  const [selected, setSelected] = useState<Recommendation>();
  const [question, setQuestion] = useState("");
  const [topic, setTopic] = useState("");
  const [style, setStyle] = useState<Recommendation["question_style"]>("SHORT");
  const [aliases, setAliases] = useState<string[]>([]);
  const [sources, setSources] = useState<RecommendationSource[]>([]);
  const [disabledReason, setDisabledReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [documents, setDocuments] = useState<WanshitongDocument[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void wanshitongAdminApi
      .recommendations(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setItems(result.items);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      });
    void wanshitongAdminApi
      .listDocuments(undefined, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setDocuments(result.items);
        setNextCursor(result.next_cursor);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    const found = items.find((item) => item.recommendation_id === selectedId);
    if (found) select(found);
  }, [items, selectedId]);

  function select(item: Recommendation) {
    setSelected(item);
    setQuestion(item.question_text);
    setTopic(item.topic_key);
    setStyle(item.question_style);
    setAliases(item.alias_keys);
    setSources(item.validated_sources);
    setDisabledReason(item.disabled_reason ?? "");
    setConfirmed(false);
    setError(undefined);
  }

  async function loadMoreDocuments() {
    if (!nextCursor) return;
    setBusy(true);
    try {
      const result = await wanshitongAdminApi.listDocuments(nextCursor);
      setDocuments((current) => [
        ...current,
        ...result.items.filter(
          (item) =>
            !current.some((known) => known.document_id === item.document_id),
        ),
      ]);
      setNextCursor(result.next_cursor);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function save(state: RecommendationState) {
    if (!selected) return;
    setBusy(true);
    setError(undefined);
    try {
      const result = await wanshitongAdminApi.updateRecommendation(
        selected.recommendation_id,
        {
          expected_version: selected.version,
          question_text: question.trim(),
          question_style: style,
          topic_key: topic.trim(),
          state,
          alias_keys: aliases,
          validated_sources: sources,
          review_confirmed: state === "APPROVED" && confirmed,
          disabled_reason: state === "DISABLED" ? disabledReason.trim() : null,
        },
      );
      setItems((current) =>
        current.map((item) =>
          item.recommendation_id === result.recommendation_id ? result : item,
        ),
      );
      select(result);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  function toggleAlias(key: string) {
    setAliases((current) =>
      current.includes(key)
        ? current.filter((value) => value !== key)
        : [...current, key],
    );
    setConfirmed(false);
  }

  function toggleSource(source: RecommendationSource) {
    const key = sourceKey(source);
    setSources((current) =>
      current.some((value) => sourceKey(value) === key)
        ? current.filter((value) => sourceKey(value) !== key)
        : [...current, source],
    );
    setConfirmed(false);
  }

  return (
    <div className="recommendation-catalog">
      <h3>公共问题审核目录</h3>
      <p>
        原始问题只供管理员参考；修改成可公开、独立的问题后核对资料版本并审核。
      </p>
      {error !== undefined && <ErrorPanel error={error} />}
      {items.length === 0 && <p>暂无候选。可从高频问题榜创建。</p>}
      <div className="recommendation-catalog-items">
        {items.map((item) => (
          <button
            aria-pressed={
              selected?.recommendation_id === item.recommendation_id
            }
            key={item.recommendation_id}
            onClick={() => select(item)}
            type="button"
          >
            {item.question_text} · {item.state} · v{item.version}
          </button>
        ))}
      </div>
      {selected && (
        <div className="recommendation-editor">
          <label>
            可公开题面
            <textarea
              maxLength={500}
              onChange={(event) => {
                setQuestion(event.target.value);
                setConfirmed(false);
              }}
              value={question}
            />
          </label>
          <label>
            主题
            <input
              maxLength={100}
              onChange={(event) => setTopic(event.target.value)}
              value={topic}
            />
          </label>
          <label>
            问题形式
            <select
              onChange={(event) =>
                setStyle(event.target.value as Recommendation["question_style"])
              }
              value={style}
            >
              <option value="SHORT">短问</option>
              <option value="STANDARD">标准</option>
              <option value="COMPOUND">复合</option>
            </select>
          </label>
          <fieldset>
            <legend>确认等义问题组</legend>
            <p>
              只选择与公开题面同义的精确组。更改后须重新统计才会显示新热度。
            </p>
            {availableGroups
              .filter((group) => group.group_kind === "EXACT")
              .map((group) => (
                <label key={group.group_key}>
                  <input
                    checked={aliases.includes(group.group_key)}
                    onChange={() => toggleAlias(group.group_key)}
                    type="checkbox"
                  />
                  {group.representative_question ?? group.group_key}
                </label>
              ))}
            {aliases
              .filter(
                (key) =>
                  !availableGroups.some((group) => group.group_key === key),
              )
              .map((key) => (
                <label key={key}>
                  <input
                    checked
                    onChange={() => toggleAlias(key)}
                    type="checkbox"
                  />
                  已关联问题键 {key.slice(0, 12)}…
                </label>
              ))}
          </fieldset>
          <fieldset>
            <legend>已核对的当前资料版本</legend>
            <p>只选实际读过或在测试环境核对过的资料；版本失效会立即下架。</p>
            {documents
              .filter(
                (document) =>
                  document.current_version_id && document.retrievable,
              )
              .map((document) => {
                const source = {
                  document_id: document.document_id,
                  version_id: document.current_version_id!,
                };
                return (
                  <label key={sourceKey(source)}>
                    <input
                      checked={sources.some(
                        (value) => sourceKey(value) === sourceKey(source),
                      )}
                      onChange={() => toggleSource(source)}
                      type="checkbox"
                    />
                    {document.display_name}
                  </label>
                );
              })}
            {sources
              .filter(
                (source) =>
                  !documents.some(
                    (document) =>
                      document.document_id === source.document_id &&
                      document.current_version_id === source.version_id,
                  ),
              )
              .map((source) => (
                <label key={sourceKey(source)}>
                  <input
                    checked
                    onChange={() => toggleSource(source)}
                    type="checkbox"
                  />
                  旧版或未加载资料 {source.document_id}
                </label>
              ))}
            {nextCursor && (
              <button
                disabled={busy}
                onClick={() => void loadMoreDocuments()}
                type="button"
              >
                加载更多资料
              </button>
            )}
          </fieldset>
          {!selected.source_current && selected.state === "APPROVED" && (
            <p role="alert">已核对资料版本发生变化，公共榜不会继续展示本题。</p>
          )}
          <label>
            <input
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              type="checkbox"
            />
            我已核对题面、等义关系和所选资料版本，确认可以公开
          </label>
          <label>
            下架原因
            <input
              maxLength={500}
              onChange={(event) => setDisabledReason(event.target.value)}
              value={disabledReason}
            />
          </label>
          <div className="row-actions">
            <button
              disabled={busy || !question.trim() || !topic.trim()}
              onClick={() => void save("DRAFT")}
              type="button"
            >
              保存草稿
            </button>
            <button
              disabled={
                busy ||
                !confirmed ||
                aliases.length === 0 ||
                sources.length === 0
              }
              onClick={() => void save("APPROVED")}
              type="button"
            >
              审核通过
            </button>
            <button
              disabled={busy || !disabledReason.trim()}
              onClick={() => void save("DISABLED")}
              type="button"
            >
              立即下架
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
