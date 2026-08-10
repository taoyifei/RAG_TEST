"use strict";

const tokenNode = document.querySelector("#admin-token");
const filterTraceNode = document.querySelector("#filter-trace");
const listNode = document.querySelector("#trace-list");
const detailSection = document.querySelector("#detail-section");
const selectedTraceNode = document.querySelector("#selected-trace");
const diagnosticNode = document.querySelector("#diagnostic");
const traceQuestionNode = document.querySelector("#trace-question");
const waterfallNode = document.querySelector("#waterfall");
const funnelNode = document.querySelector("#funnel");
const tabsNode = document.querySelector("#artifact-tabs");
const artifactNode = document.querySelector("#artifact-content");
const expectedNode = document.querySelector("#expected-chunk");
const expectedDiagnosisNode = document.querySelector("#expected-diagnosis");
const chunkIdNode = document.querySelector("#chunk-id");
const chunkDetailNode = document.querySelector("#chunk-detail");
const errorNode = document.querySelector("#error");
const exportLink = document.querySelector("#export-link");
const selectPageNode = document.querySelector("#select-page");
const exportSelectedNode = document.querySelector("#export-selected");
const selectedCountNode = document.querySelector("#selected-count");
let page = 1;
let total = 0;
let selectedTrace = null;
let artifactPayloads = new Map();
let pageTraceIds = [];
const selectedTraceIds = new Set();
const storedAdminToken = sessionStorage.getItem("ragv1.adminToken");
if (storedAdminToken) tokenNode.value = storedAdminToken;

function headers() {
  return { Authorization: `Bearer ${tokenNode.value}` };
}

function appendCell(row, value, code = false) {
  const cell = document.createElement("td");
  const content = code ? document.createElement("code") : document.createElement("span");
  content.textContent = value === null || value === undefined ? "—" : String(value);
  cell.append(content);
  row.append(cell);
}

async function adminFetch(path, options = {}) {
  if (!tokenNode.value) throw new Error("请先填写管理令牌。");
  const { headers: extraHeaders = {}, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    headers: { ...headers(), ...extraHeaders },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  sessionStorage.setItem("ragv1.adminToken", tokenNode.value);
  return response;
}

function listQuery() {
  const params = new URLSearchParams({ page: String(page), page_size: "25" });
  const filters = [
    ["trace_id", "#filter-trace"],
    ["status", "#filter-status"],
    ["refusal_code", "#filter-refusal"],
    ["error_code", "#filter-error"],
  ];
  for (const [name, selector] of filters) {
    const value = document.querySelector(selector).value.trim();
    if (value) params.set(name, value);
  }
  return params;
}

function updateSelection() {
  const selectedOnPage = pageTraceIds.filter((traceId) =>
    selectedTraceIds.has(traceId)).length;
  selectPageNode.disabled = pageTraceIds.length === 0;
  selectPageNode.checked =
    pageTraceIds.length > 0 && selectedOnPage === pageTraceIds.length;
  selectPageNode.indeterminate =
    selectedOnPage > 0 && selectedOnPage < pageTraceIds.length;
  exportSelectedNode.disabled = selectedTraceIds.size === 0;
  selectedCountNode.textContent = `已选择 ${selectedTraceIds.size} 条`;
}

function appendSelectionCell(row, traceId) {
  const cell = document.createElement("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.setAttribute("aria-label", `选择 Trace ${traceId}`);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) selectedTraceIds.add(traceId);
    else selectedTraceIds.delete(traceId);
    updateSelection();
  });
  cell.append(checkbox);
  row.append(cell);
}

async function loadList() {
  errorNode.textContent = "";
  const response = await adminFetch(`/api/admin/traces?${listQuery()}`);
  const payload = await response.json();
  total = payload.total;
  pageTraceIds = payload.items.map((trace) => trace.trace_id);
  selectedTraceIds.clear();
  listNode.replaceChildren();
  for (const trace of payload.items) {
    const row = document.createElement("tr");
    appendSelectionCell(row, trace.trace_id);
    const traceCell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "trace-link";
    button.textContent = trace.trace_id;
    button.addEventListener("click", () => loadDetail(trace.trace_id));
    traceCell.append(button);
    row.append(traceCell);
    appendCell(row, trace.question_preview);
    appendCell(row, trace.created_at);
    appendCell(row, trace.duration_ms === null ? "—" : `${trace.duration_ms} ms`);
    appendCell(row, trace.status);
    appendCell(row, trace.refusal_code || trace.error_code);
    appendCell(row, trace.mode === "FULL" ? "是" : "否");
    appendCell(
      row,
      trace.feedback_useful === null ? "—" : trace.feedback_useful ? "有用" : "没用",
    );
    listNode.append(row);
  }
  document.querySelector("#page-state").textContent =
    `第 ${page} 页 · 共 ${total} 条`;
  updateSelection();
}

