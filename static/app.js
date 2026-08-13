const $ = (selector) => document.querySelector(selector);

let provider = "deepseek";
let execution = null;
let eventSource = null;
let commandBusy = false;
let countdownTimer = null;
let previewSocket = null;
let previewCameraId = null;
let previewObjectUrl = null;
let previewLastFrameAt = 0;
let previewLatestCapturedAt = 0;
let previewMeasuredFps = 0;
let previewArrivalTimes = [];
let previewReconnectTimer = null;

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
  "execution.mode.changed": "执行模式已切换",
  "execution.resumed": "人工恢复当前步骤",
  "execution.paused": "连续失败，执行已暂停",
  "execution.completed": "全部原子操作已完成",
  "execution.terminated": "执行会话已终止",
  "step.started": "开始执行原子操作",
  "attempt.succeeded": "Monitor 回报成功",
  "attempt.failed": "Monitor 回报失败",
  "attempt.timed_out": "Monitor 等待超时",
  "report.rejected": "拒绝过期的 Monitor 回报",
  "workflow.claimed": "GraspArm Agent 已领取任务",
  "workflow.node.updated": "GraspArm 内部步骤更新",
  "workflow.agent.stale": "GraspArm Agent 心跳中断",
  "workflow.agent.replaced": "GraspArm Agent 进程已更换，自动恢复被拒绝",
  "visual_monitor.claimed": "视觉采集端已连接",
  "visual_monitor.baseline.ready": "视觉初始帧已就绪",
  "visual_monitor.observation": "VLM 返回最新观察",
  "visual_monitor.error": "VLM 请求失败",
  "visual_monitor.inference.started": "VLM 推理已开始",
  "visual_monitor.resumed": "继续视觉监控",
  "chain_visual_monitor.claimed": "整链视觉采集端已连接",
  "chain_visual_monitor.baseline.ready": "整链视觉初始帧已就绪",
  "chain_visual_monitor.observation": "VLM 返回整链观察",
  "chain_visual_monitor.error": "整链 VLM 请求失败",
  "chain_visual_monitor.inference.started": "整链 VLM 推理已开始",
  "chain_visual_monitor.resumed": "继续整链视觉监控",
  "chain_visual_monitor.merged": "整链步骤状态已合并",
  "visual_monitor.pipeline": "视觉流水线阶段更新",
  "chain_visual_monitor.pipeline": "整链视觉流水线阶段更新",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function randomId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function desiredPreviewCamera() {
  if (execution?.execution_mode === "chain_visual_monitor") {
    return execution?.chain_visual_monitor?.camera_id || null;
  }
  if (execution?.execution_mode === "visual_monitor") {
    return execution?.active_attempt?.visual_monitor?.camera_id || null;
  }
  return null;
}

function setPreviewStatus(text, kind = "wait") {
  const node = $("#livePreviewStatus");
  if (!node) return;
  node.textContent = text;
  node.className = `live-preview-status ${kind}`;
}

function closeLivePreview() {
  if (previewReconnectTimer) clearTimeout(previewReconnectTimer);
  previewReconnectTimer = null;
  const socket = previewSocket;
  previewSocket = null;
  previewCameraId = null;
  previewLastFrameAt = 0;
  previewLatestCapturedAt = 0;
  previewMeasuredFps = 0;
  previewArrivalTimes = [];
  if (socket) socket.close();
}

