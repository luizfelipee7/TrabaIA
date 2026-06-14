const state = {
  mediaRecorder: null,
  audioChunks: [],
  speechRecognition: null,
  speechBaseText: "",
  speechFinalText: "",
  recording: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

document.addEventListener("DOMContentLoaded", () => {
  setupRichRendering();
  setupVoiceSupportHint();
  bindTabs();
  bindActions();
  loadStatus();
  loadDashboard();
  loadDocuments();
  loadMeetings();
  runSearch();
});

function bindTabs() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab || button.dataset.page));
  });
}

function bindActions() {
  $("#refresh-dashboard")?.addEventListener("click", loadDashboard);
  $("#reload-search")?.addEventListener("click", runSearch);
  $("#run-search")?.addEventListener("click", runSearch);
  $("#db-search")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch();
  });
  $("#reload-documents")?.addEventListener("click", loadDocuments);
  $("#bill-file-selector")?.addEventListener("change", handleOcrUpload);
  $("#save-document")?.addEventListener("click", saveDocument);
  $("#summarize-meeting")?.addEventListener("click", summarizeMeeting);
  $("#meeting-file-selector")?.addEventListener("change", readMeetingFile);
  $("#send-request")?.addEventListener("click", sendAgentRequest);
  $("#btn-clear-painel")?.addEventListener("click", clearAssistantPanel);
  $("#mic-btn")?.addEventListener("click", toggleVoiceInput);
  $("#mic-btn-panel")?.addEventListener("click", toggleVoiceInput);
  $("#audio-file-selector")?.addEventListener("change", handleAudioFile);
  $$("[data-run-report]").forEach((button) => {
    button.addEventListener("click", () => runReport(button.dataset.runReport, false));
  });
  $$("[data-run-ai-report]").forEach((button) => {
    button.addEventListener("click", () => runReport(button.dataset.runAiReport, true));
  });
  $$("[data-quick-search]").forEach((button) => {
    button.addEventListener("click", () => {
      switchTab("estoque");
      $("#db-search").value = button.dataset.quickSearch || "";
      $("#db-scope").value = "baixo_estoque";
      runSearch();
    });
  });
  $$("[data-suggestion]").forEach((button) => {
    button.addEventListener("click", () => {
      switchTab("assistente");
      $("#main-prompt").value = button.dataset.suggestion || "";
      sendAgentRequest();
    });
  });
  $$("[data-tab-jump]").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tabJump));
  });
}

function switchTab(tabName) {
  if (tabName === "conversa") tabName = "assistente";
  if (tabName === "banco") tabName = "estoque";
  $$(".nav-item").forEach((button) => button.classList.toggle("active", (button.dataset.tab || button.dataset.page) === tabName));
  $$(".page-view").forEach((pane) => pane.classList.remove("active"));
  $(`#page-${tabName}`)?.classList.add("active");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : {"Content-Type": "application/json"},
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || response.statusText);
  }
  return response.json();
}

async function loadStatus() {
  try {
    const data = await api("/ops/status");
    renderStatus(data);
  } catch (error) {
    $("#model-status-text").textContent = `Status indisponivel: ${error.message}`;
  }
}

function renderStatus(data) {
  const modelDot = $("#model-status-dot");
  const model = data.model || {};
  const roles = model.policy?.roles || {};
  modelDot.classList.toggle("danger", !model.available);
  $("#model-status-text").textContent = model.message || "Modelo sem status.";
  $("#selected-model").textContent = roles.worker
    ? `worker ${roles.worker.model} - quality ${roles.quality?.model || "-"} - balanced ${roles.balanced?.model || "-"}`
    : model.selected_model || $("#selected-model").textContent;
  $("#stt-status-text").textContent = `STT: ${(data.stt || {}).available ? "disponivel" : "indisponivel"}`;
  $("#ocr-status-text").textContent = `OCR: ${(data.ocr || {}).available ? "disponivel" : "aguardando modelo"}`;
  if ($("#runtime-status-text")) {
    const runtime = data.ai_runtime || model.runtime || null;
    $("#runtime-status-text").textContent = runtime ? `Runtime: ocupado com ${runtime.kind}` : "Runtime: livre";
  }
  if (data.ai_runtime || model.runtime) {
    $("#model-status-text").textContent = `IA ocupada: ${(data.ai_runtime || model.runtime).kind}`;
  }
  const voiceMode = $("#voice-mode");
  if (voiceMode) {
    const stt = data.stt || {};
    const nativeSpeech = window.SpeechRecognition || window.webkitSpeechRecognition;
    voiceMode.textContent = stt.engine === "browser"
      ? (nativeSpeech ? "Nativo do navegador" : "Fallback por arquivo")
      : stt.engine || "indefinido";
    voiceMode.classList.toggle("green", stt.engine === "browser" && Boolean(nativeSpeech));
    voiceMode.classList.toggle("gold", stt.engine !== "browser" || !nativeSpeech);
  }
}

