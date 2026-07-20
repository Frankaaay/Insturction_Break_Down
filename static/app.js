const $ = (selector) => document.querySelector(selector);

let provider = "deepseek";
let execution = null;
let eventSource = null;
let commandBusy = false;
let countdownTimer = null;

const STATE_LABELS = {
  ready: "待确认",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  terminated: "已终止",
};

const EVENT_LABELS = {
  "execution.created": "执行计划已创建",
  "execution.started": "执行会话已启动",
  "execution.resumed": "人工恢复当前步骤",
  "execution.paused": "连续失败，执行已暂停",
  "execution.completed": "全部原子操作已完成",
  "execution.terminated": "执行会话已终止",
  "step.started": "开始执行原子操作",
  "attempt.succeeded": "Monitor 回报成功",
  "attempt.failed": "Monitor 回报失败",
  "attempt.timed_out": "Monitor 等待超时",
  "report.rejected": "拒绝过期的 Monitor 回报",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function randomId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isOpenSession(item = execution) {
  return item && !["completed", "terminated"].includes(item.state);
}

function setConnection(kind, text) {
  const node = $("#connection");
  node.className = `connection ${kind}`;
  node.lastElementChild.textContent = text;
}

function applyExecution(next) {
  if (!next) return;
  if (execution && next.execution_id === execution.execution_id) {
    const currentVersion = execution.version ?? 0;
    const nextVersion = next.version ?? 0;
    if (nextVersion < currentVersion) return;
  }
  execution = next;
  commandBusy = false;
  localStorage.setItem("activeExecutionId", execution.execution_id);
  renderExecution();
  updateInputLock();
}

async function initProviders() {
  try {
    const response = await fetch("/api/providers");
    const data = await response.json();
    provider = data.default;
    const row = $("#providerRow");
    data.providers.forEach((name) => {
      const button = document.createElement("button");
      button.className = `pill${name === provider ? " active" : ""}`;
      button.textContent = name;
      button.dataset.provider = name;
      button.onclick = () => {
        if (isOpenSession()) return;
        provider = name;
        row.querySelectorAll(".pill").forEach((item) => item.classList.toggle("active", item === button));
      };
      row.appendChild(button);
    });
  } catch (_) {
    setConnection("wait", "提供商列表加载失败");
  }
}

function operationDetail(operation) {
  const logics = (operation.logics || []).map((logic) =>
    `<div><b>logic${esc(logic.n)}</b> ${esc(logic.zh || logic.en)}</div>`).join("");
  const extras = [];
  Object.entries(operation.coefs || {}).forEach(([name, values]) => {
    extras.push(`<div>${esc(name)}：${(values.zh || values.en || []).map((v) => `<span class="tag">${esc(v)}</span>`).join("")}</div>`);
  });
  Object.entries(operation.categories || {}).forEach(([name, values]) => {
    extras.push(`<div>${esc(name)}：${values.map((v) => `<span class="tag">${esc(v)}</span>`).join("")}</div>`);
  });
  if (operation.typical_length != null) extras.push(`<div>典型长度：${esc(operation.typical_length)}</div>`);
  return logics + extras.join("");
}

function operationItem(operation, expert = false) {
  const search = `${operation.id} ${operation.zh} ${operation.en}`.toLowerCase();
  return `<details class="op-item${expert ? " expert" : ""}" data-search="${esc(search)}">
    <summary><span class="oid">${esc(operation.id)}</span><span class="op-name">${esc(operation.zh)}</span><span class="op-en">${esc(operation.en)}</span></summary>
    <div class="op-detail">${operationDetail(operation)}</div>
  </details>`;
}

async function loadOperations() {
  try {
    const response = await fetch("/api/operations");
    const data = await response.json();
    $("#ops").innerHTML = `
      <details class="op-group" open>
        <summary>原子操作 <span class="op-count">${data.atomic.length}</span></summary>
        ${data.atomic.map((item) => operationItem(item)).join("")}
      </details>
      <details class="op-group">
        <summary>专家操作 <span class="op-count">${data.expert.length}</span></summary>
        ${data.expert.map((item) => operationItem(item, true)).join("")}
      </details>`;
  } catch (_) {
    $("#ops").innerHTML = '<div class="notice bad">操作库加载失败，不影响执行会话。</div>';
  }
}

function filterOperations() {
  const query = $("#opSearch").value.trim().toLowerCase();
  document.querySelectorAll(".op-item").forEach((item) => {
    item.hidden = Boolean(query) && !item.dataset.search.includes(query);
  });
  if (query) document.querySelectorAll(".op-group").forEach((group) => { group.open = true; });
}

function renderSlots(step) {
  const tags = [`<span class="tag action">${esc(step.action_id)} ${esc(step.action)} · logic${esc(step.logic)}</span>`];
  Object.entries(step.slots || {}).forEach(([key, value]) => {
    tags.push(`<span class="tag">${esc(key)} = ${esc(value)}</span>`);
  });
  return tags.join("");
}

function attemptChips(step) {
  if (!step.attempts?.length) return "";
  const chips = step.attempts.map((attempt) => {
    const label = {
      waiting: "等待中", success: "成功", failure: "失败", timeout: "超时", cancelled: "取消",
    }[attempt.status] || attempt.status;
    return `<span class="attempt-chip ${esc(attempt.status)}">#${attempt.attempt_no} ${label}</span>`;
  }).join("");
  return `<div class="attempts">${chips}</div>`;
}

function renderStep(step) {
  const icon = step.status === "succeeded" ? "✓" : (step.status === "blocked" ? "!" : step.index + 1);
  return `<div class="execution-step ${esc(step.status)}">
    <div class="step-dot">${icon}</div>
    <div class="step-main">
      <div class="step-title">${esc(step.zh)}${step.status === "active" ? '<span class="current-label">CURRENT</span>' : ""}</div>
      <div class="step-en">${esc(step.en)}</div>
      <div class="step-tags">${renderSlots(step)}</div>
      ${attemptChips(step)}
    </div>
  </div>`;
}

function currentStep() {
  const index = execution?.current_step_index;
  return index == null ? null : execution.steps[index];
}

function monitorControls() {
  if (execution.state === "ready") {
    return `<div class="monitor-kicker">Plan review</div>
      <div class="monitor-action">计划等待确认</div>
      <div class="monitor-sub">启动后，第一步将进入 ${esc(execution.timeout_seconds)} 秒 Monitor 窗口。</div>
      <div class="monitor-buttons">
        <button class="control-btn primary" data-action="start" ${commandBusy ? "disabled" : ""}>开始执行</button>
        <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>放弃计划</button>
      </div>`;
  }

  const step = currentStep();
  if (execution.state === "running" && step && execution.active_attempt) {
    const attempt = execution.active_attempt;
    return `<div class="monitor-kicker">Virtual monitor · Attempt ${attempt.attempt_no}</div>
      <div class="timer-ring" id="timerRing"><div class="timer-copy"><strong id="secondsLeft">--</strong><span>SECONDS LEFT</span></div></div>
      <div class="monitor-action">${esc(step.zh)}</div>
      <div class="monitor-sub">${esc(step.action_id)} ${esc(step.action)} / logic${esc(step.logic)}</div>
      <div class="monitor-slots">${renderSlots(step)}</div>
      <div class="monitor-buttons">
        <button class="control-btn success" data-action="report-success" ${commandBusy ? "disabled" : ""}>✓ 操作成功</button>
        <button class="control-btn failure" data-action="report-failure" ${commandBusy ? "disabled" : ""}>✕ 操作失败</button>
        <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>终止任务</button>
      </div>
      <div class="monitor-note">按钮仅向后端上报；后端验证 attempt 后决定是否推进。</div>`;
  }

  if (execution.state === "paused") {
    return `<div class="monitor-kicker">Human intervention required</div>
      <div class="monitor-action">当前步骤已阻塞</div>
      <div class="monitor-sub">自动尝试已耗尽。可以再给当前步骤一次机会，或安全终止整条任务。</div>
      <div class="monitor-buttons">
        <button class="control-btn primary" data-action="retry" ${commandBusy ? "disabled" : ""}>重试当前步骤</button>
        <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>终止任务</button>
      </div>`;
  }

  const completed = execution.state === "completed";
  return `<div class="monitor-kicker">Execution closed</div>
    <div class="timer-ring" style="--timer-angle:${completed ? "360deg" : "0deg"};background:conic-gradient(${completed ? "var(--green)" : "var(--red)"} var(--timer-angle),#e9ebf3 0)">
      <div class="timer-copy"><strong>${completed ? "✓" : "■"}</strong><span>${completed ? "COMPLETED" : "TERMINATED"}</span></div>
    </div>
    <div class="monitor-action">${completed ? "任务执行完成" : "任务已终止"}</div>
    <div class="monitor-sub">现在可以生成一条新的执行计划。</div>`;
}

function formatTime(value) {
  try { return new Date(value).toLocaleTimeString("zh-CN", { hour12: false }); }
  catch (_) { return "--:--:--"; }
}

function eventDescription(event) {
  const data = event.data || {};
  const source = data.source ? `<span class="event-source">${esc(data.source)}</span>` : "";
  let detail = EVENT_LABELS[event.type] || event.type;
  if (data.attempt_no) detail += ` · attempt ${data.attempt_no}`;
  return `<b>${esc(detail)}</b>${source}${data.detail ? `<div>${esc(data.detail)}</div>` : ""}`;
}

function renderEvents() {
  const events = [...(execution.events || [])].reverse();
  return events.map((event) => `<div class="event-row"><span class="event-time">${formatTime(event.occurred_at)}</span><span class="event-text">${eventDescription(event)}</span></div>`).join("");
}

function renderExecution() {
  if (!execution) return;
  const progress = execution.progress || { succeeded: 0, total: execution.steps.length, ratio: 0 };
  const percentage = Math.round(progress.ratio * 100);
  $("#result").innerHTML = `
    <section class="execution-head fade-in">
      <div class="execution-top">
        <div class="execution-title"><div class="eyebrow">Execution ${esc(execution.execution_id.slice(0, 8))}</div><h2>${esc(execution.instruction)}</h2></div>
        <span class="state-pill ${esc(execution.state)}">${STATE_LABELS[execution.state] || esc(execution.state)}</span>
      </div>
      <div class="progress-row"><div class="progress-track"><div class="progress-bar" style="width:${percentage}%"></div></div><span class="progress-copy">${progress.succeeded} / ${progress.total} · ${percentage}%</span></div>
    </section>
    <div class="execution-grid fade-in">
      <section class="chain-card">
        <div class="card-heading"><h3>原子操作链</h3><span>${execution.steps.length} 个步骤</span></div>
        <div class="execution-steps">${execution.steps.map(renderStep).join("")}</div>
      </section>
      <aside class="monitor-card"><div class="card-heading"><h3>Monitor 控制台</h3><span>后端权威</span></div><div class="monitor-body">${monitorControls()}</div></aside>
    </div>
    <details class="log-card fade-in">
      <summary class="card-heading"><h3>执行事件日志</h3><span>${execution.events?.length || 0} 条 · 点击展开</span></summary>
      <div class="event-list">${renderEvents()}</div>
    </details>`;
  updateCountdown();
}

function updateCountdown() {
  const secondsNode = $("#secondsLeft");
  const ring = $("#timerRing");
  if (!secondsNode || !ring || !execution?.active_attempt) return;
  const deadline = new Date(execution.active_attempt.deadline_at).getTime();
  const total = Number(execution.timeout_seconds) * 1000;
  const remaining = Math.max(0, deadline - Date.now());
  secondsNode.textContent = Math.ceil(remaining / 1000);
  ring.style.setProperty("--timer-angle", `${Math.max(0, Math.min(360, remaining / total * 360))}deg`);
}

function renderIssue(data) {
  execution = null;
  const warning = data.status === "ambiguous";
  const title = warning ? "指令不明确" : (data.status === "infeasible" ? "无法执行" : "请求出错");
  $("#result").innerHTML = `<div class="notice ${warning ? "" : "bad"}"><strong>${title}</strong><br>${esc(data.reason || "未知错误")}</div>`;
  updateInputLock();
}

function updateInputLock() {
  const locked = Boolean(isOpenSession());
  $("#inputPanel").classList.toggle("locked", locked);
  $("#instruction").disabled = locked;
  $("#submit").disabled = locked || commandBusy;
  document.querySelectorAll("#providerRow .pill").forEach((button) => { button.disabled = locked; });
}

async function parseResponse(response) {
  let body = {};
  try { body = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) throw new Error(body.detail || body.reason || `HTTP ${response.status}`);
  return body;
}

async function submitExecution() {
  if (isOpenSession()) return;
  const instruction = $("#instruction").value.trim();
  if (!instruction) { $("#instruction").focus(); return; }
  commandBusy = true;
  const button = $("#submit");
  button.disabled = true;
  button.classList.add("loading");
  $("#submitText").textContent = "Planner 拆解中";
  setConnection("wait", "正在生成计划");
  try {
    const response = await fetch("/api/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction, provider }),
    });
    const data = await parseResponse(response);
    commandBusy = false;
    if (data.status === "ok" && data.execution) {
      execution = null;
      applyExecution(data.execution);
      connectEvents(data.execution.execution_id);
    } else {
      renderIssue(data);
      setConnection("wait", "等待执行会话");
    }
  } catch (error) {
    commandBusy = false;
    renderIssue({ status: "error", reason: error.message });
    setConnection("wait", "请求失败");
  } finally {
    button.classList.remove("loading");
    $("#submitText").textContent = "生成执行计划";
    updateInputLock();
  }
}