function syncLivePreview() {
  const cameraId = desiredPreviewCamera();
  const image = $("#livePreviewImage");
  if (!cameraId || !image) {
    closeLivePreview();
    return;
  }
  if (previewObjectUrl) image.src = previewObjectUrl;
  if (previewCameraId === cameraId && previewSocket && previewSocket.readyState <= WebSocket.OPEN) return;
  closeLivePreview();
  previewCameraId = cameraId;
  setPreviewStatus("连接实时画面…");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/visual-monitor/live/view/${encodeURIComponent(cameraId)}`);
  socket.binaryType = "arraybuffer";
  previewSocket = socket;
  socket.onopen = () => {
    socket.send(JSON.stringify({ token: localStorage.getItem("operatorToken") || "" }));
  };
  socket.onmessage = (event) => {
    if (previewSocket !== socket) return;
    if (typeof event.data === "string") {
      try {
        if (JSON.parse(event.data).type === "ready") setPreviewStatus("实时画面已连接", "live");
      } catch (_) { /* ignore malformed control messages */ }
      return;
    }
    const packet = event.data;
    if (!(packet instanceof ArrayBuffer) || packet.byteLength <= 12) return;
    const view = new DataView(packet, 0, 12);
    const capturedAt = view.getFloat64(0, false) * 1000;
    const arrival = Date.now();
    previewArrivalTimes.push(arrival);
    const cutoff = arrival - 2000;
    while (previewArrivalTimes.length > 1 && previewArrivalTimes[0] < cutoff) previewArrivalTimes.shift();
    if (previewArrivalTimes.length > 1) {
      previewMeasuredFps = (previewArrivalTimes.length - 1) * 1000
        / Math.max(1, arrival - previewArrivalTimes[0]);
    }
    previewLastFrameAt = arrival;
    previewLatestCapturedAt = capturedAt;
    const oldUrl = previewObjectUrl;
    previewObjectUrl = URL.createObjectURL(new Blob([packet.slice(12)], { type: "image/jpeg" }));
    const currentImage = $("#livePreviewImage");
    if (currentImage) currentImage.src = previewObjectUrl;
    if (oldUrl) URL.revokeObjectURL(oldUrl);
    const latency = Math.max(0, arrival - capturedAt);
    setPreviewStatus(`${previewMeasuredFps.toFixed(1)} FPS · ${Math.round(latency)} ms`, "live");
  };
  socket.onclose = (event) => {
    if (previewSocket !== socket) return;
    previewSocket = null;
    setPreviewStatus(event.code === 4401 ? "需要操作员认证" : "实时画面重连中…", "wait");
    if (desiredPreviewCamera() === cameraId) {
      previewReconnectTimer = setTimeout(syncLivePreview, event.code === 4401 ? 3000 : 1000);
    }
  };
  socket.onerror = () => setPreviewStatus("实时画面连接异常", "bad");
}

setInterval(() => {
  if (desiredPreviewCamera() && previewLastFrameAt && Date.now() - previewLastFrameAt > 2000) {
    setPreviewStatus("画面超过2秒未更新", "bad");
  }
}, 1000);

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

function workflowForStep(step) {
  const attempts = step.attempts || [];
  const attempt = [...attempts].reverse().find(
    (item) => item.workflow || item.workflow_preview
  );
  return attempt?.workflow || attempt?.workflow_preview || null;
}

function workflowStateLabel(state) {
  return {
    pending: "等待", starting: "启动中", running: "运行中",
    waiting_input: "等待选择", passed: "通过", failed: "失败",
    blocked: "已阻断", cancelled: "已取消",
    registered: "等待 Agent", succeeded: "成功",
    needs_operator: "需要人工处理",
  }[state] || state;
}

function renderWorkflow(step) {
  const workflow = workflowForStep(step);
  if (!workflow) return "";
  const nodes = workflow.nodes || [];
  return `<div class="workflow-panel">
    <div class="workflow-head">
      <b>${esc(workflow.label || workflow.workflow_id || "Robot workflow")} · ${esc(workflow.version)}</b>
      <span class="workflow-run ${esc(workflow.state)}">${esc(workflowStateLabel(workflow.state))}</span>
    </div>
    ${workflow.message ? `<div class="workflow-message">${esc(workflow.message)}</div>` : ""}
    <div class="workflow-nodes">${nodes.map((node) => `
      <div class="workflow-node ${esc(node.state)}">
        <span class="workflow-dot"></span>
        <div class="workflow-copy">
          <div><b>${esc(node.label)}</b><em>${esc(node.type || "command")}</em><span>${esc(workflowStateLabel(node.state))}</span>
            ${node.service_health ? `<em class="service-health ${esc(node.service_health)}">${esc(node.service_health)}</em>` : ""}
          </div>
          ${node.description ? `<small>${esc(node.description)}</small>` : ""}
          ${node.start_after ? `<small>触发条件：${esc(node.start_after.node_id)} 输出 ${esc(node.start_after.marker)}</small>` : ""}
          ${node.message ? `<p>${esc(node.message)}</p>` : ""}
          ${node.exit_code != null ? `<small>exit=${esc(node.exit_code)}${node.marker ? ` · ${esc(node.marker)}` : ""}</small>` : ""}
          ${node.log_tail ? `<pre>${esc(node.log_tail)}</pre>` : ""}
        </div>
      </div>`).join("")}</div>
  </div>`;
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
      ${renderWorkflow(step)}
    </div>
  </div>`;
}