async function loadDashboard() {
  setRight("dashboard", card("Carregando", "Consultando dados reais do projeto.", true));
  try {
    const data = await api("/ops/dashboard");
    renderDashboard(data);
    renderStatus({model: data.model_status, stt: data.stt_status, ocr: data.ocr_status});
  } catch (error) {
    setRight("dashboard", card("Falha ao carregar dashboard", error.message, true));
  }
}

function renderDashboard(data) {
  const metrics = data.metrics || {};
  $("#dashboard-kpis").innerHTML = [
    kpi(metrics.open_alerts, "Alertas abertos"),
    kpi(metrics.low_stock, "Estoque baixo"),
    kpi(metrics.expiration_risks, "Vencimentos"),
    kpi(metrics.pending_actions, "Acoes pendentes"),
  ].join("");
  if ($("#badge-alertas")) $("#badge-alertas").textContent = metrics.open_alerts ?? 0;

  const statusRows = [
    statusRow("Modelo", data.model_status),
    statusRow("STT", data.stt_status),
    statusRow("OCR", data.ocr_status),
  ];
  $("#integration-status").innerHTML = statusRows.join("");
  $("#integration-tag").textContent = data.model_status?.available ? "Online" : "Parcial";

  $("#recent-runs").innerHTML = (data.runs || []).map((run) => row(
    run.run_id || "Execucao",
    `${run.status || "sem status"} - modelo ${run.model || "-"} - ${run.duration_ms || 0}ms`,
    tag(run.status || "run", run.status === "completed" ? "green" : "gold"),
  )).join("") || empty("Nenhuma execucao de QA encontrada.");

  $("#recent-logs").innerHTML = (data.logs || []).map((log) => row(
    log.message || "Log",
    log.created_at || "",
    tag("log", "purple"),
  )).join("") || empty("Nenhum log registrado.");

  setRight("dashboard", [
    card("Resumo operacional", `Produtos: ${metrics.products || 0}. Alertas: ${metrics.open_alerts || 0}. Relatorios: ${metrics.reports || 0}.`, true),
    listCard("Estoque baixo", data.low_stock, productSummary),
    listCard("Vencimentos proximos", data.expiration_risks, productSummary),
    listCard("Fornecedores incompletos", data.supplier_issues, supplierSummary),
  ].join(""));
}

async function runSearch() {
  const query = $("#db-search")?.value || "";
  const scope = $("#db-scope")?.value || "estoque";
  $("#inventory-table-body").innerHTML = tableLoading();
  try {
    const data = await api("/ops/search", {
      method: "POST",
      body: JSON.stringify({query, scope}),
    });
    renderSearch(data);
  } catch (error) {
    $("#inventory-table-body").innerHTML = tableEmpty(`Erro: ${escapeHtml(error.message)}`);
  }
}

function renderSearch(data) {
  const rows = data.results || [];
  const columns = columnsForSearch(data.intent);
  $("#inventory-table-body").innerHTML = rows.map((item) => {
    const title = item.name || item.title || item.supplier_name || item.name || item.path || "Item";
    const type = item.sku ? "produto" : item.alert_type ? "alerta" : item.missing ? "fornecedor" : "relatorio";
    const indicator = item.sku
      ? `${item.sku} - estoque ${item.current_stock ?? "-"} / minimo ${item.minimum_stock ?? "-"}`
      : item.description || item.created_at || item.email || item.title || "";
    return `<tr><td>${escapeHtml(title)}</td><td>${escapeHtml(type)}</td><td>${escapeHtml(indicator)}</td><td>${badge(item.status || item.severity || data.intent)}</td></tr>`;
  }).join("") || tableEmpty("Nenhum resultado encontrado.");

  setRight("banco", [
    renderDelivery({
      status: "completed",
      mode: "database",
      source: "sqlite",
      title: "Resultado do banco",
      summary: `${data.count} item(ns) encontrados. Consulta direta no SQLite.`,
      visualization: {type: "table", columns, rows},
      metadata: {intent: data.intent, llm_calls: 0},
    }),
  ].join(""));
}

async function runReport(reportType, useAi) {
  const renderInAssistant = Boolean($("#page-assistente.active"));
  if (renderInAssistant) {
    setResultStage(renderNotice("Executando rotina", `${reportType}${useAi ? " com IA" : ""}.`, "loading"));
  }
  setRight("dashboard", card("Executando rotina", `${reportType}${useAi ? " com IA" : ""}.`, true));
  try {
    const data = await api("/ops/reports/run", {
      method: "POST",
      body: JSON.stringify({report_type: reportType, use_ai: useAi}),
    });
    const html = renderOperationalResult("Rotina finalizada", data);
    setRight("dashboard", html);
    if (renderInAssistant) setResultStage(html);
    loadDashboard();
  } catch (error) {
    setRight("dashboard", card("Falha na rotina", error.message, true));
    if (renderInAssistant) setResultStage(renderNotice("Falha na rotina", error.message, "danger"));
  }
}

