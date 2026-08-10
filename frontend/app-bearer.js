"use strict";

// Browser Bearer compatibility entrypoint.

const tokenInput = document.querySelector("#token");
const questionInput = document.querySelector("#question");
const askButton = document.querySelector("#ask");
const clearButton = document.querySelector("#clear");
const resultSection = document.querySelector("#result");
const stageList = document.querySelector("#stages");
const answerNode = document.querySelector("#answer");
const citationsNode = document.querySelector("#citations");
const traceIdNode = document.querySelector("#trace-id");
const copyTraceButton = document.querySelector("#copy-trace");
const viewTraceLink = document.querySelector("#view-trace");
const usefulButton = document.querySelector("#feedback-useful");
const notUsefulButton = document.querySelector("#feedback-not-useful");
const conversationId = crypto.randomUUID();
let currentTraceId = null;
let activeRequestController = null;
let streamedClaims = new Map();
let streamStatus = null;

const refusalMessages = {
  EVIDENCE_INSUFFICIENT:
    "知识库中暂未找到能够支持该问题的资料。请核对名称、编号或时间，或补充相关文档。",
  NO_EVIDENCE: "知识库中暂未检索到可用资料。",
  LOW_CONFIDENCE_OCR_ONLY:
    "当前仅检索到低置信度 OCR 内容，暂不能作为可靠回答依据。",
  MODEL_UNAVAILABLE: "回答服务暂时不可用，请稍后重试并查看 Trace。",
  VALIDATION_FAILED:
    "已找到相关资料，但回答引用校验未通过，请稍后重试并查看 Trace。",
};

function authorization() {
  return { Authorization: `Bearer ${tokenInput.value}` };
}

function resetOutput() {
  stageList.replaceChildren();
  citationsNode.replaceChildren();
  answerNode.textContent = "正在处理。";
  currentTraceId = null;
  traceIdNode.textContent = "尚无";
  copyTraceButton.disabled = true;
  viewTraceLink.hidden = true;
  viewTraceLink.href = "/debug/";
  usefulButton.disabled = true;
  notUsefulButton.disabled = true;
  streamedClaims = new Map();
  streamStatus = null;
}

function renderStage(event) {
  const item = document.createElement("li");
  item.textContent = `${event.stage} · 请求累计 ${event.elapsed_ms} ms`;
  stageList.append(item);
}

function claimKey(event) {
  return `${event.claim_index}\u0000${event.text}`;
}

function renderAnswerState() {
  const claimTexts = Array.from(streamedClaims.values(), (claim) => claim.text);
  answerNode.textContent = [streamStatus, ...claimTexts]
    .filter(Boolean)
    .join("\n\n");
}

function appendSupports(supports) {
  for (const support of supports) {
    const item = document.createElement("article");
    item.className = "citation";
    const locator = document.createElement("strong");
    locator.textContent = support.locator;
    const quote = document.createElement("p");
    quote.textContent = support.quote;
    const chunk = document.createElement("code");
    chunk.textContent = support.chunk_id;
    item.append(locator, quote, chunk);
    citationsNode.append(item);
  }
}

function renderAnswerStart() {
  streamStatus = "正在生成并校验回答…";
  renderAnswerState();
}

function renderClaim(event) {
  const key = claimKey(event);
  if (streamedClaims.has(key)) return;
  streamedClaims.set(key, event);
  appendSupports(event.supports);
  streamStatus = `正在生成并校验回答… 已验证 ${streamedClaims.size} 条`;
  renderAnswerState();
}

function renderAnswerProgress(event) {
  streamStatus =
    `正在生成并校验回答… 已验证 ${event.validated_claims} 条，` +
    `已用时 ${event.elapsed_ms} ms`;
  renderAnswerState();
}

function renderInterrupted() {
  streamStatus = "回答流中断，已显示内容可能不完整";
  renderAnswerState();
}

