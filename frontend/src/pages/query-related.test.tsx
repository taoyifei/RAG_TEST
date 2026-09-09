import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { api, type Evidence, type QueryResponse } from "../api/client";
import { QueryPage } from "./QueryPage";

const consoleState = vi.hoisted(() => ({
  tokens: { query: "session", admin: "session" },
  scope: { projectId: "prj_a", kbId: "kb_a", revisionId: "irev_a" },
}));
vi.mock("../state/console-context", () => ({ useConsole: () => consoleState }));

const excerpt =
  "<script>alert(1)</script> [外链](https://evil.example) 隐私专员负责处理请求。";
const diagnosticEvidence: Evidence = {
  metadata: [],
  evidence_id: "synthetic",
  chunk_id: "chunk_a",
  citation_text: "未获正式资格的原文",
  source_label: "采购流程.docx",
  source_spans: [],
  retrieval_origins: ["lexical"],
  selection_reason: "retrieval_candidate",
  publishable: true,
  table_context: false,
  heading_path: [],
  quality_flags: [],
};
function response(overrides: Partial<QueryResponse> = {}): QueryResponse {
  return {
    active_index_revision_id: "irev_a",
    cache_hit: false,
    cache_key: "sha256:synthetic",
    result_origin: "fresh",
    generation_called_this_request: false,
    rewrite_called_this_request: false,
    confidence: {
      status: "INSUFFICIENT_EVIDENCE",
      score: 0,
      provisional: true,
      reason_codes: [],
      feature_values: [],
    },
    query_kind: "simple_fact",
    index_fingerprint: "sha256:synthetic",
    serving_fingerprint: "sha256:synthetic",
    route_reason_code: "LEXICAL_ONLY",
    rerank_execution_mode: "bypass",
    generation_mode: "none",
    query_id: "trace_a",
    project_id: "prj_a",
    knowledge_base_id: "kb_a",
    index_revision_id: "irev_a",
    dense_available: false,
    rerank_mode: "bypass",
    quality_profile_status: "offline_validated_remote_uncalibrated",
    degraded_reason_codes: [],
    status: "INSUFFICIENT_EVIDENCE",
    answer: null,
    evidence: [],
    evidence_count: 0,
    reason_code: "INSUFFICIENT_SUPPORT",
    trace_id: "trace_a",
    display_message:
      "本次检索未找到足以直接回答这个问题的依据。下面这些内容可能相关，供你查阅。",
    related_contents: [
      {
        related_id: "related_a",
        document_id: "doc_a",
        document_version_id: "dver_a",
        index_revision_id: "irev_a",
        chunk_id: "chunk_a",
        document_name: "隐私手册.docx",
        heading_path: ["联系方式"],
        excerpt,
        source_spans: [],
        is_answer_evidence: false,
        relevance_reason: "RELEVANCE_UNVERIFIED",
        rerank_verified: false,
      },
    ],
    ...overrides,
  };
}
async function submit() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("查询文本"), "隐私专员电话");
  await user.click(screen.getByRole("button", { name: "执行" }));
  return user;
}
beforeEach(() => {
  vi.restoreAllMocks();
  consoleState.scope.kbId = "kb_a";
  consoleState.tokens.admin = "session";
  consoleState.tokens.query = "session";
});

it("拒答相关原文独立展示、纯文本转义，查看原文重新授权", async () => {
  const answer = vi.spyOn(api, "answerStream").mockResolvedValue(response());
  const read = vi
    .spyOn(api, "readRelatedSource")
    .mockRejectedValue(new Error("原文当前不可读取。"));
  const { container } = render(<QueryPage mode="answer" />);
  const user = await submit();
  const related = await screen.findByRole("region", { name: "相关内容" });
  expect(within(related).getByText(excerpt)).toBeVisible();
  expect(
    within(related).getByText("未完成相关性复核，仅供查阅。"),
  ).toBeVisible();
  expect(container.querySelector("script")).toBeNull();
  expect(within(related).queryByRole("link")).toBeNull();
  expect(screen.queryByRole("region", { name: "正式答案" })).toBeNull();
  expect(answer.mock.calls[0][5]).toBe(true);
  await user.click(within(related).getByRole("button", { name: "查看原文" }));
  expect(await screen.findByText("原文当前不可读取。")).toBeVisible();
  expect(read).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("region", { name: "相关内容" })).toBeNull();
  expect(answer).toHaveBeenCalledTimes(1);
});