async function sendAgentRequest() {
  switchTab("assistente");
  const prompt = ($("#main-prompt")?.value || "").trim();
  if (!prompt) {
    setResultStage(renderNotice("Pedido vazio", "Digite ou grave uma solicitacao antes de enviar.", "warning"));
    return;
  }
  appendRequestHistory(prompt, "enviado");
  startAgentRunView(prompt);
  updateAgentStatus("Processando...", "#b7791f");
  const events = [];
  let finalData = null;
  try {
    const response = await fetch("/ops/agent/request/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({prompt}),
    });
    if (!response.ok || !response.body) {
      throw new Error(await response.text() || response.statusText);
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
        events.push(event);
        appendAgentTraceEvent(event);
        if (event.result && ["run_completed", "run_error", "run_busy"].includes(event.event)) {
          finalData = event.result;
          $("#agent-live-result").innerHTML = renderDelivery(finalData);
          hydrateRichContent($("#agent-live-result"));
          finalizeAgentRunPanel();
        }
      }
    }
    if (!finalData) throw new Error("Stream encerrado sem resultado final.");
    appendRequestHistory(prompt, finalData.status || "concluido", finalData.metadata);
    updateAgentStatus(finalData.status || "Pronto", finalData.status === "completed" ? "#1f9d67" : "#b7791f");
  } catch (error) {
    const fallback = renderNotice("Falha na entrega", error.message, "danger");
    if ($("#agent-live-result")) $("#agent-live-result").innerHTML = fallback;
    else setResultStage(fallback);
    appendRequestHistory(prompt, "erro");
    updateAgentStatus("Erro", "#ba2433");
  }
}

async function handleOcrUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  $("#ocr-preview").textContent = `Enviando ${file.name}...`;
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/ops/ocr/process", {method: "POST", body: form});
    const fields = data.extracted || {};
    $("#ocr-file-path").value = data.file?.path || "";
    $("#ocr-supplier").value = fields.supplier_name || "";
    $("#ocr-date").value = fields.date || "";
    $("#ocr-amount").value = fields.amount || "";
    $("#ocr-due-date").value = fields.due_date || "";
    $("#ocr-category").value = fields.category || "";
    $("#ocr-description").value = fields.description || "";
    $("#ocr-notes").value = fields.notes || "";
    $("#ocr-preview").innerHTML = `${escapeHtml(file.name)}<br><span>${escapeHtml(data.message || data.status)}</span>`;
    setRight("contas", jsonCard("OCR", data, true));
  } catch (error) {
    $("#ocr-preview").textContent = "Falha no upload.";
    setRight("contas", card("Falha no OCR", error.message, true));
  }
}

async function saveDocument() {
  const payload = {
    supplier_name: $("#ocr-supplier").value,
    date: $("#ocr-date").value,
    amount: $("#ocr-amount").value,
    due_date: $("#ocr-due-date").value,
    description: $("#ocr-description").value,
    category: $("#ocr-category").value,
    notes: $("#ocr-notes").value,
    file_path: $("#ocr-file-path").value,
  };
  try {
    const data = await api("/ops/ocr/save", {method: "POST", body: JSON.stringify(payload)});
    setRight("contas", jsonCard("Conta salva", data, true));
    loadDocuments();
  } catch (error) {
    setRight("contas", card("Falha ao salvar", error.message, true));
  }
}

async function loadDocuments() {
  try {
    const data = await api("/ops/ocr/documents");
    $("#account-list").innerHTML = (data.documents || []).map((doc) => `
      <article class="doc-item">
        <div class="doc-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        </div>
        <div class="doc-info">
          <div class="doc-name">${escapeHtml(doc.supplier_name || "Fornecedor")}</div>
          <div class="doc-meta">${escapeHtml(doc.description || doc.category || "Documento salvo")} - Vence ${escapeHtml(doc.due_date || "-")}</div>
        </div>
        <div class="doc-amount">${escapeHtml(doc.amount || "-")}</div>
      </article>
    `).join("") || empty("Nenhuma conta salva.");
  } catch (error) {
    $("#account-list").innerHTML = empty(`Erro ao carregar contas: ${error.message}`);
  }
}

async function loadMeetings() {
  try {
    const data = await api("/ops/meetings");
    const meetings = data.meetings || [];
    const memory = data.memory || [];
    if ($("#meeting-history")) {
      $("#meeting-history").innerHTML = meetings.map((meeting) => row(
        meeting.title || "Reuniao",
        `${meeting.status || "salva"} - ${meeting.created_at || ""}`,
        tag((meeting.keywords || [])[0] || "reuniao", meeting.status === "completed" ? "green" : "gold"),
      )).join("") || empty("Nenhuma reuniao salva.");
    }
    if ($("#meeting-memory")) {
      $("#meeting-memory").innerHTML = memory.map((note) => row(
        note.title || "Memoria",
        `${note.summary || "Sem resumo"}${note.topics?.length ? ` - Topicos: ${note.topics.slice(0, 3).join(", ")}` : ""}`,
        tag(note.status || "memoria", note.status === "active" ? "green" : "gold"),
      )).join("") || empty("Nenhuma memoria escrita ainda.");
    }
  } catch (error) {
    if ($("#meeting-history")) $("#meeting-history").innerHTML = empty(`Erro ao carregar reunioes: ${error.message}`);
  }
}

