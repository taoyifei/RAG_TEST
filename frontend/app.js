"use strict";

const tokenInput = document.querySelector("#token");
const questionInput = document.querySelector("#question");
const askButton = document.querySelector("#ask");
const clearButton = document.querySelector("#clear");
const stageList = document.querySelector("#stages");
const answerNode = document.querySelector("#answer");
const citationsNode = document.querySelector("#citations");
const traceIdNode = document.querySelector("#trace-id");
const copyTraceButton = document.querySelector("#copy-trace");
const usefulButton = document.querySelector("#feedback-useful");
const notUsefulButton = document.querySelector("#feedback-not-useful");
const conversationId = crypto.randomUUID();
let currentTraceId = null;

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
  usefulButton.disabled = false;
  notUsefulButton.disabled = false;
  answerNode.textContent =
    event.status === "answered"
      ? event.answer
      : `拒答：${event.refusal_code}`;
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
        answerNode.textContent = `处理失败：${event.code}`;
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