it("空结果不渲染空卡片标题；正常答案独立提供原始答案文本", async () => {
  vi.spyOn(api, "answerStream")
    .mockResolvedValueOnce(response({ related_contents: [] }))
    .mockResolvedValueOnce(
      response({
        status: "ANSWERABLE",
        answer: "已确认内容",
        related_contents: [],
      }),
    );
  render(<QueryPage mode="answer" />);
  const user = await submit();
  expect(await screen.findByText(/本次检索未找到足以/)).toBeVisible();
  expect(screen.queryByRole("region", { name: "相关内容" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(
    await screen.findByRole("region", { name: "正式答案" }),
  ).toHaveAttribute("data-raw-answer", "已确认内容");
});

it("知识库切换和晚到响应不能重新显示旧原文", async () => {
  let resolve!: (value: QueryResponse) => void;
  let oldHandlers!: NonNullable<Parameters<typeof api.answerStream>[7]>;
  vi.spyOn(api, "answerStream").mockImplementation(
    (...args) => {
      oldHandlers = args[7] ?? {};
      return (
      new Promise((done) => {
        resolve = done;
      })
      );
    },
  );
  const view = render(<QueryPage mode="answer" />);
  await submit();
  consoleState.scope.kbId = "kb_b";
  view.rerender(<QueryPage mode="answer" />);
  await act(async () => {
    oldHandlers.onClaim?.({
      claim_index: 0,
      text: "旧知识库暂存事实",
      supports: [{ support_id: "S1", quote: "旧知识库原文" }],
      active_index_revision_id: "irev_old",
    });
    resolve(response());
    await Promise.resolve();
  });
  expect(screen.queryByText(excerpt)).toBeNull();
  expect(screen.queryByText("旧知识库暂存事实")).toBeNull();
});

it("合法 claim 立即显示为暂存内容，final 到达后由正式答案替代", async () => {
  let resolve!: (value: QueryResponse) => void;
  let handlers!: NonNullable<Parameters<typeof api.answerStream>[7]>;
  vi.spyOn(api, "answerStream").mockImplementation((...args) => {
    handlers = args[7] ?? {};
    return new Promise((done) => {
      resolve = done;
    });
  });
  render(<QueryPage mode="answer" />);
  await submit();

  act(() => {
    handlers.onClaim?.({
      claim_index: 0,
      text: "资料员每周核对设备清单。",
      supports: [
        { support_id: "support-1", quote: "每周核对设备清单" },
      ],
      active_index_revision_id: "irev_stream",
    });
  });
  expect(
    screen.getByRole("region", { name: "已核验暂存内容" }),
  ).toHaveTextContent("资料员每周核对设备清单。");
  expect(screen.queryByRole("region", { name: "正式答案" })).toBeNull();

  await act(async () => {
    resolve(
      response({
        status: "ANSWERABLE",
        answer: "资料员每周核对设备清单。 [support-1]",
        related_contents: [],
      }),
    );
    await Promise.resolve();
  });
  expect(
    await screen.findByRole("region", { name: "正式答案" }),
  ).toHaveTextContent("资料员每周核对设备清单。 [support-1]");
  expect(
    screen.queryByRole("region", { name: "已核验暂存内容" }),
  ).toBeNull();
});

it("停止按钮中断当前 fetch，且停止后的晚到 claim 不再显示", async () => {
  let signal!: AbortSignal;
  let handlers!: NonNullable<Parameters<typeof api.answerStream>[7]>;
  vi.spyOn(api, "answerStream").mockImplementation((...args) => {
    signal = args[4] as AbortSignal;
    handlers = args[7] ?? {};
    return new Promise((_resolve, reject) => {
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("synthetic abort", "AbortError")),
        { once: true },
      );
    });
  });
  render(<QueryPage mode="answer" />);
  const user = await submit();
  await user.click(screen.getByRole("button", { name: "停止" }));
  expect(signal.aborted).toBe(true);
  expect(await screen.findByRole("button", { name: "执行" })).toBeEnabled();
  expect(screen.getByText(/暂存内容不是最终答案/)).toBeVisible();

  act(() => {
    handlers.onClaim?.({
      claim_index: 0,
      text: "停止后到达的内容",
      supports: [{ support_id: "S1", quote: "不得显示" }],
      active_index_revision_id: "irev_stopped",
    });
  });
  expect(screen.queryByText("停止后到达的内容")).toBeNull();
});

it("搜索拒答保留未发布候选检查，管理员身份变化清除原文", async () => {
  vi.spyOn(api, "search").mockResolvedValue(
    response({
      evidence: [diagnosticEvidence],
    }),
  );
  vi.spyOn(api, "diagnostics").mockRejectedValue(
    new Error("synthetic diagnostics unavailable"),
  );
  const view = render(<QueryPage mode="search" />);
  const user = await submit();
  expect(await screen.findByRole("region", { name: "相关内容" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "引用依据" })).toBeNull();
  const candidates = screen.getByRole("region", { name: "检索候选" });
  expect(candidates).toHaveAttribute(
    "data-content-role",
    "diagnostic-evidence",
  );
  expect(within(candidates).getByText("未发布候选 · 排序 —")).toBeVisible();
  await user.click(
    within(candidates).getByRole("button", { name: /采购流程/ }),
  );
  const drawer = screen.getByRole("dialog", { name: "证据详情" });
  expect(within(drawer).getByText("检索候选（未发布）")).toBeVisible();
  expect(within(drawer).queryByText("引用依据")).toBeNull();
  expect(within(drawer).getByText("本次未发布为答案引用")).toBeVisible();
  consoleState.tokens.admin = "changed-session";
  view.rerender(<QueryPage mode="search" />);
  expect(screen.queryByRole("region", { name: "相关内容" })).toBeNull();
  expect(screen.queryByRole("dialog", { name: "证据详情" })).toBeNull();
});

it("问答拒答不开放诊断候选，正常答案才展示正式引用", async () => {
  vi.spyOn(api, "answerStream")
    .mockResolvedValueOnce(response({ evidence: [diagnosticEvidence] }))
    .mockResolvedValueOnce(
      response({
        status: "ANSWERABLE",
        answer: "已确认内容",
        evidence: [diagnosticEvidence],
        related_contents: [],
      }),
    );
  render(<QueryPage mode="answer" />);
  const user = await submit();
  expect(await screen.findByRole("region", { name: "相关内容" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "检索候选" })).toBeNull();
  expect(screen.queryByText("未获正式资格的原文")).toBeNull();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(await screen.findByRole("region", { name: "引用依据" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "检索候选" })).toBeNull();
});

it("准确区分本次模型生成、缓存、预算回退、无授权与澄清", async () => {
  vi.spyOn(api, "answerStream")
    .mockResolvedValueOnce(
      response({
        status: "ANSWERABLE",
        answer: "模型答案",
        related_contents: [],
        display_message: null,
        generation_mode: "llm",
        result_origin: "fresh",
        generation_called_this_request: true,
        rewrite_called_this_request: false,
      }),
    )
    .mockResolvedValueOnce(
      response({
        status: "ANSWERABLE",
        answer: "模型答案",
        related_contents: [],
        display_message: null,
        generation_mode: "llm",
        cache_hit: true,
        result_origin: "cache",
        generation_called_this_request: false,
        rewrite_called_this_request: false,
      }),
    )
    .mockResolvedValueOnce(
      response({
        status: "ANSWERABLE",
        answer: "证据摘录",
        related_contents: [],
        display_message: null,
        generation_mode: "extractive_fallback",
        generation_reason_code: "BLOCKED_BUDGET",
        generation_called_this_request: false,
      }),
    )
    .mockResolvedValueOnce(
      response({
        related_contents: [],
        display_message: null,
        generation_reason_code: "DATA_EGRESS_NOT_AUTHORIZED",
      }),
    )
    .mockResolvedValueOnce(
      response({
        status: "AMBIGUOUS_NEEDS_CLARIFICATION",
        related_contents: [],
        display_message: null,
      }),
    );
  render(<QueryPage mode="answer" />);
  const user = await submit();
  expect(
    await screen.findByText(/本次答案由回答模型.*已通过来源校验/),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(await screen.findByText(/本次结果来自查询缓存/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(await screen.findByText(/因预算不可用.*回退/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(await screen.findByText(/未获本次资料出网授权/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "执行" }));
  expect(await screen.findByText(/请补充对象、范围或所问关系/)).toBeVisible();
});
