const $ = (selector) => document.querySelector(selector);
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
const storedTheme = localStorage.getItem("kalshi-mobile-theme-v1");
let followsSystemTheme = !storedTheme;
let reconnectDelay = 1000;
let socket;
let timerDeadline = null;
let texasState = null;
let texasAnimation = null;

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#theme-toggle").checked = dark;
}

applyTheme(storedTheme || (themeMedia.matches ? "dark" : "light"));

$("#theme-toggle").addEventListener("change", (event) => {
  const theme = event.target.checked ? "dark" : "light";
  followsSystemTheme = false;
  localStorage.setItem("kalshi-mobile-theme-v1", theme);
  applyTheme(theme);
});

themeMedia.addEventListener("change", (event) => {
  if (followsSystemTheme) applyTheme(event.matches ? "dark" : "light");
});

function percent(value, digits = 1) {
  return value == null || !Number.isFinite(Number(value))
    ? "--"
    : `${(Number(value) * 100).toFixed(digits)}%`;
}

function cents(value, digits = 1) {
  return value == null || !Number.isFinite(Number(value))
    ? "--"
    : `${(Number(value) * 100).toFixed(digits)}¢`;
}

function money(value) {
  return value == null || !Number.isFinite(Number(value))
    ? "—"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(value));
}

function signedMoney(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : number < 0 ? "−" : ""}${money(Math.abs(number))}`;
}

function price(value) {
  return value == null || !Number.isFinite(Number(value))
    ? "--"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(Number(value));
}

function updateTimer() {
  if (timerDeadline == null) {
    $("#market-timer").textContent = "--:--";
    return;
  }
  const remaining = Math.max(0, Math.floor((timerDeadline - Date.now()) / 1000));
  const minutes = Math.floor(remaining / 60);
  $("#market-timer").textContent = `${String(minutes).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}

function compact(value) {
  return value == null || !Number.isFinite(Number(value))
    ? "--"
    : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value));
}

function sideLabel(side) {
  return String(side || "").toUpperCase() === "YES" ? "Up" : String(side || "").toUpperCase() === "NO" ? "Down" : "--";
}