async function summarizeMeeting() {
  const text = ($("#meeting-text")?.value || "").trim();
  if (!text) {
    $("#meeting-results").innerHTML = empty("Cole uma transcricao ou selecione arquivo de texto.");
    return;
  }
  $("#meeting-results").innerHTML = empty("Pipeline iniciada: preparando texto, buscando memoria e chamando IA.");
  setRight("reunioes", renderMeetingPipeline({
    pipeline_events: [
      {type: "input", event: "raw_text_received", message: "Texto cru recebido."},
      {type: "system", event: "text_preparation_pending", message: "Preparando texto para analise."},
    ],
  }));
  try {
    const data = await api("/ops/meetings/summary", {method: "POST", body: JSON.stringify({text})});
    $("#meeting-results").innerHTML = row("Resumo processado", data.message || data.status, tag(data.status, data.status === "completed" ? "green" : "gold"));
    setRight("reunioes", renderMeetingPipeline(data));
    loadMeetings();
  } catch (error) {
    $("#meeting-results").innerHTML = empty(`Erro: ${error.message}`);
    setRight("reunioes", renderNotice("Falha na reuniao", error.message, "danger"));
  }
}

function readMeetingFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("#meeting-text").value = String(reader.result || "");
    if ($("#upload-status")) $("#upload-status").textContent = `${file.name} importado.`;
  };
  reader.readAsText(file);
}

async function handleAudioFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  await sendAudioForTranscription(file);
}

async function toggleVoiceInput() {
  if (state.recording) {
    stopVoiceCapture();
    return;
  }
  if (startBrowserSpeechRecognition()) return;
  await startRecordedAudioFallback();
}

function startBrowserSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return false;

  state.speechBaseText = ($("#main-prompt")?.value || "").trim();
  state.speechFinalText = "";
  const recognition = new SpeechRecognition();
  recognition.lang = "pt-BR";
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    state.speechRecognition = recognition;
    setRecording(true, "Ouvindo no navegador...");
    setVoiceHint("Fale normalmente. A transcricao aparece em tempo real.");
  };
  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const transcript = event.results[index][0]?.transcript || "";
      if (event.results[index].isFinal) {
        state.speechFinalText = `${state.speechFinalText} ${transcript}`.trim();
      } else {
        interim = `${interim} ${transcript}`.trim();
      }
    }
    updateSpeechPrompt(interim);
  };
  recognition.onerror = (event) => {
    setVoiceHint(`STT do navegador: ${event.error || "erro desconhecido"}.`);
    updateAgentStatus("Erro STT", "#ba2433");
  };
  recognition.onend = () => {
    const finalText = updateSpeechPrompt("");
    setRecording(false);
    state.speechRecognition = null;
    if (state.speechFinalText) setVoiceHint("Transcricao inserida no campo de pedido.");
  };

  try {
    recognition.start();
    return true;
  } catch (error) {
    setVoiceHint(`Nao foi possivel iniciar STT nativo: ${error.message}`);
    return false;
  }
}

async function startRecordedAudioFallback() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    setVoiceHint("Gravacao indisponivel neste navegador. Envie um arquivo de audio se o backend STT estiver configurado.");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    state.audioChunks = [];
    state.mediaRecorder = new MediaRecorder(stream);
    state.mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) state.audioChunks.push(event.data);
    };
    state.mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      setRecording(false);
      const blob = new Blob(state.audioChunks, {type: "audio/webm"});
      await sendAudioForTranscription(new File([blob], "gravacao.webm", {type: "audio/webm"}));
    };
    state.mediaRecorder.start();
    setRecording(true, "Gravando para fallback...");
    setVoiceHint("Seu navegador nao oferece STT nativo. O audio sera enviado ao backend configurado.");
  } catch (error) {
    setVoiceHint(`Nao foi possivel gravar audio: ${error.message}`);
  }
}

function stopVoiceCapture() {
  if (state.speechRecognition) {
    state.speechRecognition.stop();
    return;
  }
  if (state.mediaRecorder && state.mediaRecorder.state !== "inactive") {
    state.mediaRecorder.stop();
  }
}

function updateSpeechPrompt(interimText) {
  const pieces = [state.speechBaseText, state.speechFinalText, interimText].filter(Boolean);
  const text = pieces.join(" ").replace(/\s+/g, " ").trim();
  $("#main-prompt").value = text;
  return text;
}

async function sendAudioForTranscription(file) {
  updateAgentStatus("Transcrevendo...", "#b7791f");
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/ops/stt/transcribe", {method: "POST", body: form});
    if (data.status === "completed") {
      const text = data.transcription?.text || "";
      $("#main-prompt").value = text;
      setVoiceHint("Transcricao inserida no campo de pedido.");
      updateAgentStatus("Transcricao pronta", "#1f9d67");
    } else {
      setVoiceHint(data.message || data.status || "STT por arquivo indisponivel.");
      updateAgentStatus("Use microfone em tempo real", "#b7791f");
    }
  } catch (error) {
    setVoiceHint(`Falha no STT: ${error.message}`);
    updateAgentStatus("Erro STT", "#ba2433");
  }
}

