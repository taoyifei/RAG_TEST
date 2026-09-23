import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { withAppBase } from "../../app/basePath";
import { wanshitongRoutes } from "../../app/router";
import { historyTime } from "../../components/HistoryTrace";
import { EmptyState, ErrorPanel, StatusBadge } from "../../components/ui";
import {
  wanshitongAdminApi,
  type QuestionAnalyticsBoard,
  type QuestionAnalyticsItem,
  type QuestionAnalyticsPage,
  type QuestionAnalyticsRun,
  type QuestionAnalyticsSamples,
} from "./adminApi";

const PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_POLL_ERRORS = 3;

const boards: ReadonlyArray<[QuestionAnalyticsBoard, string, string]> = [
  ["frequent", "高频问题", "按受信人数、用户日热度和请求次数排序"],
  ["unresolved", "高频但答不好", "按已确认未解决问题、受信人数和请求次数排序"],
];

const runLabels: Record<QuestionAnalyticsRun["state"], string> = {
  BUILDING: "统计中",
  COMPLETE: "已完成",
  FAILED: "统计失败",
  LIMITED: "超出有界统计范围",
};

function coverageLabel(run: QuestionAnalyticsRun): string {
  if (!run.coverage_start) return "有效采集起点未标记";
  const start = Math.max(
    Date.parse(run.window_start),
    Date.parse(run.coverage_start),
  );
  const end = Date.parse(run.window_end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return `有效采集从 ${historyTime(run.coverage_start)} 起`;
  }
  const days = (end - start) / 86_400_000;
  return `有效采集从 ${historyTime(run.coverage_start)} 起，约 ${days.toFixed(1)} 天`;
}

function helpfulRate(item: QuestionAnalyticsItem): string {
  return item.feedback_count === 0
    ? "未评价"
    : `${((item.helpful_count / item.feedback_count) * 100).toFixed(1)}%`;
}

function runStatus(
  run: QuestionAnalyticsRun,
  published: QuestionAnalyticsRun | null,
) {
  if (run.state === "BUILDING") {
    return published
      ? "新统计正在运行；完成前继续显示上一份完整结果。"
      : "新统计正在运行；完成后才会显示榜单。";
  }
  if (run.state === "FAILED" || run.state === "LIMITED") {
    const detail = run.failure_code ? `（${run.failure_code}）` : "";
    return `最近一次统计${runLabels[run.state]}${detail}；${published ? "当前显示上一份完整结果。" : "尚无完整结果。"}`;
  }
  return "";
}

