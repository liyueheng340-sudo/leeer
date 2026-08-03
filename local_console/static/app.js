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
const GATE_ACTION_LABELS = { ANALYSE: "分析", WATCH: "技术参考", WAIT: "事件禁行", BLOCKED: "异常拦截" };

let pollingTimer = null;
let pollFailures = 0;

const byId = (id) => document.getElementById(id);

function classFor(state) { return `state-${String(state || "watch").toLowerCase()}`; }
function actionLabel(action) { return ACTION_LABELS[action] || "分析"; }

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
function isTerminalFailure(stage) { return stage === "FAILED" || stage === "REJECTED"; }
function jobDisplayState(job) {
  return isTerminalFailure(job.stage) ? job.stage : job.gate?.action || "ANALYSE";
}

const TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
function formatTime(iso) {
  if (!iso) return "";
  return TIME_FORMATTER.format(new Date(iso));
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
  ["layer-direction", "layer-summary", "layer-suggestions", "layer-invalidation", "layer-next", "layer-risk"].forEach((id) => {
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
    /* 用 transition-delay 制造分层错峰：transition 期间元素保持可见（opacity 1），
       不会像 animation-delay+fill-mode 那样在延迟期隐藏内容 */
    el.style.transitionDelay = `${delay}ms`;
  }
}

function revealAllLayers(report, stage) {
  const hasReport = report && report.summary;
  const terminalFailure = isTerminalFailure(stage);
  if (hasReport || terminalFailure) revealLayer("layer-direction", 100);
  if (hasReport) revealLayer("layer-summary", 250);
  if (report && Array.isArray(report.suggestions) && report.suggestions.length) revealLayer("layer-suggestions", 350);
  if (hasReport) revealLayer("layer-invalidation", 450);
  if (hasReport) revealLayer("layer-next", 550);
  if (report && report.risk_note) revealLayer("layer-risk", 650);
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
  const terminalFailure = isTerminalFailure(job.stage);
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

  const structure = snapshot.timeframe_structure || {};
  byId("atr-m15").textContent = structure.m15?.atr_14 ? structure.m15.atr_14.toFixed(1) : (snapshot.atr_m15 ? snapshot.atr_m15.toFixed(1) : "—");
  byId("atr-h1").textContent = structure.h1?.atr_14 ? structure.h1.atr_14.toFixed(1) : "—";
  byId("atr-h4").textContent = structure.h4?.atr_14 ? structure.h4.atr_14.toFixed(1) : "—";

  byId("structure-grid").innerHTML = ["m1", "m5", "m15", "h1", "h4"].map((timeframe) => {
    const item = structure[timeframe] || snapshot.latest_closed_bars?.[timeframe];
    const direction = item?.body_direction || (item?.close > item?.open ? "buy" : item?.close < item?.open ? "sell" : undefined);
    const label = direction === "buy" ? "偏强" : direction === "sell" ? "偏弱" : "等待";
    return `<div class="structure-row ${direction === "buy" ? "up" : direction === "sell" ? "down" : ""}"><span>${timeframe.toUpperCase()}</span><strong>${label}</strong><i></i></div>`;
  }).join("");
}

function renderGate(gate, stage) {
  const terminalFailure = isTerminalFailure(stage);
  const action = terminalFailure ? stage : gate?.action || "ANALYSE";
  const reason = gate?.reason || (terminalFailure ? "任务未完成" : "等待首份快照。");
  const badge = byId("gate-badge");
  badge.textContent = actionLabel(action);
  badge.className = `chip chip-${action.toLowerCase()}`;
  byId("gate-reason").textContent = reason;

  /* 军师模式：风险标注不阻断分析，随闸门呈现给交易者 */
  const warnings = Array.isArray(gate?.warnings) ? gate.warnings : [];
  const warningEl = byId("gate-warnings");
  warningEl.innerHTML = warnings.map((w) => `<li class="gate-warning">${escapeHtml(String(w))}</li>`).join("");
  warningEl.hidden = warnings.length === 0;

  const eventContext = gate?.event_context;
  const nextEvent = eventContext?.next_event;
  const evStatus = eventContext?.status;
  /* 军师模式：action 恒为 ANALYSE，事件核验状态由 event_context.status 如实呈现 */
  byId("event-status").textContent =
    action === "ANALYSE" && evStatus === "wait" ? "事件窗口"
    : action === "ANALYSE" && evStatus === "unverified" ? "未核验"
    : action === "ANALYSE" && nextEvent ? `已核验 · 下期 ${nextEvent.title}`
    : action === "ANALYSE" ? "已核验"
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

  /* IV 波动层：GLD 期权链推导的波动预期（ATM IV / 环境 / 偏斜 / Rank / 期限结构） */
  renderIv(gate?.iv);
}

const IV_ENV_LABELS = { high: "高波动预期", low: "低波动预期", neutral: "中性" };
function renderIv(iv) {
  const ivEl = byId("iv-status");
  const ivCard = byId("iv-card");
  if (!iv || !iv.atm_iv) {
    ivEl.textContent = "离线";
    ivEl.className = "mono";
    ivCard.hidden = true;
    return;
  }
  const atm = (iv.atm_iv * 100).toFixed(1) + "%";
  const env = IV_ENV_LABELS[iv.iv_vs_hv] || "中性";
  ivEl.textContent = `ATM ${atm} · ${env}`;
  ivEl.className = `mono ${iv.iv_vs_hv === "high" ? "online" : iv.iv_vs_hv === "low" ? "degraded" : ""}`;
  ivCard.hidden = false;

  byId("iv-atm").textContent = atm;
  byId("iv-vs-hv").textContent = env;
  byId("iv-skew").textContent = typeof iv.skew === "number"
    ? `${iv.skew > 0 ? "+" : ""}${(iv.skew * 100).toFixed(1)}%${iv.skew > 0.005 ? "（下行偏斜）" : iv.skew < -0.005 ? "（上行偏斜）" : "（中性）"}`
    : "—";
  byId("iv-rank").textContent = typeof iv.iv_rank === "number" ? `${Math.round(iv.iv_rank * 100)}%` : "积累中";
  byId("iv-term").textContent = typeof iv.term_slope === "number"
    ? `${iv.term_slope > 0 ? "+" : ""}${(iv.term_slope * 100).toFixed(1)}%（远端${iv.term_slope > 0 ? "更高" : "更低"}）`
    : "—";
  byId("iv-expiry").textContent = iv.expiry ? iv.expiry.slice(0, 10) : "—";
}

function renderReport(job) {
  // 遗留英文报告不符合当前中文输出标准：主报告卡与历史区一致，不冒充合规报告展示
  const legacyReport = job.report && job.report.summary && !reportIsChinese(job.report.summary);
  const report = legacyReport ? null : job.report;
  const terminalFailure = isTerminalFailure(job.stage);
  const action = terminalFailure ? job.stage : report?.action || job.gate?.action || "ANALYSE";

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
    const dirLabel = DIRECTION_LABELS[dir] || "观望";
    byId("direction-label").textContent = dirLabel;
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
  /* 可执行建议：关键位置 / 预案 / 应避免（模型输出，全部转义） */
  if (report && Array.isArray(report.suggestions)) {
    const box = byId("suggestions-box");
    const blocks = [];
    if (Array.isArray(report.suggestions) && report.suggestions.length) {
      blocks.push(suggestionBlockHtml("操作建议", report.suggestions, "sg-action"));
    }
    if (Array.isArray(report.scenarios) && report.scenarios.length) {
      blocks.push(suggestionBlockHtml("预案（如果……就……）", report.scenarios, "sg-scenario"));
    }
    if (Array.isArray(report.avoid) && report.avoid.length) {
      blocks.push(suggestionBlockHtml("应避免", report.avoid, "sg-avoid"));
    }
    box.innerHTML = blocks.join("");
  }
  /* 军师模式：验收时的方向/点位纪律标注（共振相悖、震荡市强方向等），不阻断但如实呈现 */
  const vwarnings = Array.isArray(report?.validation_warnings) ? report.validation_warnings : [];
  const vwEl = byId("validation-warnings");
  vwEl.innerHTML = vwarnings.map((w) => `<li class="gate-warning">${escapeHtml(String(w))}</li>`).join("");
  vwEl.hidden = vwarnings.length === 0;

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

/* 可执行建议渲染：三组（操作/预案/应避免），全部转义防注入 */
function suggestionBlockHtml(title, items, cls) {
  const lis = items.map((item) => `<li class="${cls}">${escapeHtml(String(item))}</li>`).join("");
  return `<div class="sg-block"><span class="sg-title">${title}</span><ul class="sg-list">${lis}</ul></div>`;
}

/* ─── STATUS & HISTORY ─── */
/* 自检灯配色：MT5=RX-78 蓝 / FRED=黄 / CAL=红（机甲三原色点缀） */
const LAMP_TONES = { "lamp-mt5": "tone-blue", "lamp-fred": "tone-yellow", "lamp-cal": "tone-red" };
function renderSelfCheck(selfCheck) {
  const lamps = { "lamp-mt5": selfCheck?.mt5, "lamp-fred": selfCheck?.fred, "lamp-cal": selfCheck?.calendar };
  Object.entries(lamps).forEach(([id, state]) => {
    const el = byId(id);
    if (!el) return;
    const good = state === "ok" || state === "configured" || state === "fresh";
    // missing = 日历文件从未写入（长期拉取失败）→ 红灯，与 offline/missing_key 同级
    const bad = state === "offline" || state === "missing_key" || state === "missing";
    const tone = LAMP_TONES[id] || "";
    el.className = `lamp ${good ? "ok" : bad ? "bad" : state ? "warn" : ""} ${tone}`;
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
    refreshMode();
  } catch (error) {
    byId("system-time").textContent = "服务异常";
    renderFailure(error?.message || "无法连接本机分析服务。");
  }
}

async function refreshMode() {
  try {
    const status = await jsonRequest("/api/mode");
    renderMode(status.mode);
  } catch (error) {
    /* mode 读取失败不阻断主界面 */
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

/* 点差/时段分组标签（2026-08-03 新增：监控点差闸门与时段纪律效果） */
const SPREAD_LABELS = { spread_low: "点差低位", spread_mid: "点差中位", spread_high: "点差高位" };
const SESSION_LABELS = { asia: "亚洲", london: "伦敦", london_ny_overlap: "伦纽重叠", ny_late: "纽约午盘" };

function renderContextStats(contexts) {
  const el = byId("review-contexts");
  if (!el) return;
  if (!contexts) { el.innerHTML = ""; return; }
  const html = [
    contextBlockHtml("按闸门", contexts.by_gate_action, GATE_ACTION_LABELS),
    contextBlockHtml("按共振", contexts.by_resonance),
    contextBlockHtml("按方向", contexts.by_direction, DIRECTION_LABELS),
    contextBlockHtml("按模式", contexts.by_mode, MODE_LABELS),
    contextBlockHtml("波动环境", contexts.by_vol_regime, VOL_REGIME_LABELS),
    contextBlockHtml("按点差", contexts.by_spread_percentile, SPREAD_LABELS),
    contextBlockHtml("按时段", contexts.by_session, SESSION_LABELS),
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

/* ─── 交易模式切换：scalp（剥头皮）/ swing（日内波段），随任务快照记录 ─── */
const MODE_LABELS = { scalp: "剥头皮", swing: "日内波段" };
/* 波动环境复盘分组（review_stats by_vol_regime；vol_na 永不出现） */
const VOL_REGIME_LABELS = { vol_high: "高IV", vol_low: "低IV", vol_neutral: "中性" };
function renderMode(mode) {
  const state = byId("mode-state");
  if (!state) return;
  const current = mode || "scalp";
  state.textContent = MODE_LABELS[current] || "剥头皮";
  /* 小灯式：点哪个哪个亮（active class 驱动金色小灯） */
  document.querySelectorAll(".mode-pill").forEach((pill) => {
    const isActive = pill.dataset.mode === current;
    pill.classList.toggle("active", isActive);
    pill.title = `${MODE_LABELS[pill.dataset.mode] || pill.dataset.mode}${isActive ? "（当前模式）" : ""}`;
  });
}

async function setMode(mode) {
  if (mode !== "scalp" && mode !== "swing") return;
  try {
    const status = await jsonRequest("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    renderMode(status.mode);
  } catch (error) {
    byId("mode-state").textContent = "异常";
  }
}

/* mode-pill 直接绑定：点哪个就选哪个（类似自主调度的小灯交互） */
function bindModePills() {
  document.querySelectorAll(".mode-pill").forEach((pill) => {
    pill.addEventListener("click", () => setMode(pill.dataset.mode));
  });
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
    el.textContent = TIME_FORMATTER.format(new Date());
  }
}

/* ─── 视图导航：决策台 / 市场背景 / 复盘记录 / 深度复盘 ─── */
function switchView(view) {
  document.body.dataset.view = view;
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  /* 切换视图后立即刷新目标视图的数据 */
  if (view === "review") {
    refreshHistory();
    refreshReviewStats();
  }
  if (view === "debate") {
    refreshDebate();
  }
}

/* ─── 深度复盘：三家模型辩论过程展示 ─── */
const DEBATE_ROLE_META = {
  "技术面主攻": { icon: "◈", color: "qwen" },
  "宏观情绪主攻": { icon: "◉", color: "deepseek" },
  "风险对抗": { icon: "⚠", color: "glm" },
};

async function refreshDebate() {
  const container = byId("debate-latest");
  if (!container) return;
  try {
    const history = await jsonRequest("/api/history");
    const debates = (history.jobs || []).filter((j) => j.kind === "deep_review" && j.debate);
    if (!debates.length) {
      container.innerHTML = '<p class="empty-state">尚无辩论记录。点击「发起辩论复盘」。</p>';
      return;
    }
    const latest = debates[0];
    container.innerHTML = renderDebate(latest);
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(errorText(error, "无法读取辩论记录。"))}</p>`;
  }
}

function renderDebate(job) {
  const debate = job.debate || {};
  const rounds = Array.isArray(debate.rounds) ? debate.rounds : [];
  const consensus = debate.consensus || {};
  const parts = [];

  // 共识结论卡（置顶）
  const report = consensus.report;
  const votes = report?.debate_votes || consensus.votes || {};
  const votesHtml = Object.entries(votes).map(([k, v]) => `<span class="vote-chip">${k} ${v}</span>`).join("");
  const direction = consensus.direction || report?.direction || "—";
  const dirArrow = direction === "LONG" ? "↑" : direction === "SHORT" ? "↓" : "→";
  parts.push(`<article class="debate-consensus">
    <div class="side-card-head">
      <span class="side-label">辩论共识${votesHtml ? " " + votesHtml : ""}</span>
      <span class="chip chip-analyse">${job.stage}</span>
    </div>
    <div class="debate-dir"><span class="direction-arrow ${direction === "LONG" ? "long" : direction === "SHORT" ? "short" : "neutral"}">${dirArrow}</span>
      <div><strong>${direction || "—"}</strong><span class="mono">有效报告 ${consensus.valid_count ?? "—"} 家</span></div></div>
    ${report?.entry_zone ? `<div class="trade-levels">
      <div class="level-item level-entry"><span>入场</span><strong class="mono">${escapeHtml(report.entry_zone)}</strong></div>
      <div class="level-item level-tp"><span>止盈</span><strong class="mono">${escapeHtml(report.take_profit || "—")}</strong></div>
      <div class="level-item level-sl"><span>止损</span><strong class="mono">${escapeHtml(report.stop_loss || "—")}</strong></div>
    </div>` : ""}
    ${report?.summary ? `<p class="report-body">${escapeHtml(report.summary)}</p>` : ""}
    ${Array.isArray(report?.debate_disagreements) && report.debate_disagreements.length
      ? `<div class="debate-disagree"><span class="sg-title">分歧点</span><ul class="sg-list">${report.debate_disagreements.map((d) => `<li class="sg-scenario">${escapeHtml(d.model || d.note || "—")}</li>`).join("")}</ul></div>` : ""}
  </article>`);

  // 每轮辩论
  rounds.forEach((round) => {
    const stmts = Array.isArray(round.statements) ? round.statements : [];
    parts.push(`<article class="debate-round">
      <div class="side-card-head"><span class="side-label">第 ${round.round} 轮 · ${round.round === 1 ? "独立分析" : round.round === 2 ? "交叉辩论" : "分歧收敛"}</span></div>
      <div class="debate-stmts">${stmts.map(renderStatement).join("")}</div>
    </article>`);
  });
  return parts.join("");
}

function renderStatement(stmt) {
  const meta = DEBATE_ROLE_META[stmt.role] || { icon: "◈", color: "" };
  const body = stmt.error
    ? `<p class="sg-avoid">${escapeHtml(stmt.error)}</p>`
    : `<p class="debate-text">${escapeHtml(stmt.content || "（无输出）")}</p>`;
  return `<div class="debate-stmt ${meta.color}">
    <div class="debate-stmt-head"><span class="debate-ico">${meta.icon}</span>
      <strong>${escapeHtml(stmt.role || stmt.model)}</strong>
      <span class="mono chip ${stmt.ok ? "chip-analyse" : "chip-muted"}">${stmt.ok ? "有效" : "失败"}</span>
      <span class="mono debate-model">${escapeHtml(stmt.model)}</span>
    </div>
    ${body}
  </div>`;
}

/* ─── BOOT ─── */
async function boot() {
  window.setInterval(tickClock, 1000);
  await refreshStatus();
  await refreshHistory();
  await refreshReviewStats();
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});
byId("start-brief").addEventListener("click", () => startJob("brief"));
byId("start-deep-review").addEventListener("click", () => startJob("deep_review"));
const debateRun = byId("debate-run");
if (debateRun) debateRun.addEventListener("click", () => startJob("deep_review"));
byId("auto-toggle").addEventListener("click", toggleAuto);
bindModePills();
boot();