async function postControl(suffix, body = null) {
  if (!execution || commandBusy) return;
  commandBusy = true;
  renderExecution();
  updateInputLock();
  try {
    const options = { method: "POST", headers: {} };
    if (body) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(`/api/executions/${execution.execution_id}/${suffix}`, options);
    const data = await parseResponse(response);
    commandBusy = false;
    if (data.execution) applyExecution(data.execution);
  } catch (error) {
    commandBusy = false;
    alert(error.message);
    try {
      const current = await parseResponse(await fetch(`/api/executions/${execution.execution_id}`));
      applyExecution(current.execution);
    } catch (_) {
      renderExecution();
      updateInputLock();
    }
  }
}

function handleControl(action) {
  if (!execution) return;
  if (action === "start") return postControl("start");
  if (action === "retry") return postControl("retry");
  if (action === "terminate") return postControl("terminate");
  if (action.startsWith("report-")) {
    const step = currentStep();
    const attempt = execution.active_attempt;
    if (!step || !attempt) return;
    return postControl("reports", {
      report_id: randomId(),
      step_id: step.step_id,
      attempt_id: attempt.attempt_id,
      outcome: action === "report-success" ? "success" : "failure",
      source: "human",
    });
  }
}

function connectEvents(executionId) {
  if (eventSource) eventSource.close();
  setConnection("wait", "连接 Monitor 事件流");
  eventSource = new EventSource(`/api/executions/${executionId}/events`);
  eventSource.onopen = () => setConnection("live", "Monitor 已连接");
  eventSource.addEventListener("execution", (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.snapshot) applyExecution(event.snapshot);
    } catch (_) { /* malformed SSE event is ignored */ }
  });
  eventSource.onerror = () => setConnection("wait", "Monitor 重连中");
}