export function QuestionAnalyticsSection({
  onInspectFeedback,
}: {
  onInspectFeedback: (traceId: string) => void;
}) {
  const [board, setBoard] = useState<QuestionAnalyticsBoard>("frequent");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<QuestionAnalyticsPage>();
  const [latestRun, setLatestRun] = useState<QuestionAnalyticsRun | null>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>();
  const [refreshError, setRefreshError] = useState<unknown>();
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [pollEpoch, setPollEpoch] = useState(0);
  const [pollingStalled, setPollingStalled] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<string>();
  const [samples, setSamples] = useState<QuestionAnalyticsSamples>();
  const [samplesError, setSamplesError] = useState<unknown>();
  const [samplesLoading, setSamplesLoading] = useState(false);
  const samplesController = useRef<AbortController | null>(null);
  const activeRunId = latestRun?.state === "BUILDING" ? latestRun.run_id : null;

  useEffect(() => {
    const controller = new AbortController();
    void wanshitongAdminApi
      .questionAnalytics(board, offset, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setPage(value);
        setLatestRun(value.latest_run);
        setError(undefined);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [board, offset, reload]);

  useEffect(() => {
    if (!activeRunId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let consecutiveErrors = 0;
    const poll = async () => {
      try {
        const next = await wanshitongAdminApi.questionAnalyticsRun(
          activeRunId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        consecutiveErrors = 0;
        setRefreshError(undefined);
        setPollingStalled(false);
        setLatestRun(next);
        if (next.state === "BUILDING") {
          timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);
        } else {
          if (next.state === "COMPLETE") {
            samplesController.current?.abort();
            setSelectedGroup(undefined);
            setSamples(undefined);
            setOffset(0);
          }
          setLoading(true);
          setReload((value) => value + 1);
        }
      } catch (reason) {
        if (controller.signal.aborted) return;
        consecutiveErrors += 1;
        setRefreshError(reason);
        if (consecutiveErrors < MAX_CONSECUTIVE_POLL_ERRORS) {
          timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);
        } else {
          setPollingStalled(true);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [activeRunId, pollEpoch]);

  useEffect(() => () => samplesController.current?.abort(), []);

  function closeSamples() {
    samplesController.current?.abort();
    samplesController.current = null;
    setSelectedGroup(undefined);
    setSamples(undefined);
    setSamplesError(undefined);
    setSamplesLoading(false);
  }

  function turnBoard(next: QuestionAnalyticsBoard) {
    if (next === board) return;
    closeSamples();
    setBoard(next);
    setOffset(0);
    setPage(undefined);
    setLoading(true);
    setError(undefined);
  }

  function turnPage(next: number) {
    closeSamples();
    setOffset(next);
    setPage(undefined);
    setLoading(true);
    setError(undefined);
  }

  function reloadPage() {
    closeSamples();
    setLoading(true);
    setError(undefined);
    setRefreshError(undefined);
    setPollingStalled(false);
    setReload((value) => value + 1);
    setPollEpoch((value) => value + 1);
  }

  async function refresh() {
    setRefreshBusy(true);
    setRefreshError(undefined);
    setPollingStalled(false);
    try {
      const run = await wanshitongAdminApi.refreshQuestionAnalytics();
      setLatestRun(run);
      if (run.state !== "BUILDING") {
        if (run.state === "COMPLETE") setOffset(0);
        reloadPage();
      }
    } catch (reason) {
      setRefreshError(reason);
    } finally {
      setRefreshBusy(false);
    }
  }

  function openSamples(item: QuestionAnalyticsItem) {
    if (!page?.run) return;
    if (selectedGroup === item.group_key) {
      closeSamples();
      return;
    }
    samplesController.current?.abort();
    const controller = new AbortController();
    samplesController.current = controller;
    setSelectedGroup(item.group_key);
    setSamples(undefined);
    setSamplesError(undefined);
    setSamplesLoading(true);
    void wanshitongAdminApi
      .questionAnalyticsSamples(
        page.run.run_id,
        item.group_key,
        controller.signal,
      )
      .then((value) => {
        if (!controller.signal.aborted) setSamples(value);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setSamplesError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) setSamplesLoading(false);
      });
  }

  const published = page?.run?.state === "COMPLETE" ? page.run : null;
  const stateMessage = pollingStalled
    ? "任务状态连续读取失败；点击“刷新显示”重新检查。当前完整快照仍可查看。"
    : latestRun
      ? runStatus(latestRun, published)
      : "";
  const boardDescription = boards.find(([value]) => value === board)?.[2];

  return (
    <section
      className="panel question-analytics"
      aria-labelledby="question-analytics-title"
    >
      <div className="section-heading question-analytics-heading">
        <div>
          <h2 id="question-analytics-title">问题运营榜</h2>
          <p>按已完成的统计快照查看真实需求与需要优先处理的问题。</p>
        </div>
        <div className="row-actions">
          <button
            className="secondary"
            disabled={loading}
            onClick={reloadPage}
            type="button"
          >
            刷新显示
          </button>
          <button
            className="primary"
            disabled={loading || refreshBusy || latestRun?.state === "BUILDING"}
            onClick={() => void refresh()}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={17} />
            {refreshBusy ? "正在提交…" : "重新统计"}
          </button>
        </div>
      </div>

      <div
        className="question-analytics-tabs"
        role="group"
        aria-label="问题运营榜类型"
      >
        {boards.map(([value, label]) => (
          <button
            aria-pressed={board === value}
            className={board === value ? "active" : ""}
            key={value}
            onClick={() => turnBoard(value)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      <p className="question-analytics-sort">{boardDescription}</p>
      {error !== undefined && <ErrorPanel error={error} />}
      {refreshError !== undefined && <ErrorPanel error={refreshError} />}
      {stateMessage && (
        <p className="question-analytics-run-state" role="status">
          {stateMessage}
        </p>
      )}
      {loading && <p role="status">正在读取问题运营榜…</p>}

      {published && (
        <div className="question-analytics-coverage">
          <p>
            <strong>统计窗口：</strong>
            <time dateTime={published.window_start}>
              {historyTime(published.window_start)}
            </time>
            {" 至 "}
            <time dateTime={published.window_end}>
              {historyTime(published.window_end)}
            </time>
            {"（结束时刻不计入）"}
          </p>
          <p>
            <strong>有效覆盖：</strong>
            {coverageLabel(published)} · 快照观察于{" "}
            {historyTime(published.observed_at)}
          </p>
          <p>
            <strong>更新：</strong>
            {historyTime(published.finished_at || published.created_at)} · 部署{" "}
            {published.deployment_id} · 有效 {published.eligible_count} / 来源{" "}
            {published.source_total} · 未知流量 {published.unknown_count} · 排除{" "}
            {published.excluded_count} · 身份未知{" "}
            {published.unknown_identity_count} · 待终态{" "}
            {published.pending_count}
          </p>
          <p>
            缺正文 {published.body_missing_count} · 不可读取{" "}
            {published.unreadable_count}。
            人数只统计受信主体；回答次数不代表答案正确。
          </p>
        </div>
      )}

      {!loading && error === undefined && !published && (
        <EmptyState title="尚无完整统计快照">
          可点击“重新统计”；失败或超限的任务不会替换上一次完整结果。
        </EmptyState>
      )}
      {published && !loading && page?.items.length === 0 && (
        <EmptyState title="当前榜单没有可展示的题组">
          窗口内无符合条件的题组，或可用问题正文尚未覆盖到这段时间。
        </EmptyState>
      )}
      {published && page && page.items.length > 0 && (
        <div className="table-wrap question-analytics-table">
          <table>
            <caption className="sr-only">
              {board === "frequent" ? "高频问题" : "高频但答不好"}
            </caption>
            <thead>
              <tr>
                <th scope="col">问题组</th>
                <th scope="col">受信人数 / 用户日</th>
                <th scope="col">请求来源</th>
                <th scope="col">实际终态</th>
                <th scope="col">反馈与待办</th>
                <th scope="col">最近出现</th>
                <th scope="col">样本</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.group_key}>
                  <td>
                    <strong>
                      {item.representative_question || "题面当前不可用"}
                    </strong>
                    <small>
                      {item.group_kind === "CONTEXT"
                        ? "上下文依赖问题"
                        : item.group_kind === "APPROVED_ALIAS"
                          ? "人工确认同义组"
                          : "原题分组"}
                    </small>
                  </td>
                  <td>
                    <strong>
                      {item.distinct_users} 人 / {item.user_day_heat} 用户日
                    </strong>
                    <small>
                      人工：{item.manual_distinct_users} 人 /{" "}
                      {item.manual_user_day_heat} 用户日
                    </small>
                  </td>
                  <td>
                    <strong>共 {item.request_count} 次</strong>
                    <small>
                      人工 {item.manual_request_count} · 推荐{" "}
                      {item.suggestion_count} · 热门 {item.popular_count}
                    </small>
                    <small>
                      重试 {item.retry_count} · 入口未知{" "}
                      {item.unknown_entry_count}
                    </small>
                  </td>
                  <td>
                    <strong>回答 {item.answered_count}</strong>
                    <small>
                      拒答 {item.refused_count} · 系统失败 {item.failed_count}
                    </small>
                  </td>
                  <td>
                    <strong>帮助率 {helpfulRate(item)}</strong>
                    <small>
                      反馈 {item.feedback_count} · 负反馈{" "}
                      {item.negative_feedback_count}
                    </small>
                    <small>
                      误拒 {item.false_refusal_count} · 已确认未解决{" "}
                      {item.confirmed_open_issue_count}
                    </small>
                  </td>
                  <td>
                    <time dateTime={item.last_seen_at}>
                      {historyTime(item.last_seen_at)}
                    </time>
                  </td>
                  <td>
                    <button
                      aria-expanded={selectedGroup === item.group_key}
                      onClick={() => openSamples(item)}
                      type="button"
                    >
                      {selectedGroup === item.group_key
                        ? "收起样本"
                        : "查看样本"}
                    </button>
                    {selectedGroup === item.group_key && (
                      <div className="question-analytics-samples">
                        {samplesLoading && (
                          <p role="status">正在读取可用样本…</p>
                        )}
                        {samplesError !== undefined && (
                          <ErrorPanel error={samplesError} />
                        )}
                        {!samplesLoading && samples?.items.length === 0 && (
                          <p>样本当前不可用或已到期。</p>
                        )}
                        {samples?.items.map((sample) => (
                          <div
                            className="question-analytics-sample"
                            key={sample.trace_id}
                          >
                            <strong>
                              {sample.question || "题面当前不可用"}
                            </strong>
                            <small>
                              {historyTime(sample.created_at)} ·{" "}
                              <StatusBadge value={sample.status} />
                            </small>
                            <div className="row-actions">
                              <a
                                href={withAppBase(
                                  `${wanshitongRoutes.history}?trace_id=${encodeURIComponent(sample.trace_id)}`,
                                )}
                              >
                                查看问答历史
                              </a>
                              {sample.has_feedback && (
                                <button
                                  onClick={() =>
                                    onInspectFeedback(sample.trace_id)
                                  }
                                  type="button"
                                >
                                  查看反馈详情
                                </button>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {published && page && (
        <div
          className="row-actions feedback-pagination question-analytics-pagination"
          aria-label="问题运营榜分页"
        >
          <span>
            共 {page.total} 组 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页
          </span>
          <button
            disabled={offset === 0 || loading}
            onClick={() => turnPage(Math.max(0, offset - PAGE_SIZE))}
            type="button"
          >
            上一页
          </button>
          <button
            disabled={page.next_offset === null || loading}
            onClick={() =>
              page.next_offset !== null && turnPage(page.next_offset)
            }
            type="button"
          >
            下一页
          </button>
        </div>
      )}
    </section>
  );
}