function setRecording(value, label = null) {
  state.recording = value;
  ["#mic-btn", "#mic-btn-panel"].forEach((selector) => $(selector)?.classList.toggle("recording", value));
  updateAgentStatus(value ? label || "Ouvindo..." : "Ocioso", value ? "#b7791f" : "#1f9d67");
  $(".voice-meter")?.classList.toggle("active", value);
}

function setVoiceHint(text) {
  const target = $("#voice-hint");
  if (target) target.textContent = text;
}

function updateAgentStatus(text, color) {
  if ($("#agent-status-text")) $("#agent-status-text").textContent = text;
  if ($("#agent-status-led")) $("#agent-status-led").style.backgroundColor = color;
}

function setRight(tab, html) {
  const targetByTab = {
    dashboard: "#dashboard-output",
    banco: "#estoque-output",
    estoque: "#estoque-output",
    contas: "#contas-output",
    reunioes: "#reunioes-output",
  };
  const selector = targetByTab[tab];
  if (!selector) return;
  const body = $(selector);
  if (body) {
    body.innerHTML = html;
    hydrateRichContent(body);
  }
}

function setResultStage(html) {
  const stage = $("#agent-result-stage");
  if (!stage) return;
  stage.innerHTML = html;
  hydrateRichContent(stage);
}

function startAgentRunView(prompt) {
  setResultStage(`
    <div class="run-monitor" id="agent-run-monitor">
      <section class="final-output waiting" id="agent-live-result">
        <div>
          <span class="result-source">Resposta final</span>
          <p>Aguardando a IA concluir a entrega.</p>
        </div>
      </section>
      <details class="monitor-details" id="agent-steps-details" open>
        <summary>Etapas em tempo real</summary>
        <div class="step-list" id="agent-stage-events">
          <article class="step-event system"><strong>pedido</strong><p>${escapeHtml(prompt)}</p></article>
        </div>
      </details>
      <details class="monitor-details" id="agent-raw-details" open>
        <summary>Eventos tecnicos e JSON</summary>
        <div class="raw-event-list" id="agent-trace-events"></div>
      </details>
    </div>
  `);
}

function clearAssistantPanel() {
  $("#main-prompt").value = "";
  setResultStage(`
    <div class="delivery-empty">
      <svg viewBox="0 0 24 24" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>
      <h3>Selecione uma opcao</h3>
      <p>Escolha uma consulta acima ou use o campo abaixo para obter uma entrega estruturada.</p>
    </div>
  `);
  const history = $("#request-history");
  if (history) history.innerHTML = '<div class="history-empty">Nenhuma consulta ainda.<br>Selecione uma opcao acima.</div>';
  updateAgentStatus("Ocioso", "#16a34a");
}

function appendAgentTraceEvent(event) {
  const stageList = $("#agent-stage-events");
  if (stageList) {
    const stageItem = document.createElement("article");
    stageItem.className = `step-event ${event.type || "system"}`;
    stageItem.innerHTML = `<strong>${escapeHtml(traceLabel(event.type || "system"))}</strong><p>${escapeHtml(event.message || event.event || event.tool_name || "")}</p>`;
    stageList.appendChild(stageItem);
    stageList.scrollTop = stageList.scrollHeight;
  }

  const list = $("#agent-trace-events");
  if (!list) return;
  const item = document.createElement("pre");
  const type = event.type || "system";
  item.className = `raw-event ${type}`;
  const detail = {...event};
  item.textContent = JSON.stringify(detail, null, 2);
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
  if (event.type === "tool") updateAgentStatus(`Tool: ${event.tool_name || "executando"}`, "#b7791f");
  if (event.type === "model") updateAgentStatus("IA trabalhando...", "#b7791f");
  if (event.type === "response") updateAgentStatus("Formatando entrega...", "#1f9d67");
}

function finalizeAgentRunPanel() {
  const live = $("#agent-live-result");
  const monitor = $("#agent-run-monitor");
  if (live) live.classList.remove("waiting");
  if (monitor) monitor.classList.add("completed");
  const steps = $("#agent-steps-details");
  const raw = $("#agent-raw-details");
  if (steps) steps.open = false;
  if (raw) raw.open = false;
}

function traceLabel(type) {
  return {
    system: "sistema",
    model: "modelo",
    tool: "tool",
    response: "resposta",
    error: "erro",
    warning: "aviso",
  }[type] || type;
}

function card(title, text, highlight = false) {
  return `<div class="answer-card ${highlight ? "highlight" : ""}"><b>${escapeHtml(title)}</b><p>${escapeHtml(text)}</p></div>`;
}

function listCard(title, rows, formatter) {
  const body = (rows || []).length
    ? rows.slice(0, 6).map((item) => `<li>${escapeHtml(formatter(item))}</li>`).join("")
    : "<li>Nenhum item.</li>";
  return `<div class="answer-card"><b>${escapeHtml(title)}</b><ul>${body}</ul></div>`;
}