function renderFinal(event) {
  for (const [claimIndex, claim] of event.claims.entries()) {
    renderClaim({ ...claim, claim_index: claimIndex });
  }
  streamStatus = null;
  currentTraceId = event.trace_id;
  traceIdNode.textContent = event.trace_id;
  copyTraceButton.disabled = false;
  viewTraceLink.href =
    "/debug/?trace_id=" + encodeURIComponent(event.trace_id);
  viewTraceLink.hidden = false;
  usefulButton.disabled = false;
  notUsefulButton.disabled = false;
  const canonicalAnswer =
    event.answer ||
    Array.from(streamedClaims.values(), (claim) => claim.text).join("\n\n");
  answerNode.textContent =
    event.status === "answered"
      ? [event.user_message, canonicalAnswer].filter(Boolean).join("\n\n")
      : event.user_message ||
        refusalMessages[event.refusal_code] ||
        "当前无法生成可靠回答，请稍后重试并查看 Trace。";
  resultSection.focus({ preventScroll: true });
  resultSection.scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "auto"
      : "smooth",
    block: "start",
  });
}

async function submitFeedback(useful) {
  if (currentTraceId === null) return;
  const response = await fetch("/api/feedback", {
    method: "POST",
    headers: {
      ...authorization(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ trace_id: currentTraceId, useful }),
  });
  if (response.ok) {
    usefulButton.disabled = true;
    notUsefulButton.disabled = true;
  } else {
    answerNode.textContent = `反馈失败：${response.status}`;
  }
}

async function readEvents(response) {
  if (!response.ok || response.body === null) {
    throw new Error(`请求失败：${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let finalReceived = false;

  function handleEvent(event) {
    if (event.type === "stage") renderStage(event);
    if (event.type === "answer_start") renderAnswerStart(event);
    if (event.type === "claim") renderClaim(event);
    if (event.type === "answer_progress") renderAnswerProgress(event);
    if (event.type === "final") {
      renderFinal(event);
      finalReceived = true;
    }
    if (event.type === "error") {
      throw new Error("QUERY_FAILED");
    }
  }

  while (true) {
    const result = await reader.read();
    pending += decoder.decode(result.value || new Uint8Array(), {
      stream: !result.done,
    });
    const lines = pending.split("\n");
    pending = lines.pop() || "";
    for (const line of lines) {
      if (line.length === 0) continue;
      handleEvent(JSON.parse(line));
    }
    if (result.done) {
      if (pending.trim().length > 0) {
        handleEvent(JSON.parse(pending));
      }
      if (!finalReceived) throw new Error("ANSWER_STREAM_INCOMPLETE");
      return;
    }
  }
}

askButton.addEventListener("click", async () => {
  const question = questionInput.value.trim();
  if (!question || !tokenInput.value) return;
  if (activeRequestController !== null) {
    activeRequestController.abort();
  }
  const controller = new AbortController();
  activeRequestController = controller;
  resetOutput();
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        ...authorization(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        question,
      }),
      signal: controller.signal,
    });
    await readEvents(response);
  } catch (error) {
    if (activeRequestController === controller) {
      renderInterrupted();
    }
  } finally {
    if (activeRequestController === controller) {
      activeRequestController = null;
    }
  }
});

clearButton.addEventListener("click", async () => {
  if (activeRequestController !== null) {
    activeRequestController.abort();
    activeRequestController = null;
  }
  const response = await fetch(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE", headers: authorization() },
  );
  if (response.ok) {
    questionInput.value = "";
    stageList.replaceChildren();
    citationsNode.replaceChildren();
    answerNode.textContent = "会话已清空。";
    currentTraceId = null;
    traceIdNode.textContent = "尚无";
    copyTraceButton.disabled = true;
    viewTraceLink.hidden = true;
    viewTraceLink.href = "/debug/";
    usefulButton.disabled = true;
    notUsefulButton.disabled = true;
    streamedClaims = new Map();
    streamStatus = null;
  } else {
    answerNode.textContent = `清空失败：${response.status}`;
  }
});

window.addEventListener("pagehide", () => {
  if (activeRequestController !== null) {
    activeRequestController.abort();
  }
});

usefulButton.addEventListener("click", () => submitFeedback(true));
notUsefulButton.addEventListener("click", () => submitFeedback(false));
copyTraceButton.addEventListener("click", async () => {
  if (currentTraceId !== null) {
    await navigator.clipboard.writeText(currentTraceId);
  }
});
