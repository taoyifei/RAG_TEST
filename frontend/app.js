"use strict";

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
}

function renderStage(event) {
  const item = document.createElement("li");
  item.textContent = `${event.stage} · 请求累计 ${event.elapsed_ms} ms`;
  stageList.append(item);
}

function renderFinal(event) {
  currentTraceId = event.trace_id;
  traceIdNode.textContent = event.trace_id;
  copyTraceButton.disabled = false;
  viewTraceLink.href =
    "/debug/?trace_id=" + encodeURIComponent(event.trace_id);
  viewTraceLink.hidden = false;
  usefulButton.disabled = false;
  notUsefulButton.disabled = false;
  answerNode.textContent =
    event.status === "answered"
      ? [event.user_message, event.answer].filter(Boolean).join("\n\n")
      : event.user_message ||
        refusalMessages[event.refusal_code] ||
        "当前无法生成可靠回答，请稍后重试并查看 Trace。";
  for (const claim of event.claims) {
    for (const support of claim.supports) {
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
  while (true) {
    const result = await reader.read();
    pending += decoder.decode(result.value || new Uint8Array(), {
      stream: !result.done,
    });
    const lines = pending.split("\n");
    pending = lines.pop() || "";
    for (const line of lines) {
      if (line.length === 0) continue;
      const event = JSON.parse(line);
      if (event.type === "stage") renderStage(event);
      if (event.type === "final") renderFinal(event);
      if (event.type === "error") {
        answerNode.textContent =
          "请求处理失败，请稍后重试；如持续出现，请通过 Trace 页面排查。";
      }
    }
    if (result.done) return;
  }
}

askButton.addEventListener("click", async () => {
  const question = questionInput.value.trim();
  if (!question || !tokenInput.value) return;
  resetOutput();
  askButton.disabled = true;
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
    });
    await readEvents(response);
  } catch (error) {
    answerNode.textContent = String(error);
  } finally {
    askButton.disabled = false;
  }
});

clearButton.addEventListener("click", async () => {
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
  } else {
    answerNode.textContent = `清空失败：${response.status}`;
  }
});

usefulButton.addEventListener("click", () => submitFeedback(true));
notUsefulButton.addEventListener("click", () => submitFeedback(false));
copyTraceButton.addEventListener("click", async () => {
  if (currentTraceId !== null) {
    await navigator.clipboard.writeText(currentTraceId);
  }
});