function jsonCard(title, value, highlight = false) {
  return `<div class="answer-card ${highlight ? "highlight" : ""}"><b>${escapeHtml(title)}</b><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></div>`;
}

function jsonDetails(title, value, open = false) {
  return `
    <details class="monitor-details" ${open ? "open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <div class="raw-event-list">
        <pre class="raw-event">${escapeHtml(JSON.stringify(value, null, 2))}</pre>
      </div>
    </details>
  `;
}

function renderMeetingPipeline(payload) {
  const record = payload?.record || payload || {};
  const events = payload?.pipeline_events || record.pipeline_events || [];
  const context = Array.isArray(payload?.relevant_context)
    ? payload.relevant_context
    : (Array.isArray(record.relevant_context) ? record.relevant_context : []);
  const memory = Array.isArray(payload?.memory_updates)
    ? payload.memory_updates
    : _memoryUpdatesAsRows(record.memory_updates);
  const markdown = record.markdown_report || record.summary || payload?.message || "Aguardando processamento da reuniao.";
  const mermaid = record.mermaid_diagram ? `\n\n\`\`\`mermaid\n${record.mermaid_diagram}\n\`\`\`` : "";
  return `
    <div class="answer-card highlight">
      <b>${escapeHtml(record.title || "Reuniao operacional")}</b>
      <div class="markdown-body">${renderMarkdown(`${markdown}${mermaid}`)}</div>
    </div>
    <div class="report-grid">
      ${meetingSection("Insights", record.insights)}
      ${meetingSection("Decisoes", record.decisions)}
      ${meetingSection("Riscos", record.risks)}
      ${meetingSection("Proximas acoes", record.next_actions)}
      ${meetingSection("Perguntas abertas", record.open_questions)}
      ${meetingSection("Contexto usado", context.map((item) => item.summary || item.id))}
      ${meetingSection("Memoria escrita", memory.map((item) => item.summary || item.title))}
    </div>
    <details class="monitor-details" ${payload?.status === "completed" ? "" : "open"}>
      <summary>Pipeline observavel</summary>
      <div class="step-list">
        ${events.map((event) => `<article class="step-event ${escapeHtml(event.type || "system")}"><strong>${escapeHtml(traceLabel(event.type || "system"))}</strong><p>${escapeHtml(event.message || event.event || "")}</p></article>`).join("") || `<div class="empty">Nenhum evento registrado ainda.</div>`}
      </div>
    </details>
    ${jsonDetails("Registro tecnico salvo", payload, false)}
  `;
}

function _memoryUpdatesAsRows(memoryUpdates) {
  if (!memoryUpdates || typeof memoryUpdates !== "object") return [];
  return [
    ...(memoryUpdates.entities || []).map((value) => ({title: `Entidade: ${value}`})),
    ...(memoryUpdates.topics || []).map((value) => ({title: `Topico: ${value}`})),
    ...(memoryUpdates.commitments || []).map((value) => ({title: `Compromisso: ${value}`})),
    ...(memoryUpdates.watch_items || []).map((value) => ({title: `Acompanhar: ${value}`})),
  ];
}

function meetingSection(title, values) {
  const items = Array.isArray(values) && values.length
    ? values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")
    : "<li>Nenhum item.</li>";
  return `<div class="report-section"><span>${escapeHtml(title)}</span><ul>${items}</ul></div>`;
}

function renderOperationalResult(title, payload) {
  if (payload?.visualization) return renderDelivery(payload);
  if (payload?.record?.cleaned_meeting || payload?.record?.markdown_report) return renderMeetingPipeline(payload);
  const report = payload?.result?.final_report || payload?.final_report || payload?.result?.report || null;
  if (!report) return jsonCard(title, payload, true);

  const sections = [
    ["Falta de estoque", report.stock_shortages],
    ["Validade proxima", report.expiration_risks],
    ["Consumo anormal", report.abnormal_consumption],
    ["Fornecedores", report.supplier_issues],
    ["Sugestoes de compra", report.purchase_suggestions],
    ["Aprovacao", report.actions_requiring_approval],
    ["Proximas acoes", report.next_actions],
    ["Qualidade dos dados", report.data_quality_issues],
  ];
  const cards = sections.map(([label, rows]) => {
    const items = Array.isArray(rows) && rows.length
      ? rows.slice(0, 5).map((item) => `<li>${escapeHtml(item.product_name || item.title || item.issue || item.recommended_action || JSON.stringify(item))}</li>`).join("")
      : "<li>Nenhum item.</li>";
    return `<div class="report-section"><span>${escapeHtml(label)}</span><ul>${items}</ul></div>`;
  }).join("");

  return `
    <div class="answer-card highlight">
      <b>${escapeHtml(title)}</b>
      <div class="markdown-body">${renderMarkdown(report.executive_summary || payload?.message || "Relatorio gerado.")}</div>
      <div class="report-grid">${cards}</div>
    </div>
    ${jsonCard("JSON validado", payload)}
  `;
}