async function downloadResponse(response, fallbackName) {
  const disposition = response.headers.get("Content-Disposition") || "";
  const filenameMatch = disposition.match(/filename="([^"]+)"/);
  const filename = filenameMatch ? filenameMatch[1] : fallbackName;
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

function diagnostic(detail) {
  const reasons = new Set(detail.candidate_decisions.map((item) => item.reason_code));
  if (detail.trace.status === "ANSWERED") return "ANSWERED";
  if (reasons.has("PROMPT_INJECTION") && !reasons.has("SELECTED")) {
    return "PROMPT_INJECTION_ONLY";
  }
  if (reasons.has("TOKEN_BUDGET")) return "EVIDENCE_BUDGET_DROP";
  if (detail.trace.refusal_code === "MODEL_UNAVAILABLE") return "MODEL_UNAVAILABLE";
  if (detail.trace.refusal_code === "VALIDATION_FAILED") return "VALIDATION_FAILED";
  const recalled = detail.candidate_decisions.some((item) =>
    item.stage.startsWith("retrieve.") || item.stage === "rrf.fuse");
  if (!recalled) return "RETRIEVAL_EMPTY";
  const reranked = detail.candidate_decisions.some(
    (item) => item.stage === "rerank" && item.selected,
  );
  return reranked ? "VALIDATION_FAILED" : "RERANK_DROP";
}

function renderWaterfall(detail) {
  waterfallNode.replaceChildren();
  const rootStart = Date.parse(detail.trace.created_at);
  const totalDuration = Math.max(1, detail.trace.duration_ms || 1);
  for (const span of detail.spans) {
    const row = document.createElement("div");
    row.className = `waterfall-row${span.status === "ERROR" ? " error" : ""}`;
    const label = document.createElement("code");
    label.textContent = `${span.name} · ${span.reason_code}`;
    const track = document.createElement("div");
    const bar = document.createElement("div");
    const offset = Math.max(0, Date.parse(span.started_at) - rootStart);
    bar.className = "waterfall-bar";
    bar.style.marginLeft = `${Math.min(95, (offset / totalDuration) * 100)}%`;
    bar.style.width = `${Math.max(0.5, ((span.duration_ms || 0) / totalDuration) * 100)}%`;
    track.append(bar);
    const duration = document.createElement("span");
    duration.textContent = `${span.duration_ms || 0} ms`;
    row.append(label, track, duration);
    waterfallNode.append(row);
  }
}

function renderFunnel(detail) {
  funnelNode.replaceChildren();
  for (const decision of detail.candidate_decisions) {
    const row = document.createElement("tr");
    appendCell(row, decision.stage);
    appendCell(row, decision.chunk_id, true);
    appendCell(row, decision.selected ? "是" : "否");
    appendCell(row, decision.reason_code);
    appendCell(row, JSON.stringify(decision.details));
    funnelNode.append(row);
  }
}

async function loadArtifact(traceId, artifact) {
  errorNode.textContent = "";
  const response = await adminFetch(
    `/api/admin/traces/${traceId}/artifacts/${artifact.artifact_id}`,
  );
  const text = await response.text();
  let payload = text;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
  }
  artifactPayloads.set(artifact.kind, payload);
  artifactNode.textContent =
    typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
}

function renderTabs(traceId, artifacts) {
  tabsNode.replaceChildren();
  artifactPayloads = new Map();
  for (const artifact of artifacts) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = artifact.kind;
    button.addEventListener("click", () => loadArtifact(traceId, artifact));
    tabsNode.append(button);
  }
  if (artifacts.length === 0) {
    artifactNode.textContent = "该 Trace 没有 FULL artifact。";
  }
}