function modeLabel(mode) {
  const normalized = String(mode || "PAPER").toUpperCase();
  if (normalized === "LIVE") return "KALSHI LIVE";
  if (normalized === "DEMO") return "KALSHI DEMO";
  return "PAPER TRADING";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function timeLabel(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function setConnection(status) {
  const strip = $(".connection-strip");
  strip.dataset.state = status.toLowerCase();
  $("#connection-state").textContent = status;
}

function setProgress(selector, metric = {}) {
  const progress = Math.max(0, Math.min(1, Number(metric.progress || 0)));
  const bar = $(selector);
  bar.style.width = `${(progress * 100).toFixed(1)}%`;
  bar.classList.toggle("passed", Boolean(metric.passed));
}

function setGate(selector, gate = {}, label) {
  const element = $(selector);
  element.classList.toggle("pass", Boolean(gate.passed));
  element.title = gate.detail || "";
  element.querySelector("small").textContent = label;
}

function renderHud(readiness) {
  const metrics = readiness?.metrics || {};
  const probability = metrics.probability || {};
  const ev = metrics.net_ev || {};
  const confirmation = metrics.confirmation || {};
  const gates = readiness?.gates || {};
  const status = String(readiness?.status || "WATCHING").toLowerCase();
  $("#hud-side").textContent = sideLabel(readiness?.side);
  $("#hud-status").textContent = status.charAt(0).toUpperCase() + status.slice(1);
  $("#hud-status").dataset.status = status;
  $("#probability-value").textContent = probability.current == null
    ? `-- / ${percent(probability.required, 0)}`
    : `${percent(probability.current)} / ${percent(probability.required, 0)}`;
  $("#ev-value").textContent = ev.current == null
    ? `-- / ${cents(ev.required)}`
    : `${cents(ev.current)} / ${cents(ev.required)}`;
  $("#confirmation-value").textContent = confirmation.locked
    ? "Locked"
    : `${Number(confirmation.current_seconds || 0).toFixed(1)} / ${Number(confirmation.required_seconds || 0).toFixed(0)}s`;
  setProgress("#probability-progress", probability);
  setProgress("#ev-progress", ev);
  setProgress("#confirmation-progress", confirmation);

  const spread = gates.spread || {};
  const spreadLabel = spread.current == null ? "--" : spread.required == null
    ? cents(spread.current) : `${cents(spread.current)} / ${cents(spread.required)}`;
  const liquidity = gates.liquidity || {};
  const liquidityLabel = liquidity.current == null ? "--" : `${compact(liquidity.current)} / ${compact(liquidity.required)}`;
  const quality = gates.quality || {};
  setGate("#gate-spread", spread, spreadLabel);
  setGate("#gate-liquidity", liquidity, liquidityLabel);
  setGate("#gate-data", gates.data, gates.data?.passed ? "Fresh" : "Blocked");
  setGate("#gate-quality", quality, quality.current == null ? "--" : `${quality.current} / ${quality.required || "--"}`);
  const threshold = gates.threshold_margin || {};
  const thresholdLabel = threshold.enabled === false ? "Off" : threshold.current == null
    ? `-- / ${signedMoney(threshold.required)}`
    : `${signedMoney(threshold.current)} / ${signedMoney(threshold.required)}`;
  setGate("#gate-threshold", threshold, thresholdLabel);
  const direction = gates.directional_momentum || {};
  const directionLabel = direction.enabled === false ? "Off" : direction.current == null
    ? `-- / ${signedMoney(direction.required)}`
    : `${signedMoney(direction.current)} / ${signedMoney(direction.required)}`;
  setGate("#gate-direction", direction, directionLabel);
  const volatility = gates.volatility || {};
  const volatilityLabel = volatility.enabled === false ? "Off" : volatility.current == null
    ? `${volatility.status === "LEARNING" ? "Learning" : "--"} / ${Number(volatility.required || 0).toFixed(1)}`
    : `${Number(volatility.current).toFixed(1)} / ${Number(volatility.required || 0).toFixed(1)}`;
  setGate("#gate-volatility", volatility, volatilityLabel);
  const cushion = Number(volatility.cushion_ratio);
  $("#hud-cushion").textContent = Number.isFinite(cushion)
    ? `Cushion ${cushion > 99 ? ">99" : cushion.toFixed(1)}× expected move`
    : "Cushion --";
  setGate("#gate-risk", gates.risk, gates.risk?.passed ? "Clear" : "Blocked");
  $("#hud-blocker").textContent = readiness?.blocker || "Waiting for live trade data.";
}

function texasTiming(openTime) {
  const opened = new Date(openTime || "").getTime();
  const elapsed = Number.isFinite(opened)
    ? Math.max(0, Math.min(900, (Date.now() - opened) / 1000))
    : 0;
  return { elapsed, phase: elapsed < 300 ? "FLOP" : elapsed < 600 ? "TURN" : "RIVER" };
}

function animateTexasHud() {
  const hud = $("#texas-mobile-hud");
  if (!hud || hud.hidden || !texasState) {
    texasAnimation = null;
    return;
  }
  const timing = texasTiming(texasState.market_open_time);
  const starts = { FLOP: 0, TURN: 300, RIVER: 600 };
  document.querySelectorAll("[data-mobile-phase]").forEach((row) => {
    const key = row.dataset.mobilePhase;
    const progress = Math.max(0, Math.min(1, (timing.elapsed - starts[key]) / 300));
    row.querySelector(".track i").style.width = `${(progress * 100).toFixed(3)}%`;
    row.classList.toggle("complete", progress >= 1);
    row.classList.toggle("active", key === timing.phase);
  });
  $("#texas-mobile-phase").textContent = `THE ${timing.phase}`;
  texasAnimation = window.requestAnimationFrame(animateTexasHud);
}

function renderTexasHud(texas = {}) {
  const enabled = Boolean(texas.enabled);
  $("#standard-hud-content").hidden = enabled;
  $("#texas-mobile-hud").hidden = !enabled;
  texasState = enabled ? texas : null;
  if (!enabled) {
    if (texasAnimation) window.cancelAnimationFrame(texasAnimation);
    texasAnimation = null;
    return;
  }
  const phase = String(texas.phase?.key || texasTiming(texas.market_open_time).phase);
  $("#texas-mobile-phase").textContent = `THE ${phase}`;
  $("#texas-mobile-title").textContent = String(texas.display_name || "Texas Hold’em 2.0").toUpperCase();
  $("#texas-mobile-status").textContent = String(texas.status || "WAITING").replaceAll("_", " ");
  $("#texas-mobile-status").dataset.status = String(texas.status || "WATCHING").toLowerCase();
  $("#texas-mobile-flop-target").textContent = `${cents(texas.targets?.flop, 0)} target · ${cents(texas.targets?.flop_stop, 0)} stop`;
  $("#texas-mobile-turn-target").textContent = `${cents(texas.targets?.turn, 0)} target · ${cents(texas.targets?.turn_stop, 0)} stop`;
  $("#texas-mobile-river-target").textContent = `${cents(texas.targets?.river, 0)} target · ${cents(texas.targets?.river_stop, 0)} stop`;
  $("#texas-mobile-side").textContent = sideLabel(texas.side);
  $("#texas-mobile-cap").textContent = cents(texas.entry_price_cap, 0);
  $("#texas-mobile-attempts").textContent = `${Number(texas.attempt_count || 0)} / ${Number(texas.maximum_attempts || 3)}`;
  $("#texas-mobile-filled").textContent = texas.target_contracts == null
    ? compact(texas.filled_contracts || 0)
    : `${compact(texas.filled_contracts || 0)} / ${compact(texas.target_contracts)}`;
  $("#texas-mobile-bid").textContent = cents(texas.executable_bid);
  $("#texas-mobile-active-target").textContent = cents(texas.active_target, 0);
  $("#texas-mobile-blocker").textContent = texas.blocker || "Opening play is active.";
  const thesis = texas.thesis || {};
  const detail = thesis.status === "EXIT_TRIGGERED"
    ? "Thesis failure exit triggered"
    : thesis.status === "NO_EXIT" ? "5m thesis checkpoint held"
      : thesis.status === "BREACHED" ? "Post-fill breach recorded"
        : "5m thesis checkpoint pending";
  $("#texas-mobile-rules").textContent = texas.rules?.version
    ? `MVI ≥${Number(texas.rules?.mvi_minimum ?? 4).toFixed(1)} · 5m no-breach >$50 exit · ${detail}`
    : "Legacy Texas rules";
  if (texas.allocation_boosted) {
    $("#texas-mobile-status").textContent += " · BOOSTED 1.5×";
  }
  if (!texasAnimation) texasAnimation = window.requestAnimationFrame(animateTexasHud);
}

function tradeResult(trade) {
  const status = String(trade.display_status || trade.status || trade.action || "Open");
  const reason = String(trade.exit_reason || "").toUpperCase();
  const exitLabels = {
    THRESHOLD_BREACH_EXIT: "Threshold breach exit",
    TEXAS_FLOP_TARGET: "Texas Hold’em · Flop target",
    TEXAS_TURN_TARGET: "Texas Hold’em · Turn target",
    TEXAS_RIVER_TARGET: "Texas Hold’em · River target",
    TEXAS_RIVER_STOP: "Texas Hold’em · River stop",
    TEXAS_THESIS_FAILURE: "Texas Hold’em 2.0 · Thesis failure",
  };
  const exitLabel = exitLabels[reason] ? ` · ${exitLabels[reason]}` : "";
  return trade.realized_pnl == null
    ? `${status}${exitLabel}`
    : `${status}${exitLabel} · ${money(trade.realized_pnl)}`;
}

function thresholdBreachExitText(protection = {}) {
  const fields = [
    `Threshold breach exit: ${protection.enabled === false ? "Off" : "On"}`,
    `Exit level ${price(protection.exit_level)}`,
    `Current BTC proxy ${price(protection.btc_proxy ?? protection.trigger_btc_proxy)}`,
    `Distance to exit ${signedMoney(protection.distance_to_exit)}`,
    `Status: ${protection.status || "Blocked"}`,
  ];
  if (protection.reason) fields.push(`Reason: ${protection.reason}`);
  if (protection.last_attempt_at) fields.push(`Last attempt ${timeLabel(protection.last_attempt_at)}`);
  if (protection.remaining_contracts != null) fields.push(`Remaining ${compact(protection.remaining_contracts)} contracts`);
  return fields.join(" · ");
}

function texasExitText(state = {}) {
  const fields = [
    `Texas Hold’em exit: ${state.status || "Watching"}`,
    `Phase ${state.phase?.label || "Opening play"}`,
    `Active target ${cents(state.active_target, 0)}`,
    `Current bid ${cents(state.current_bid)}`,
  ];
  if (state.reason) fields.push(`Reason: ${state.reason}`);
  return fields.join(" · ");
}

function displayStrategy(value) {
  const strategy = String(value || "Manual").toUpperCase();
  if (strategy === "TEXAS_HOLDEM_2_0") return "Texas Hold’em 2.0";
  if (strategy === "TEXAS_HOLDEM") return "Texas Hold’em";
  return String(value || "Manual").replaceAll("_", " ");
}

function renderTrades(trades, mode) {
  const target = $("#trade-list");
  if (!trades?.length) {
    target.innerHTML = `<p class="empty">No ${mode === "PAPER" ? "paper trades" : "fills"} yet.</p>`;
    return;
  }
  target.innerHTML = trades.slice(0, 10).map((trade) => {
    const pnlClass = Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : "";
    const strategy = displayStrategy(trade.strategy || trade.source || "Manual");
    return `<article class="trade">
      <div class="trade-head"><strong>${escapeHtml(sideLabel(trade.side))} · ${escapeHtml(strategy)}</strong><time>${escapeHtml(timeLabel(trade.activity_at || trade.opened_at || trade.filled_at))}</time></div>
      <div class="trade-grid">
        <div><span>Price</span><b>${escapeHtml(cents(trade.entry_price ?? trade.price))}</b></div>
        <div><span>Quantity</span><b>${escapeHtml(compact(trade.contracts))}</b></div>
        <div><span>Settle margin</span><b class="${Number(trade.settlement_margin) > 0 ? "positive" : Number(trade.settlement_margin) < 0 ? "negative" : ""}">${escapeHtml(signedMoney(trade.settlement_margin))}</b></div>
        <div><span>Strategy</span><b>${escapeHtml(strategy)}</b></div>
        <div><span>Result / P&amp;L</span><b class="${pnlClass}">${escapeHtml(tradeResult(trade))}</b></div>
        <div><span>Available after</span><b>${escapeHtml(money(trade.available_cash_after))}</b></div>
      </div>
    </article>`;
  }).join("");
}

function renderOpenTrades(trades, mode, availableCash, market) {
  const accountLabel = $("#open-trades-label");
  accountLabel.textContent = modeLabel(mode);
  accountLabel.dataset.mode = String(mode || "PAPER").toUpperCase();
  $("#available-funds").textContent = money(availableCash);
  $("#current-up-price").textContent = cents(market?.up_price);
  $("#current-down-price").textContent = cents(market?.down_price);
  const target = $("#open-trade-list");
  if (!trades?.length) {
    target.innerHTML = '<p class="empty compact">No open trades.</p>';
    return;
  }
  target.innerHTML = trades.map((trade) => {
    const strategy = displayStrategy(trade.strategy || "Manual");
    const sideClass = String(trade.side).toUpperCase() === "YES" ? "positive" : "negative";
    return `<article class="open-trade">
      <div class="open-trade-head"><strong class="${sideClass}">${escapeHtml(sideLabel(trade.side))}</strong><span>${escapeHtml(strategy)}</span></div>
      <p title="${escapeHtml(trade.ticker)}">${escapeHtml(trade.ticker || "Current market")}</p>
      <div><span>${escapeHtml(compact(trade.contracts))} contracts</span><span>${escapeHtml(cents(trade.entry_price))} entry</span><span>${escapeHtml(money(trade.exposure))} exposure</span></div>
      <p class="threshold-breach-state">${escapeHtml(["TEXAS_HOLDEM", "TEXAS_HOLDEM_2_0"].includes(String(trade.strategy).toUpperCase()) ? texasExitText(trade.texas_holdem_exit || {}) : thresholdBreachExitText(trade.threshold_breach_exit || {}))}</p>
    </article>`;
  }).join("");
}

function render(data) {
  const mode = String(data?.mode || "PAPER").toUpperCase();
  $("#environment").textContent = mode === "LIVE" ? "Live" : mode === "DEMO" ? "Demo" : "Paper";
  $("#trades-label").textContent = mode;
  $("#last-updated").textContent = data?.updated_at ? `Updated ${timeLabel(data.updated_at)}` : "Waiting for data";
  $("#market-to-beat").textContent = price(data?.market?.to_beat);
  $("#market-btc-proxy").textContent = price(data?.market?.btc_proxy);
  $("#market-btc-margin").textContent = `${signedMoney(data?.market?.btc_margin)} to beat`;
  const remaining = Number(data?.market?.time_remaining_seconds);
  timerDeadline = Number.isFinite(remaining) ? Date.now() + Math.max(0, remaining) * 1000 : null;
  updateTimer();
  renderHud(data?.readiness);
  renderTexasHud(data?.texas_holdem || {});
  renderOpenTrades(data?.open_trades || [], mode, data?.available_cash, data?.market);
  renderTrades(data?.recent_trades || [], mode);
}

function connect() {
  if (!navigator.onLine) {
    setConnection("Offline");
    return;
  }
  setConnection(socket ? "Reconnecting" : "Connecting");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws/live`);
  socket.addEventListener("open", () => {
    reconnectDelay = 1000;
    setConnection("Live");
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") render(message.data);
  });
  socket.addEventListener("close", () => {
    setConnection(navigator.onLine ? "Reconnecting" : "Offline");
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    window.setTimeout(connect, delay);
  });
  socket.addEventListener("error", () => socket.close());
}

window.addEventListener("online", () => {
  reconnectDelay = 1000;
  if (!socket || socket.readyState >= WebSocket.CLOSING) connect();
});
window.addEventListener("offline", () => setConnection("Offline"));

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
window.setInterval(updateTimer, 250);
connect();