function currentStep() {
  const index = execution?.current_step_index;
  return index == null ? null : execution.steps[index];
}

function currentVisualMonitor() {
  if (execution?.execution_mode === "chain_visual_monitor") {
    return execution.chain_visual_monitor || null;
  }
  const step = currentStep();
  const lastAttempt = step?.attempts?.[step.attempts.length - 1];
  return execution?.active_attempt?.visual_monitor || lastAttempt?.visual_monitor || null;
}

function pipelineMarkup(visual, latest) {
  if (!visual) return "";
  const pipeline = visual.pipeline || {};
  const timings = pipeline.timings_ms || latest?.timings_ms || {};
  return `<div class="pipeline-panel" data-phase="${esc(pipeline.phase || "waiting")}">
    <div class="pipeline-head"><span id="pipelinePhase">等待采集端</span><strong id="pipelineElapsed">--</strong></div>
    <div class="pipeline-progress"><i id="pipelineProgress"></i></div>
    <div class="pipeline-stages">
      <span id="pipelineCapture">采集 --</span>
      <span id="pipelineEncode">编码 ${timings.encode != null ? `${esc(Math.round(timings.encode))} ms` : "--"}</span>
      <span id="pipelineUpload">上传 ${timings.upload_to_server != null ? `${esc(Math.round(timings.upload_to_server))} ms` : "--"}</span>
      <span id="pipelineApi">百炼 ${timings.bailian_total != null ? `${esc((timings.bailian_total / 1000).toFixed(1))} s` : "--"}</span>
      <span id="pipelineWindow">动态窗口 --</span>
      <span id="pipelineLag">模型落后画面 --</span>
    </div>
  </div>`;
}

