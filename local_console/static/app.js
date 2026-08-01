/* ═══════════════════════════════════════════════════════
   XAU//CONSOLE — Cyber Analysis Desk Renderer
   分层揭示：状态 → 结论 → 证据链 → 依据 → 失效 → 下一观察
   不变量：轮询服务端持久任务状态；历史渲染全部转义；
   遗留英文报告不冒充合规报告展示。
   ═══════════════════════════════════════════════════════ */

const STAGES = [
  ["QUEUED", "任务已创建"],
  ["SNAPSHOT", "读取 MT5"],
  ["GATE", "事实校验"],
  ["MODEL", "Qwen 分析"],
  ["VALIDATE", "报告校验"],
  ["COMPLETE", "分析完成"],
];
const TERMINAL = new Set(["COMPLETE", "REJECTED", "FAILED"]);
const ACTION_LABELS = { ANALYSE: "分析", WATCH: "观察", WAIT: "等待", BLOCKED: "阻断", REJECTED: "拒绝", FAILED: "失败", COMPLETE: "完成" };
const DIRECTION_LABELS = { LONG: "开多", SHORT: "开空", NEUTRAL: "观望" };
const DIRECTION_ARROWS = { LONG: "↑", SHORT: "↓", NEUTRAL: "→" };
/* 复盘结果语义：止盈红 / 止损绿（涨红跌绿），待复盘琥珀，未决品红，未触发灰 */
const REVIEW_LABELS = { TP_FIRST: "止盈", SL_FIRST: "止损", NOT_TRIGGERED: "未触发", EXPIRED_UNRESOLVED: "未决", PENDING: "待复盘" };
const REVIEW_CLASSES = { TP_FIRST: "rv-tp", SL_FIRST: "rv-sl", NOT_TRIGGERED: "rv-skip", EXPIRED_UNRESOLVED: "rv-expired", PENDING: "rv-pending" };
/* 情境复盘的闸门动作中文映射（方向复用顶部 DIRECTION_LABELS） */
const GATE_ACTION_LABELS = { ANALYSE: "已核验", WATCH: "技术参考", WAIT: "事件禁行", BLOCKED: "异常拦截" };

let pollingTimer = null;
let pollFailures = 0;

const byId = (id) => document.getElementById(id);

function classFor(state) { return `state-${String(state || "watch").toLowerCase()}`; }
function actionLabel(action) { return ACTION_LABELS[action] || "观察"; }

/* 历史/详情文本全部经过转义——模型与错误信息都是不可控输入 */
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function detailText(value) { return typeof value === "string" && value.trim() ? value : "（无详情）"; }
function errorText(error, fallback) {
  const message = error && error.message ? String(error.message) : "";
  return message.trim() ? message : fallback;
}
/* 遗留英文报告不符合当前中文输出标准，历史列表中不展示其正文 */
function reportIsChinese(text) { return typeof text === "string" && /[一-鿿]/.test(text); }
/* 历史与进度共用的展示状态：失败任务看阶段，其余看闸门动作 */
function jobDisplayState(job) {
  return job.stage === "FAILED" || job.stage === "REJECTED" ? job.stage : job.gate?.action || "WATCH";
}

function formatTime(iso) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(iso));
}

function formatNumber(value) { return typeof value === "number" ? value.toFixed(2) : "—"; }