async function restoreExecution() {
  const executionId = localStorage.getItem("activeExecutionId");
  if (!executionId) return;
  try {
    const response = await fetch(`/api/executions/${executionId}`);
    if (response.status === 404) {
      localStorage.removeItem("activeExecutionId");
      $("#result").innerHTML = '<div class="notice">上次内存会话已失效，可能是后端刚刚重启。请重新生成执行计划。</div>';
      return;
    }
    const data = await parseResponse(response);
    applyExecution(data.execution);
    connectEvents(executionId);
  } catch (_) {
    setConnection("wait", "会话恢复失败");
  }
}

function closeMobileLibrary() {
  $("#library").classList.remove("open");
  $("#scrim").classList.remove("open");
}

$("#submit").onclick = submitExecution;
$("#instruction").addEventListener("keydown", (event) => { if (event.key === "Enter") submitExecution(); });
document.querySelectorAll(".chip").forEach((chip) => {
  chip.onclick = () => { $("#instruction").value = chip.dataset.i; submitExecution(); };
});
$("#result").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (button) handleControl(button.dataset.action);
});
$("#opSearch").addEventListener("input", filterOperations);
$("#closeLibrary").onclick = () => {
  if (window.matchMedia("(max-width: 980px)").matches) closeMobileLibrary();
  else {
    $("#library").classList.toggle("collapsed");
    document.querySelector(".app-shell").classList.toggle("library-collapsed");
  }
};
$("#openLibrary").onclick = () => {
  $("#library").classList.add("open");
  $("#scrim").classList.add("open");
};
$("#scrim").onclick = closeMobileLibrary;

countdownTimer = setInterval(updateCountdown, 200);
window.addEventListener("beforeunload", () => {
  if (eventSource) eventSource.close();
  if (countdownTimer) clearInterval(countdownTimer);
});

initProviders();
loadOperations();
restoreExecution();