function renderDelivery(payload) {
  const visualization = payload?.visualization || {};
  const metadata = payload?.metadata || {};
  if (["error", "model_unavailable", "ai_response_invalid"].includes(payload?.status)) {
    return renderNotice(payload?.title || "Falha na entrega", payload?.summary || payload?.message || "A execucao falhou.", "danger");
  }
  if (payload?.status === "ai_busy" || visualization.type === "notice") {
    return renderNotice(payload?.title || "IA ocupada", payload?.summary || payload?.message || "Aguarde a execucao atual terminar.", "warning");
  }

  const header = `
    <div class="result-headline">
      <div>
        <span class="result-source">${escapeHtml(payload?.source || payload?.mode || "operacional")}</span>
        <h3>${escapeHtml(payload?.title || "Resultado operacional")}</h3>
        <p>${escapeHtml(payload?.summary || "")}</p>
      </div>
      <div class="result-metrics">
        <strong>${escapeHtml(metadata.duration_ms ?? "-")}</strong><span>ms</span>
        <strong>${escapeHtml(metadata.llm_calls ?? 0)}</strong><span>IA calls</span>
      </div>
    </div>
  `;

  if (visualization.type === "table") {
    return `${header}${renderTable(visualization.columns || [], visualization.rows || [])}`;
  }
  if (visualization.type === "list") {
    return `${header}${renderList(visualization.items || [])}`;
  }
  if (visualization.type === "answer") {
    return `${header}<div class="result-answer">${renderMarkdown(payload?.answer || payload?.summary || "")}</div>`;
  }
  if (visualization.type === "report") {
    return `${header}${renderReportBlocks(visualization.report || {})}`;
  }
  if (visualization.type === "raw") {
    return `${header}${jsonCard("Detalhes tecnicos", visualization.payload || payload)}`;
  }
  return `${header}${jsonCard("Detalhes tecnicos", payload)}`;
}

function renderTable(columns, rows) {
  if (!rows.length) return `<div class="empty">Nenhum item encontrado.</div>`;
  const head = columns.map((column) => `<th>${escapeHtml(column.label || column.key)}</th>`).join("");
  const body = rows.map((rowData) => (
    `<tr>${columns.map((column, index) => {
      const value = Array.isArray(rowData.cells) ? rowData.cells[index] : rowData[column.key];
      return `<td>${formatCell(value)}</td>`;
    }).join("")}</tr>`
  )).join("");
  return `<div class="result-table-wrap"><table class="result-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderList(items) {
  if (!items.length) return `<div class="empty">Nenhum item retornado.</div>`;
  return `<ol class="result-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>`;
}

function renderReportBlocks(report) {
  const sections = [
    ["Falta de estoque", report.stock_shortages],
    ["Validade proxima", report.expiration_risks],
    ["Consumo anormal", report.abnormal_consumption],
    ["Fornecedores", report.supplier_issues],
    ["Sugestoes de compra", report.purchase_suggestions],
    ["Aprovacao", report.actions_requiring_approval],
    ["Proximas acoes", report.next_actions],
    ["Qualidade dos dados", report.data_quality_issues],
  ];
  return `<div class="report-grid">${sections.map(([label, rows]) => {
    const list = Array.isArray(rows) && rows.length
      ? rows.map((item) => `<li>${escapeHtml(item.product_name || item.title || item.issue || item.recommended_action || JSON.stringify(item))}</li>`).join("")
      : "<li>Nenhum item.</li>";
    return `<div class="report-section"><span>${escapeHtml(label)}</span><ul>${list}</ul></div>`;
  }).join("")}</div>`;
}

function renderNotice(title, message, level = "info") {
  return `<div class="result-notice ${escapeHtml(level)}"><b>${escapeHtml(title)}</b><p>${escapeHtml(message)}</p></div>`;
}

function appendRequestHistory(prompt, status, metadata = null) {
  const history = $("#request-history");
  if (!history) return;
  const emptyNode = history.querySelector(".empty, .history-empty");
  if (emptyNode) emptyNode.remove();
  const pending = history.querySelector('[data-pending="true"]');
  if (pending && status !== "enviado") {
    pending.dataset.pending = "false";
    pending.classList.add("active");
    const timing = metadata?.duration_ms != null ? ` - ${metadata.duration_ms}ms` : "";
    pending.innerHTML = `
      <div class="history-block-label">${escapeHtml(status || "concluido")}${escapeHtml(timing)}</div>
      <div class="history-block-meta"><span>${escapeHtml(prompt)}</span></div>
    `;
    return;
  }
  const item = document.createElement("div");
  item.className = "history-block active";
  item.dataset.pending = status === "enviado" ? "true" : "false";
  history.querySelectorAll(".history-block").forEach((entry) => entry.classList.remove("active"));
  const timing = metadata?.duration_ms != null ? ` - ${metadata.duration_ms}ms` : "";
  item.innerHTML = `
    <div class="history-block-label">${escapeHtml(status || "pedido")}${escapeHtml(timing)}</div>
    <div class="history-block-meta"><span>${escapeHtml(prompt)}</span></div>
  `;
  history.prepend(item);
}

