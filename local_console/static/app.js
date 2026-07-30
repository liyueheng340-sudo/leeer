const STAGES = [
  ["QUEUED", "任务已创建"],
  ["SNAPSHOT", "读取 MT5"],
  ["GATE", "事实校验"],
  ["MODEL", "Qwen 分析"],
  ["VALIDATE", "报告校验"],
  ["COMPLETE", "分析完成"],
];
const TERMINAL = new Set(["COMPLETE", "REJECTED", "FAILED"]);
const ACTION_LABELS = { ANALYSE: "分析", WATCH: "观察", WAIT: "等待", BLOCKED: "阻断", REJECTED: "拒绝", FAILED: "失败" };
const DETAIL_LABELS = { Queued: "任务已创建", "report accepted": "报告已验收" };
const REASON_LABELS = {
  "Event context is unverified": "事件上下文未核验",
  "MT5 broker or symbol identity mismatch": "MT5 经纪商或品种身份不匹配",
  "MT5 quote is unavailable": "MT5 报价不可用",
  "MT5 snapshot is older than 60 seconds": "MT5 快照已超过 60 秒",
  "Fresh MT5 snapshot and verified event context": "MT5 快照新鲜且事件状态已核验",
};
let pollingTimer = null;

const byId = (id) => document.getElementById(id);

function classFor(state) {
  return `state-${String(state || "watch").toLowerCase()}`;
}

function actionLabel(action) {
  return ACTION_LABELS[action] || "观察";
}

function detailText(detail) {
  return DETAIL_LABELS[detail] || detail || "等待任务状态";
}

function reasonText(reason) {
  return REASON_LABELS[reason] || reason || "等待新的 MT5 快照与事件状态。";
}

function errorText(error, fallback) {
  const message = error?.message || "";
  return /[\u4e00-\u9fff]/.test(message) ? message : fallback;
}

function jobDisplayState(job) {
  if (job.stage === "FAILED" || job.stage === "REJECTED") return job.stage;
  return job.gate?.action || "WATCH";
}

function reportIsChinese(report) {
  return report && ["summary", "invalidation", "next_observation"].every(
    (key) => /[\u4e00-\u9fff]/.test(report[key] || "")
  );
}

function formatTime(iso) {
  if (!iso) return "尚未读取";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(iso));
}

