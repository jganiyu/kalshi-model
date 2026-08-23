const state = {
  dashboard: null,
  chartPoints: [],
  chartWindow: 90,
  closeTime: null,
  lastNotification: null,
  activePage: "dashboard",
  liveSocket: null,
  liveConnected: false,
  liveRetryMs: 1000,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function money(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(Number(value));
}

function percent(value, digits = 1, signed = false) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const number = Number(value) * 100;
  const sign = signed && number > 0 ? "+" : "";
  return `${sign}${number.toFixed(digits)}%`;
}

function points(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const number = Number(value) * 100;
  return `${number > 0 ? "+" : ""}${number.toFixed(1)} pts`;
}

function compact(value) {
  if (value === null || value === undefined) return "--";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value));
}

function shortDate(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(new Date(value));
}

function countdown(seconds) {
  if (!Number.isFinite(seconds)) return "--:--";
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response was not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function signalTitle(decision) {
  if (!decision) return "Waiting for live data";
  if (decision.reason_code === "DATA_UNRELIABLE") return "NO TRADE — Data Unreliable";
  if (decision.reason_code === "RISK_LIMIT") return "NO TRADE — Risk Limit";
  return `${decision.signal} — ${decision.confidence} Confidence`;
}

function renderDashboard(data) {
  state.dashboard = data;
  const system = data.system || {};
  const current = data.current;
  const btc = data.btc || {};
  const decision = current?.decision;
  const streams = system.streams || {};
  const statusDot = $("#sidebar-status-dot");
  statusDot.className = `status-dot ${system.status || "degraded"}`;
  $("#sidebar-status").textContent = state.liveConnected
    ? "Streaming live"
    : system.status === "live" ? "REST fallback" : "Data guarded";
  $("#last-update").textContent = system.updated_at ? `Updated ${new Date(system.updated_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : "Connecting";

  const band = $("#decision-band");
  band.classList.remove("trade-yes", "trade-no");
  if (decision?.signal === "TRADE YES") band.classList.add("trade-yes");
  if (decision?.signal === "TRADE NO") band.classList.add("trade-no");
  $("#signal-title").textContent = signalTitle(decision);
  $("#signal-explanation").textContent = decision?.explanation || system.message || "Connecting to public feeds.";
  $("#model-probability").textContent = percent(decision?.model_probability, 1);
  $("#market-probability").textContent = percent(decision?.market_probability, 1);
  $("#edge").textContent = points(decision?.edge);
  $("#ev").textContent = decision?.expected_value === null || decision?.expected_value === undefined ? "--" : money(decision.expected_value, 3);
  $("#edge").className = Number(decision?.edge) > 0 ? "positive" : Number(decision?.edge) < 0 ? "negative" : "";
  $("#ev").className = Number(decision?.expected_value) > 0 ? "positive" : Number(decision?.expected_value) < 0 ? "negative" : "";

  $("#btc-price").textContent = money(btc.price);
  $("#btc-dispersion").textContent = btc.price
    ? `${btc.exchange_count} feeds · ${Number(btc.dispersion_pct || 0).toFixed(3)}% dispersion`
    : "No composite available";
  $("#composite-source").textContent = btc.quotes?.map((quote) => quote.exchange).join(" · ") || "Multi-exchange median";

  $("#contract-ticker").textContent = current?.ticker || "No active contract";
  state.closeTime = current?.close_time ? new Date(current.close_time) : null;
  updateCountdown();
  $("#threshold").textContent = money(current?.strike);
  const distance = btc.price && current?.strike ? Number(btc.price) - Number(current.strike) : null;
  $("#distance-to-strike").textContent = distance === null ? "--" : `${distance >= 0 ? "+" : ""}${money(distance)} from threshold`;
  $("#yes-market").textContent = `${percent(current?.yes_bid, 1)} / ${percent(current?.yes_ask, 1)}`;
  $("#no-market").textContent = `${percent(current?.no_bid, 1)} / ${percent(current?.no_ask, 1)}`;
  $("#spread").textContent = points(current?.spread);
  $("#open-interest").textContent = compact(current?.open_interest);
  $("#volume").textContent = compact(current?.volume);
  $("#position-size").textContent = decision?.suggested_contracts
    ? `${money(decision.suggested_dollars)} · ${decision.suggested_contracts} contracts`
    : "No position";
  const quality = current?.data_quality || { reliable: false, reason: "Waiting for market" };
  $("#quality-dot").className = `quality-dot ${quality.reliable ? "good" : "bad"}`;
  $("#quality-text").textContent = quality.reason || "Checking feeds";
  const kalshiConnection = $("#kalshi-connection");
  const btcConnection = $("#btc-connection");
  if (kalshiConnection) {
    kalshiConnection.textContent = streams.kalshi?.connected
      ? "WebSocket live"
      : streams.kalshi?.configured ? "Reconnecting" : "Key ID required";
  }
  if (btcConnection) {
    const sources = streams.bitcoin?.sources || [];
    btcConnection.textContent = sources.length ? `${sources.join(" + ")} live` : "REST fallback";
  }

  renderNext(data.next);
  renderCalibrationGlance(data.calibration, data.model);
  drawChart();
  if (data.notification?.signal_id && data.notification.signal_id !== state.lastNotification) {
    state.lastNotification = data.notification.signal_id;
    showToast(data.notification.title, data.notification.detail);
  }
}

function renderNext(next) {
  $("#next-title").textContent = next?.title || "Waiting for next contract";
  $("#next-time").textContent = next?.open_time ? `Opens ${shortDate(next.open_time)}` : "--";
  $("#next-threshold").textContent = money(next?.strike);
  const priced = Boolean(next?.strike && Number(next?.yes_ask) > 0 && Number(next?.no_ask) > 0);
  $("#next-yes").textContent = priced ? percent(next.yes_ask, 1) : "--";
  $("#next-no").textContent = priced ? percent(next.no_ask, 1) : "--";
}

function renderCalibrationGlance(calibration, model) {
  $("#brier-score").textContent = calibration?.brier_score === null || calibration?.brier_score === undefined ? "--" : Number(calibration.brier_score).toFixed(3);
  $("#calibration-error").textContent = percent(calibration?.calibration_error, 1);
  $("#calibration-sample").textContent = calibration?.sample_size || 0;
  $("#model-version").textContent = model?.version || "baseline-1.0";
}

function updateCountdown() {
  if (!state.closeTime) {
    $("#countdown").textContent = "--:--";
    return;
  }
  $("#countdown").textContent = countdown((state.closeTime.getTime() - Date.now()) / 1000);
}

function drawChart() {
  const canvas = $("#price-chart");
  if (!canvas) return;
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, box.width);
  const height = Math.max(1, box.height);
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  const points = state.chartPoints.filter((point) => Number.isFinite(Number(point.price)));
  const current = state.dashboard?.current;
  const next = state.dashboard?.next;
  if (!points.length) {
    context.fillStyle = "#8a9299";
    context.font = "12px -apple-system, sans-serif";
    context.textAlign = "center";
    context.fillText("Collecting live price history", width / 2, height / 2);
    return;
  }
  const values = points.map((point) => Number(point.price));
  if (current?.strike) values.push(Number(current.strike));
  if (next?.strike) values.push(Number(next.strike));
  let low = Math.min(...values);
  let high = Math.max(...values);
  const padding = Math.max((high - low) * 0.18, high * 0.00035, 20);
  low -= padding;
  high += padding;
  const left = 8;
  const right = 62;
  const top = 14;
  const bottom = 25;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const x = (index) => left + (points.length === 1 ? chartWidth / 2 : (index / (points.length - 1)) * chartWidth);
  const y = (value) => top + (1 - (value - low) / (high - low)) * chartHeight;

  context.strokeStyle = "#e7eaec";
  context.fillStyle = "#7b838a";
  context.font = "10px -apple-system, sans-serif";
  context.textAlign = "left";
  for (let index = 0; index < 4; index += 1) {
    const value = low + ((high - low) * index) / 3;
    const rowY = y(value);
    context.beginPath(); context.moveTo(left, rowY); context.lineTo(width - right + 4, rowY); context.stroke();
    context.fillText(money(value, 0), width - right + 9, rowY + 3);
  }

  const drawLevel = (value, color, dashed) => {
    if (!value) return;
    context.save();
    context.strokeStyle = color;
    context.lineWidth = 1.25;
    context.setLineDash(dashed ? [5, 4] : []);
    context.beginPath(); context.moveTo(left, y(Number(value))); context.lineTo(width - right + 4, y(Number(value))); context.stroke();
    context.restore();
  };
  drawLevel(current?.strike, "#c9473e", false);
  if (next?.strike && next.strike !== current?.strike) drawLevel(next.strike, "#2767c5", true);

  if (points.length > 1) {
    const gradient = context.createLinearGradient(0, top, 0, height - bottom);
    gradient.addColorStop(0, "rgba(21, 23, 25, .14)");
    gradient.addColorStop(1, "rgba(21, 23, 25, 0)");
    context.beginPath();
    points.forEach((point, index) => index === 0 ? context.moveTo(x(index), y(Number(point.price))) : context.lineTo(x(index), y(Number(point.price))));
    context.lineTo(x(points.length - 1), height - bottom);
    context.lineTo(x(0), height - bottom);
    context.closePath();
    context.fillStyle = gradient; context.fill();
    context.beginPath();
    points.forEach((point, index) => index === 0 ? context.moveTo(x(index), y(Number(point.price))) : context.lineTo(x(index), y(Number(point.price))));
    context.strokeStyle = "#191c1f"; context.lineWidth = 2; context.lineJoin = "round"; context.stroke();
  }
  const lastIndex = points.length - 1;
  context.beginPath(); context.arc(x(lastIndex), y(Number(points[lastIndex].price)), 3.5, 0, Math.PI * 2); context.fillStyle = "#117a51"; context.fill();
}

function appendLiveChartPoint(data) {
  const btc = data?.btc;
  if (!btc?.observed_at || !Number.isFinite(Number(btc.price))) return;
  const last = state.chartPoints.at(-1);
  const point = {
    observed_at: btc.observed_at,
    price: Number(btc.price),
    dispersion_pct: btc.dispersion_pct,
    volatility_15m: btc.volatility_15m,
  };
  if (last?.observed_at === point.observed_at) state.chartPoints[state.chartPoints.length - 1] = point;
  else state.chartPoints.push(point);
  const cutoff = Date.now() - state.chartWindow * 60 * 1000;
  state.chartPoints = state.chartPoints.filter((item) => new Date(item.observed_at).getTime() >= cutoff);
}

function connectLive() {
  if (state.liveSocket && state.liveSocket.readyState < WebSocket.CLOSING) return;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
  state.liveSocket = socket;
  socket.addEventListener("open", () => {
    state.liveConnected = true;
    state.liveRetryMs = 1000;
  });
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type !== "dashboard" || !message.data) return;
      appendLiveChartPoint(message.data);
      renderDashboard(message.data);
    } catch (_) { /* Ignore malformed stream frames and wait for the next snapshot. */ }
  });
  socket.addEventListener("close", () => {
    state.liveConnected = false;
    state.liveSocket = null;
    $("#sidebar-status").textContent = "Reconnecting";
    window.setTimeout(connectLive, state.liveRetryMs);
    state.liveRetryMs = Math.min(15000, state.liveRetryMs * 2);
  });
  socket.addEventListener("error", () => socket.close());
}

async function refreshDashboard() {
  try {
    const [dashboard, chart] = await Promise.all([
      api("/api/dashboard"), api(`/api/chart?minutes=${state.chartWindow}`),
    ]);
    state.chartPoints = chart.points || [];
    renderDashboard(dashboard);
  } catch (error) {
    $("#sidebar-status").textContent = "App offline";
    $("#sidebar-status-dot").className = "status-dot degraded";
  }
}

function statCard(label, value, detail = "", className = "") {
  return `<div class="stats-card"><span>${label}</span><strong class="${className}">${value}</strong>${detail ? `<small>${detail}</small>` : ""}</div>`;
}

async function loadCalibration() {
  const data = await api("/api/calibration");
  const summary = data.summary || {};
  $("#calibration-stats").innerHTML = [
    statCard("Settled sample", summary.sample_size || 0, "Independent contracts"),
    statCard("Brier score", summary.brier_score == null ? "--" : Number(summary.brier_score).toFixed(3), "Lower is better"),
    statCard("Calibration error", percent(summary.calibration_error, 1), "Predicted vs actual"),
  ].join("");
  const buckets = summary.buckets || [];
  $("#calibration-bars").innerHTML = buckets.length ? buckets.map((bucket) => `
    <div class="bucket">
      <i style="height:${Math.max(2, bucket.predicted * 100)}%" title="Predicted ${percent(bucket.predicted)}"></i>
      <i class="actual" style="height:${Math.max(2, bucket.actual * 100)}%" title="Actual ${percent(bucket.actual)}"></i>
      <label>${bucket.label}</label><small>n=${bucket.count}</small>
    </div>`).join("") : '<p class="empty-state">Settled observations will populate this chart.</p>';
  const reports = data.reports || [];
  $("#report-list").innerHTML = reports.length ? reports.map((row) => {
    const report = row.report || {};
    const limitations = (report.limitations || []).map((item) => `<li>${item}</li>`).join("");
    return `<details class="report-item">
      <summary><time>${shortDate(row.created_at)}</time><strong>${row.tldr}</strong><span>${row.promoted ? "Promoted" : "Incumbent kept"}</span></summary>
      <div class="report-body">
        <h4>Validation</h4><p>${report.validation || "Calibration-only report."}</p>
        <h4>Scores</h4><p>Brier: ${row.brier_before == null ? "--" : Number(row.brier_before).toFixed(3)} · Calibration error: ${percent(row.calibration_error, 1)} · Settled contracts: ${row.settled_contracts}</p>
        <h4>Models</h4><p>Active: ${row.active_model_version}${row.candidate_model_version ? ` · Candidate: ${row.candidate_model_version}` : ""}</p>
        ${limitations ? `<h4>Limitations</h4><ul>${limitations}</ul>` : ""}
        <p>${(report.signal_snapshot_ids || []).length} underlying signal snapshots are preserved in SQLite.</p>
      </div>
    </details>`;
  }).join("") : '<p class="empty-state">No calibration reports yet.</p>';
}

async function loadPaper() {
  const data = await api("/api/paper");
  const pnlClass = data.realized_pnl > 0 ? "positive" : data.realized_pnl < 0 ? "negative" : "";
  $("#paper-stats").innerHTML = [
    statCard("Current bankroll", money(data.current_bankroll), `Started ${money(data.starting_bankroll)}`),
    statCard("Realized P&L", money(data.realized_pnl), percent(data.realized_return_pct), pnlClass),
    statCard("Record", `${data.wins}–${data.losses}`, `${data.open_positions} open`),
    statCard("Average edge", points(data.average_edge), "At entry"),
    statCard("Maximum drawdown", percent(data.max_drawdown_pct), "Settled equity"),
  ].join("");
  const trades = data.trades || [];
  $("#trade-table").innerHTML = trades.length ? trades.map((trade) => `
    <tr><td>${shortDate(trade.opened_at)}</td><td>${trade.ticker}</td><td>${trade.side}</td>
    <td>${percent(trade.entry_price, 1)}</td><td>${trade.contracts}</td><td>${points(trade.edge)}</td>
    <td><span class="status-pill ${trade.status}">${trade.status.toUpperCase()}</span></td>
    <td class="${Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : ""}">${trade.realized_pnl == null ? "--" : money(trade.realized_pnl)}</td></tr>
  `).join("") : '<tr><td colspan="8" class="empty-state">No paper trades yet.</td></tr>';
}

const percentSettingIds = ["min_edge", "fractional_kelly", "max_position_pct", "max_risk_per_trade_pct", "max_session_drawdown_pct"];

async function loadSettings() {
  const [settings, database] = await Promise.all([api("/api/settings"), api("/api/database")]);
  for (const [key, value] of Object.entries(settings)) {
    const input = document.getElementById(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = percentSettingIds.includes(key) ? Number(value) * 100 : value;
  }
  $("#database-path").textContent = database.path;
  $("#database-counts").innerHTML = Object.entries(database.counts).map(([key, value]) => `<span>${key.replaceAll("_", " ")}: ${value}</span>`).join("");
}

async function saveSettings() {
  const ids = [
    "starting_bankroll", "paper_trading_enabled", "min_edge", "slippage_cents",
    "fractional_kelly", "risk_controls_enabled", "max_position_pct",
    "max_risk_per_trade_pct", "max_session_drawdown_pct", "max_exchange_dispersion_pct",
  ];
  const payload = {};
  ids.forEach((id) => {
    const input = document.getElementById(id);
    let value = input.type === "checkbox" ? input.checked : Number(input.value);
    if (percentSettingIds.includes(id)) value /= 100;
    payload[id] = value;
  });
  const button = $("#save-settings");
  button.disabled = true;
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    showToast("Settings saved", "New decisions will use the updated risk and edge thresholds.");
    await refreshDashboard();
  } catch (error) {
    showToast("Settings not saved", error.message);
  } finally { button.disabled = false; }
}

async function runBacktest() {
  const button = $("#run-backtest");
  button.disabled = true;
  try {
    const result = await api("/api/backtest", { method: "POST", body: "{}" });
    const panel = $("#backtest-result");
    panel.hidden = false;
    panel.textContent = `${result.trades} historical decisions · ${result.wins} wins · ${percent(result.return_pct)} return · ${percent(result.max_drawdown_pct)} max drawdown. ${result.look_ahead_guard}`;
  } catch (error) { showToast("Backtest failed", error.message); }
  finally { button.disabled = false; }
}

async function runBootstrap() {
  const button = $("#run-bootstrap");
  const panel = $("#settings-result");
  button.disabled = true; panel.hidden = false; panel.textContent = "Importing point-in-time Kalshi and Coinbase history…";
  try {
    const result = await api("/api/bootstrap", { method: "POST" });
    panel.textContent = `Imported ${result.imported} observations; skipped ${result.skipped}. ${result.limitation || ""}`;
    await loadSettings();
  } catch (error) { panel.textContent = `History refresh failed: ${error.message}`; }
  finally { button.disabled = false; }
}

async function backupDatabase() {
  const button = $("#backup-database");
  const panel = $("#settings-result");
  button.disabled = true;
  try {
    const result = await api("/api/database/backup", { method: "POST" });
    panel.hidden = false; panel.textContent = `Backup created: ${result.created}`;
  } catch (error) { showToast("Backup failed", error.message); }
  finally { button.disabled = false; }
}

function showToast(title, detail) {
  $("#toast-title").textContent = title;
  $("#toast-detail").textContent = detail;
  const toast = $("#toast");
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 6500);
}

async function switchPage(page) {
  state.activePage = page;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  $$(".page").forEach((section) => section.classList.toggle("active", section.id === `page-${page}`));
  try {
    if (page === "calibration") await loadCalibration();
    if (page === "paper") await loadPaper();
    if (page === "settings") await loadSettings();
  } catch (error) { showToast("Unable to load view", error.message); }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchPage(button.dataset.page)));
  $$("[data-window]").forEach((button) => button.addEventListener("click", async () => {
    state.chartWindow = Number(button.dataset.window);
    $$("[data-window]").forEach((item) => item.classList.toggle("active", item === button));
    const chart = await api(`/api/chart?minutes=${state.chartWindow}`);
    state.chartPoints = chart.points || []; drawChart();
  }));
  $("#save-settings").addEventListener("click", saveSettings);
  $("#run-backtest").addEventListener("click", runBacktest);
  $("#run-bootstrap").addEventListener("click", runBootstrap);
  $("#backup-database").addEventListener("click", backupDatabase);
  window.addEventListener("resize", drawChart);
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await refreshDashboard();
  connectLive();
  setInterval(refreshDashboard, 15000);
  setInterval(updateCountdown, 1000);
});