function columnsForSearch(intent) {
  if (["low_stock", "expiration_risks", "free_product_search"].includes(intent)) {
    return [
      {key: "sku", label: "SKU"},
      {key: "name", label: "Produto"},
      {key: "category", label: "Categoria"},
      {key: "current_stock", label: "Estoque"},
      {key: "minimum_stock", label: "Minimo"},
      {key: "ideal_stock", label: "Ideal"},
      {key: "criticality", label: "Criticidade"},
      {key: "expiration_date", label: "Validade"},
      {key: "status", label: "Status"},
    ];
  }
  if (intent === "supplier_issues") {
    return [
      {key: "name", label: "Fornecedor"},
      {key: "email", label: "Email"},
      {key: "phone", label: "Telefone"},
      {key: "missing", label: "Campos faltando"},
      {key: "default_lead_time_days", label: "Prazo"},
    ];
  }
  if (intent === "abnormal_consumption") {
    return [
      {key: "title", label: "Alerta"},
      {key: "severity", label: "Severidade"},
      {key: "product_id", label: "Produto ID"},
      {key: "description", label: "Descricao"},
      {key: "created_at", label: "Criado em"},
    ];
  }
  return [{key: "name", label: "Item"}, {key: "status", label: "Status"}];
}

function formatCell(value) {
  if (Array.isArray(value)) return escapeHtml(value.join(", "));
  if (value && typeof value === "object") return escapeHtml(JSON.stringify(value));
  if (value === null || value === undefined || value === "") return '<span class="muted">-</span>';
  return escapeHtml(value);
}

function setupRichRendering() {
  if (window.mermaid) {
    window.mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: "neutral"});
  }
}

function setupVoiceSupportHint() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    setVoiceHint("Este navegador nao oferece STT nativo. Use upload de audio com STT_ENGINE=proxy/embedded.");
    const mode = $("#voice-mode");
    if (mode) {
      mode.textContent = "Fallback por arquivo";
      mode.classList.remove("green");
      mode.classList.add("gold");
    }
  }
}

function hydrateRichContent(root = document) {
  renderMermaidBlocks(root);
}

function renderMarkdown(markdown) {
  const text = String(markdown ?? "");
  if (!window.marked) return escapeHtml(text).replace(/\n/g, "<br>");
  const parsed = window.marked.parse(text, {breaks: true, gfm: true});
  return sanitizeHtml(parsed);
}

async function renderMermaidBlocks(root) {
  if (!window.mermaid) return;
  const blocks = Array.from(root.querySelectorAll("code.language-mermaid, pre.mermaid"));
  for (const block of blocks) {
    const source = block.textContent || "";
    const host = block.closest("pre") || block;
    if (!source.trim() || host.dataset.rendered === "true") continue;
    try {
      const id = `mermaid-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const rendered = await window.mermaid.render(id, source);
      host.outerHTML = `<div class="mermaid-block">${rendered.svg}</div>`;
      host.dataset.rendered = "true";
    } catch (error) {
      host.classList.add("mermaid-error");
      host.title = error.message;
    }
  }
}

function sanitizeHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script,style,iframe,object,embed").forEach((node) => node.remove());
  template.content.querySelectorAll("*").forEach((node) => {
    Array.from(node.attributes).forEach((attribute) => {
      const name = attribute.name.toLowerCase();
      const value = attribute.value || "";
      if (name.startsWith("on") || (/^(href|src)$/i.test(name) && /^javascript:/i.test(value))) {
        node.removeAttribute(attribute.name);
      }
    });
  });
  return template.innerHTML;
}

function productSummary(product) {
  return `${product.name} (${product.sku}) - estoque ${product.current_stock}/${product.minimum_stock}`;
}

function supplierSummary(supplier) {
  return `${supplier.name} - faltando ${(supplier.missing || []).join(", ")}`;
}

function statusRow(label, status) {
  const ok = status?.available;
  return row(label, status?.message || "Sem status.", tag(ok ? "online" : "indisponivel", ok ? "green" : "gold"));
}

function row(title, subtitle, right = "") {
  return `<div class="row-item"><div><b>${escapeHtml(title)}</b><span>${escapeHtml(subtitle || "")}</span></div>${right}</div>`;
}

function kpi(value, label) {
  return `<div class="kpi"><strong>${escapeHtml(value ?? 0)}</strong><span>${escapeHtml(label)}</span></div>`;
}

function tag(text, color = "") {
  return `<span class="tag ${color}">${escapeHtml(text || "-")}</span>`;
}

function badge(value) {
  const text = String(value || "normal");
  let cls = "badge-success";
  if (/(low|critical|high|baixo|erro|danger)/i.test(text)) cls = "badge-danger";
  if (/(near|medium|venc|warning|gold|pending|model)/i.test(text)) cls = "badge-warning";
  return `<span class="badge ${cls}">${escapeHtml(text)}</span>`;
}

function empty(message) {
  return `<div class="empty">${escapeHtml(message)}</div>`;
}

function tableLoading() {
  return `<tr><td colspan="4">Carregando...</td></tr>`;
}

function tableEmpty(message) {
  return `<tr><td colspan="4">${message}</td></tr>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
  })[char]);
}