function expectedLoss() {
  if (selectedTrace === null) return;
  const chunkId = expectedNode.value.trim();
  if (!chunkId) {
    expectedDiagnosisNode.textContent = "";
    return;
  }
  const rows = selectedTrace.candidate_decisions.filter(
    (item) => item.chunk_id === chunkId,
  );
  const reachedRecall = rows.some((item) =>
    item.stage.startsWith("retrieve.") || item.stage === "rrf.fuse");
  const reachedRerank = rows.some((item) => item.stage === "rerank" && item.selected);
  const reachedEvidence = rows.some(
    (item) => item.stage === "evidence.assemble" && item.selected,
  );
  const cited = rows.some((item) => item.stage === "citation" && item.selected);
  let result = "generation/validation loss";
  if (!reachedRecall) result = "retrieval loss";
  else if (!reachedRerank) result = "rerank loss";
  else if (!reachedEvidence) result = "assembly loss";
  else if (cited) result = "已引用";
  expectedDiagnosisNode.textContent = result;
}

function chunkDetails() {
  if (selectedTrace === null) return;
  const chunkId = chunkIdNode.value.trim();
  if (!chunkId) {
    chunkDetailNode.textContent = "";
    return;
  }
  const decisions = selectedTrace.candidate_decisions.filter(
    (item) => item.chunk_id === chunkId,
  );
  const artifacts = [];
  for (const [kind, payload] of artifactPayloads) {
    const serialized = JSON.stringify(payload);
    if (serialized.includes(chunkId)) artifacts.push({ kind, payload });
  }
  chunkDetailNode.textContent = JSON.stringify({ decisions, artifacts }, null, 2);
}

async function loadDetail(traceId) {
  errorNode.textContent = "";
  const response = await adminFetch(`/api/admin/traces/${traceId}`);
  selectedTrace = await response.json();
  selectedTraceNode.textContent = traceId;
  traceQuestionNode.textContent =
    selectedTrace.trace.question_text || "旧 Trace 未保存问题正文。";
  diagnosticNode.textContent = diagnostic(selectedTrace);
  detailSection.hidden = false;
  exportLink.href = `/api/admin/traces/${traceId}/export`;
  exportLink.onclick = async (event) => {
    event.preventDefault();
    errorNode.textContent = "";
    try {
      const exported = await adminFetch(exportLink.href);
      await downloadResponse(exported, `${traceId}.json`);
    } catch (error) {
      showError(error);
    }
  };
  renderWaterfall(selectedTrace);
  renderFunnel(selectedTrace);
  renderTabs(traceId, selectedTrace.artifacts);
  expectedLoss();
  chunkDetails();
  window.history.replaceState(
    null,
    "",
    `/debug/?trace_id=${encodeURIComponent(traceId)}`,
  );
  detailSection.focus({ preventScroll: true });
  detailSection.scrollIntoView({ block: "start" });
}

function showError(error) {
  errorNode.textContent = String(error);
}

function loadSelection() {
  const traceId = filterTraceNode.value.trim();
  if (traceId) {
    loadDetail(traceId).catch(showError);
    return;
  }
  page = 1;
  loadList().catch(showError);
}

document.querySelector("#load-list").addEventListener("click", loadSelection);
selectPageNode.addEventListener("change", () => {
  for (const traceId of pageTraceIds) {
    if (selectPageNode.checked) selectedTraceIds.add(traceId);
    else selectedTraceIds.delete(traceId);
  }
  for (const checkbox of listNode.querySelectorAll('input[type="checkbox"]')) {
    checkbox.checked = selectPageNode.checked;
  }
  updateSelection();
});
exportSelectedNode.addEventListener("click", async () => {
  errorNode.textContent = "";
  exportSelectedNode.disabled = true;
  try {
    const response = await adminFetch("/api/admin/traces/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trace_ids: [...selectedTraceIds] }),
    });
    await downloadResponse(response, "rag-traces.zip");
  } catch (error) {
    showError(error);
  } finally {
    updateSelection();
  }
});
document.querySelector("#previous-page").addEventListener("click", () => {
  if (page > 1) {
    page -= 1;
    loadList().catch(showError);
  }
});
document.querySelector("#next-page").addEventListener("click", () => {
  if (page * 25 < total) {
    page += 1;
    loadList().catch(showError);
  }
});
expectedNode.addEventListener("input", expectedLoss);
chunkIdNode.addEventListener("input", chunkDetails);
tokenNode.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadSelection();
});

const requestedTraceId = new URLSearchParams(window.location.search).get("trace_id");
if (requestedTraceId && /^[0-9a-f]{32}$/.test(requestedTraceId)) {
  filterTraceNode.value = requestedTraceId;
  if (tokenNode.value) {
    loadDetail(requestedTraceId).catch(showError);
  } else {
    tokenNode.focus();
  }
}