function updatePipelineClock() {
  const pipeline = currentVisualMonitor()?.pipeline;
  const panel = document.querySelector(".pipeline-panel");
  if (!pipeline || !panel) return;
  const configuredDurationSeconds = Number(pipeline.window_duration_s || 7);
  const phaseLabels = {
    capturing: `正在采集 ${configuredDurationSeconds.toFixed(1)} 秒窗口`, encoding: "正在编码视频", uploading: "正在上传服务器",
    inferencing: "百炼正在判断", completed: "本轮判断完成", error: "本轮判断失败",
  };
  const started = Date.parse(pipeline.phase_started_at || "");
  const elapsedMs = Number.isFinite(started) ? Math.max(0, Date.now() - started) : 0;
  const shownMs = ["completed", "error"].includes(pipeline.phase)
    ? Number(pipeline.timings_ms?.server_job_total ?? elapsedMs) : elapsedMs;
  panel.dataset.phase = pipeline.phase || "waiting";
  if ($("#pipelinePhase")) $("#pipelinePhase").textContent = phaseLabels[pipeline.phase] || "等待采集端";
  if ($("#pipelineElapsed")) $("#pipelineElapsed").textContent = `${(shownMs / 1000).toFixed(1)} s`;
  const windowStart = Date.parse(pipeline.window_started_at || "");
  const windowEnd = Date.parse(pipeline.window_ended_at || "");
  const windowDuration = Number.isFinite(windowStart) && Number.isFinite(windowEnd)
    ? Math.max(1, windowEnd - windowStart) : configuredDurationSeconds * 1000;
  const capturedMs = pipeline.phase === "capturing"
    ? Math.min(windowDuration, Math.max(0, Date.now() - windowStart))
    : Number(pipeline.capture_ms ?? windowDuration);
  if ($("#pipelineCapture")) {
    $("#pipelineCapture").textContent = `采集 ${(Math.min(windowDuration, capturedMs) / 1000).toFixed(1)} / ${(windowDuration / 1000).toFixed(1)} s`;
  }
  if ($("#pipelineWindow")) {
    $("#pipelineWindow").textContent = `动态窗口 ${(windowDuration / 1000).toFixed(1)} s`;
  }
  if ($("#pipelineLag")) {
    const cameraHead = previewLatestCapturedAt || Date.now();
    const lagMs = Number.isFinite(windowEnd) ? Math.max(0, cameraHead - windowEnd) : 0;
    $("#pipelineLag").textContent = `模型落后画面 ${(lagMs / 1000).toFixed(1)} s`;
  }
  const progress = pipeline.phase === "capturing"
    ? Math.min(100, capturedMs / windowDuration * 100)
    : pipeline.phase === "encoding" ? 25
      : pipeline.phase === "uploading" ? 50
        : pipeline.phase === "inferencing" ? 75 : 100;
  if ($("#pipelineProgress")) $("#pipelineProgress").style.width = `${progress}%`;
}

setInterval(updatePipelineClock, 100);

function visualMonitorControls(step, attempt, chainMode, visual) {
  const chainLatest = visual?.latest;
  const latest = chainMode
    ? chainLatest?.step_updates?.find((item) => item.step_id === step.step_id) || chainLatest
    : chainLatest;
  const completed = execution.state === "completed";
  const statusLabel = {
    in_progress: "执行中", succeeded: "已完成", failed: "已失败", unknown: "执行中",
  }[latest?.status] || (visual ? "等待首次判断" : "等待 ROS2 相机客户端");
  const completionTimestamp = Number(latest?.completion_evidence_timestamp_s);
  const completionTimestampLabel = Number.isFinite(completionTimestamp)
    ? `完成证据 · ${completionTimestamp.toFixed(1)}s` : "完成证据";
  const evidenceImage = latest?.completion_evidence_url
    ? `<figure class="monitor-evidence-wrap"><img class="monitor-evidence" src="${esc(latest.completion_evidence_url)}" alt="完成证据帧"><figcaption>${esc(completionTimestampLabel)}</figcaption></figure>` : "";
  const evidenceItems = (latest?.evidence || []).map((item) =>
    `<li><span>${esc(Number(item.timestamp_s).toFixed(1))}s</span>${esc(item.observation)}</li>`
  ).join("");
  const outcomeEvidence = latest?.status === "succeeded" && evidenceItems
    ? `<div class="visual-success"><strong>成功证据</strong><ul>${evidenceItems}</ul></div>`
    : latest?.failure_reason
      ? `<div class="visual-failure"><strong>失败原因</strong><div>${esc(latest.failure_reason)}</div>${evidenceItems ? `<ul>${evidenceItems}</ul>` : ""}</div>`
      : "";
  const awaitingConfirmation = visual?.state === "awaiting_confirmation";
  const requestedModel = visual?.model_requested || latest?.model_requested || chainLatest?.model_requested;
  const actualModel = chainLatest?.model_actual || latest?.model_actual;
  const modelLabel = actualModel && requestedModel && actualModel !== requestedModel
    ? `${requestedModel} → ${actualModel}` : actualModel || requestedModel || "等待模型信息";
  const livePreview = visual?.camera_id
    ? `<div class="live-preview"><img id="livePreviewImage" alt="ROS2 相机实时画面"><div class="live-preview-meta"><span>${esc(visual.camera_id)}</span><span class="live-preview-model">${esc(modelLabel)}</span><span id="livePreviewStatus" class="live-preview-status wait">连接实时画面…</span></div></div>`
    : `<div class="live-preview waiting"><div>等待 ROS2 相机客户端连接</div></div>`;
  const controls = completed ? "" : `<div class="monitor-buttons">
      <button class="control-btn failure" data-action="report-failure" ${commandBusy ? "disabled" : ""}>人工确认失败</button>
      ${awaitingConfirmation ? `<button class="control-btn primary" data-action="resume-monitor" ${commandBusy ? "disabled" : ""}>判断不准确，继续监控</button>` : ""}
      <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>终止任务</button>
    </div>`;
  const note = completed
    ? "整条任务已经完成；实时预览继续保持连接。"
    : awaitingConfirmation
      ? "VLM 判定当前步骤失败，已暂停请求并等待人工确认。"
      : chainMode ? "中间步骤按视频内事件确认；最终步骤必须在 NOW 仍成立。" : "VLM 判定成功后将自动进入下一步骤。";
  return `<div class="monitor-kicker">${chainMode ? "Chain Visual Monitor" : "Visual Monitor"} · Attempt ${esc(attempt?.attempt_no || "-")}</div>
    ${livePreview}
    ${pipelineMarkup(visual, chainLatest)}
    <div class="visual-status ${esc(latest?.status || "waiting")}">${esc(statusLabel)}</div>
    <div class="monitor-action">${esc(step.zh)}</div>
    <div class="monitor-sub">${esc(latest?.description_zh || (chainMode ? "连续动态窗口会联合判断整条原子操作链，并可跨窗口推进多个连续步骤。" : "本地客户端以 7 秒为名义周期上传连续动态窗口，并触发一次百炼判断。"))}</div>
    ${outcomeEvidence}
    ${evidenceImage}
    ${controls}
    <div class="monitor-note ${completed ? "success" : ""}">${note}</div>`;
}