function formatNumber(value) {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

async function jsonRequest(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

async function startJob(kind) {
  setControlsBusy(true);
  try {
    const job = await jsonRequest("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    });
    renderJob(job);
    pollJob(job.id);
  } catch (error) {
    renderFailure(errorText(error, "无法启动分析，请确认本机服务正在运行。"));
  }
}

function pollJob(jobId) {
  window.clearInterval(pollingTimer);
  pollingTimer = window.setInterval(async () => {
    try {
      const job = await jsonRequest(`/api/jobs/${jobId}`);
      renderJob(job);
      if (TERMINAL.has(job.stage)) {
        window.clearInterval(pollingTimer);
        setControlsBusy(false);
        refreshHistory();
      }
    } catch (error) {
      renderFailure(errorText(error, "无法读取任务状态，请确认本机服务正在运行。"));
    }
  }, 1000);
}

function setControlsBusy(isBusy) {
  byId("start-brief").disabled = isBusy;
  byId("start-deep-review").disabled = isBusy;
  byId("start-brief").textContent = isBusy ? "分析任务进行中" : "↗ 刷新 MT5 并生成简报";
}

function renderJob(job) {
  setControlsBusy(!TERMINAL.has(job.stage));
  byId("job-elapsed").textContent = `${Number(job.elapsed_seconds || 0).toFixed(1)} 秒`;
  byId("job-detail").textContent = detailText(job.detail);
  renderProgress(job);
  renderSnapshot(job.snapshot);
  renderGate(job.gate, job.stage);
  renderDecision(job);
}

function renderProgress(job) {
  const terminalFailure = job.stage === "REJECTED" || job.stage === "FAILED";
  const currentIndex = terminalFailure ? STAGES.length - 1 : STAGES.findIndex(([stage]) => stage === job.stage);
  const events = new Map((job.events || []).map((event) => [event.stage, event]));
  const stages = terminalFailure
    ? [...STAGES.slice(0, -1), [job.stage, job.stage === "REJECTED" ? "报告被拒绝" : "任务失败"]]
    : STAGES;
  byId("progress-stages").innerHTML = stages.map(([stage, label], index) => {
    const event = events.get(stage);
    const state = stage === job.stage ? "active" : index < currentIndex || job.stage === "COMPLETE" ? "done" : "";
    const failed = stage === job.stage && (job.stage === "REJECTED" || job.stage === "FAILED") ? "failed" : "";
    return `<div class="progress-stage ${state} ${failed}"><i></i><span>${label}</span><small>${event ? formatTime(event.at) : "等待"}</small></div>`;
  }).join("");
}

function renderSnapshot(snapshot) {
  if (!snapshot) return;
  byId("bid").textContent = formatNumber(snapshot.bid);
  byId("ask").textContent = formatNumber(snapshot.ask);
  byId("spread").textContent = formatNumber(snapshot.spread);
  byId("snapshot-time").textContent = formatTime(snapshot.timestamp);
  byId("snapshot-age").textContent = "已刷新";
  byId("source-status").textContent = snapshot.identity_match ? "Bitget MT5 快照已验证" : "身份不匹配";
  const structure = snapshot.timeframe_structure || {};
  byId("structure-grid").innerHTML = ["m1", "m5", "m15"].map((timeframe) => {
    const item = structure[timeframe] || snapshot.latest_closed_bars?.[timeframe];
    const direction = item?.body_direction || (
      item?.close > item?.open ? "buy" : item?.close < item?.open ? "sell" : undefined
    );
    const label = direction === "buy" ? "收盘偏强" : direction === "sell" ? "收盘偏弱" : "等待结构";
    return `<div class="structure-row ${direction === "buy" ? "up" : direction === "sell" ? "down" : ""}"><span>${timeframe.toUpperCase()}</span><strong>${label}</strong><i></i></div>`;
  }).join("");
}

function renderGate(gate, stage) {
  const action = stage === "FAILED" ? "BLOCKED" : gate?.action || "WATCH";
  const reason = reasonText(gate?.reason || (stage === "FAILED" ? "任务未完成，请查看任务进度。" : ""));
  const badge = byId("gate-badge");
  badge.textContent = actionLabel(action);
  badge.className = `badge ${classFor(action)}`;
  byId("event-status").textContent = action === "WATCH" ? "未核验，观察模式" : actionLabel(action);
  byId("action-status").textContent = actionLabel(action);
  byId("gate-reason").textContent = reason;
}

function renderDecision(job) {
  const report = reportIsChinese(job.report) ? job.report : null;
  const action = job.stage === "FAILED" ? "BLOCKED" : report?.action || job.gate?.action || "WATCH";
  const state = byId("decision-state");
  state.textContent = actionLabel(action);
  state.className = `decision-state ${classFor(action)}`;
  const historicalEnglishReport = job.report && !report;
  byId("decision-summary").textContent = report?.summary || (historicalEnglishReport ? "历史报告不符合当前中文输出标准，请重新生成分析。" : reasonText(job.gate?.reason) || detailText(job.detail) || "等待分析。");
  byId("invalidation").textContent = report?.invalidation || "数据过期、身份不匹配或事件状态变化时失效。";
  byId("next-observation").textContent = report?.next_observation || "刷新 MT5 快照并确认事件状态。";
}

function renderFailure(message) {
  window.clearInterval(pollingTimer);
  setControlsBusy(false);
  byId("job-detail").textContent = message;
  byId("decision-summary").textContent = message;
  byId("decision-state").textContent = "失败";
  byId("decision-state").className = "decision-state state-failed";
}

async function refreshStatus() {
  try {
    const status = await jsonRequest("/api/status");
    byId("model-status").textContent = `${status.quick_model} · ${status.deep_model}`;
    byId("system-time").textContent = "本机服务就绪";
    if (status.latest_job) {
      renderJob(status.latest_job);
      if (!TERMINAL.has(status.latest_job.stage)) pollJob(status.latest_job.id);
    }
  } catch (error) {
    byId("system-time").textContent = "本机服务异常";
    renderFailure(errorText(error, "无法连接本机分析服务。"));
  }
}

async function refreshHistory() {
  const container = byId("history");
  try {
    const history = await jsonRequest("/api/history");
    if (!history.jobs.length) {
      container.innerHTML = '<p class="empty-state">没有历史任务。</p>';
      return;
    }
    container.innerHTML = history.jobs.map((job) => `
      <article class="history-item">
        <span class="history-state ${classFor(jobDisplayState(job))}">${actionLabel(jobDisplayState(job))}</span>
        <div><strong>${job.kind === "deep_review" ? "深度复盘" : "实时简报"}</strong><p class="history-detail">${detailText(job.detail)}</p></div>
        <time class="history-time mono">${formatTime(job.created_at)}</time>
      </article>`).join("");
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${errorText(error, "无法读取历史任务，请确认本机服务正在运行。")}</p>`;
  }
}

async function boot() {
  await refreshStatus();
  await refreshHistory();
}

byId("start-brief").addEventListener("click", () => startJob("brief"));
byId("start-deep-review").addEventListener("click", () => startJob("deep_review"));
boot();