async function jsonRequest(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

/* ─── JOB CONTROL ─── */
async function startJob(kind) {
  setControlsBusy(true);
  resetReportLayers();
  try {
    const job = await jsonRequest("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    });
    renderJob(job);
    pollJob(job.id);
  } catch (error) {
    renderFailure(error?.message || "无法启动分析，请确认本机服务正在运行。");
  }
}

function pollJob(jobId) {
  window.clearInterval(pollingTimer);
  // 轮询失败不判死：指数退避后自动重试。瞬时 500 / 网络抖动不应让页面
  // 永久停在失败态（2026-08-01 服务短暂故障时前端一次性放弃轮询的问题）。
  const delayMs = Math.min(1000 * 2 ** pollFailures, 30000);
  pollingTimer = window.setInterval(async () => {
    try {
      const job = await jsonRequest(`/api/jobs/${jobId}`);
      pollFailures = 0;
      renderJob(job);
      if (TERMINAL.has(job.stage)) {
        window.clearInterval(pollingTimer);
        setControlsBusy(false);
        refreshHistory();
        refreshReviewStats();
      }
    } catch (error) {
      pollFailures += 1;
      window.clearInterval(pollingTimer);
      const waitSeconds = Math.min(2 ** pollFailures, 30);
      byId("job-detail").textContent = `状态读取暂时失败，${waitSeconds} 秒后自动重试…`;
      pollJob(jobId);
    }
  }, delayMs);
}

function setControlsBusy(isBusy) {
  byId("start-brief").disabled = isBusy;
  byId("start-deep-review").disabled = isBusy;
}

/* ─── LAYERED REVEAL ─── */
function resetReportLayers() {
  ["layer-direction", "layer-evidence", "layer-summary", "layer-invalidation", "layer-next", "layer-risk"].forEach((id) => {
    const el = byId(id);
    el.hidden = true;
    el.classList.remove("reveal");
  });
}

function revealLayer(id, delay) {
  const el = byId(id);
  if (el.hidden) {
    el.hidden = false;
    el.classList.add("reveal");
    el.style.animationDelay = `${delay}ms`;
  }
}

function revealAllLayers(report, stage) {
  const hasReport = report && report.summary;
  const terminalFailure = ["FAILED", "REJECTED"].includes(stage);
  if (hasReport || terminalFailure) revealLayer("layer-direction", 100);
  if (report && Array.isArray(report.evidence_fields) && report.evidence_fields.length) revealLayer("layer-evidence", 250);
  if (hasReport) revealLayer("layer-summary", 400);
  if (hasReport) revealLayer("layer-invalidation", 550);
  if (hasReport) revealLayer("layer-next", 700);
  if (report && report.risk_note) revealLayer("layer-risk", 850);
}

/* ─── RENDER FUNCTIONS ─── */
function renderJob(job) {
  setControlsBusy(!TERMINAL.has(job.stage));
  byId("job-elapsed").textContent = `${Number(job.elapsed_seconds || 0).toFixed(1)}s`;
  byId("job-detail").textContent = job.detail || "等待任务状态";
  renderProgress(job);
  renderSnapshot(job.snapshot);
  renderGate(job.gate, job.stage);
  renderReport(job);
}

function renderProgress(job) {
  const terminalFailure = ["FAILED", "REJECTED"].includes(job.stage);
  const currentIndex = terminalFailure ? STAGES.length - 1 : STAGES.findIndex(([s]) => s === job.stage);
  const stages = terminalFailure
    ? [...STAGES.slice(0, -1), [job.stage, job.stage === "REJECTED" ? "报告被拒绝" : "任务失败"]]
    : STAGES;

  byId("progress-stages").innerHTML = stages.map(([stage, label], index) => {
    let cls = "";
    if (stage === job.stage && terminalFailure) cls = "failed";
    else if (stage === job.stage) cls = "active";
    else if (index < currentIndex || job.stage === "COMPLETE") cls = "done";
    return `<div class="progress-node ${cls}"><i></i><span>${label}</span></div>`;
  }).join("");
}

function renderSnapshot(snapshot) {
  if (!snapshot) return;
  byId("bid").textContent = formatNumber(snapshot.bid);
  byId("ask").textContent = formatNumber(snapshot.ask);
  byId("spread").textContent = formatNumber(snapshot.spread);
  byId("snapshot-time").textContent = formatTime(snapshot.timestamp);
  byId("snapshot-age").textContent = "已刷新";
  byId("snapshot-age").className = "chip chip-analyse";
  byId("source-status").textContent = snapshot.identity_match ? "MT5 已验证" : "身份不匹配";

  const tf = snapshot.timeframe_structure || {};
  byId("atr-m15").textContent = tf.m15?.atr_14 ? tf.m15.atr_14.toFixed(1) : (snapshot.atr_m15 ? snapshot.atr_m15.toFixed(1) : "—");
  byId("atr-h1").textContent = tf.h1?.atr_14 ? tf.h1.atr_14.toFixed(1) : "—";
  byId("atr-h4").textContent = tf.h4?.atr_14 ? tf.h4.atr_14.toFixed(1) : "—";

  const structure = snapshot.timeframe_structure || {};
  byId("structure-grid").innerHTML = ["m1", "m5", "m15", "h1", "h4"].map((timeframe) => {
    const item = structure[timeframe] || snapshot.latest_closed_bars?.[timeframe];
    const direction = item?.body_direction || (item?.close > item?.open ? "buy" : item?.close < item?.open ? "sell" : undefined);
    const label = direction === "buy" ? "偏强" : direction === "sell" ? "偏弱" : "等待";
    return `<div class="structure-row ${direction === "buy" ? "up" : direction === "sell" ? "down" : ""}"><span>${timeframe.toUpperCase()}</span><strong>${label}</strong><i></i></div>`;
  }).join("");
}

function renderGate(gate, stage) {
  const terminalFailure = ["FAILED", "REJECTED"].includes(stage);
  const action = terminalFailure ? stage : gate?.action || "WATCH";
  const reason = gate?.reason || (terminalFailure ? "任务未完成" : "事件上下文未核验。系统只允许观察型简报。");
  const badge = byId("gate-badge");
  badge.textContent = actionLabel(action);
  badge.className = `chip chip-${action.toLowerCase()}`;
  byId("gate-reason").textContent = reason;

  const eventContext = gate?.event_context;
  const nextEvent = eventContext?.next_event;
  byId("event-status").textContent =
    action === "ANALYSE" && nextEvent ? `已核验 · 下期 ${nextEvent.title}`
    : action === "ANALYSE" ? "已核验"
    : action === "WATCH" ? "未核验"
    : actionLabel(action);

  renderSensors(gate);
}

/* tick 传感器与宏观背景只是闸门的辅助读数，离线不视为故障 */
function renderSensors(gate) {
  const tick = gate?.tick_health;
  const tickEl = byId("tick-status");
  if (tick && tick.available === true) {
    const parts = [];
    if (typeof tick.spread_median === "number") parts.push(`中位 ${tick.spread_median.toFixed(2)}`);
    if (typeof tick.spread_max === "number") parts.push(`峰值 ${tick.spread_max.toFixed(2)}`);
    tickEl.textContent = tick.stalled ? `停滞 · ${parts.join(" / ")}` : parts.join(" / ") || "在线";
    tickEl.className = tick.stalled ? "mono degraded" : "mono online";
  } else {
    tickEl.textContent = "离线";
    tickEl.className = "mono";
  }

  const macroEl = byId("macro-status");
  const macroCard = byId("macro-card");
  const summary = gate?.macro_summary;
  if (gate?.macro_status === "ok" && summary) {
    macroEl.textContent = "FRED 已加载";
    macroEl.className = "mono online";
    macroCard.hidden = false;
    byId("macro-strip").innerHTML = Object.entries(summary).map(([sid, item]) => {
      const change = typeof item.change_recent === "number" ? item.change_recent : null;
      const cls = change === null ? "flat" : change > 0 ? "up" : change < 0 ? "down" : "flat";
      const sign = change !== null && change > 0 ? "+" : "";
      return `<div class="macro-row"><span class="macro-name">${escapeHtml(item.label || sid)}</span><span class="macro-value">${escapeHtml(String(item.latest ?? "—"))}</span><span class="macro-change ${cls}">${change === null ? "—" : `${sign}${change.toFixed(2)}`}</span></div>`;
    }).join("");
  } else {
    macroEl.textContent = "离线";
    macroEl.className = "mono";
    macroCard.hidden = true;
  }

  /* 新闻背景是纯上下文层：只呈现头条供交易员判断预期差，绝不暗示为触发信号 */
  const newsEl = byId("news-status");
  const newsCard = byId("news-card");
  const newsSummary = gate?.news_summary;
  if (gate?.news_status === "ok" && newsSummary) {
    const items = Array.isArray(newsSummary.items) ? newsSummary.items : [];
    newsEl.textContent = newsSummary.count ? `已加载 · ${newsSummary.count} 条` : "无相关";
    newsEl.className = newsSummary.count ? "mono online" : "mono";
    newsCard.hidden = items.length === 0;
    byId("news-list").innerHTML = items.map((item) => {
      const time = item.utc ? formatTime(item.utc) : "";
      const topic = item.topic || "市场动态";
      const meta = [item.publisher, time].filter(Boolean).join(" · ");
      return `<div class="news-row"><div class="news-head"><span class="news-topic">${escapeHtml(topic)}</span><span class="news-meta">${escapeHtml(meta)}</span></div><span class="news-title">${escapeHtml(String(item.title || "—"))}</span></div>`;
    }).join("");
  } else {
    newsEl.textContent = "离线";
    newsEl.className = "mono";
    newsCard.hidden = true;
  }
}

function renderReport(job) {
  // 遗留英文报告不符合当前中文输出标准：主报告卡与历史区一致，不冒充合规报告展示
  const legacyReport = job.report && job.report.summary && !reportIsChinese(job.report.summary);
  const report = legacyReport ? null : job.report;
  const terminalFailure = ["FAILED", "REJECTED"].includes(job.stage);
  const action = terminalFailure ? job.stage : report?.action || job.gate?.action || "WATCH";

  const badge = byId("decision-state");
  badge.textContent = actionLabel(action);
  badge.className = `action-badge action-${action.toLowerCase()}`;
  byId("report-kind").textContent = job.kind === "deep_review" ? "深度复盘" : "实时简报";
  byId("report-time").textContent = formatTime(job.created_at);

  // 方向层
  if (report && report.direction) {
    const dir = report.direction;
    const arrow = byId("direction-arrow");
    arrow.textContent = DIRECTION_ARROWS[dir] || "→";
    arrow.className = `direction-arrow ${dir.toLowerCase()}`;
    const isWatchMode = action === "WATCH";
    const dirLabel = DIRECTION_LABELS[dir] || "观望";
    byId("direction-label").textContent = isWatchMode && dir !== "NEUTRAL" ? `${dirLabel}（技术面参考）` : dirLabel;
    byId("direction-note").textContent = report.summary ? report.summary.slice(0, 80) + (report.summary.length > 80 ? "…" : "") : "";

    if (report.entry_zone || report.take_profit || report.stop_loss) {
      byId("trade-levels").hidden = false;
      byId("entry-zone").textContent = report.entry_zone || "—";
      byId("take-profit").textContent = report.take_profit || "—";
      byId("stop-loss").textContent = report.stop_loss || "—";
    }
  } else if (terminalFailure || legacyReport) {
    const arrow = byId("direction-arrow");
    arrow.textContent = "✕";
    arrow.className = "direction-arrow neutral";
    byId("direction-label").textContent = legacyReport ? "历史报告（非中文，已隐藏）" : (job.detail || "任务未完成");
    byId("direction-note").textContent = "";
    byId("trade-levels").hidden = true;
  } else {
    const arrow = byId("direction-arrow");
    arrow.textContent = "…";
    arrow.className = "direction-arrow neutral";
    byId("direction-label").textContent = "等待分析结果";
    byId("direction-note").textContent = "";
    byId("trade-levels").hidden = true;
  }

  // 证据链
  if (report && Array.isArray(report.evidence_fields)) {
    byId("evidence-row").innerHTML = report.evidence_fields
      .map((field) => `<span class="evidence-chip">${escapeHtml(field)}</span>`)
      .join("");
  }

  if (report && report.summary) {
    byId("report-summary").textContent = report.summary;
  } else if (legacyReport) {
    byId("report-summary").textContent = "历史报告不符合当前中文输出标准，已隐藏正文。";
  } else if (!terminalFailure) {
    byId("report-summary").textContent = "点击「刷新 MT5 并生成简报」开始。系统会先取新的 MT5 快照，经事实闸门校验后再允许模型分析。";
  }
  if (report && report.invalidation) byId("report-invalidation").textContent = report.invalidation;
  if (report && report.next_observation) byId("report-next").textContent = report.next_observation;
  if (report && report.risk_note) byId("risk-note").textContent = `⚠ ${report.risk_note}`;

  if (TERMINAL.has(job.stage)) {
    revealAllLayers(report, job.stage);
  } else if (job.stage === "MODEL" || job.stage === "VALIDATE") {
    revealLayer("layer-direction", 0);
  }
}

function renderFailure(message) {
  window.clearInterval(pollingTimer);
  setControlsBusy(false);
  byId("job-detail").textContent = message;
  byId("report-summary").textContent = message;
  byId("decision-state").textContent = "失败";
  byId("decision-state").className = "action-badge action-failed";
  revealLayer("layer-direction", 0);
  revealLayer("layer-summary", 200);
  const arrow = byId("direction-arrow");
  arrow.textContent = "✕";
  arrow.className = "direction-arrow neutral";
  byId("direction-label").textContent = "分析失败";
}

/* ─── STATUS & HISTORY ─── */
function renderSelfCheck(selfCheck) {
  const lamps = { "lamp-mt5": selfCheck?.mt5, "lamp-fred": selfCheck?.fred, "lamp-cal": selfCheck?.calendar };
  Object.entries(lamps).forEach(([id, state]) => {
    const el = byId(id);
    if (!el) return;
    const good = state === "ok" || state === "configured" || state === "fresh";
    const bad = state === "offline" || state === "missing_key";
    el.className = `lamp ${good ? "ok" : bad ? "bad" : state ? "warn" : ""}`;
    el.title = `${el.textContent} 自检：${state || "未知"}`;
  });
}

async function refreshStatus() {
  try {
    const status = await jsonRequest("/api/status");
    byId("model-status").textContent = `${status.quick_model}`;
    renderSelfCheck(status.self_check);
    renderAuto(status.auto);
    if (status.latest_job) {
      renderJob(status.latest_job);
      if (!TERMINAL.has(status.latest_job.stage)) pollJob(status.latest_job.id);
    }
  } catch (error) {
    byId("system-time").textContent = "服务异常";
    renderFailure(error?.message || "无法连接本机分析服务。");
  }
}

/* ─── 情境复盘：按单维度切分（闸门/共振/方向），小样本明确标注 ─── */
function contextRowHtml(label, group) {
  const thin = (group.decided || 0) < 5;
  const win = group.win_rate === null || group.win_rate === undefined ? "—" : `${(group.win_rate * 100).toFixed(0)}%`;
  const avgr = group.avg_r === null || group.avg_r === undefined ? "—" : `${group.avg_r > 0 ? "+" : ""}${group.avg_r.toFixed(2)}R`;
  const thinTag = thin ? '<em class="ctx-thin">样本少</em>' : "";
  return `<tr class="${thin ? "is-thin" : ""}">
    <td class="ctx-label">${escapeHtml(label)}${thinTag}</td>
    <td class="mono">${group.decided}/${group.reviewed}</td>
    <td class="mono">${win}</td>
    <td class="mono">${avgr}</td>
  </tr>`;
}

function contextBlockHtml(title, groups, labelMap) {
  const entries = Object.entries(groups || {});
  if (!entries.length) return "";
  const rows = entries.map(([key, group]) => contextRowHtml((labelMap && labelMap[key]) || key, group)).join("");
  return `<div class="ctx-block">
    <span class="ctx-title">${title}</span>
    <table class="ctx-table">
      <thead><tr><th>情境</th><th>已决/样本</th><th>胜率</th><th>平均R</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function renderContextStats(contexts) {
  const el = byId("review-contexts");
  if (!el) return;
  if (!contexts) { el.innerHTML = ""; return; }
  const html = [
    contextBlockHtml("按闸门", contexts.by_gate_action, GATE_ACTION_LABELS),
    contextBlockHtml("按共振", contexts.by_resonance),
    contextBlockHtml("按方向", contexts.by_direction, DIRECTION_LABELS),
  ].join("");
  el.innerHTML = html || '<p class="empty-state">情境样本积累中。</p>';
}

/* ─── 建议质量复盘（测量层） ─── */
async function refreshReviewStats() {
  try {
    const stats = await jsonRequest("/api/review-stats");
    byId("review-winrate").textContent = stats.win_rate === null ? "—" : `${(stats.win_rate * 100).toFixed(0)}%`;
    byId("review-avgr").textContent = stats.avg_r === null ? "—" : `${stats.avg_r > 0 ? "+" : ""}${stats.avg_r.toFixed(2)}R`;
    byId("review-samples").textContent = `${stats.decided}/${stats.reviewed}`;
    const counts = stats.counts || {};
    byId("review-counts").innerHTML = Object.entries(REVIEW_LABELS)
      .filter(([key]) => counts[key] > 0)
      .map(([key, label]) => `<span class="review-chip ${REVIEW_CLASSES[key]}">${label} ${counts[key]}</span>`)
      .join("") || '<span class="review-chip rv-skip">暂无复盘样本</span>';
    byId("review-disclaimer").textContent = stats.disclaimer || "复盘为测量层统计，不构成可实盘 edge 的证据。";
    renderContextStats(stats.contexts);
  } catch (error) {
    byId("review-disclaimer").textContent = "复盘统计读取失败，请确认本机服务正在运行。";
  }
}

/* ─── 自主调度开关：按节奏采样，结论变化时才醒目 ─── */
function renderAuto(auto) {
  const toggle = byId("auto-toggle");
  const state = byId("auto-state");
  if (!toggle || !state || !auto) return;
  const on = !!auto.enabled;
  toggle.classList.toggle("on", on);
  state.textContent = on ? "开" : "关";
  const minutes = Math.round((auto.interval_seconds || 0) / 60);
  const last = auto.last_trigger_at ? `上次触发 ${formatTime(auto.last_trigger_at)}` : "尚未触发";
  toggle.title = `自主调度：每 ${minutes} 分钟自动采样，结论变化时才醒目。${last}。`;
}

async function toggleAuto() {
  const toggle = byId("auto-toggle");
  const next = !toggle.classList.contains("on");
  try {
    const status = await jsonRequest("/api/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: next }),
    });
    renderAuto(status);
  } catch (error) {
    byId("auto-state").textContent = "异常";
  }
}

/* 结论签名：闸门动作 + 方向 + 共振标签；连续同签名的历史任务降透明度 */
function conclusionSignature(job) {
  if (!job || job.stage !== "COMPLETE" || !job.report || !job.gate) return null;
  const resonance = job.gate.resonance || {};
  return [job.gate.action, job.report.direction, resonance.label || "—"].join("|");
}

async function refreshHistory() {
  const container = byId("history");
  try {
    const history = await jsonRequest("/api/history");
    if (!history.jobs.length) {
      container.innerHTML = '<p class="empty-state">没有历史任务。</p>';
      return;
    }
    let prevSignature = null;
    container.innerHTML = history.jobs.map((job) => {
      const state = jobDisplayState(job);
      const legacy = job.report && job.report.summary && !reportIsChinese(job.report.summary);
      const kind = job.kind === "deep_review" ? "深度复盘" : "实时简报";
      const safeDetail = `${escapeHtml(detailText(job.detail))}`;
      const detailHtml = legacy ? "历史报告不符合当前中文输出标准" : safeDetail;
      const outcome = job.review && job.review.outcome;
      const reviewHtml = outcome && REVIEW_LABELS[outcome]
        ? `<span class="review-chip ${REVIEW_CLASSES[outcome]}" title="建议复盘结果">${REVIEW_LABELS[outcome]}</span>`
        : "";
      const signature = conclusionSignature(job);
      const unchanged = signature !== null && signature === prevSignature;
      if (signature !== null) prevSignature = signature;
      const unchangedHtml = unchanged ? '<span class="unchanged-tag" title="结论与上一条相同">同前</span>' : "";
      return `<article class="history-item${unchanged ? " is-unchanged" : ""}">
        <span class="history-state ${classFor(state)}">${actionLabel(state)}</span>
        <div><strong>${kind}</strong> ${reviewHtml}${unchangedHtml}<p class="history-detail">${detailHtml}</p></div>
        <time class="history-time mono">${formatTime(job.created_at)}</time>
      </article>`;
    }).join("");
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(errorText(error, "无法读取历史任务，请确认本机服务正在运行。"))}</p>`;
  }
}

/* 本机时钟，赛博氛围；服务状态异常时让位于错误提示 */
function tickClock() {
  const el = byId("system-time");
  if (el.textContent !== "服务异常") {
    el.textContent = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
  }
}

/* ─── BOOT ─── */
async function boot() {
  window.setInterval(tickClock, 1000);
  await refreshStatus();
  await refreshHistory();
  await refreshReviewStats();
}

byId("start-brief").addEventListener("click", () => startJob("brief"));
byId("start-deep-review").addEventListener("click", () => startJob("deep_review"));
byId("auto-toggle").addEventListener("click", toggleAuto);
boot();