function monitorControls() {
  if (execution.state === "ready") {
    const mode = execution.execution_mode || "robot_agent";
    return `<div class="monitor-kicker">Plan review</div>
      <div class="monitor-action">计划等待确认</div>
      <div class="monitor-sub">先选择执行链路，再开始当前原子操作。</div>
      <div class="mode-switch">
        <button data-action="mode-robot_agent" class="${mode === "robot_agent" ? "active" : ""}" ${commandBusy ? "disabled" : ""}>机器人 Agent</button>
        <button data-action="mode-visual_monitor" class="${mode === "visual_monitor" ? "active" : ""}" ${commandBusy ? "disabled" : ""}>原子视觉</button>
        <button data-action="mode-chain_visual_monitor" class="${mode === "chain_visual_monitor" ? "active" : ""}" ${commandBusy ? "disabled" : ""}>整链视觉</button>
      </div>
      <div class="monitor-buttons">
        <button class="control-btn primary" data-action="start" ${commandBusy ? "disabled" : ""}>开始执行</button>
        <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>放弃计划</button>
      </div>`;
  }

  const step = currentStep();
  const chainMode = execution.execution_mode === "chain_visual_monitor";
  const visualMode = execution.execution_mode === "visual_monitor" || chainMode;
  const lastAttempt = step?.attempts?.[step.attempts.length - 1];
  const visualAttempt = execution.active_attempt || lastAttempt;
  if (visualMode && step && ["running", "completed"].includes(execution.state)) {
    return visualMonitorControls(step, visualAttempt, chainMode, currentVisualMonitor());
  }
  if (execution.state === "running" && step && execution.active_attempt) {
    const attempt = execution.active_attempt;
    const workflow = attempt.workflow || attempt.workflow_preview;
    if (workflow) {
      const waiting = workflow.state === "waiting_input";
      const registered = workflow.state === "registered";
      return `<div class="monitor-kicker">GraspArm Agent · Attempt ${attempt.attempt_no}</div>
        <div class="agent-state ${waiting || registered ? "waiting" : "running"}">${waiting || registered ? "⌁" : "●"}</div>
        <div class="monitor-action">${registered ? "等待 arm Agent 领取" : waiting ? "等待现场操作" : "机器人流程执行中"}</div>
        <div class="monitor-sub">${registered
          ? "Agent 已注册当前 YAML；开启执行权限后将按依赖关系领取并运行。"
          : waiting
            ? esc(workflow.message || "请按当前节点提示完成现场操作。")
            : `Run ${esc((workflow.run_id || "").slice(0, 8))} · Agent 正在按依赖关系推进内部步骤。`}</div>
        <div class="monitor-buttons">
          <button class="control-btn danger" data-action="terminate" ${commandBusy ? "disabled" : ""}>终止后续调度</button>
        </div>
        <div class="monitor-note">网页终止不是物理急停；异常运动必须使用现场急停。</div>`;
    }
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
      <div class="monitor-sub">当前操作失败或监控中断。可以显式重试当前步骤，或安全终止整条任务。</div>
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
  const observation = data.observation || {};
  const status = observation.status || data.status || "in_progress";
  const statusText = { succeeded: "成功", in_progress: "执行中", failed: "失败", unknown: "执行中" }[status] || status;
  const sequence = observation.sequence ?? data.sequence;
  const requestedModel = observation.model_requested;
  const actualModel = observation.model_actual;
  const model = actualModel && requestedModel && actualModel !== requestedModel
    ? `${requestedModel} → ${actualModel}` : actualModel || requestedModel;
  const evidenceList = (observation.evidence || []).map((item) =>
    `<li><span>${esc(Number(item.timestamp_s).toFixed(1))}s</span>${esc(item.observation)}</li>`
  ).join("");
  const stepUpdates = (observation.step_updates || []).map((item) => {
    const step = execution.steps.find((candidate) => candidate.step_id === item.step_id);
    const stepStatus = { succeeded: "成功", in_progress: "执行中", failed: "失败", unknown: "执行中" }[item.status] || item.status;
    const evidence = (item.evidence || []).map((entry) =>
      `<li><span>${esc(Number(entry.timestamp_s).toFixed(1))}s</span>${esc(entry.observation)}</li>`
    ).join("");
    return `<div class="event-step-result ${esc(item.status)}">
      <div><strong>${esc(step?.zh || item.step_id)}</strong><em>${esc(stepStatus)}</em></div>
      <p>${esc(item.description_zh || "无描述")}</p>
      ${item.failure_reason ? `<div class="event-reason">${esc(item.failure_reason)}</div>` : ""}
      ${evidence ? `<ul>${evidence}</ul>` : ""}
    </div>`;
  }).join("");
  return `<div class="event-result-head"><b>VLM 判断：${esc(statusText)}</b>${sequence != null ? `<span>#${esc(sequence)}</span>` : ""}${model ? `<span>${esc(model)}</span>` : ""}</div>
    <p class="event-description">${esc(observation.description_zh || data.detail || (event.type.endsWith(".error") ? "VLM 请求或结果解析失败" : "旧日志未保存详细描述"))}</p>
    ${observation.failure_reason ? `<div class="event-reason">${esc(observation.failure_reason)}</div>` : ""}
    ${observation.error ? `<div class="event-reason">技术错误：${esc(observation.error)}</div>` : ""}
    ${evidenceList ? `<ul class="event-evidence">${evidenceList}</ul>` : ""}
    ${stepUpdates}`;
}

function visualEvents() {
  const allowed = new Set([
    "visual_monitor.observation", "visual_monitor.error",
    "chain_visual_monitor.observation", "chain_visual_monitor.error",
  ]);
  return (execution.events || []).filter((event) => allowed.has(event.type));
}

function renderEvents() {
  const events = [...visualEvents()].reverse();
  if (!events.length) return `<div class="event-empty">等待 VLM 返回第一次判断…</div>`;
  return events.map((event) => `<div class="event-row"><span class="event-time">${formatTime(event.occurred_at)}</span><span class="event-text">${eventDescription(event)}</span></div>`).join("");
}

function patchMonitorBody() {
  const body = document.querySelector(".monitor-body");
  if (!body) return;
  const template = document.createElement("template");
  template.innerHTML = monitorControls();
  const oldPreview = body.querySelector(".live-preview:not(.waiting)");
  const newPreview = template.content.querySelector(".live-preview:not(.waiting)");
  if (oldPreview && newPreview && previewCameraId === desiredPreviewCamera()) {
    const nextModel = newPreview.querySelector(".live-preview-model")?.textContent;
    const oldModel = oldPreview.querySelector(".live-preview-model");
    if (oldModel && nextModel) oldModel.textContent = nextModel;
    newPreview.replaceWith(oldPreview);
  }
  body.replaceChildren(template.content);
}

function patchExecution() {
  const progress = execution.progress || { succeeded: 0, total: execution.steps.length, ratio: 0 };
  const percentage = Math.round(progress.ratio * 100);
  const statePill = document.querySelector(".state-pill");
  if (statePill) {
    statePill.className = `state-pill ${execution.state}`;
    statePill.textContent = STATE_LABELS[execution.state] || execution.state;
  }
  const progressBar = document.querySelector(".progress-bar");
  if (progressBar) progressBar.style.width = `${percentage}%`;
  const progressCopy = document.querySelector(".progress-copy");
  if (progressCopy) progressCopy.textContent = `${progress.succeeded} / ${progress.total} · ${percentage}%`;
  const steps = document.querySelector(".execution-steps");
  if (steps) steps.innerHTML = execution.steps.map(renderStep).join("");
  patchMonitorBody();
  const logCard = document.querySelector(".log-card");
  const logCount = logCard?.querySelector("summary .card-heading span, summary > span");
  if (logCount) logCount.textContent = `${visualEvents().length} 次判断 · 点击展开`;
  const eventList = document.querySelector(".event-list");
  if (eventList) eventList.innerHTML = renderEvents();
  updateCountdown();
  updatePipelineClock();
  syncLivePreview();
}

function renderExecution() {
  if (!execution) return;
  const result = $("#result");
  if (result.dataset.executionId === execution.execution_id && result.querySelector(".execution-head")) {
    patchExecution();
    return;
  }
  const progress = execution.progress || { succeeded: 0, total: execution.steps.length, ratio: 0 };
  const percentage = Math.round(progress.ratio * 100);
  result.dataset.executionId = execution.execution_id;
  result.innerHTML = `
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
      <summary class="card-heading"><h3>VLM 判断日志</h3><span>${visualEvents().length} 次判断 · 点击展开</span></summary>
      <div class="event-list">${renderEvents()}</div>
    </details>`;
  updateCountdown();
  updatePipelineClock();
  syncLivePreview();
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

async function postControl(suffix, body = null, tokenRetry = false) {
  if (!execution || commandBusy) return;
  commandBusy = true;
  renderExecution();
  updateInputLock();
  try {
    const options = { method: "POST", headers: {} };
    const operatorToken = localStorage.getItem("operatorToken");
    if (operatorToken) options.headers["X-Operator-Token"] = operatorToken;
    if (body) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(`/api/executions/${execution.execution_id}/${suffix}`, options);
    if (response.status === 401 && !tokenRetry) {
      commandBusy = false;
      const token = prompt("请输入 Operator Token（仅保存在当前浏览器）");
      if (token) {
        localStorage.setItem("operatorToken", token);
        return postControl(suffix, body, true);
      }
    }
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
  if (action.startsWith("mode-")) return postControl("mode", { mode: action.slice(5) });
  if (action === "retry") return postControl("retry");
  if (action === "terminate") return postControl("terminate");
  if (action === "resume-monitor") {
    const attempt = execution.active_attempt;
    if (!attempt) return;
    const monitorId = execution.execution_mode === "chain_visual_monitor"
      ? execution.chain_visual_monitor?.monitor_session_id : attempt.attempt_id;
    if (!monitorId) return;
    return postControl("visual-monitor/resume", { attempt_id: monitorId });
  }
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
