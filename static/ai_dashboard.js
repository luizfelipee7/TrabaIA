const modelSelect = document.querySelector("#model-select");
const modelStatus = document.querySelector("#model-status");
const selectedModel = document.querySelector("#selected-model");
const runStatus = document.querySelector("#run-status");
const batchStatus = document.querySelector("#batch-status");
const metricsEl = document.querySelector("#metrics");
const timelineEl = document.querySelector("#timeline");
const reportEl = document.querySelector("#report");
const alertsEl = document.querySelector("#alerts");
const logsEl = document.querySelector("#logs");
const runsTable = document.querySelector("#runs-table");
const runMeta = document.querySelector("#run-meta");
const exportRun = document.querySelector("#export-run");
const batchSummary = document.querySelector("#batch-summary");
const streamRuns = document.querySelector("#stream-runs");
const streamStatus = document.querySelector("#stream-status");

let currentRun = null;
let currentFilter = "all";
let streamCounter = 0;

function setStatus(el, message, kind = "muted") {
  el.textContent = message;
  el.className = `status ${kind}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || data.message || "Erro na requisicao.");
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;");
}

function formatItem(item) {
  if (typeof item === "string") return item;
  return JSON.stringify(item);
}

function optionLabel(model) {
  const bits = [model.id || model.object || "modelo"];
  if (model.owned_by) bits.push(model.owned_by);
  return bits.join(" - ");
}

async function refreshModels() {
  setStatus(modelStatus, "Consultando LM Studio...");
  modelSelect.innerHTML = "";
  const data = await fetchJson("/ai/models");
  selectedModel.textContent = data.selected_model;
  if (!data.ok) {
    setStatus(modelStatus, data.message, "error");
    return;
  }
  for (const model of data.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = optionLabel(model);
    if (model.id === data.selected_model) option.selected = true;
    modelSelect.appendChild(option);
  }
  if (!data.models.length) {
    const option = document.createElement("option");
    option.value = data.selected_model;
    option.textContent = data.selected_model;
    modelSelect.appendChild(option);
  }
  setStatus(modelStatus, `${data.models.length} modelo(s) encontrados.`, "ok");
}

async function selectModel() {
  const modelName = modelSelect.value || selectedModel.textContent;
  const data = await fetchJson("/ai/models/select", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({model_name: modelName}),
  });
  selectedModel.textContent = data.selected_model || selectedModel.textContent;
  let message = data.message || "Modelo selecionado.";
  setStatus(modelStatus, message, data.ok ? "ok" : "error");
}

function metricCard(label, value) {
  return `<div class="metric-card"><span class="label">${escapeHtml(label)}</span><span class="value">${escapeHtml(value)}</span></div>`;
}

function renderMetrics(metrics = {}) {
  metricsEl.innerHTML = [
    metricCard("Status", metrics.status || "-"),
    metricCard("Tempo", `${metrics.duration_ms ?? 0} ms`),
    metricCard("Chamadas modelo", metrics.model_call_count ?? 0),
    metricCard("Tools usadas", metrics.tool_call_count ?? 0),
    metricCard("Tools falharam", metrics.failed_tool_count ?? 0),
    metricCard("Faltas", metrics.stock_shortage_count ?? 0),
    metricCard("Validade", metrics.expiration_risk_count ?? 0),
    metricCard("Consumo anormal", metrics.abnormal_consumption_count ?? 0),
    metricCard("Fornecedores", metrics.supplier_issue_count ?? 0),
    metricCard("Compras", metrics.purchase_suggestion_count ?? 0),
    metricCard("Aprovacoes", metrics.approval_action_count ?? 0),
    metricCard("Dados", metrics.data_quality_issue_count ?? 0),
    metricCard("Skills", metrics.skill_event_count ?? 0),
    metricCard("Eventos", metrics.timeline_event_count ?? 0),
    metricCard("Tools unicas", metrics.unique_tool_count ?? 0),
    metricCard("Repeticoes", metrics.repeated_tool_call_count ?? 0),
  ].join("");
}

function renderTimeline(timeline = []) {
  if (!timeline.length) {
    timelineEl.className = "timeline empty";
    timelineEl.textContent = "Nenhuma execucao nesta sessao.";
    return;
  }
  timelineEl.className = "timeline";
  timelineEl.innerHTML = timeline.map((event, index) => {
    const type = event.type || "system";
    const detail = JSON.stringify(event.detail || {}, null, 2);
    const hidden = currentFilter !== "all" && type !== currentFilter ? " hidden" : "";
    return `
      <article class="event${hidden}" data-type="${escapeHtml(type)}">
        <div><span class="badge ${escapeHtml(type)}">${escapeHtml(labelForType(type))}</span></div>
        <div>
          <div class="event-title">
            <h3>${index + 1}. ${escapeHtml(event.label || type)}</h3>
            <span class="muted">${event.ok === false ? "falha" : "ok"}</span>
          </div>
          <p>${escapeHtml(event.summary || "")}</p>
          <pre>${escapeHtml(detail)}</pre>
        </div>
      </article>
    `;
  }).join("");
}

function labelForType(type) {
  return {
    objective: "objetivo",
    tool: "tool",
    response: "resposta",
    skill: "skill",
    system: "sistema",
  }[type] || type;
}

function renderProductItems(items) {
  if (!items || !items.length) return "<p class=\"muted\">Sem itens.</p>";
  return `<ul>${items.map((item) => `
    <li>
      <strong>${escapeHtml(item.product_name || item.supplier_name || item.action || item.issue_type || "item")}</strong>
      <span class="muted"> ${escapeHtml(item.sku || "")} ${escapeHtml(item.severity || item.priority || "")}</span><br>
      ${escapeHtml(item.evidence || item.approval_reason || "")}<br>
      <span>${escapeHtml(item.recommended_action || item.action || "")}</span>
      ${item.suggested_quantity !== undefined && item.suggested_quantity !== null ? `<br><strong>Qtd. sugerida:</strong> ${escapeHtml(item.suggested_quantity)}` : ""}
      ${item.requires_approval !== undefined ? `<br><strong>Aprovacao:</strong> ${item.requires_approval ? "sim" : "nao"}` : ""}
    </li>
  `).join("")}</ul>`;
}

function renderReport(data) {
  const report = data.final_report;
  if (!report) {
    reportEl.className = "empty";
    reportEl.textContent = data.message || "Sem relatorio final.";
    return;
  }
  reportEl.className = "";
  reportEl.innerHTML = `
    <p><strong>Tipo:</strong> ${escapeHtml(report.report_type || "-")} · <strong>Gerado em:</strong> ${escapeHtml(report.generated_at || "-")}</p>
    <p><strong>Escopo:</strong> ${escapeHtml((report.scope || []).join(", "))}</p>
    <p><strong>Resumo:</strong> ${escapeHtml(report.executive_summary || "")}</p>
    <div class="report-grid">
      <section class="report-section"><h3>Falta de estoque</h3>${renderProductItems(report.stock_shortages)}</section>
      <section class="report-section"><h3>Validade proxima</h3>${renderProductItems(report.expiration_risks)}</section>
      <section class="report-section"><h3>Consumo anormal</h3>${renderProductItems(report.abnormal_consumption)}</section>
      <section class="report-section"><h3>Fornecedores</h3>${renderProductItems(report.supplier_issues)}</section>
      <section class="report-section"><h3>Sugestoes de compra</h3>${renderProductItems(report.purchase_suggestions)}</section>
      <section class="report-section"><h3>Exige aprovacao</h3>${renderProductItems(report.actions_requiring_approval)}</section>
      <section class="report-section"><h3>Proximas acoes</h3>${renderProductItems(report.next_actions)}</section>
      <section class="report-section"><h3>Qualidade dos dados</h3>${renderProductItems(report.data_quality_issues)}</section>
    </div>
  `;
}

function renderReportBlocks(report) {
  if (!report) return "<p class=\"muted\">Aguardando relatório final validado.</p>";
  return `
    <p><strong>Tipo:</strong> ${escapeHtml(report.report_type || "-")}</p>
    <p><strong>Gerado em:</strong> ${escapeHtml(report.generated_at || "-")}</p>
    <p><strong>Escopo:</strong> ${escapeHtml((report.scope || []).join(", "))}</p>
    <p><strong>Resumo:</strong> ${escapeHtml(report.executive_summary || "")}</p>
    <div class="report-grid">
      <section class="report-section"><h3>Falta de estoque</h3>${renderProductItems(report.stock_shortages)}</section>
      <section class="report-section"><h3>Validade proxima</h3>${renderProductItems(report.expiration_risks)}</section>
      <section class="report-section"><h3>Consumo anormal</h3>${renderProductItems(report.abnormal_consumption)}</section>
      <section class="report-section"><h3>Fornecedores</h3>${renderProductItems(report.supplier_issues)}</section>
      <section class="report-section"><h3>Sugestoes de compra</h3>${renderProductItems(report.purchase_suggestions)}</section>
      <section class="report-section"><h3>Exige aprovacao</h3>${renderProductItems(report.actions_requiring_approval)}</section>
      <section class="report-section"><h3>Proximas acoes</h3>${renderProductItems(report.next_actions)}</section>
      <section class="report-section"><h3>Qualidade dos dados</h3>${renderProductItems(report.data_quality_issues)}</section>
    </div>
  `;
}

function extractAlertsFromRun(data) {
  const alerts = [];
  for (const event of data.qa?.timeline || []) {
    const result = event.detail?.tool_result?.result;
    if (Array.isArray(result?.alerts)) alerts.push(...result.alerts);
  }
  return alerts;
}

function renderAlerts(data) {
  const alerts = extractAlertsFromRun(data);
  if (!alerts.length) {
    alertsEl.className = "empty";
    alertsEl.textContent = "Nenhum alerta retornado pelas tools nesta execucao.";
    return;
  }
  alertsEl.className = "";
  alertsEl.innerHTML = `<ul class="alert-list">${alerts.map((alert) => {
    const label = alert.title || alert.type || alert.alert_type || "alerta";
    const detail = alert.description || "";
    const source = alert.source_tool ? ` · ${alert.source_tool}` : "";
    return `<li><strong>${escapeHtml(label)}</strong><span class="muted">${escapeHtml(source)}</span><br>${escapeHtml(detail)}</li>`;
  }).join("")}</ul>`;
}

function renderRun(data) {
  currentRun = data;
  const qa = data.qa || {};
  renderMetrics(qa.metrics || {});
  renderTimeline(qa.timeline || []);
  renderReport(data);
  renderAlerts(data);
  runMeta.textContent = qa.run_id
    ? `${qa.run_id} · ${qa.model || data.model || "-"} · ${qa.duration_ms || 0} ms`
    : "Execucao sem metadados de QA.";
  if (qa.run_id) {
    exportRun.href = `/ai/qa/runs/${qa.run_id}/export`;
    exportRun.setAttribute("aria-disabled", "false");
  }
}

async function runReview() {
  const button = document.querySelector("#run-review");
  button.disabled = true;
  setStatus(runStatus, "Executando revisao...");
  try {
    const objective = document.querySelector("#objective").value.trim() || null;
    const data = await fetchJson("/ai/daily-inventory-review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({objective}),
    });
    renderRun(data);
    setStatus(runStatus, data.message || data.status, data.status === "completed" ? "ok" : "error");
    await refreshLogs();
    await refreshRuns();
  } catch (error) {
    setStatus(runStatus, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function createRunBox(objective) {
  streamCounter += 1;
  const box = document.createElement("article");
  box.className = "run-box";
  box.dataset.streamId = String(streamCounter);
  box.innerHTML = `
    <header class="run-box-header">
      <div class="run-box-title">
        <h2>Execução #${streamCounter}</h2>
        <p class="muted">${escapeHtml(objective || "Objetivo padrão da revisão diária.")}</p>
      </div>
      <span class="badge system">iniciando</span>
    </header>
    <div class="run-box-body">
      <section>
        <h2>Agent Trace</h2>
        <div class="trace-events"></div>
      </section>
      <section class="stream-report">
        <h2>Relatório</h2>
        <div class="stream-report-blocks"><p class="muted">Aguardando conclusão.</p></div>
      </section>
    </div>
  `;
  streamRuns.prepend(box);
  return box;
}

function setRunBoxState(box, state, label) {
  const badge = box.querySelector(".run-box-header .badge");
  badge.className = `badge ${state}`;
  badge.textContent = label;
}

function appendTraceEvent(box, event) {
  const eventsEl = box.querySelector(".trace-events");
  const item = document.createElement("article");
  const type = event.type || "system";
  item.className = `trace-event ${type}`;
  const title = event.event || type;
  const detail = {...event};
  delete detail.final_report;
  delete detail.result;
  item.innerHTML = `
    <h3>${escapeHtml(labelForType(type))} · ${escapeHtml(title)}</h3>
    <p>${escapeHtml(event.message || event.tool_name || "")}</p>
    <pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
  `;
  eventsEl.appendChild(item);
  eventsEl.scrollTop = eventsEl.scrollHeight;

  if (event.event === "final_report") {
    box.querySelector(".stream-report-blocks").innerHTML = renderReportBlocks(event.final_report);
  }
  if (event.event === "run_completed") {
    setRunBoxState(box, "response", "concluído");
    if (event.result) {
      renderRun(event.result);
      refreshRuns().catch(() => {});
      refreshLogs().catch(() => {});
    }
  }
  if (event.event === "run_error") {
    setRunBoxState(box, "error", "erro");
    if (event.result) {
      box.querySelector(".stream-report-blocks").innerHTML = `<pre>${escapeHtml(JSON.stringify(event.result, null, 2))}</pre>`;
    }
  }
}

async function runStream() {
  const button = document.querySelector("#run-stream");
  const objective = document.querySelector("#trace-objective").value.trim() || null;
  const box = createRunBox(objective);
  button.disabled = true;
  setStatus(streamStatus, "Streaming em andamento...");
  try {
    const response = await fetch("/ai/daily-inventory-review/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({objective}),
    });
    if (!response.ok || !response.body) {
      throw new Error("Nao foi possivel iniciar o stream.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const line = part.split("\n").find((entry) => entry.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        appendTraceEvent(box, event);
      }
    }
    setStatus(streamStatus, "Stream encerrado.", "ok");
  } catch (error) {
    setRunBoxState(box, "error", "erro");
    appendTraceEvent(box, {event: "frontend_error", type: "error", message: error.message});
    setStatus(streamStatus, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function runBatch() {
  const button = document.querySelector("#run-batch");
  const count = Number(document.querySelector("#batch-count").value || 1);
  const objective = document.querySelector("#objective").value.trim() || null;
  button.disabled = true;
  setStatus(batchStatus, `Executando batch com ${count} teste(s)...`);
  try {
    const data = await fetchJson("/ai/daily-inventory-review/batch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({count, objective}),
    });
    if (data.runs?.length) renderRun(data.runs[data.runs.length - 1]);
    renderBatchSummary(data.batch);
    renderRunsTable(data.batch?.runs || []);
    setStatus(batchStatus, `Batch ${data.batch?.batch_id || ""} concluido.`, "ok");
    await refreshLogs();
  } catch (error) {
    setStatus(batchStatus, error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderBatchSummary(batch) {
  if (!batch) {
    batchSummary.textContent = "Sem batch carregado.";
    return;
  }
  const aggregate = batch.aggregate || {};
  batchSummary.innerHTML = `
    <strong>${escapeHtml(batch.batch_id)}</strong> · ${batch.run_count} run(s) · tempo medio ${aggregate.avg_duration_ms || 0} ms ·
    <a href="/ai/qa/batches/${encodeURIComponent(batch.batch_id)}/export?format=json">JSON</a> ·
    <a href="/ai/qa/batches/${encodeURIComponent(batch.batch_id)}/export?format=csv">CSV</a>
  `;
}

function renderRunsTable(runs = []) {
  runsTable.innerHTML = runs.map((run) => `
    <tr>
      <td><button class="secondary" type="button" data-run-id="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id || "-")}</button></td>
      <td>${escapeHtml(run.status || "-")}</td>
      <td>${escapeHtml(run.model || "-")}</td>
      <td>${escapeHtml(run.duration_ms ?? 0)} ms</td>
      <td>${escapeHtml(run.tool_call_count ?? 0)} / ${escapeHtml(run.unique_tool_count ?? 0)}</td>
      <td>${escapeHtml(run.failed_tool_count ?? 0)}</td>
      <td>${escapeHtml(run.stock_shortage_count ?? run.alert_count ?? 0)}</td>
      <td>${run.run_id ? `<a href="/ai/qa/runs/${encodeURIComponent(run.run_id)}/export">JSON</a>` : "-"}</td>
    </tr>
  `).join("");
  runsTable.querySelectorAll("button[data-run-id]").forEach((button) => {
    button.addEventListener("click", () => loadRun(button.dataset.runId));
  });
}

async function refreshRuns() {
  const data = await fetchJson("/ai/qa/runs");
  renderRunsTable(data.runs || []);
}

async function loadRun(runId) {
  const data = await fetchJson(`/ai/qa/runs/${encodeURIComponent(runId)}`);
  renderRun(data);
}

async function refreshLogs() {
  const data = await fetchJson("/ai/logs");
  logsEl.textContent = JSON.stringify(data.logs, null, 2);
}

document.querySelector("#refresh-models").addEventListener("click", () => refreshModels().catch((err) => {
  setStatus(modelStatus, err.message, "error");
}));
document.querySelector("#select-model").addEventListener("click", () => selectModel().catch((err) => {
  setStatus(modelStatus, err.message, "error");
}));
document.querySelector("#run-stream").addEventListener("click", runStream);
document.querySelector("#clear-streams").addEventListener("click", () => {
  streamRuns.innerHTML = "";
  setStatus(streamStatus, "Boxes limpas. Pronto para novo trace.");
});
document.querySelector("#run-review").addEventListener("click", runReview);
document.querySelector("#run-batch").addEventListener("click", runBatch);
document.querySelector("#refresh-runs").addEventListener("click", () => refreshRuns().catch(() => {}));
document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    currentFilter = button.dataset.filter;
    if (currentRun) renderTimeline(currentRun.qa?.timeline || []);
  });
});
document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
    button.classList.add("active");
    document.querySelector(`#${button.dataset.tab}`).classList.add("active");
  });
});

renderMetrics({});
refreshModels().catch((err) => setStatus(modelStatus, err.message, "error"));
refreshLogs().catch(() => {});
refreshRuns().catch(() => {});
