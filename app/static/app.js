const state = {
  dashboard: null,
  chartPoints: [],
  volatilityPoints: [],
  chartMode: "btc",
  maximumMvi: 0,
  chartWindow: 5,
  closeTime: null,
  lastNotification: null,
  activePage: "dashboard",
  liveSocket: null,
  liveConnected: false,
  liveRetryMs: 1000,
  chartAxis: { low: null, high: null, updatedAt: null },
  chartLastFrame: 0,
  chartTicker: null,
  priceMovement: { direction: null, until: 0 },
  themePreference: localStorage.getItem("kalshi-theme-v2") || "light",
  paperOrder: {
    side: localStorage.getItem("kalshi-display-side-v1") === "NO" ? "NO" : "YES",
    action: "BUY", limit: false, submitting: false,
    stopInitialized: false, sideUpdating: false, expanded: false,
  },
  paperReset: { confirming: false, resetting: false, timer: null },
  trading: {
    mode: "PAPER", pendingConfirmation: null, switching: false,
    armConfirmation: { mode: null, confirming: false, submitting: false, timer: null },
  },
  tradeReview: {
    tradeRef: null, mode: null, data: null, chartMode: "btc",
    selectedIndex: null, requestToken: 0,
  },
  calibration: {
    saved: null, defaults: null, dirty: false, controlsRendered: false,
    summary: null, evidence: null, evidenceUpdatedAt: 0, evidenceRequest: null,
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");

function syncThemeButtons() {
  $$('[data-theme-choice]').forEach((button) => {
    const selected = button.dataset.themeChoice === state.themePreference;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function applyTheme(preference = state.themePreference) {
  state.themePreference = preference;
  localStorage.setItem("kalshi-theme-v2", preference);
  const dark = preference === "dark" || (preference === "system" && themeMedia.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.dataset.themePreference = preference;
  syncThemeButtons();
  window.requestAnimationFrame(drawChart);
}

function money(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(Number(value));
}

function signedMoney(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : number < 0 ? "−" : ""}${money(Math.abs(number), digits)}`;
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
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

function cents(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}\u00a2`;
}

function paperFee(price, contracts = 1) {
  if (!Number.isFinite(price) || !Number.isFinite(contracts)) return 0;
  return Math.ceil(0.07 * contracts * price * (1 - price) * 10000) / 10000;
}

function marketSideLabel(side) {
  return String(side).toUpperCase() === "YES" ? "Up" : "Down";
}

function selectedTrading(data = state.dashboard) {
  const trading = data?.trading || {};
  const mode = trading.selected_mode || state.trading.mode || "PAPER";
  const selected = trading.selected || trading.modes?.[mode] || data?.paper || {};
  return { mode, selected, modes: trading.modes || {} };
}

function modeLabel(mode) {
  return mode === "LIVE" ? "Kalshi Live" : mode === "DEMO" ? "Kalshi Demo" : "Paper Trading";
}

function formatMarketLanguage(value) {
  return String(value || "")
    .replace(/\bNO TRADE\b/g, "HOLD")
    .replace(/\bTRADE YES\b/g, "UP")
    .replace(/\bTRADE NO\b/g, "DOWN")
    .replace(/\bYES\b/g, "UP")
    .replace(/\bNO\b/g, "DOWN");
}

function shortDate(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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

function forecastPosition(forecast) {
  if (forecast?.signal === "LIKELY_UP") return "up";
  if (forecast?.signal === "LIKELY_DOWN") return "down";
  return "uncertain";
}

function tradeActionLabel(decision) {
  if (!decision) return "--";
  const side = marketSideLabel(decision.side);
  if (decision.signal === "BUY") return `Buy ${side}`;
  if (decision.signal === "SPECULATIVE") return `Speculative ${side}`;
  if (decision.signal === "SELL") return `Sell ${side}`;
  return `Hold ${side}`;
}

function normalizeBookLevels(levels) {
  if (!Array.isArray(levels)) return [];
  return levels
    .map((level) => ({ price: Number(level?.[0]), quantity: Number(level?.[1]) }))
    .filter((level) => Number.isFinite(level.price) && level.price > 0 && level.price < 1
      && Number.isFinite(level.quantity) && level.quantity > 0)
    .slice(0, 5);
}

function orderBookRows(levels, type, maxQuantity, reverse = false) {
  const rows = levels.map((level) => {
    const depth = Math.max(3, Math.min(100, (level.quantity / maxQuantity) * 100));
    return `<tr class="book-row book-${type}" style="--depth:${depth.toFixed(1)}%">
      <td class="book-price"><span>${type}</span>${cents(level.price)}</td>
      <td>${compact(level.quantity)}</td>
      <td>${money(level.price * level.quantity)}</td>
    </tr>`;
  });
  return reverse ? rows.reverse() : rows;
}

function renderOrderBook(outcome, orderbook, environment = "LIVE") {
  const dataPrefix = outcome === "YES" ? "yes" : "no";
  const viewPrefix = outcome === "YES" ? "up" : "down";
  const target = $(`#${viewPrefix}-orderbook-rows`);
  if (!target) return;
  const source = String(environment || "LIVE").toUpperCase() === "DEMO" ? "Demo" : "Live";
  const sourceLabel = $(`#${viewPrefix}-orderbook-environment-label`);
  if (sourceLabel) sourceLabel.textContent = source;
  const bids = normalizeBookLevels(orderbook?.[`${dataPrefix}_bids`]);
  const asks = normalizeBookLevels(orderbook?.[`${dataPrefix}_asks`]);
  if (!bids.length && !asks.length) {
    target.innerHTML = `<tr><td class="book-empty" colspan="3">No ${source} depth available</td></tr>`;
    return;
  }
  const maxQuantity = Math.max(1, ...bids.map((level) => level.quantity), ...asks.map((level) => level.quantity));
  const bestBid = bids[0]?.price;
  const bestAsk = asks[0]?.price;
  const spread = Number.isFinite(bestBid) && Number.isFinite(bestAsk) ? bestAsk - bestBid : null;
  const spreadText = Number.isFinite(bestBid) && Number.isFinite(bestAsk)
    ? `Bid ${cents(bestBid)} <b>${spread >= 0 ? `${cents(spread)} spread` : "Crossed"}</b> Ask ${cents(bestAsk)}`
    : `${Number.isFinite(bestBid) ? `Bid ${cents(bestBid)}` : "No bid"} <b>Market</b> ${Number.isFinite(bestAsk) ? `Ask ${cents(bestAsk)}` : "No ask"}`;
  target.innerHTML = [
    ...orderBookRows(asks, "ask", maxQuantity, true),
    `<tr class="book-spread"><td colspan="3">${spreadText}</td></tr>`,
    ...orderBookRows(bids, "bid", maxQuantity),
  ].join("");
}

function renderRecentTrades(trades, mode = "PAPER") {
  const target = $("#recent-paper-trades");
  if (!target) return;
  target.innerHTML = trades?.length ? trades.map((trade) => `
    <tr><td>${shortDate(trade.activity_at || trade.opened_at || trade.filled_at)}</td><td>${marketSideLabel(trade.side)}</td>
    <td>${cents(trade.entry_price ?? trade.price)}</td><td>${trade.contracts}</td>
    <td class="${Number(trade.settlement_margin) > 0 ? "positive" : Number(trade.settlement_margin) < 0 ? "negative" : ""}">${signedMoney(trade.settlement_margin)}</td>
    <td>${String(trade.strategy || trade.source || "manual").replaceAll("_", " ")}</td>
    <td class="${Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : ""}">${String(trade.display_status || trade.status || (mode === "PAPER" ? "open" : trade.action || "filled"))}${trade.realized_pnl == null ? "" : ` · ${money(trade.realized_pnl)}`}</td>
    <td>${trade.available_cash_after == null ? "—" : money(trade.available_cash_after)}</td></tr>
  `).join("") : `<tr><td class="book-empty" colspan="8">No ${mode === "PAPER" ? "paper trades" : "fills"} yet</td></tr>`;
}

function dashboardAllocatedCapital(mode, portfolio = {}) {
  if (mode !== "PAPER") return numberOrNull(portfolio.allocated_capital) ?? 0;
  const positionCapital = (portfolio.positions || []).reduce(
    (total, position) => total + Number(position.committed_dollars || 0), 0,
  );
  return positionCapital + Number(portfolio.reserved_cash || 0);
}

function renderDashboardOpenTrades(mode, portfolio = {}) {
  $("#dashboard-available-balance").textContent = money(portfolio.available_cash);
  $("#dashboard-allocated-balance").textContent = money(
    dashboardAllocatedCapital(mode, portfolio),
  );
  const positions = portfolio.positions || [];
  const restingOrders = portfolio.open_orders || [];
  const target = $("#dashboard-open-trades");
  if (!positions.length && !restingOrders.length) {
    target.innerHTML = '<p class="dashboard-open-trades-empty">No open trades.</p>';
    return;
  }
  const positionRows = positions.map((position) => {
    const side = String(position.side || "").toUpperCase();
    const strategy = position.strategy
      || position.source
      || position.entries?.[0]?.strategy
      || position.entries?.[0]?.source
      || "Manual";
    const entryPrice = position.entry_price ?? position.average_price;
    const exposure = position.committed_dollars ?? position.market_exposure;
    const status = position.display_status || position.status || "Open";
    return `<article class="dashboard-open-trade">
      <strong class="${side === "YES" ? "yes" : "no"}" title="${escapeHtml(position.ticker || "Current market")}">${escapeHtml(marketSideLabel(side))} · ${escapeHtml(String(strategy).replaceAll("_", " "))}</strong>
      <small>${escapeHtml(status)}</small>
      <p>${escapeHtml(compact(position.contracts))} contracts · ${escapeHtml(cents(entryPrice))} entry · ${escapeHtml(money(exposure))} exposure</p>
    </article>`;
  });
  const orderRows = restingOrders.map((order) => {
    const side = String(order.side || "").toUpperCase();
    const quantity = order.remaining_contracts ?? order.requested_contracts;
    const status = order.display_status || order.status || "Resting";
    return `<article class="dashboard-open-trade resting">
      <strong class="${side === "YES" ? "yes" : "no"}" title="${escapeHtml(order.ticker || "Current market")}">${escapeHtml(order.action || "Order")} ${escapeHtml(marketSideLabel(side))} · Limit</strong>
      <small>${escapeHtml(status)}</small>
      <p>${escapeHtml(compact(quantity))} contracts · ${escapeHtml(cents(order.limit_price))} limit</p>
    </article>`;
  });
  target.innerHTML = [...positionRows, ...orderRows].join("");
}

function renderTradeAssessment() {
  const current = state.dashboard?.current;
  const side = state.paperOrder.side;
  const action = state.paperOrder.action.toLowerCase();
  const decision = current?.trade_decisions?.[side]
    || (current?.trade_decision?.side === side ? current.trade_decision : null);
  const assessment = current?.trade_assessments?.[side];
  const economics = assessment?.[action];
  $("#trade-assessment-side").textContent = `Selected: ${marketSideLabel(side)}`;
  $("#trade-action").textContent = tradeActionLabel(decision);
  $("#trade-confidence").textContent = decision?.confidence
    || assessment?.decision_confidence || "--";
  $("#trade-open-interest").textContent = compact(current?.open_interest);
  $("#trade-volume").textContent = compact(current?.volume);
  $("#trade-fee").textContent = economics?.fee_per_contract == null ? "--" : money(economics.fee_per_contract, 4);
  $("#trade-slippage").textContent = cents(economics?.slippage);
  $("#trade-edge").textContent = points(economics?.net_edge);
  $("#trade-ev").textContent = economics?.expected_value == null ? "--" : money(economics.expected_value, 3);
  const verb = state.paperOrder.action === "BUY" ? "buying" : "selling";
  $("#trade-explanation").textContent = economics?.expected_value == null
    ? formatMarketLanguage(decision?.explanation || "Waiting for an executable quote.")
    : `${marketSideLabel(side)} ${verb} value after the displayed fee and slippage allowance.`;
  $("#trade-edge").className = Number(economics?.net_edge) > 0 ? "positive" : Number(economics?.net_edge) < 0 ? "negative" : "";
  $("#trade-ev").className = Number(economics?.expected_value) > 0 ? "positive" : Number(economics?.expected_value) < 0 ? "negative" : "";
}

function renderReadinessProgress(trackSelector, metric, locked = false) {
  const track = $(trackSelector);
  if (!track) return;
  const progress = Math.max(0, Math.min(1, Number(metric?.progress || 0)));
  track.style.setProperty("--progress", `${(progress * 100).toFixed(1)}%`);
  track.classList.toggle("passed", Boolean(metric?.passed));
  track.classList.toggle("locked", Boolean(locked));
  track.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
  track.setAttribute("aria-valuetext", locked ? "Locked" : `${Math.round(progress * 100)}%`);
}

function renderReadinessGate(selector, gate, value) {
  const element = $(selector);
  if (!element) return;
  element.classList.toggle("pass", Boolean(gate?.passed));
  element.querySelector("small").textContent = value;
  element.title = gate?.detail || "";
}

function renderStandardEdgeHud(readiness) {
  const hud = $("#standard-edge-hud");
  if (!hud) return;
  const metrics = readiness?.metrics || {};
  const probability = metrics.probability || {};
  const netEv = metrics.net_ev || {};
  const confirmation = metrics.confirmation || {};
  const gates = readiness?.gates || {};
  const status = String(readiness?.status || "WATCHING").toLowerCase();

  hud.dataset.status = status;
  $("#standard-edge-hud-side").textContent = readiness?.side
    ? marketSideLabel(readiness.side)
    : "--";
  const statusElement = $("#standard-edge-hud-status");
  statusElement.dataset.status = status;
  statusElement.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  $("#standard-edge-probability-value").textContent = probability.current == null
    ? `-- / ${percent(probability.required, 0)}`
    : `${percent(probability.current, 1)} / ${percent(probability.required, 0)}`;
  $("#standard-edge-ev-value").textContent = netEv.current == null
    ? `-- / ${cents(netEv.required, 1)}`
    : `${cents(netEv.current, 1)} / ${cents(netEv.required, 1)}`;
  $("#standard-edge-confirmation-value").textContent = confirmation.locked
    ? "Locked"
    : `${Number(confirmation.current_seconds || 0).toFixed(1)} / ${Number(confirmation.required_seconds || 0).toFixed(0)}s`;
  renderReadinessProgress("#standard-edge-probability-track", probability);
  renderReadinessProgress("#standard-edge-ev-track", netEv);
  renderReadinessProgress(
    "#standard-edge-confirmation-track", confirmation, Boolean(confirmation.locked),
  );

  const spread = gates.spread || {};
  const spreadValue = spread.current == null
    ? "--"
    : spread.required == null ? cents(spread.current, 1) : `${cents(spread.current, 1)} / ${cents(spread.required, 1)}`;
  const liquidity = gates.liquidity || {};
  const liquidityValue = liquidity.current == null
    ? "--"
    : `${compact(liquidity.current)} / ${compact(liquidity.required)}`;
  renderReadinessGate("#standard-edge-spread-gate", spread, spreadValue);
  renderReadinessGate("#standard-edge-liquidity-gate", liquidity, liquidityValue);
  renderReadinessGate("#standard-edge-data-gate", gates.data, gates.data?.passed ? "Fresh" : "Blocked");
  const quality = gates.quality || {};
  const qualityValue = quality.current == null
    ? "--"
    : `${quality.current} / ${quality.required || "--"}`;
  renderReadinessGate("#standard-edge-quality-gate", quality, qualityValue);
  const threshold = gates.threshold_margin || {};
  const thresholdValue = threshold.enabled === false
    ? "Off"
    : threshold.current == null
      ? `-- / ${signedMoney(threshold.required, 0)}`
      : `${signedMoney(threshold.current, 0)} / ${signedMoney(threshold.required, 0)}`;
  renderReadinessGate("#standard-edge-threshold-gate", threshold, thresholdValue);
  const volatility = gates.volatility || {};
  state.maximumMvi = Number(volatility.required || 0);
  const volatilityValue = volatility.enabled === false
    ? "Off"
    : volatility.current == null
      ? `${volatility.status === "LEARNING" ? "Learning" : "--"} / ${Number(volatility.required || 0).toFixed(1)}`
      : `${Number(volatility.current).toFixed(1)} / ${Number(volatility.required || 0).toFixed(1)}`;
  renderReadinessGate("#standard-edge-volatility-gate", volatility, volatilityValue);
  const cushion = numberOrNull(volatility.cushion_ratio);
  $("#standard-edge-cushion").textContent = cushion == null
    ? "Cushion --"
    : `Cushion ${cushion > 99 ? ">99" : cushion.toFixed(1)}× expected move`;
  renderReadinessGate("#standard-edge-risk-gate", gates.risk, gates.risk?.passed ? "Clear" : "Blocked");
  $("#standard-edge-hud-blocker").textContent = readiness?.blocker
    || "Waiting for live trade data.";
}

function renderDashboard(data) {
  state.dashboard = data;
  const trading = selectedTrading(data);
  state.trading.mode = trading.mode;
  document.documentElement.dataset.tradingMode = trading.mode.toLowerCase();
  const liveArmed = Boolean(trading.modes?.LIVE?.readiness?.session_armed);
  const liveIndicator = $("#live-armed-indicator");
  liveIndicator.hidden = !liveArmed;
  liveIndicator.textContent = trading.modes?.LIVE?.readiness?.automatic_armed
    ? "LIVE AUTO ARMED" : "LIVE ARMED";
  $$('[data-trading-mode]').forEach((button) => {
    const active = button.dataset.tradingMode === trading.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const system = data.system || {};
  const current = data.current;
  const btc = data.btc || {};
  const forecast = current?.forecast;
  const streams = system.streams || {};
  if (current?.ticker && current.ticker !== state.chartTicker) {
    state.chartTicker = current.ticker;
    resetChartAxis();
  }
  const statusDot = $("#sidebar-status-dot");
  statusDot.className = `status-dot ${system.status || "degraded"}`;
  $("#sidebar-status").textContent = state.liveConnected
    ? "Streaming live"
    : system.status === "live" ? "REST fallback" : "Data guarded";
  $("#last-update").textContent = system.updated_at ? `Updated ${new Date(system.updated_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : "Connecting";
  $("#header-model-version").textContent = data.model?.version || "baseline-1.0";

  const position = forecastPosition(forecast);
  const pill = $("#signal-pill");
  pill.dataset.position = position;
  const forecastLabel = position === "up" ? "Likely Up" : position === "down" ? "Likely Down" : "Uncertain";
  pill.setAttribute("aria-label", `Outcome forecast: ${forecastLabel}`);
  pill.querySelectorAll("[data-signal-option]").forEach((option) => {
    option.classList.toggle("active", option.dataset.signalOption === position);
  });
  const paper = trading.selected || {};
  const modeName = modeLabel(trading.mode);
  const readiness = paper.readiness || {};
  $("#dashboard-trading-label").textContent = modeName.toUpperCase();
  $("#recent-trades-label").textContent = modeName.toUpperCase();
  $("#recent-trades-badge").textContent = trading.mode === "PAPER" ? "SIMULATED" : trading.mode;
  $("#recent-trades-badge").classList.toggle("live", trading.mode === "LIVE");
  renderDashboardOpenTrades(trading.mode, paper);
  $("#signal-explanation").textContent = forecast?.explanation || system.message || "Connecting to public feeds.";
  $("#up-probability").textContent = percent(forecast?.up_probability, 1);
  $("#down-probability").textContent = percent(forecast?.down_probability, 1);

  const referencePrice = numberOrNull(btc.price);
  const threshold = numberOrNull(current?.strike);
  const distance = referencePrice !== null && threshold !== null ? referencePrice - threshold : null;
  $("#chart-to-beat").textContent = money(threshold);
  $("#btc-price").textContent = money(referencePrice);
  syncPriceMovement();
  $("#chart-now-distance").textContent = threshold === null
    ? "Waiting for threshold"
    : distance === null ? "Waiting for proxy price"
    : `${distance > 0 ? "+" : ""}${money(distance)} (${percent(distance / threshold, 3, true)})`;
  $("#btc-dispersion").textContent = btc.price
    ? `${btc.exchange_count} feeds · ${Number(btc.dispersion_pct || 0).toFixed(3)}% dispersion`
    : "No composite available";
  $("#composite-source").textContent = btc.quotes?.map((quote) => quote.exchange).join(" · ") || "Multi-exchange median";

  state.closeTime = current?.close_time ? new Date(current.close_time) : null;
  updateCountdown();
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

  renderStandardEdgeHud(
    current?.standard_edge_readiness
      || current?.automatic_entry?.standard_edge_readiness,
  );
  syncArmButton(readiness, trading.mode);
  renderTradeAssessment();
  renderOrderBook("YES", current?.orderbook || {}, current?.execution_market_mode || "LIVE");
  renderOrderBook("NO", current?.orderbook || {}, current?.execution_market_mode || "LIVE");
  const recent = trading.mode === "PAPER"
    ? data.paper?.recent_paper_trades || []
    : (paper.ledger || []).slice(0, 8);
  renderRecentTrades(recent, trading.mode);
  renderPaperController();
  drawChart();
  if (data.notification?.signal_id && data.notification.signal_id !== state.lastNotification) {
    state.lastNotification = data.notification.signal_id;
    showToast(formatMarketLanguage(data.notification.title), formatMarketLanguage(data.notification.detail));
  }
}

function paperQuote(side = state.paperOrder.side, action = state.paperOrder.action) {
  const current = state.dashboard?.current;
  const value = current?.[`${side.toLowerCase()}_${action === "BUY" ? "ask" : "bid"}`];
  return Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : null;
}

function paperAvailableContracts(side = state.paperOrder.side) {
  const paper = selectedTrading().selected || {};
  const ticker = state.dashboard?.current?.ticker;
  const held = (paper.positions || [])
    .filter((position) => position.ticker === ticker && position.side === side)
    .reduce((total, position) => total + Number(position.contracts || 0), 0);
  const reserved = (paper.open_orders || [])
    .filter((order) => order.ticker === ticker && order.side === side && order.action === "SELL")
    .reduce((total, order) => total + Number(
      order.remaining_contracts ?? order.requested_contracts ?? 0
    ), 0);
  return Math.max(0, held - reserved);
}

function paperOrderDraft() {
  const { mode, selected: paper } = selectedTrading();
  const current = state.dashboard?.current;
  const bestPrice = paperQuote();
  const available = Number(paper.available_cash || 0);
  let contracts = 0;
  let referencePrice = bestPrice;
  let requestedValue = 0;
  let error = "";
  const stopValue = $("#paper-stop-loss").value.trim();

  if (!current?.ticker) error = "Waiting for an active Kalshi contract.";
  else if (!current?.data_quality?.reliable) error = current?.data_quality?.reason || "Market data is not reliable.";

  if (state.paperOrder.limit) {
    const rawContracts = Number($("#paper-contracts").value);
    const limitCents = Number($("#paper-limit-price").value);
    contracts = Number.isInteger(rawContracts) ? rawContracts : 0;
    referencePrice = limitCents / 100;
    if (!error && (!Number.isInteger(rawContracts) || rawContracts < 1)) error = "Enter a whole number of contracts.";
    if (!error && (!Number.isFinite(limitCents) || limitCents < 1 || limitCents > 99)) error = "Enter a limit price from 1 to 99 cents.";
    const marketable = bestPrice !== null && (
      (state.paperOrder.action === "BUY" && bestPrice <= referencePrice)
      || (state.paperOrder.action === "SELL" && bestPrice >= referencePrice)
    );
    if (marketable) referencePrice = bestPrice;
  } else {
    const dollars = Number($("#paper-dollars").value);
    requestedValue = Number.isFinite(dollars) ? dollars : 0;
    if (!error && requestedValue <= 0) error = "Enter a dollar amount.";
    if (!error && bestPrice === null) error = "No executable price is available.";
    if (bestPrice !== null && requestedValue > 0) {
      const unitValue = state.paperOrder.action === "BUY" ? bestPrice + paperFee(bestPrice) : bestPrice;
      contracts = Math.floor(requestedValue / unitValue);
      if (!error && contracts < 1) error = "Amount is too small for one contract.";
    }
  }

  const fee = referencePrice && contracts ? paperFee(referencePrice, contracts) : 0;
  const gross = referencePrice && contracts ? referencePrice * contracts : 0;
  const orderValue = state.paperOrder.action === "BUY" ? gross + fee : Math.max(0, gross - fee);
  if (!requestedValue) requestedValue = orderValue;
  if (!error && state.paperOrder.action === "BUY" && orderValue > available + 0.000001) {
    error = "Order exceeds the remaining bankroll.";
  }
  if (!error && mode === "PAPER" && state.paperOrder.action === "BUY" && paper.risk_controls_enabled) {
    const bankroll = Number(paper.current_bankroll || 0);
    if (Number(paper.session_drawdown_pct || 0) >= Number(paper.max_session_drawdown_pct || 0)) {
      error = "The session drawdown limit is active.";
    } else if (orderValue > bankroll * Number(paper.max_risk_per_trade_pct || 0) + 0.000001) {
      error = "Order exceeds the maximum risk per trade.";
    } else {
      const ticker = current?.ticker;
      const committed = (paper.positions || [])
        .filter((position) => position.ticker === ticker && position.side === state.paperOrder.side)
        .reduce((total, position) => total + Number(position.committed_dollars || 0), 0);
      const pending = (paper.open_orders || [])
        .filter((order) => order.ticker === ticker && order.side === state.paperOrder.side && order.action === "BUY")
        .reduce((total, order) => {
          const price = Number(order.limit_price || 0);
          const count = Number(order.requested_contracts || 0);
          return total + price * count + paperFee(price, count);
        }, 0);
      if (committed + pending + orderValue > bankroll * Number(paper.max_position_pct || 0) + 0.000001) {
        error = "Order exceeds the maximum position size.";
      }
    }
  }
  if (!error && state.paperOrder.action === "SELL" && contracts > paperAvailableContracts()) {
    error = `Only ${paperAvailableContracts()} ${marketSideLabel(state.paperOrder.side)} contracts are available to sell.`;
  }
  if (!error && state.paperOrder.action === "BUY" && stopValue !== "") {
    const stopCents = Number(stopValue);
    if (!Number.isFinite(stopCents) || (stopCents !== 0 && (stopCents < 1 || stopCents > 99))) {
      error = "Stop-loss must be 0 (off) or between 1 and 99 cents.";
    }
  }
  if (!error && mode !== "PAPER") {
    const readiness = paper.readiness || {};
    if (!readiness.ready_for_manual) error = readiness.blocker || `${modeLabel(mode)} is not armed.`;
    else if (state.paperOrder.action === "BUY" && orderValue > Number(paper.remaining_allocation || 0) + 0.000001) {
      error = "Order exceeds the remaining allocation.";
    }
  }
  return { available, bestPrice, contracts, orderValue, requestedValue, error };
}

function renderOpenPaperOrders() {
  const { mode, selected } = selectedTrading();
  const orders = selected?.open_orders || [];
  const panel = $("#open-paper-orders");
  panel.hidden = orders.length === 0;
  $("#open-order-count").textContent = orders.length;
  $("#open-order-list").innerHTML = orders.map((order) => `
    <div class="open-order-row">
      <span><strong>${order.action} ${marketSideLabel(order.side).toUpperCase()}</strong><small>${order.requested_contracts} at ${cents(order.limit_price)}</small></span>
      ${order.stop_loss_price == null ? "" : `<small>Stop ${cents(order.stop_loss_price)}</small>`}
      <button type="button" data-cancel-paper-order="${order.id}" data-exchange-order-id="${order.exchange_order_id || ""}" aria-label="Cancel ${marketSideLabel(order.side)} limit order">Cancel</button>
    </div>
  `).join("");
}

function renderPaperController() {
  const current = state.dashboard?.current;
  const { mode, selected: portfolio } = selectedTrading();
  const action = state.paperOrder.action;
  $$('[data-paper-action]').forEach((button) => button.classList.toggle("active", button.dataset.paperAction === action));
  const expanded = Boolean(state.paperOrder.expanded);
  const controller = $(".paper-controller");
  controller.classList.toggle("manual-collapsed", !expanded);
  $("#manual-order-ticket").hidden = !expanded;
  const manualToggle = $("#manual-order-toggle");
  manualToggle.textContent = expanded ? "Collapse manual order" : "Manual order";
  manualToggle.setAttribute("aria-expanded", String(expanded));
  $$('[data-paper-side]').forEach((button) => {
    const selected = button.dataset.paperSide === state.paperOrder.side;
    button.classList.toggle("active", expanded && selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("#paper-limit-toggle").checked = state.paperOrder.limit;
  $("#paper-market-fields").hidden = state.paperOrder.limit;
  $("#paper-limit-fields").hidden = !state.paperOrder.limit;
  const stopField = $("#paper-stop-field");
  stopField.hidden = action !== "BUY";
  $("#paper-stop-loss").disabled = action !== "BUY";
  if (action === "BUY" && !state.paperOrder.stopInitialized) {
    const globalStop = state.dashboard?.paper?.default_stop_loss_cents;
    $("#paper-stop-loss").value = globalStop == null ? "" : globalStop;
    state.paperOrder.stopInitialized = true;
  }
  $("#paper-up-price").textContent = cents(paperQuote("YES", action));
  $("#paper-down-price").textContent = cents(paperQuote("NO", action));
  const draft = paperOrderDraft();
  $("#paper-best-price").textContent = `Best ${action === "BUY" ? "ask" : "bid"} ${cents(draft.bestPrice)}`;
  $("#paper-bankroll").textContent = money(draft.available);
  $("#paper-order-value").textContent = draft.orderValue > 0 ? money(draft.orderValue) : "--";
  $("#paper-bankroll-pct").textContent = draft.available > 0 && draft.requestedValue > 0
    ? percent(draft.requestedValue / draft.available, 1)
    : "--";
  $("#paper-estimate").textContent = draft.contracts > 0
    ? `${draft.contracts} contract${draft.contracts === 1 ? "" : "s"} · ${state.paperOrder.limit ? (draft.bestPrice && ((action === "BUY" && draft.bestPrice <= Number($("#paper-limit-price").value) / 100) || (action === "SELL" && draft.bestPrice >= Number($("#paper-limit-price").value) / 100)) ? "fills now" : "rests until matched") : "at current price"}`
    : "Enter an amount";
  $("#paper-order-message").textContent = draft.error;
  const submit = $("#paper-submit");
  submit.textContent = `${action === "BUY" ? "Buy" : "Sell"} ${marketSideLabel(state.paperOrder.side)}`;
  submit.dataset.side = state.paperOrder.side.toLowerCase();
  submit.dataset.action = action.toLowerCase();
  submit.disabled = Boolean(draft.error) || draft.contracts < 1 || state.paperOrder.submitting || !current;
  submit.classList.toggle("live", mode === "LIVE");
  $("#paper-limit-toggle").closest("label").querySelector("span").textContent = mode === "PAPER" ? "Limit order" : "Custom limit";
  $("#paper-bankroll").textContent = money(portfolio?.available_cash);
  renderOpenPaperOrders();
}

async function submitPaperOrder() {
  if (state.paperOrder.submitting) return;
  state.paperOrder.submitting = true;
  renderPaperController();
  const payload = {
    side: state.paperOrder.side,
    action: state.paperOrder.action,
    order_type: state.paperOrder.limit ? "limit" : "market",
  };
  if (state.paperOrder.limit) {
    payload.contracts = Number($("#paper-contracts").value);
    payload.limit_price_cents = Number($("#paper-limit-price").value);
  } else payload.dollars = Number($("#paper-dollars").value);
  if (state.paperOrder.action === "BUY" && $("#paper-stop-loss").value.trim() !== "") {
    payload.stop_loss_cents = Number($("#paper-stop-loss").value);
  }
  try {
    const { mode } = selectedTrading();
    if (mode !== "PAPER") {
      const preview = await api(`/api/trading/${mode}/orders/preview`, {
        method: "POST", body: JSON.stringify(payload),
      });
      state.trading.pendingConfirmation = preview;
      $("#confirmation-environment").textContent = `${preview.environment} ORDER REVIEW`;
      $("#confirmation-details").innerHTML = [
        ["Account", preview.account], ["Market", preview.market],
        ["Order", `${preview.action} ${preview.quantity} ${preview.contract}`],
        ["Worst price", cents(preview.limit_price)],
        ["Maximum exposure", money(preview.maximum_cash_exposure)],
        ["Estimated fees", money(preview.estimated_fees, 4)],
        ["Slippage", cents(preview.slippage_allowance)],
        ["Stop-loss", typeof preview.stop_loss === "number" ? cents(preview.stop_loss) : preview.stop_loss],
        ["Profit take", typeof preview.global_profit_take === "number" ? cents(preview.global_profit_take) : preview.global_profit_take],
        ["Remaining allocation", money(preview.remaining_allocation)],
        ["Risk review", preview.risk?.passed ? "Passed" : preview.risk?.primary_blocker || "Blocked"],
      ].map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("");
      $("#confirm-exchange-order").disabled = !preview.risk?.passed;
      $("#trade-confirmation").classList.toggle("live", mode === "LIVE");
      $("#trade-confirmation").showModal();
      return;
    }
    const result = await api("/api/paper/orders", { method: "POST", body: JSON.stringify(payload) });
    const status = result.order?.status === "filled" ? "filled" : "placed";
    showToast(`Paper order ${status}`, `${result.order.action} ${result.order.requested_contracts} ${marketSideLabel(result.order.side)} contract${result.order.requested_contracts === 1 ? "" : "s"}.`);
    $("#paper-dollars").value = "";
    $("#paper-contracts").value = "";
    $("#paper-limit-price").value = "";
    state.paperOrder.stopInitialized = false;
    await refreshDashboard();
    if (state.activePage === "paper") await loadPaper();
  } catch (error) {
    $("#paper-order-message").textContent = error.message;
    showToast("Paper order not placed", error.message);
  } finally {
    state.paperOrder.submitting = false;
    renderPaperController();
  }
}

async function confirmExchangeOrder() {
  const preview = state.trading.pendingConfirmation;
  if (!preview) return;
  const button = $("#confirm-exchange-order");
  button.disabled = true;
  try {
    const result = await api(`/api/trading/${preview.environment}/orders/confirm`, {
      method: "POST",
      body: JSON.stringify({ confirmation_token: preview.confirmation_token }),
    });
    $("#trade-confirmation").close();
    state.trading.pendingConfirmation = null;
    showToast(`${preview.environment} order submitted`, `${preview.action} ${preview.quantity} ${preview.contract} at no worse than ${cents(preview.limit_price)}.`);
    $("#paper-dollars").value = "";
    $("#paper-contracts").value = "";
    $("#paper-limit-price").value = "";
    await refreshDashboard();
    if (state.activePage === "paper") await loadPaper();
    return result;
  } catch (error) {
    showToast("Order not submitted", error.message);
  } finally {
    button.disabled = !state.trading.pendingConfirmation?.risk?.passed;
  }
}

async function cancelPaperOrder(orderId) {
  try {
    const { mode } = selectedTrading();
    const selector = `[data-cancel-paper-order="${orderId}"]`;
    const exchangeOrderId = $(selector)?.dataset.exchangeOrderId;
    const path = mode === "PAPER"
      ? `/api/paper/orders/${orderId}`
      : `/api/trading/${mode}/orders/${exchangeOrderId || orderId}`;
    await api(path, { method: "DELETE" });
    showToast("Limit order canceled", "Reserved funds or contracts are available again after reconciliation.");
    await refreshDashboard();
  } catch (error) { showToast("Unable to cancel order", error.message); }
}

function updateSelectedSide(side) {
  if (side === state.paperOrder.side) return;
  state.paperOrder.side = side;
  localStorage.setItem("kalshi-display-side-v1", side);
  renderPaperController();
  renderTradeAssessment();
}

function toggleManualOrder(expanded = !state.paperOrder.expanded) {
  state.paperOrder.expanded = Boolean(expanded);
  renderPaperController();
}

function handlePaperSide(side) {
  if (!state.paperOrder.expanded) {
    updateSelectedSide(side);
    toggleManualOrder(true);
    return;
  }
  if (side === state.paperOrder.side) {
    toggleManualOrder(false);
    return;
  }
  updateSelectedSide(side);
}

async function selectTradingMode(mode) {
  if (state.trading.switching || mode === state.trading.mode) return;
  resetArmConfirmation();
  state.trading.switching = true;
  try {
    await api("/api/trading/mode", {
      method: "PUT", body: JSON.stringify({ mode }),
    });
    state.trading.mode = mode;
    state.paperOrder.stopInitialized = false;
    await refreshDashboard();
    if (state.activePage === "paper") await loadPaper();
  } catch (error) {
    showToast("Trading mode unchanged", error.message);
  } finally {
    state.trading.switching = false;
  }
}

async function reconcileSelectedTrading() {
  const { mode } = selectedTrading();
  if (mode === "PAPER") return;
  resetArmConfirmation();
  try {
    await api(`/api/trading/${mode}/reconcile`, { method: "POST" });
    await refreshDashboard();
    await loadPaper();
    showToast(`${modeLabel(mode)} reconciled`, "Balances, positions, orders, and fills are synchronized.");
  } catch (error) { showToast("Reconciliation failed", error.message); }
}

function syncArmButton(readiness = selectedTrading().selected?.readiness || {}, mode = selectedTrading().mode) {
  const confirmation = state.trading.armConfirmation;
  const pending = confirmation.confirming && confirmation.mode === mode && !readiness.session_armed;
  $$('[data-arm-session]').forEach((button) => {
    button.hidden = button.id === "hud-arm-trading" && mode === "PAPER";
    button.classList.toggle("confirming", pending);
    button.classList.toggle("armed", Boolean(readiness.session_armed));
    button.disabled = confirmation.submitting;
    button.textContent = readiness.session_armed
      ? "Disarm session"
      : confirmation.submitting ? "Arming…" : pending ? "Confirm" : "Arm session";
  });
}

function resetArmConfirmation() {
  const confirmation = state.trading.armConfirmation;
  clearTimeout(confirmation.timer);
  confirmation.mode = null;
  confirmation.confirming = false;
  confirmation.submitting = false;
  confirmation.timer = null;
  syncArmButton();
}

async function armSelectedTrading() {
  const { mode, selected } = selectedTrading();
  if (mode === "PAPER") return;
  if (selected?.readiness?.session_armed) {
    resetArmConfirmation();
    await api(`/api/trading/${mode}/disarm`, { method: "POST" });
    await refreshDashboard();
    await loadPaper();
    return;
  }

  const confirmationState = state.trading.armConfirmation;
  if (!confirmationState.confirming || confirmationState.mode !== mode) {
    resetArmConfirmation();
    confirmationState.mode = mode;
    confirmationState.confirming = true;
    syncArmButton(selected?.readiness, mode);
    showToast(
      `Arm ${modeLabel(mode)}?`,
      "Click Confirm within 6 seconds to authorize this session.",
    );
    confirmationState.timer = setTimeout(resetArmConfirmation, 6000);
    return;
  }

  clearTimeout(confirmationState.timer);
  confirmationState.submitting = true;
  syncArmButton(selected?.readiness, mode);
  const phrase = mode === "LIVE" ? "ARM LIVE TRADING" : "ARM DEMO TRADING";
  try {
    await api(`/api/trading/${mode}/arm`, {
      method: "POST", body: JSON.stringify({ confirmation: phrase, automatic: false }),
    });
    await refreshDashboard();
    await loadPaper();
    showToast(`${modeLabel(mode)} armed`, "Manual limit orders are now authorized for this session.");
  } catch (error) {
    showToast("Session not armed", error.message);
  } finally {
    resetArmConfirmation();
  }
}

async function killSelectedTrading() {
  const { mode, selected } = selectedTrading();
  if (mode === "PAPER") return;
  resetArmConfirmation();
  try {
    const release = Boolean(selected?.readiness?.kill_switch);
    await api(`/api/trading/${mode}/kill${release ? "/release" : ""}`, { method: "POST" });
    await refreshDashboard();
    await loadPaper();
    showToast(
      release ? `${modeLabel(mode)} kill switch released` : `${modeLabel(mode)} kill switch active`,
      release ? "The session remains disarmed." : "New submissions are blocked and resting-order cancellations were attempted.",
    );
  } catch (error) { showToast("Kill switch failed", error.message); }
}

async function toggleAutomaticTrading(event) {
  const { mode } = selectedTrading();
  if (mode === "PAPER") return;
  try {
    await api(`/api/trading/${mode}/automatic`, {
      method: "PUT", body: JSON.stringify({ enabled: event.target.checked }),
    });
    await refreshDashboard();
    await loadPaper();
  } catch (error) {
    event.target.checked = !event.target.checked;
    showToast("Automatic trading unchanged", error.message);
  }
}

function updateCountdown() {
  const value = state.closeTime
    ? countdown((state.closeTime.getTime() - Date.now()) / 1000)
    : "--:--";
  $("#chart-countdown").textContent = value;
}

function resetChartAxis() {
  state.chartAxis = { low: null, high: null, updatedAt: null };
}

function niceChartStep(range, targetTicks = 4) {
  const rough = Math.max(Number.EPSILON, range / targetTicks);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return factor * magnitude;
}

function chartTickInterval(windowMs, chartWidth) {
  const targetTicks = Math.max(3, Math.floor(chartWidth / 100));
  const rough = windowMs / targetTicks;
  const intervals = [
    1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000,
    300000, 600000, 900000, 1800000, 3600000,
  ];
  return intervals.find((interval) => interval >= rough) || intervals.at(-1);
}

function chartTimeLabel(timestamp, includeSeconds) {
  const date = new Date(timestamp);
  const hour = date.getHours() % 12 || 12;
  const minute = String(date.getMinutes()).padStart(2, "0");
  const second = String(date.getSeconds()).padStart(2, "0");
  return includeSeconds ? `${hour}:${minute}:${second}` : `${hour}:${minute}`;
}

function smoothChartAxis(targetLow, targetHigh, frameTime) {
  const axis = state.chartAxis;
  if (!Number.isFinite(axis.low) || !Number.isFinite(axis.high)) {
    axis.low = targetLow;
    axis.high = targetHigh;
    axis.updatedAt = frameTime;
    return axis;
  }
  const elapsed = Math.min(100, Math.max(0, frameTime - (axis.updatedAt ?? frameTime)));
  const contraction = 1 - Math.exp(-elapsed / 520);
  axis.low = targetLow < axis.low ? targetLow : axis.low + (targetLow - axis.low) * contraction;
  axis.high = targetHigh > axis.high ? targetHigh : axis.high + (targetHigh - axis.high) * contraction;
  axis.updatedAt = frameTime;
  return axis;
}

function drawVolatilityChart(context, width, height, color, numberFont) {
  const windowMs = state.chartWindow * 60 * 1000;
  const liveGutterMs = Math.min(10000, windowMs * 0.025);
  const viewEnd = Date.now() + liveGutterMs;
  const viewStart = viewEnd - windowMs;
  const points = state.volatilityPoints
    .map((point) => ({ ...point, time: new Date(point.observed_at).getTime(), value: Number(point.mvi) }))
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value)
      && point.time >= viewStart && point.time <= viewEnd);
  const left = 8;
  const right = width < 430 ? 52 : 60;
  const top = 14;
  const bottom = 32;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const plotRight = width - right;
  const x = (timestamp) => left + ((timestamp - viewStart) / windowMs) * chartWidth;
  const y = (value) => top + (1 - value / 10) * chartHeight;

  context.strokeStyle = color("--chart-grid");
  context.fillStyle = color("--chart-label");
  context.font = `10px ${numberFont}`;
  context.textAlign = "left";
  [0, 2.5, 5, 7.5, 10].forEach((value) => {
    const rowY = y(value);
    context.beginPath(); context.moveTo(left, rowY); context.lineTo(plotRight, rowY); context.stroke();
    context.fillText(value.toFixed(1), plotRight + 8, rowY + 3);
  });
  const timeInterval = chartTickInterval(windowMs, chartWidth);
  const firstTimeTick = Math.ceil(viewStart / timeInterval) * timeInterval;
  context.textAlign = "center";
  for (let timestamp = firstTimeTick; timestamp <= viewEnd; timestamp += timeInterval) {
    const columnX = x(timestamp);
    context.beginPath(); context.moveTo(columnX, top); context.lineTo(columnX, top + chartHeight); context.stroke();
    if (columnX >= left + 34 && columnX <= plotRight - 34) {
      context.fillText(chartTimeLabel(timestamp, state.chartWindow <= 5), columnX, height - 8);
    }
  }
  if (state.maximumMvi > 0) {
    const maximumY = y(Math.max(0, Math.min(10, state.maximumMvi)));
    context.save();
    context.strokeStyle = color("--red");
    context.setLineDash([4, 4]);
    context.beginPath(); context.moveTo(left, maximumY); context.lineTo(plotRight, maximumY); context.stroke();
    context.restore();
  }
  if (!points.length) {
    context.fillStyle = color("--chart-label");
    context.font = "12px -apple-system, sans-serif";
    context.textAlign = "center";
    context.fillText("Learning reliable margin volatility", width / 2, height / 2);
    return;
  }
  if (points.length > 1) {
    context.beginPath();
    points.forEach((point, index) => index === 0
      ? context.moveTo(x(point.time), y(point.value))
      : context.lineTo(x(point.time), y(point.value)));
    context.strokeStyle = color("--hud-warning");
    context.lineWidth = 2.25;
    context.lineJoin = "round";
    context.stroke();
  }
  const last = points.at(-1);
  context.beginPath(); context.arc(x(last.time), y(last.value), 3.25, 0, Math.PI * 2);
  context.fillStyle = color("--hud-warning"); context.fill();
}

function drawChart(frameTime = performance.now()) {
  const canvas = $("#price-chart");
  if (!canvas) return;
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, box.width);
  const height = Math.max(1, box.height);
  const pixelWidth = Math.floor(width * ratio);
  const pixelHeight = Math.floor(height * ratio);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const context = canvas.getContext("2d");
  const styles = getComputedStyle(document.documentElement);
  const color = (name) => styles.getPropertyValue(name).trim();
  const numberFont = color("--number-font");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const volatilityMode = state.chartMode === "volatility";
  $("#price-legend").hidden = volatilityMode;
  $("#threshold-legend").hidden = volatilityMode;
  $("#volatility-legend").hidden = !volatilityMode;
  $("#volatility-max-legend").hidden = !volatilityMode || state.maximumMvi <= 0;
  if (volatilityMode) {
    drawVolatilityChart(context, width, height, color, numberFont);
    return;
  }
  const windowMs = state.chartWindow * 60 * 1000;
  const liveGutterMs = Math.min(10000, windowMs * 0.025);
  const viewEnd = Date.now() + liveGutterMs;
  const viewStart = viewEnd - windowMs;
  const points = state.chartPoints
    .map((point) => ({ ...point, time: new Date(point.observed_at).getTime(), price: Number(point.price) }))
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.price)
      && point.time >= viewStart && point.time <= viewEnd);
  const current = state.dashboard?.current;
  const threshold = numberOrNull(current?.strike);
  const currentPrice = numberOrNull(state.dashboard?.btc?.price) ?? numberOrNull(points.at(-1)?.price);
  const thresholdColor = color("--chart-threshold");
  const thresholdSwatch = $("#threshold-legend-swatch");
  if (thresholdSwatch) thresholdSwatch.style.backgroundColor = thresholdColor;
  const thresholdLegend = $("#threshold-legend");
  if (thresholdLegend) thresholdLegend.hidden = !Number.isFinite(threshold);
  if (!points.length) {
    context.fillStyle = color("--chart-label");
    context.font = "12px -apple-system, sans-serif";
    context.textAlign = "center";
    context.fillText("Collecting live price history", width / 2, height / 2);
    return;
  }
  const values = points.map((point) => point.price);
  if (Number.isFinite(threshold)) values.push(threshold);
  const visibleLow = Math.min(...values);
  const visibleHigh = Math.max(...values);
  const padding = Math.max((visibleHigh - visibleLow) * 0.18, visibleHigh * 0.00035, 20);
  const step = niceChartStep((visibleHigh - visibleLow) + padding * 2);
  const targetLow = Math.floor((visibleLow - padding) / step) * step;
  const targetHigh = Math.ceil((visibleHigh + padding) / step) * step;
  const axis = smoothChartAxis(targetLow, targetHigh, frameTime);
  const low = axis.low;
  const high = axis.high;
  const left = 8;
  const right = width < 430 ? 68 : 76;
  const top = 14;
  const bottom = 32;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const plotRight = width - right;
  const x = (timestamp) => left + ((timestamp - viewStart) / windowMs) * chartWidth;
  const y = (value) => top + (1 - (value - low) / (high - low)) * chartHeight;

  context.strokeStyle = color("--chart-grid");
  context.fillStyle = color("--chart-label");
  context.font = `10px ${numberFont}`;
  context.textAlign = "left";
  for (let index = 0; index < 5; index += 1) {
    const value = low + ((high - low) * index) / 4;
    const rowY = y(value);
    context.beginPath(); context.moveTo(left, rowY); context.lineTo(plotRight, rowY); context.stroke();
    context.fillText(money(value, 0), plotRight + 8, rowY + 3);
  }

  const timeInterval = chartTickInterval(windowMs, chartWidth);
  const firstTimeTick = Math.ceil(viewStart / timeInterval) * timeInterval;
  context.textAlign = "center";
  for (let timestamp = firstTimeTick; timestamp <= viewEnd; timestamp += timeInterval) {
    const columnX = x(timestamp);
    context.beginPath(); context.moveTo(columnX, top); context.lineTo(columnX, top + chartHeight); context.stroke();
    if (columnX >= left + 34 && columnX <= plotRight - 34) {
      context.fillText(chartTimeLabel(timestamp, state.chartWindow <= 5), columnX, height - 8);
    }
  }

  const drawDoubleChevron = (centerX, centerY, direction, arrowColor, scale = 1) => {
    context.save();
    context.strokeStyle = arrowColor;
    context.lineWidth = 1.5;
    context.lineCap = "round";
    context.lineJoin = "round";
    [-3, 3].forEach((offset) => {
      const arrowY = centerY + offset * scale;
      context.beginPath();
      if (direction === "up") {
        context.moveTo(centerX - 3 * scale, arrowY + 2 * scale);
        context.lineTo(centerX, arrowY - scale);
        context.lineTo(centerX + 3 * scale, arrowY + 2 * scale);
      } else {
        context.moveTo(centerX - 3 * scale, arrowY - 2 * scale);
        context.lineTo(centerX, arrowY + scale);
        context.lineTo(centerX + 3 * scale, arrowY - 2 * scale);
      }
      context.stroke();
    });
    context.restore();
  };

  const drawThreshold = (value) => {
    if (!Number.isFinite(value)) return;
    const levelY = y(value);
    const direction = Number.isFinite(currentPrice)
      ? currentPrice < value ? "up" : currentPrice > value ? "down" : null
      : null;
    const label = `${money(value, 2)} target`;
    context.save();
    context.strokeStyle = thresholdColor;
    context.lineWidth = 1.25;
    context.setLineDash([]);
    context.beginPath(); context.moveTo(left, levelY); context.lineTo(plotRight, levelY); context.stroke();
    context.font = `500 11px ${numberFont}`;
    const labelWidth = context.measureText(label).width;
    const arrowWidth = direction ? 17 : 0;
    const groupWidth = labelWidth + arrowWidth;
    const groupLeft = left + (chartWidth - groupWidth) / 2;
    context.fillStyle = color("--surface");
    context.fillRect(groupLeft - 8, levelY - 11, groupWidth + 16, 22);
    context.fillStyle = thresholdColor;
    context.textAlign = "left";
    context.textBaseline = "middle";
    context.fillText(label, groupLeft, levelY);
    if (direction) {
      drawDoubleChevron(groupLeft + labelWidth + 10, levelY, direction, thresholdColor, 0.9);
    }
    context.restore();
  };

  const drawCurrentPriceLine = (value) => {
    if (!Number.isFinite(value)) return;
    const levelY = y(value);
    const delta = Number.isFinite(threshold) ? value - threshold : null;
    const lineColor = delta === null
      ? color("--green")
      : delta > 0 ? color("--green") : delta < 0 ? color("--red") : thresholdColor;
    context.save();
    context.strokeStyle = lineColor;
    context.lineWidth = 1.25;
    context.setLineDash([3, 4]);
    context.beginPath(); context.moveTo(left, levelY); context.lineTo(plotRight, levelY); context.stroke();
    context.setLineDash([]);
    context.fillStyle = color("--surface");
    context.fillRect(plotRight + 4, levelY - 9, right - 4, 18);
    context.fillStyle = lineColor;
    context.font = `500 9px ${numberFont}`;
    context.textAlign = "left";
    context.textBaseline = "middle";
    context.fillText(money(value, 2), plotRight + 8, levelY);
    context.restore();
  };

  drawThreshold(threshold);
  drawCurrentPriceLine(currentPrice);

  const lastPoint = points.at(-1);

  if (points.length > 1) {
    context.beginPath();
    points.forEach((point, index) => index === 0 ? context.moveTo(x(point.time), y(point.price)) : context.lineTo(x(point.time), y(point.price)));
    context.strokeStyle = color("--chart-line"); context.lineWidth = 2.25; context.lineJoin = "round"; context.stroke();
  }
  const movement = activePriceMovement(frameTime);
  if (movement) {
    const pointX = x(lastPoint.time);
    const pointY = y(lastPoint.price);
    const arrowY = movement === "up" ? pointY - 13 : pointY + 13;
    drawDoubleChevron(
      pointX,
      Math.min(top + chartHeight - 7, Math.max(top + 7, arrowY)),
      movement,
      movement === "up" ? color("--green") : color("--red"),
      0.85,
    );
  }
  context.beginPath(); context.arc(x(lastPoint.time), y(lastPoint.price), 3.25, 0, Math.PI * 2); context.fillStyle = color("--chart-line"); context.fill();
}

function activePriceMovement(frameTime = performance.now()) {
  return frameTime <= state.priceMovement.until ? state.priceMovement.direction : null;
}

function syncPriceMovement(frameTime = performance.now()) {
  const movement = activePriceMovement(frameTime);
  const price = $("#btc-price");
  if (!price) return;
  price.classList.toggle("price-up", movement === "up");
  price.classList.toggle("price-down", movement === "down");
}

function animateChart(frameTime) {
  if (state.activePage === "dashboard" && frameTime - state.chartLastFrame >= 33) {
    state.chartLastFrame = frameTime;
    syncPriceMovement(frameTime);
    drawChart(frameTime);
  }
  window.requestAnimationFrame(animateChart);
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
  const previousPrice = Number(last?.price);
  if (Number.isFinite(previousPrice) && point.price !== previousPrice) {
    state.priceMovement = {
      direction: point.price > previousPrice ? "up" : "down",
      until: performance.now() + 850,
    };
  }
  if (last?.observed_at === point.observed_at) state.chartPoints[state.chartPoints.length - 1] = point;
  else state.chartPoints.push(point);
  const cutoff = Date.now() - state.chartWindow * 60 * 1000;
  state.chartPoints = state.chartPoints.filter((item) => new Date(item.observed_at).getTime() >= cutoff);
  const volatility = data?.current?.margin_volatility;
  if (volatility?.observed_at) {
    const lastVolatility = state.volatilityPoints.at(-1);
    if (lastVolatility?.observed_at === volatility.observed_at) {
      state.volatilityPoints[state.volatilityPoints.length - 1] = volatility;
    } else {
      state.volatilityPoints.push(volatility);
    }
    state.volatilityPoints = state.volatilityPoints.filter(
      (item) => new Date(item.observed_at).getTime() >= cutoff,
    );
  }
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
    state.volatilityPoints = chart.volatility_points || [];
    state.maximumMvi = Number(chart.maximum_margin_volatility || 0);
    renderDashboard(dashboard);
  } catch (error) {
    $("#sidebar-status").textContent = "App offline";
    $("#sidebar-status-dot").className = "status-dot degraded";
  }
}

function statCard(label, value, detail = "", className = "") {
  return `<div class="stats-card"><span>${label}</span><strong class="${className}">${value}</strong>${detail ? `<small>${detail}</small>` : ""}</div>`;
}

const calibrationGroups = [
  ["Decision Rules", [
    { id: "buy_edge", label: "Buy edge", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Minimum model advantage over the executable ask after fees and slippage. Default: 10%." },
    { id: "minimum_buy_probability", label: "Minimum Buy win chance", unit: "%", min: 50, max: 99, step: 1, scale: 100, tip: "Minimum estimated chance of paying $1 required for a Buy signal. Lower-probability positive-edge contracts are marked Speculative. Default: 65%." },
    { id: "sell_edge", label: "Sell edge", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Minimum executable-bid advantage over model value after costs. Default: 3%." },
    { id: "hold_buffer", label: "Hold buffer", unit: "%", min: 0, max: 10, step: .1, scale: 100, tip: "Extra dead band added to Buy and Sell thresholds to reduce churn. Default: 0.5%." },
    { id: "slippage_cents", label: "Slippage allowance", unit: "cents", min: 0, max: 10, step: .1, tip: "Price movement reserved between decision and execution in every mode. Default: 0.5 cents." },
    { id: "confidence_moderate_edge", label: "Moderate edge strength", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Minimum net edge for a Moderate Edge label. Default: 6%." },
    { id: "confidence_high_edge", label: "High edge strength", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Minimum net edge for a High Edge label. Default: 10%." },
    { id: "confidence_moderate_max_spread", label: "Moderate max spread", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Widest contract spread allowed for Moderate edge strength. Default: 3%." },
    { id: "confidence_high_max_spread", label: "High max spread", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Widest contract spread allowed for High edge strength. Default: 2%." },
    { id: "confidence_moderate_max_variant_spread", label: "Moderate model dispersion", unit: "%", min: 0, max: 100, step: 1, scale: 100, tip: "Maximum model-variant spread allowed for Moderate edge strength. Default: 7%." },
    { id: "confidence_high_max_variant_spread", label: "High model dispersion", unit: "%", min: 0, max: 100, step: 1, scale: 100, tip: "Maximum model-variant spread allowed for High edge strength. Default: 4%." },
    { id: "confidence_high_min_samples", label: "High-strength sample", unit: "samples", min: 1, max: 100000, step: 1, integer: true, tip: "Settled forecasts required before edge strength may be High. Default: 150 samples." },
    { id: "confidence_high_max_calibration_error", label: "High max calibration error", unit: "%", min: 0, max: 100, step: .5, scale: 100, tip: "Largest calibration error compatible with High edge strength. Default: 7%." },
  ]],
  ["Automatic Entry", [
    { id: "paper_trading_enabled", label: "Automatic paper trading", type: "toggle", tip: "Enables simulated Paper entries. Demo and Live each require their own switch and runtime arming. Default: on." },
    { id: "automatic_entry_window_minutes", label: "Entry window", unit: "minutes", min: .25, max: 15, step: .25, tip: "Automatic confirmation can arm only this close to market end. Default: final 15 minutes." },
    { id: "automatic_confirmation_seconds", label: "Confirmation period", unit: "seconds", min: 1, max: 120, step: 1, tip: "Rolling elapsed-time window used to confirm Buy signals. Default: 5 seconds." },
    { id: "automatic_buy_duration_pct", label: "Required Buy duration", unit: "%", min: 50, max: 100, step: 1, scale: 100, tip: "Share of the confirmation period that must be spent in Buy. Default: 50%." },
    { id: "automatic_min_confidence", label: "Minimum edge strength", type: "select", options: ["Low", "Moderate", "High"], tip: "Lowest edge-strength label allowed for an automatic entry. Speculative assessments never enter automatically. Default: Moderate." },
    { id: "threshold_margin_gate_dollars", label: "Threshold margin", unit: "dollars", min: 0, max: 100000, step: 1, tip: "Directional BTC-proxy distance required for automatic entries: Up must be above the threshold and Down below it by this amount. Use 0 to turn it off. Default: $50." },
  ]],
  ["Margin Volatility", [
    { id: "maximum_margin_volatility", label: "Maximum Margin Volatility", unit: "MVI", min: 0, max: 10, step: .1, tip: "Maximum 30-minute Margin Volatility Index allowed for automatic confirmation in Paper, Demo, and Live. Low MVI is allowed; values above this maximum block. Use 0 to turn it off. Default: off." },
  ]],
  ["Early Threshold", [
    { id: "early_threshold_enabled", label: "Early threshold strategy", type: "toggle", tip: "Allows a small automatic entry when a pre-open threshold remains favorable after activation. Default: on." },
    { id: "early_bankroll_pct", label: "Bankroll allocation", unit: "% bankroll", min: 0, max: 100, step: 1, scale: 100, tip: "Target allocation before the global trade and position caps are applied. Default: 3%." },
    { id: "early_min_probability", label: "Minimum win chance", unit: "%", min: 50, max: 99, step: 1, scale: 100, tip: "Minimum estimated chance for the side being bought. Default: 65%." },
    { id: "early_min_net_ev", label: "Minimum Buy EV", unit: "cents", min: 0, max: 25, step: .1, scale: 100, tip: "Minimum value per contract after the executable ask, fee, and slippage. Default: 0.5 cents." },
    { id: "early_entry_window_seconds", label: "Opening window", unit: "seconds", min: 1, max: 300, step: 1, tip: "Time after activation in which the early strategy may enter. Default: 60 seconds." },
    { id: "early_threshold_stability_seconds", label: "Threshold stability", unit: "seconds", min: 0, max: 120, step: .5, tip: "How long the pre-open threshold must remain unchanged. Default: 1 second." },
    { id: "early_confirmation_seconds", label: "Quote confirmation", unit: "seconds", min: 0, max: 120, step: .5, tip: "Minimum time and one fresh quote required before entry. Default: 2 seconds." },
    { id: "early_max_spread", label: "Maximum spread", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Widest accepted spread for an early entry. Default: 20%." },
    { id: "early_min_liquidity_contracts", label: "Minimum liquidity", unit: "contracts", min: 1, max: 1000000, step: 1, integer: true, tip: "Minimum contracts available at the selected ask. Default: 1 contract." },
  ]],
  ["Late Conviction", [
    { id: "late_conviction_enabled", label: "Late conviction strategy", type: "toggle", tip: "Allows a small automatic entry near expiration when one outcome is highly likely and Buy EV remains positive. Default: on." },
    { id: "late_bankroll_pct", label: "Bankroll allocation", unit: "% bankroll", min: 0, max: 100, step: 1, scale: 100, tip: "Target allocation before the global trade and position caps are applied. Default: 3%." },
    { id: "late_max_seconds_remaining", label: "Maximum time remaining", unit: "seconds", min: 1, max: 900, step: 1, tip: "Latest phase in which the strategy may begin evaluating an entry. Default: 120 seconds." },
    { id: "late_min_probability", label: "Minimum win chance", unit: "%", min: 50, max: 99, step: 1, scale: 100, tip: "Minimum estimated chance for the side being bought. Default: 79%." },
    { id: "late_min_net_ev", label: "Minimum Buy EV", unit: "cents", min: 0, max: 25, step: .1, scale: 100, tip: "Minimum value per contract after the executable ask, fee, and slippage. Default: 0.5 cents." },
    { id: "late_confirmation_seconds", label: "Confirmation period", unit: "seconds", min: 0, max: 120, step: .5, tip: "How long the late conditions must remain valid. Default: 3 seconds." },
    { id: "late_min_settlement_coverage", label: "Settlement coverage", unit: "%", min: 0, max: 100, step: 5, scale: 100, tip: "Minimum share of the final settlement window already observed. Default: 80%." },
    { id: "late_min_z_distance", label: "Minimum threshold distance", unit: "z-score", min: 0, max: 20, step: .25, tip: "Minimum absolute volatility-adjusted distance from the threshold. Default: 2.0." },
    { id: "late_max_spread", label: "Maximum spread", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Widest accepted spread for a late entry. Default: 3%." },
    { id: "late_min_liquidity_contracts", label: "Minimum liquidity", unit: "contracts", min: 1, max: 1000000, step: 1, integer: true, tip: "Minimum contracts available at the selected ask. Default: 1 contract." },
  ]],
  ["Swing Trade", [
    { id: "swing_enabled", label: "Swing strategy", type: "toggle", tip: "Buys a deeply discounted side early when the model supports it, then sells into a configured price move. Default: off." },
    { id: "swing_entry_window_seconds", label: "Opening window", unit: "seconds", min: 1, max: 600, step: 1, tip: "Time after the official market open during which Swing may enter. Default: 300 seconds." },
    { id: "swing_max_entry_price", label: "Maximum entry ask", unit: "cents", min: 1, max: 99, step: 1, scale: 100, tip: "Highest displayed ask that may qualify for Swing. Fees and slippage are added separately. Default: 5 cents." },
    { id: "swing_target_exit_price", label: "Target exit bid", unit: "cents", min: 1, max: 99, step: 1, scale: 100, tip: "Displayed executable bid that triggers a Swing exit. Default: 10 cents." },
    { id: "swing_bankroll_pct", label: "Bankroll allocation", unit: "% bankroll", min: 0, max: 100, step: 1, scale: 100, tip: "Target allocation before global trade and position caps. Default: 1%." },
    { id: "swing_min_model_advantage", label: "Minimum model advantage", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Required model probability above the all-in break-even probability after fees and slippage. Default: 3%." },
    { id: "swing_fallback_mode", label: "Fallback behavior", type: "select", options: ["Exit", "Hold to settlement"], tip: "Exit near expiration if the target is missed, or keep the position through settlement. Default: Exit." },
    { id: "swing_fallback_seconds_remaining", label: "Fallback exit", unit: "seconds remaining", min: 1, max: 900, step: 1, tip: "When the fallback exit begins if Exit is selected. Default: 120 seconds remaining." },
    { id: "swing_stop_loss_cents", label: "Swing stop-loss", unit: "cents", min: 0, max: 99, step: 1, nullable: true, tip: "Optional absolute bid trigger for Swing entries. Use 0 or leave blank to turn it off. Default: off." },
    { id: "swing_max_spread", label: "Maximum spread", unit: "cents", min: 0, max: 50, step: .5, scale: 100, tip: "Widest contract spread allowed at entry. Default: 3 cents." },
    { id: "swing_min_liquidity_contracts", label: "Minimum liquidity", unit: "contracts", min: 1, max: 1000000, step: 1, integer: true, tip: "Minimum contracts available at the qualifying ask. Default: 1 contract." },
    { id: "swing_confirmation_seconds", label: "Confirmation period", unit: "seconds", min: 0, max: 120, step: .5, tip: "How long every Swing entry requirement must remain valid. Default: immediate." },
  ]],
  ["Stops and Exits", [
    { id: "default_stop_loss_cents", label: "Default stop-loss", unit: "cents", min: 0, max: 99, step: 1, nullable: true, tip: "Optional absolute bid trigger prefilled on new Buy drafts. Use 0 or leave blank to turn it off; existing stops never change." },
    { id: "global_profit_take_enabled", label: "Global profit take", type: "toggle", tip: "Closes open positions in Paper, Demo, or Live when the executable bid reaches the configured level. Default: on." },
    { id: "global_profit_take_price", label: "Profit-take bid", unit: "cents", min: 1, max: 99, step: 1, scale: 100, tip: "Executable bid that triggers an exit for every strategy and manual trade. Demo and Live require the app to stay connected. Default: 99 cents." },
  ]],
  ["Position Sizing and Risk", [
    { id: "starting_bankroll", label: "Starting bankroll", unit: "dollars", min: 1, max: 100000000, step: 100, tip: "Paper capital used for sizing and performance. Default: $1,000." },
    { id: "risk_controls_enabled", label: "Risk controls", type: "toggle", tip: "Enforces position, trade-risk, and drawdown limits. Default: on." },
    { id: "fractional_kelly", label: "Kelly sizing", unit: "% Kelly", min: 0, max: 100, step: 5, scale: 100, tip: "Fraction of full Kelly used for suggested sizing. Default: 25%." },
    { id: "max_position_pct", label: "Maximum position", unit: "% bankroll", min: 0, max: 100, step: 1, scale: 100, tip: "Maximum paper capital committed to one outcome. Default: 5% of bankroll." },
    { id: "max_risk_per_trade_pct", label: "Maximum risk per trade", unit: "% bankroll", min: 0, max: 100, step: 1, scale: 100, tip: "Shared ceiling on capital allowed in one entry. Default: 5% of bankroll." },
    { id: "max_session_drawdown_pct", label: "Session drawdown", unit: "%", min: 0, max: 100, step: 1, scale: 100, tip: "Paper drawdown that pauses automatic execution without hiding signals. Default: 50%." },
    { id: "minimum_liquidity_contracts", label: "Minimum liquidity", unit: "contracts", min: 1, max: 1000000, step: 1, integer: true, tip: "Minimum contracts available at the selected ask for a Buy. Default: 1 contract." },
  ]],
  ["Data Quality", [
    { id: "max_data_age_seconds", label: "Maximum feed age", unit: "seconds", min: 1, max: 300, step: 1, tip: "Oldest BTC or Kalshi update considered safe. Default: 20 seconds." },
    { id: "max_exchange_dispersion_pct", label: "Exchange dispersion", unit: "%", min: .01, max: 5, step: .05, tip: "Maximum disagreement across BTC exchanges. Default: 0.40%." },
    { id: "minimum_exchange_feeds", label: "Minimum exchange feeds", unit: "feeds", min: 1, max: 3, step: 1, integer: true, tip: "Reliable BTC venues required for a signal. Default: 2 feeds." },
    { id: "closing_guard_seconds", label: "Closing guard", unit: "seconds", min: 1, max: 60, step: 1, integer: true, tip: "Final seconds in which market data is considered unsafe to trade. Default: 10 seconds." },
    { id: "settlement_min_coverage_pct", label: "Settlement coverage", unit: "%", min: 10, max: 100, step: 5, scale: 100, tip: "Required coverage of the observed final-minute proxy. Default: 50%." },
  ]],
  ["Training and Promotion", [
    { id: "training_min_samples", label: "Training sample requirement", unit: "samples", min: 4, max: 100000, step: 1, integer: true, tip: "Settled samples required to train a candidate. Default: 12 samples." },
    { id: "benchmark_calibration_min_samples", label: "Benchmark sample requirement", unit: "samples", min: 4, max: 100000, step: 1, integer: true, tip: "Final-minute proxy comparisons required to learn benchmark error. Default: 20 samples." },
    { id: "training_history_days", label: "Training-history window", unit: "days", min: 1, max: 3650, step: 1, integer: true, tip: "Maximum age of evidence admitted to training. Default: 365 days." },
    { id: "benchmark_history_samples", label: "Benchmark-history window", unit: "samples", min: 20, max: 100000, step: 1, integer: true, tip: "Maximum recent settled markets used to estimate proxy error. Default: 120 samples." },
    { id: "benchmark_uncertainty_floor_pct", label: "Benchmark uncertainty floor", unit: "%", min: 0, max: 1, step: .005, scale: 100, tip: "Minimum uncertainty retained around the learned BRTI proxy. Default: 0.015%." },
    { id: "training_max_samples", label: "Training sample cap", unit: "samples", min: 20, max: 1000000, step: 10, integer: true, tip: "Maximum recent observations in a training run. Default: 1,000 samples." },
    { id: "promotion_min_samples", label: "Promotion sample requirement", unit: "samples", min: 10, max: 1000000, step: 1, integer: true, tip: "Settled forecasts required before a candidate can be promoted. Default: 120 samples." },
    { id: "promotion_min_days", label: "Promotion history", unit: "days", min: 1, max: 3650, step: 1, integer: true, tip: "Distinct UTC days required for promotion. Default: 7 days." },
    { id: "minimum_brier_improvement", label: "Brier improvement", unit: "%", min: 0, max: 25, step: .1, scale: 100, tip: "Minimum forward-test Brier improvement for promotion. Default: 0.5%." },
    { id: "calibration_tolerance", label: "Calibration tolerance", unit: "%", min: 0, max: 50, step: .5, scale: 100, tip: "Maximum calibration-error regression allowed at promotion. Default: 1%." },
    { id: "retraining_cadence_hours", label: "Retraining cadence", unit: "hours", min: 1, max: 720, step: 1, integer: true, tip: "Minimum interval between routine retraining checks. Default: 24 hours." },
    { id: "initial_retrain_settlements", label: "Initial retraining phase", unit: "settlements", min: 0, max: 10000, step: 1, integer: true, tip: "Early settlements that trigger a retraining check each time. Default: first 20." },
  ]],
];

for (const [mode, label] of [["demo", "Demo"], ["live", "Live"]]) {
  calibrationGroups.push([`${label} Execution Limits`, [
    { id: `${mode}_automatic_trading_enabled`, label: `Automatic ${label} trading`, type: "toggle", tip: "Enables automatic submissions only while the environment is authenticated, reconciled, and deliberately armed." },
    { id: `${mode}_bankroll_cap_pct`, label: "Bankroll allocation cap", unit: "% funds", min: 0, max: 100, step: 1, scale: 100, tip: "Eligible account funds available to all strategies. Default: 100%." },
    { id: `${mode}_max_total_allocated_capital`, label: "Maximum allocated capital", unit: "dollars", min: 0, max: 100000000, step: 10, tip: "Hard dollar ceiling across positions, resting buys, and pending intents." },
    { id: `${mode}_max_amount_per_order`, label: "Maximum per order", unit: "dollars", min: 0, max: 100000000, step: 10, tip: "Hard cash-exposure ceiling for one order." },
    { id: `${mode}_max_exposure_per_market`, label: "Maximum per market", unit: "dollars", min: 0, max: 100000000, step: 10, tip: "Hard exposure ceiling for one market." },
    { id: `${mode}_max_total_open_exposure`, label: "Maximum open exposure", unit: "dollars", min: 0, max: 100000000, step: 10, tip: "Hard exposure ceiling across all open markets." },
    { id: `${mode}_max_open_orders`, label: "Maximum open orders", unit: "orders", min: 0, max: 10000, step: 1, integer: true, tip: "Maximum number of resting or partially filled orders." },
    { id: `${mode}_max_daily_loss`, label: "Maximum daily loss", unit: "dollars", min: 0, max: 100000000, step: 10, tip: "Daily realized and unrealized equity loss that blocks new exposure." },
    { id: `${mode}_max_daily_order_count`, label: "Maximum daily orders", unit: "orders", min: 0, max: 1000000, step: 1, integer: true, tip: "Maximum orders accepted for exchange handling per UTC day; rejected attempts do not count." },
    { id: `${mode}_max_entry_price`, label: "Maximum entry price", unit: "cents", min: 1, max: 99, step: 1, scale: 100, tip: "Highest outcome price allowed for new exposure." },
    { id: `${mode}_max_spread`, label: "Maximum spread", unit: "cents", min: 0, max: 99, step: 1, scale: 100, tip: "Hard spread ceiling independent of strategy settings." },
    { id: `${mode}_min_liquidity`, label: "Minimum liquidity", unit: "contracts", min: 0, max: 1000000, step: 1, integer: true, tip: "Hard minimum at the selected executable quote." },
    { id: `${mode}_min_data_quality`, label: "Minimum data quality", type: "select", options: ["Low", "Moderate", "High"], tip: "Hard data-quality floor independent of strategy settings." },
    { id: `${mode}_entry_timeout_seconds`, label: "Entry remainder timeout", unit: "seconds", min: 1, max: 300, step: 1, integer: true, tip: "Automatic unfilled remainders are canceled after this duration." },
  ]]);
}

const calibrationControlMap = new Map(calibrationGroups.flatMap(([, controls]) => controls.map((control) => [control.id, control])));

function renderCalibrationControls() {
  $("#calibration-controls").innerHTML = calibrationGroups.map(([group, controls]) => `
    <section class="calibration-group"><h2>${group}</h2>
      ${controls.map((control) => {
        const tooltipId = `tip-${control.id}`;
        let field;
        if (control.type === "toggle") {
          field = `<label class="calibration-toggle"><input id="${control.id}" type="checkbox"><i></i></label>`;
        } else if (control.type === "select") {
          field = `<div class="calibration-input"><select id="${control.id}">${control.options.map((option) => `<option>${option}</option>`).join("")}</select></div>`;
        } else {
          field = `<div class="calibration-input"><input id="${control.id}" type="number" min="${control.min}" max="${control.max}" step="${control.step}" ${control.nullable ? 'placeholder="Blank"' : ""}>${control.unit ? `<span>${control.unit}</span>` : ""}</div>`;
        }
        return `<div class="calibration-control">
          <div class="calibration-control-label"><label for="${control.id}">${control.label}</label><button class="info-button" type="button" aria-label="About ${control.label}" aria-describedby="${tooltipId}">ⓘ</button><span class="control-tooltip" id="${tooltipId}" role="tooltip">${control.tip}</span></div>
          ${field}
        </div>`;
      }).join("")}
    </section>`).join("");
}

function setCalibrationValues(values) {
  for (const [id, control] of calibrationControlMap) {
    const input = document.getElementById(id);
    const value = values?.[id];
    if (control.type === "toggle") input.checked = Boolean(value);
    else input.value = value == null ? "" : Number.isFinite(Number(value)) && control.type !== "select"
      ? Number((Number(value) * Number(control.scale || 1)).toFixed(6))
      : value;
    input.closest(".calibration-input")?.classList.remove("invalid");
  }
  setCalibrationDirty(false);
}

function readCalibrationValues() {
  const values = {};
  let error = "";
  for (const [id, control] of calibrationControlMap) {
    const input = document.getElementById(id);
    if (control.type === "toggle") values[id] = input.checked;
    else if (control.type === "select") values[id] = input.value;
    else if (control.nullable && input.value.trim() === "") values[id] = null;
    else {
      const displayValue = Number(input.value);
      const invalid = !Number.isFinite(displayValue) || displayValue < control.min || displayValue > control.max || (control.integer && !Number.isInteger(displayValue));
      input.closest(".calibration-input").classList.toggle("invalid", invalid);
      if (invalid && !error) error = `${control.label} must be ${control.min}–${control.max}${control.integer ? " as a whole number" : ""}.`;
      values[id] = displayValue / Number(control.scale || 1);
    }
  }
  $("#calibration-validation").textContent = error;
  return { values, error };
}

function setCalibrationDirty(dirty) {
  state.calibration.dirty = dirty;
  $("#calibration-save-state").textContent = dirty ? "Unsaved Changes" : "Saved";
  $("#calibration-save-state").classList.toggle("unsaved", dirty);
  $("#apply-calibration").disabled = !dirty;
  $("#discard-calibration").disabled = !dirty;
}

function markCalibrationDirty() {
  const { values, error } = readCalibrationValues();
  const dirty = [...calibrationControlMap.keys()].some((key) => values[key] !== state.calibration.saved?.[key]);
  setCalibrationDirty(dirty);
  $("#apply-calibration").disabled = !dirty || Boolean(error);
}

function compactCalibrationStat(label, value, detail) {
  return `<div class="compact-stat"><span>${label}</span><strong>${value}</strong><small>${detail}</small></div>`;
}

function renderCalibrationResults(data) {
  const summary = data.summary || {};
  $("#calibration-stats").innerHTML = [
    compactCalibrationStat("Settled", summary.sample_size || 0, "samples"),
    compactCalibrationStat("Brier", summary.brier_score == null ? "--" : Number(summary.brier_score).toFixed(3), "lower is better"),
    compactCalibrationStat("Cal. error", percent(summary.calibration_error, 1), "predicted vs actual"),
  ].join("");
  const strategyResults = data.strategy_results || {};
  const exchangeResults = data.mode && data.mode !== "PAPER";
  $("#strategy-results-subtitle").textContent = `${modeLabel(data.mode || "PAPER")} performance`;
  const strategyLabels = {
    EARLY_THRESHOLD: "Early threshold",
    STANDARD_EDGE: "Standard edge",
    LATE_CONVICTION: "Late conviction",
    SWING: "Swing trade",
  };
  $("#strategy-results").innerHTML = Object.entries(strategyLabels).map(([key, label]) => {
    const result = strategyResults[key] || {};
    const detail = exchangeResults
      ? `${result.entries || 0} entries · ${result.completed_trades || 0} completed · avg ${cents(result.average_entry_price)} → ${cents(result.average_exit_price)} · ${money(result.actual_fees || 0, 4)} fees`
      : key === "SWING"
      ? `${result.entries || 0} entries · ${result.completed_trades || 0} completed · avg ${cents(result.average_entry_price)} → ${cents(result.average_exit_price)} · ${result.average_holding_seconds == null ? "--" : `${Math.round(result.average_holding_seconds)}s`} held · ${percent(result.return_on_deployed_capital, 1)} return`
      : `${result.entries || 0} entries · ${result.settled_trades || 0} settled · avg ${percent(result.average_entry_probability, 1)} at ${cents(result.average_entry_price)} · EV ${result.average_entry_ev == null ? "--" : money(result.average_entry_ev, 3)}`;
    const rate = key === "SWING" && !exchangeResults
      ? `${percent(result.target_hit_rate, 1)} target`
      : `${percent(result.win_rate, 1)} wins`;
    return `<div class="strategy-result-row">
      <span><strong>${label}</strong><small>${detail}</small></span>
      <span title="${key === "SWING" ? "Target-hit rate" : "Win rate"}">${rate}</span>
      <span class="${Number(result.realized_pnl) > 0 ? "positive" : Number(result.realized_pnl) < 0 ? "negative" : ""}" title="Realized profit and loss">${money(result.realized_pnl || 0)}</span>
    </div>`;
  }).join("");
  const volatilityReport = data.margin_volatility_report || {};
  $("#mvi-evidence-subtitle").textContent = `${volatilityReport.reliable_observations || 0} reliable windows · ${volatilityReport.settled_entries || 0} settled entries`;
  const evidenceBuckets = (volatilityReport.buckets || []).filter(
    (bucket) => bucket.observations || bucket.entries || bucket.blocked_opportunities,
  );
  $("#mvi-evidence").innerHTML = evidenceBuckets.length
    ? `${evidenceBuckets.map((bucket) => `<div class="mvi-evidence-row"><strong>${bucket.label}</strong><span>${bucket.entries} entries</span><span>${bucket.settled} settled</span><span>${bucket.win_rate == null ? "--" : percent(bucket.win_rate, 0)} · ${money(bucket.realized_pnl || 0)}</span></div>`).join("")}<p class="mvi-evidence-note">${volatilityReport.guidance || "Sample sizes are shown before any limit is considered."}</p>`
    : `<p class="empty-state">${volatilityReport.guidance || "No reliable observations yet."}</p>`;
  const volumeReport = data.volume_signal_report || {};
  const volumeCurrent = volumeReport.current || {};
  const volumeMetrics = volumeCurrent.metrics || {};
  const volumeCandidate = volumeReport.candidate?.validation?.candidate
    || volumeReport.candidate?.validation?.candidate_metrics || null;
  const volumeContributions = volumeReport.active_volume_contributions || {};
  $("#volume-signals-subtitle").textContent = `${String(volumeCurrent.status || "Unavailable").replaceAll("_", " ")} · ${volumeCurrent.availability || "shadow-only"}`;
  const volumeRows = [
    ["Relative volume", `${volumeMetrics.btc_rvol_1m == null ? "--" : Number(volumeMetrics.btc_rvol_1m).toFixed(2)} / ${volumeMetrics.btc_rvol_5m == null ? "--" : Number(volumeMetrics.btc_rvol_5m).toFixed(2)} · 1m / 5m`],
    ["Signed BTC flow", `${percent(volumeMetrics.btc_flow_imbalance_1m, 1, true)} / ${percent(volumeMetrics.btc_flow_imbalance_5m, 1, true)}`],
    ["Momentum confirmation", `${volumeMetrics.btc_volume_confirmation_1m == null ? "--" : Number(volumeMetrics.btc_volume_confirmation_1m).toFixed(5)} / ${volumeMetrics.btc_volume_confirmation_5m == null ? "--" : Number(volumeMetrics.btc_volume_confirmation_5m).toFixed(5)}`],
    ["VWAP distance", `${percent(volumeMetrics.btc_vwap_distance_1m, 3, true)} / ${percent(volumeMetrics.btc_vwap_distance_5m, 3, true)}`],
    ["Kalshi flow / turnover", `${percent(volumeMetrics.kalshi_flow_imbalance_1m, 1, true)} / ${percent(volumeMetrics.kalshi_turnover_5m, 2)}`],
    ["Data / venue agreement", `${percent(volumeCurrent.data_completeness, 0)} / ${volumeCurrent.venue_agreement == null ? "--" : percent(volumeCurrent.venue_agreement, 0)}`],
    ["Active volume inputs", Object.keys(volumeContributions).length ? `${Object.keys(volumeContributions).length} learned inputs` : "Shadow only"],
    ["Shadow validation", volumeCandidate?.brier_score == null ? "Collecting settled samples" : `Brier ${Number(volumeCandidate.brier_score).toFixed(3)} · n=${volumeCandidate.validation_samples || 0}`],
  ];
  $("#volume-signals-evidence").innerHTML = `${volumeRows.map(([label, value]) => `<div class="volume-signal-row"><span>${label}</span><strong>${value}</strong></div>`).join("")}<p class="volume-signal-note">${escapeHtml(volumeCurrent.message || volumeReport.audit?.finding || "Volume inputs activate only after out-of-sample review.")}</p>`;
  const buckets = summary.buckets || [];
  $("#calibration-bars").innerHTML = buckets.length ? buckets.map((bucket) => `
    <div class="bucket">
      <div class="bucket-bars"><i style="height:${Math.max(2, bucket.predicted * 100)}%" title="Predicted ${percent(bucket.predicted)}"></i><i class="actual" style="height:${Math.max(2, bucket.actual * 100)}%" title="Actual ${percent(bucket.actual)}"></i></div>
      <label>${bucket.label}</label><small>n=${bucket.count}</small>
    </div>`).join("") : '<p class="empty-state">Settled observations will populate this chart.</p>';
  const snapshots = data.configuration_snapshots || [];
  $("#configuration-snapshots").innerHTML = snapshots.length ? snapshots.map((snapshot) => {
    const labels = Object.keys(snapshot.changed || {}).slice(0, 3).map((key) => calibrationControlMap.get(key)?.label || key);
    const more = Math.max(0, Object.keys(snapshot.changed || {}).length - labels.length);
    return `<div class="snapshot-row"><span><strong>${shortDate(snapshot.created_at)}</strong><small>${labels.join(", ")}${more ? ` +${more} more` : ""}${snapshot.restored_from_id ? ` · restored #${snapshot.restored_from_id}` : ""}</small></span><button type="button" data-restore-snapshot="${snapshot.id}">Restore</button></div>`;
  }).join("") : '<p class="empty-state">Applied changes will appear here.</p>';
  const reports = data.reports || [];
  $("#report-list").innerHTML = reports.length ? reports.map((row) => {
    const report = row.report || {};
    const limitations = (report.limitations || []).map((item) => `<li>${item}</li>`).join("");
    return `<details class="report-item"><summary><time>${shortDate(row.created_at)}</time><strong>${row.tldr}</strong><span>${row.promoted ? "Promoted" : "Incumbent kept"}</span></summary><div class="report-body"><h4>Validation</h4><p>${report.validation || "Calibration-only report."}</p><h4>Scores</h4><p>Brier: ${row.brier_before == null ? "--" : Number(row.brier_before).toFixed(3)} · Calibration error: ${percent(row.calibration_error, 1)} · Settled contracts: ${row.settled_contracts}</p>${limitations ? `<h4>Limitations</h4><ul>${limitations}</ul>` : ""}</div></details>`;
  }).join("") : '<p class="empty-state">No calibration reports yet.</p>';
}

function renderCalibrationControlsOnce() {
  if (state.calibration.controlsRendered) return;
  renderCalibrationControls();
  state.calibration.controlsRendered = true;
}

function setCalibrationEvidenceLoading() {
  if (state.calibration.evidence) return;
  $("#mvi-evidence-subtitle").textContent = "Loading evidence in background";
  $("#volume-signals-subtitle").textContent = "Loading evidence in background";
}

async function loadCalibrationEvidence({ force = false } = {}) {
  const freshForMs = 30_000;
  const fresh = state.calibration.evidence
    && Date.now() - state.calibration.evidenceUpdatedAt < freshForMs;
  if (!force && fresh) return state.calibration.evidence;
  if (state.calibration.evidenceRequest) return state.calibration.evidenceRequest;
  setCalibrationEvidenceLoading();
  state.calibration.evidenceRequest = api("/api/calibration/evidence")
    .then((evidence) => {
      state.calibration.evidence = evidence;
      state.calibration.evidenceUpdatedAt = Date.now();
      if (state.activePage === "calibration") {
        renderCalibrationResults({ ...state.calibration.summary, ...evidence });
      }
      return evidence;
    })
    .catch(() => {
      if (!state.calibration.evidence && state.activePage === "calibration") {
        $("#mvi-evidence-subtitle").textContent = "Evidence unavailable";
        $("#volume-signals-subtitle").textContent = "Evidence unavailable";
      }
      return null;
    })
    .finally(() => { state.calibration.evidenceRequest = null; });
  return state.calibration.evidenceRequest;
}

async function loadCalibration() {
  renderCalibrationControlsOnce();
  if (state.calibration.summary) {
    renderCalibrationResults({ ...state.calibration.summary, ...state.calibration.evidence });
  } else {
    setCalibrationEvidenceLoading();
  }
  const [data, settings, defaults] = await Promise.all([
    api("/api/calibration/summary"), api("/api/settings"), api("/api/settings/defaults"),
  ]);
  if (!state.calibration.dirty) {
    state.calibration.saved = settings;
    state.calibration.defaults = defaults;
    setCalibrationValues(settings);
  }
  state.calibration.summary = data;
  if (state.activePage === "calibration") {
    renderCalibrationResults({ ...data, ...state.calibration.evidence });
  }
  void loadCalibrationEvidence();
}

async function applyCalibration() {
  const { values, error } = readCalibrationValues();
  if (error) return;
  const button = $("#apply-calibration");
  button.disabled = true;
  try {
    const settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
    state.calibration.saved = settings;
    setCalibrationValues(settings);
    state.calibration.summary = await api("/api/calibration/summary");
    renderCalibrationResults({ ...state.calibration.summary, ...state.calibration.evidence });
    void loadCalibrationEvidence({ force: true });
    await refreshDashboard();
    showToast("Calibration saved", "New decisions now use the applied configuration.");
  } catch (errorValue) { showToast("Calibration not saved", errorValue.message); markCalibrationDirty(); }
}

async function restoreConfiguration(snapshotId) {
  try {
    await api(`/api/settings/restore/${snapshotId}`, { method: "POST" });
    await loadCalibration();
    await refreshDashboard();
    showToast("Configuration restored", `Snapshot #${snapshotId} is active and was journaled.`);
  } catch (error) { showToast("Configuration not restored", error.message); }
}

function historicalReviewSummary(review) {
  const trade = review.trade || {};
  const session = review.session || {};
  const coverage = session.coverage == null ? "--" : percent(session.coverage, 0);
  const resultClass = Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : "";
  return `
    <div class="trade-review-summary">
      <div><span>Environment / side</span><strong>${escapeHtml(review.environment)} · ${marketSideLabel(trade.side)}</strong><small>${escapeHtml(String(trade.strategy || "--").replaceAll("_", " "))}</small></div>
      <div><span>Entry / exit</span><strong>${cents(trade.entry_price)} / ${trade.exit_price == null ? "--" : cents(trade.exit_price)}</strong><small>${shortDate(trade.opened_at)}</small></div>
      <div><span>Quantity / fees</span><strong>${trade.contracts ?? "--"} / ${trade.fees == null ? "--" : money(trade.fees, 4)}</strong><small>Contracts / total fees</small></div>
      <div><span>Entry probability / EV</span><strong>${trade.model_probability == null ? "--" : percent(trade.model_probability)} / ${trade.expected_value == null ? "--" : cents(trade.expected_value)}</strong><small>Saved at entry</small></div>
      <div><span>Result</span><strong class="${resultClass}">${trade.realized_pnl == null ? "--" : signedMoney(trade.realized_pnl)}</strong><small>${escapeHtml(session.settlement_result || "--")}</small></div>
      <div><span>Settlement margin</span><strong>${session.settlement_margin == null ? "--" : signedMoney(session.settlement_margin)}</strong><small>Price to threshold</small></div>
      <div><span>Available after</span><strong>${trade.available_cash_after == null ? "--" : money(trade.available_cash_after)}</strong><small>Latest transaction</small></div>
      <div><span>Coverage</span><strong>${coverage}</strong><small>${session.gap_count || 0} recorded gap${Number(session.gap_count) === 1 ? "" : "s"}</small></div>
    </div>`;
}

function historicalReviewCoverageWarning(review) {
  const session = review.session || {};
  if (session.session_status === "FINALIZED" && !Number(session.gap_count)) return "";
  const coverage = session.coverage == null ? "unknown" : percent(session.coverage, 0);
  return `<p class="trade-review-coverage-warning">Partial history: ${coverage} coverage with ${Number(session.gap_count || 0)} recorded gap${Number(session.gap_count) === 1 ? "" : "s"}. Missing values are shown as gaps and were not reconstructed.</p>`;
}

function reviewMetricContent(point) {
  const snapshot = point?.state || {};
  const forecast = snapshot.forecast || {};
  const assessments = snapshot.trade_assessments || {};
  const readiness = snapshot.standard_edge_readiness || {};
  const quality = snapshot.data_quality || {};
  const volume = snapshot.volume_signals || {};
  const volumeMetrics = volume.metrics || {};
  const selectedSide = state.tradeReview.data?.trade?.side || readiness.side || "YES";
  const selected = assessments[selectedSide] || {};
  const selectedBuy = selected.buy || {};
  const gates = readiness.gates || {};
  const gateSummary = Object.entries(gates)
    .map(([name, detail]) => `${name.replaceAll("_", " ")} ${detail?.passed ? "✓" : "×"}`)
    .join(" · ") || "--";
  return `
    <div><span>Time / remaining</span><strong>${point ? `${new Date(point.observed_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })} / ${countdown(point.seconds_remaining)}` : "Move across chart"}</strong></div>
    <div><span>BTC / threshold</span><strong>${point ? `${money(point.btc_proxy)} / ${money(point.threshold)}` : "--"}</strong></div>
    <div><span>Margin</span><strong>${point?.margin == null ? "--" : signedMoney(point.margin)}</strong></div>
    <div><span>Up bid / ask</span><strong>${point ? `${cents(point.yes_bid)} / ${cents(point.yes_ask)}` : "--"}</strong></div>
    <div><span>Down bid / ask</span><strong>${point ? `${cents(point.no_bid)} / ${cents(point.no_ask)}` : "--"}</strong></div>
    <div><span>Up / Down</span><strong>${forecast.up_probability == null ? "--" : `${percent(forecast.up_probability)} / ${percent(forecast.down_probability)}`}</strong></div>
    <div><span>Forecast</span><strong>${escapeHtml(String(point?.forecast_signal || "--").replaceAll("_", " "))}</strong></div>
    <div><span>${marketSideLabel(selectedSide)} executable / EV</span><strong>${selectedBuy.executable_price == null ? "--" : cents(selectedBuy.executable_price)} / ${selectedBuy.expected_value == null ? "--" : cents(selectedBuy.expected_value)}</strong></div>
    <div><span>MVI / expected / cushion</span><strong>${point?.mvi == null ? "--" : Number(point.mvi).toFixed(2)} / ${point?.expected_remaining_move == null ? "--" : money(point.expected_remaining_move)} / ${point?.cushion_ratio == null ? "--" : Number(point.cushion_ratio).toFixed(2)}</strong></div>
    <div><span>RVOL 1m / 5m</span><strong>${volumeMetrics.btc_rvol_1m == null ? "--" : Number(volumeMetrics.btc_rvol_1m).toFixed(2)} / ${volumeMetrics.btc_rvol_5m == null ? "--" : Number(volumeMetrics.btc_rvol_5m).toFixed(2)}</strong></div>
    <div><span>BTC / Kalshi flow</span><strong>${percent(volumeMetrics.btc_flow_imbalance_1m, 1, true)} / ${percent(volumeMetrics.kalshi_flow_imbalance_1m, 1, true)}</strong></div>
    <div><span>VWAP distance / volume data</span><strong>${percent(volumeMetrics.btc_vwap_distance_1m, 3, true)} / ${percent(volume.data_completeness, 0)}</strong></div>
    <div><span>Spread / liquidity</span><strong>${point ? `${cents(point.spread)} / ${compact(point.liquidity)}` : "--"}</strong></div>
    <div class="wide"><span>Data / gates</span><strong>${quality.reliable ? "Reliable" : "Blocked"} · ${escapeHtml(gateSummary)}</strong></div>
    <div class="wide"><span>Readiness / blocker</span><strong>${escapeHtml(readiness.status || "--")} · ${escapeHtml(readiness.blocker || "--")}</strong></div>`;
}

function reviewTooltipContent(point) {
  if (!point) return "";
  const snapshot = point.state || {};
  const readiness = snapshot.standard_edge_readiness || {};
  const assessments = snapshot.trade_assessments || {};
  const side = state.tradeReview.data?.trade?.side || readiness.side || "YES";
  const assessment = assessments[side] || {};
  const buy = assessment.buy || {};
  const forecast = snapshot.forecast || {};
  const volume = snapshot.volume_signals || {};
  const volumeMetrics = volume.metrics || {};
  const gateSummary = Object.entries(readiness.gates || {})
    .map(([name, detail]) => `<span class="${detail?.passed ? "pass" : "fail"}">${escapeHtml(name.replaceAll("_", " "))}</span>`)
    .join("");
  return `<strong>${new Date(point.observed_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })} · ${countdown(point.seconds_remaining)} left</strong>
    <dl>
      <div><dt>BTC / threshold</dt><dd>${money(point.btc_proxy)} / ${money(point.threshold)}</dd></div>
      <div><dt>Margin</dt><dd>${point.margin == null ? "--" : signedMoney(point.margin)}</dd></div>
      <div><dt>Up / Down quote</dt><dd>${cents(point.yes_bid)}–${cents(point.yes_ask)} / ${cents(point.no_bid)}–${cents(point.no_ask)}</dd></div>
      <div><dt>Up / Down chance</dt><dd>${percent(forecast.up_probability)} / ${percent(forecast.down_probability)}</dd></div>
      <div><dt>Forecast</dt><dd>${escapeHtml(String(point.forecast_signal || "--").replaceAll("_", " "))}</dd></div>
      <div><dt>${marketSideLabel(side)} executable / EV</dt><dd>${buy.executable_price == null ? "--" : cents(buy.executable_price)} / ${buy.expected_value == null ? "--" : cents(buy.expected_value)}</dd></div>
      <div><dt>MVI / expected / cushion</dt><dd>${point.mvi == null ? "--" : Number(point.mvi).toFixed(2)} / ${point.expected_remaining_move == null ? "--" : money(point.expected_remaining_move)} / ${point.cushion_ratio == null ? "--" : Number(point.cushion_ratio).toFixed(2)}</dd></div>
      <div><dt>RVOL 1m / 5m</dt><dd>${volumeMetrics.btc_rvol_1m == null ? "--" : Number(volumeMetrics.btc_rvol_1m).toFixed(2)} / ${volumeMetrics.btc_rvol_5m == null ? "--" : Number(volumeMetrics.btc_rvol_5m).toFixed(2)}</dd></div>
      <div><dt>BTC / Kalshi flow</dt><dd>${percent(volumeMetrics.btc_flow_imbalance_1m, 1, true)} / ${percent(volumeMetrics.kalshi_flow_imbalance_1m, 1, true)}</dd></div>
      <div><dt>VWAP distance / volume data</dt><dd>${percent(volumeMetrics.btc_vwap_distance_1m, 3, true)} / ${percent(volume.data_completeness, 0)}</dd></div>
      <div><dt>Spread / liquidity</dt><dd>${cents(point.spread)} / ${compact(point.liquidity)}</dd></div>
      <div><dt>Data quality</dt><dd>${snapshot.data_quality?.reliable ? "Reliable" : "Blocked"}</dd></div>
    </dl><div class="trade-review-tooltip-gates">${gateSummary}</div><p>${escapeHtml(readiness.blocker || "No blocker recorded.")}</p>`;
}

function historicalReviewPoints() {
  return (state.tradeReview.data?.points || [])
    .filter((point) => point.sample_kind === "REGULAR")
    .map((point) => ({ ...point, timestamp: new Date(point.observed_at).getTime() }))
    .filter((point) => Number.isFinite(point.timestamp));
}

function historicalReviewSelectablePoints() {
  return (state.tradeReview.data?.points || [])
    .map((point) => ({ ...point, timestamp: new Date(point.observed_at).getTime() }))
    .filter((point) => Number.isFinite(point.timestamp));
}

function drawHistoricalTradeReview() {
  const panel = document.querySelector(".trade-review-expanded");
  const canvas = panel?.querySelector(".trade-review-canvas");
  const review = state.tradeReview.data;
  if (!canvas || !review) return;
  const points = historicalReviewPoints();
  const selectablePoints = historicalReviewSelectablePoints();
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, box.width);
  const height = Math.max(1, box.height);
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const styles = getComputedStyle(document.documentElement);
  const color = (name) => styles.getPropertyValue(name).trim();
  const left = 12; const right = 68; const top = 18; const bottom = 34;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const session = review.session || {};
  const start = new Date(session.market_open_time || points[0]?.observed_at).getTime();
  const end = new Date(session.market_close_time || points.at(-1)?.observed_at).getTime();
  if (!points.length || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    context.fillStyle = color("--muted"); context.textAlign = "center";
    context.fillText("Historical points are unavailable.", width / 2, height / 2);
    return;
  }
  const x = (timestamp) => left + ((timestamp - start) / (end - start)) * chartWidth;
  const volatility = state.tradeReview.chartMode === "volatility";
  const values = volatility
    ? points.map((point) => Number(point.mvi)).filter(Number.isFinite)
    : points.flatMap((point) => [Number(point.btc_proxy), Number(point.threshold)]).filter(Number.isFinite);
  let low = volatility ? 0 : Math.min(...values);
  let high = volatility ? 10 : Math.max(...values);
  if (!volatility) {
    const padding = Math.max((high - low) * 0.14, 4);
    low -= padding; high += padding;
  }
  const y = (value) => top + (1 - (value - low) / Math.max(high - low, 1e-9)) * chartHeight;
  context.strokeStyle = color("--chart-grid");
  context.fillStyle = color("--chart-label");
  context.font = `10px ${color("--number-font")}`;
  for (let index = 0; index < 5; index += 1) {
    const value = low + ((high - low) * index) / 4;
    const rowY = y(value);
    context.beginPath(); context.moveTo(left, rowY); context.lineTo(width - right, rowY); context.stroke();
    context.fillText(volatility ? value.toFixed(1) : money(value, 0), width - right + 8, rowY + 3);
  }
  [0, 5, 10, 15].forEach((minute) => {
    const timestamp = start + minute * 60000;
    const columnX = x(timestamp);
    context.beginPath(); context.moveTo(columnX, top); context.lineTo(columnX, top + chartHeight); context.stroke();
    context.textAlign = minute === 0 ? "left" : minute === 15 ? "right" : "center";
    context.fillText(chartTimeLabel(timestamp, false), columnX, height - 9);
  });
  (review.gaps || []).forEach((gap) => {
    const gapStart = x(new Date(gap.from).getTime());
    const gapEnd = x(new Date(gap.to).getTime());
    context.fillStyle = color("--red-soft");
    context.fillRect(gapStart, top, Math.max(2, gapEnd - gapStart), chartHeight);
  });
  const drawSeries = (valueFor, stroke, dashed = false) => {
    context.save(); context.strokeStyle = stroke; context.lineWidth = 2;
    if (dashed) context.setLineDash([5, 4]);
    context.beginPath();
    let drawing = false; let previousTime = null;
    points.forEach((point) => {
      const value = valueFor(point);
      if (!Number.isFinite(value)) { drawing = false; return; }
      const hasGap = previousTime != null && point.timestamp - previousTime > 7500;
      if (!drawing || hasGap) context.moveTo(x(point.timestamp), y(value));
      else context.lineTo(x(point.timestamp), y(value));
      drawing = true; previousTime = point.timestamp;
    });
    context.stroke(); context.restore();
  };
  if (volatility) drawSeries((point) => Number(point.mvi), color("--hud-warning"));
  else {
    drawSeries((point) => Number(point.btc_proxy), color("--chart-line"));
    drawSeries((point) => Number(point.threshold), color("--chart-threshold"), true);
  }
  const eventColors = { ENTRY: color("--green"), PARTIAL_FILL: color("--green"), EXIT: color("--blue"), PARTIAL_EXIT: color("--blue"), SETTLEMENT: color("--red") };
  (review.events || []).filter((event) => eventColors[event.event_type]
    && (event.trade_ref === review.trade_ref
      || (event.event_type === "SETTLEMENT" && !event.trade_ref))).forEach((event) => {
    const eventX = x(new Date(event.observed_at).getTime());
    if (eventX < left || eventX > width - right) return;
    context.save(); context.strokeStyle = eventColors[event.event_type]; context.lineWidth = 1.25;
    context.setLineDash([3, 3]); context.beginPath(); context.moveTo(eventX, top); context.lineTo(eventX, top + chartHeight); context.stroke();
    context.fillStyle = eventColors[event.event_type]; context.beginPath();
    context.moveTo(eventX, top); context.lineTo(eventX - 4, top + 7); context.lineTo(eventX + 4, top + 7); context.closePath(); context.fill(); context.restore();
  });
  const selectedIndex = state.tradeReview.selectedIndex;
  if (selectedIndex != null && selectablePoints[selectedIndex]) {
    const point = selectablePoints[selectedIndex]; const crosshairX = x(point.timestamp);
    context.save(); context.strokeStyle = color("--muted"); context.lineWidth = 1;
    context.beginPath(); context.moveTo(crosshairX, top); context.lineTo(crosshairX, top + chartHeight); context.stroke(); context.restore();
    const value = volatility ? Number(point.mvi) : Number(point.btc_proxy);
    if (Number.isFinite(value)) { context.beginPath(); context.arc(crosshairX, y(value), 3.5, 0, Math.PI * 2); context.fillStyle = volatility ? color("--hud-warning") : color("--chart-line"); context.fill(); }
  }
  state.tradeReview.geometry = {
    left, right, width, start, end, points: selectablePoints,
  };
}

function selectHistoricalPoint(index, pointerX = null) {
  const points = historicalReviewSelectablePoints();
  if (!points.length) return;
  state.tradeReview.selectedIndex = Math.max(0, Math.min(points.length - 1, index));
  const metrics = document.querySelector(".trade-review-metrics");
  if (metrics) metrics.innerHTML = reviewMetricContent(points[state.tradeReview.selectedIndex]);
  const tooltip = document.querySelector(".trade-review-tooltip");
  const geometry = state.tradeReview.geometry;
  if (tooltip && geometry) {
    const point = points[state.tradeReview.selectedIndex];
    tooltip.innerHTML = reviewTooltipContent(point);
    tooltip.hidden = false;
    const crosshairX = pointerX ?? geometry.left
      + ((point.timestamp - geometry.start) / (geometry.end - geometry.start))
      * (geometry.width - geometry.left - geometry.right);
    const tooltipWidth = Math.min(310, geometry.width - 24);
    const proposed = crosshairX > geometry.width * 0.62
      ? crosshairX - tooltipWidth - 12 : crosshairX + 12;
    tooltip.style.left = `${Math.max(8, Math.min(geometry.width - tooltipWidth - 8, proposed))}px`;
    tooltip.style.width = `${tooltipWidth}px`;
  }
  drawHistoricalTradeReview();
}

function bindHistoricalReviewPanel(panel) {
  panel.querySelectorAll("[data-review-chart-mode]").forEach((button) => button.addEventListener("click", () => {
    state.tradeReview.chartMode = button.dataset.reviewChartMode;
    panel.querySelectorAll("[data-review-chart-mode]").forEach((item) => item.classList.toggle("active", item === button));
    drawHistoricalTradeReview();
  }));
  const canvas = panel.querySelector(".trade-review-canvas");
  canvas.addEventListener("pointermove", (event) => {
    const geometry = state.tradeReview.geometry;
    if (!geometry?.points?.length) return;
    const rect = canvas.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const timestamp = geometry.start + ((pointerX - geometry.left) / (geometry.width - geometry.left - geometry.right)) * (geometry.end - geometry.start);
    let nearest = 0;
    geometry.points.forEach((point, index) => {
      if (Math.abs(point.timestamp - timestamp) <= Math.abs(geometry.points[nearest].timestamp - timestamp)) nearest = index;
    });
    selectHistoricalPoint(nearest, pointerX);
  });
  canvas.addEventListener("pointerleave", () => {
    const tooltip = panel.querySelector(".trade-review-tooltip");
    if (tooltip) tooltip.hidden = true;
  });
  canvas.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    selectHistoricalPoint((state.tradeReview.selectedIndex ?? 0) + (event.key === "ArrowRight" ? 1 : -1));
  });
}

async function toggleHistoricalTradeReview(row) {
  const tradeRef = row.dataset.reviewRef;
  const mode = row.dataset.reviewMode;
  if (!tradeRef || row.dataset.reviewAvailable !== "true") return;
  if (
    state.tradeReview.tradeRef === tradeRef
    && state.tradeReview.mode === mode
    && document.querySelector(".trade-review-row")
  ) {
    document.querySelector(".trade-review-row")?.remove();
    state.tradeReview = { ...state.tradeReview, tradeRef: null, mode: null, data: null, selectedIndex: null };
    row.setAttribute("aria-expanded", "false");
    return;
  }
  document.querySelector(".trade-review-row")?.remove();
  document.querySelectorAll(".trade-ledger-row[aria-expanded='true']").forEach((item) => item.setAttribute("aria-expanded", "false"));
  row.setAttribute("aria-expanded", "true");
  const reviewRow = document.createElement("tr");
  reviewRow.className = "trade-review-row";
  reviewRow.innerHTML = '<td colspan="10"><div class="trade-review-loading">Loading saved market history…</div></td>';
  row.after(reviewRow);
  const token = ++state.tradeReview.requestToken;
  state.tradeReview = { ...state.tradeReview, tradeRef, mode, data: null, chartMode: "btc", selectedIndex: null, requestToken: token };
  try {
    const review = await api(`/api/trading/${mode}/reviews/${encodeURIComponent(tradeRef)}`, { cache: "no-store" });
    if (token !== state.tradeReview.requestToken || !reviewRow.isConnected) return;
    state.tradeReview.data = review;
    reviewRow.innerHTML = `<td colspan="10"><section class="trade-review-expanded" aria-label="Historical trade review">
      <div class="trade-review-header"><div><p class="eyebrow">HISTORICAL TRADE REVIEW</p><h3>${escapeHtml(review.trade?.ticker || tradeRef)} · ${marketSideLabel(review.trade?.side)}</h3></div>
        <div class="segmented trade-review-switch"><button class="active" type="button" data-review-chart-mode="btc">BTC</button><button type="button" data-review-chart-mode="volatility">Volatility</button></div></div>
      ${historicalReviewSummary(review)}
      ${historicalReviewCoverageWarning(review)}
      <div class="trade-review-chart-shell"><canvas class="trade-review-canvas" tabindex="0" aria-label="Historical market chart. Use left and right arrow keys to inspect saved points."></canvas><div class="trade-review-tooltip" hidden></div></div>
      <div class="trade-review-legend"><span><i class="btc"></i>BTC proxy</span><span><i class="threshold"></i>Threshold</span><span><i class="entry"></i>Entry</span><span><i class="exit"></i>Exit</span><span><i class="settlement"></i>Settlement</span><small>Crosshair snaps to saved 5-second observations. Shaded areas are explicit recording gaps.</small></div>
      <div class="trade-review-metrics">${reviewMetricContent(null)}</div>
    </section></td>`;
    bindHistoricalReviewPanel(reviewRow.querySelector(".trade-review-expanded"));
    drawHistoricalTradeReview();
  } catch (error) {
    reviewRow.innerHTML = `<td colspan="10"><div class="trade-review-loading error">${escapeHtml(error.message)}</div></td>`;
  }
}

async function loadPaper() {
  const trading = await api("/api/trading/selected");
  const mode = trading.selected_mode || "PAPER";
  const data = trading.selected || {};
  state.trading.mode = mode;
  if (state.dashboard) {
    state.dashboard.trading = { ...state.dashboard.trading, ...trading };
  }
  const readiness = data.readiness || {};
  $("#trading-page-title").textContent = modeLabel(mode);
  $("#trading-page-kicker").textContent = mode === "PAPER" ? "FORWARD TEST" : "KALSHI ACCOUNT";
  $("#trading-page-badge").textContent = mode === "PAPER" ? "SIMULATED ONLY" : mode;
  $("#trading-page-badge").classList.toggle("live", mode === "LIVE");
  $("#reset-paper-round").hidden = mode !== "PAPER";
  $("#exchange-actions").hidden = mode === "PAPER";
  $("#position-section").hidden = mode === "PAPER";
  $("#trading-command-status").textContent = mode === "PAPER"
    ? "Paper is ready. No exchange order is placed."
    : readiness.blocker || `${modeLabel(mode)} is reconciled and ${readiness.session_armed ? "armed" : "disarmed"}.`;
  $("#trading-command-status").classList.toggle("blocked", mode !== "PAPER" && !readiness.ready_for_manual);
  if (readiness.session_armed || state.trading.armConfirmation.mode !== mode) {
    clearTimeout(state.trading.armConfirmation.timer);
    state.trading.armConfirmation.mode = null;
    state.trading.armConfirmation.confirming = false;
    state.trading.armConfirmation.submitting = false;
    state.trading.armConfirmation.timer = null;
  }
  syncArmButton(readiness, mode);
  $("#kill-trading").textContent = readiness.kill_switch ? "Release kill switch" : "Kill switch";
  $("#automatic-trading-toggle").checked = Boolean(readiness.automatic_armed);
  $("#automatic-trading-toggle").disabled = !readiness.session_armed || readiness.kill_switch;
  $$('[data-trading-mode]').forEach((button) => button.classList.toggle("active", button.dataset.tradingMode === mode));
  const pnlClass = data.realized_pnl > 0 ? "positive" : data.realized_pnl < 0 ? "negative" : "";
  $("#paper-stats").innerHTML = mode === "PAPER" ? [
      statCard("Current bankroll", money(data.current_bankroll), `Started ${money(data.starting_bankroll)}`),
      statCard("Realized P&L", money(data.realized_pnl), percent(data.realized_return_pct), pnlClass),
      statCard("Record", `${data.wins}–${data.losses}`, `${data.open_positions} open`),
      statCard("Average edge", points(data.average_edge), "At entry"),
      statCard("Maximum drawdown", percent(data.max_drawdown_pct), "Settled equity"),
    ].join("") : [
      statCard("Available", money(data.available_cash), readiness.authenticated ? "Kalshi account" : "Not authenticated"),
      statCard("Portfolio value", money(data.portfolio_value), `${data.open_positions || 0} unsettled position${data.open_positions === 1 ? "" : "s"}`),
      statCard("Allocated", money(data.allocated_capital), `${money(data.remaining_allocation)} remaining`),
      statCard("Resting orders", data.open_order_count || 0, `${money(data.allocation_cap)} cap`),
      statCard("Actual fees", money(data.actual_fees, 4), "Account fill history"),
    ].join("");
  const trades = mode === "PAPER" ? data.trades || [] : data.ledger || [];
  $("#trade-table").innerHTML = trades.length ? trades.map((trade) => {
    const reviewAvailable = Boolean(trade.review_available);
    const reviewRecording = trade.review_status === "RECORDING";
    const reviewUnavailable = !reviewAvailable && !reviewRecording
      && !["OPEN", "UNSETTLED", "PARTIALLY CLOSED"].includes(String(trade.status || "").toUpperCase());
    const reviewTitle = reviewAvailable
      ? "Open historical trade review"
      : reviewRecording || String(trade.status || "").toUpperCase() === "OPEN"
        ? "Review becomes available after settlement"
        : "Historical review was not recorded for this trade";
    return `
    <tr class="trade-ledger-row ${reviewAvailable ? "review-available" : ""}" data-review-ref="${escapeHtml(trade.review_ref || "")}" data-review-mode="${mode}" data-review-available="${reviewAvailable}" tabindex="${reviewAvailable ? "0" : "-1"}" aria-expanded="false" title="${escapeHtml(reviewTitle)}"><td>${shortDate(trade.activity_at || trade.opened_at || trade.filled_at)}${reviewUnavailable ? '<small class="review-unavailable">Historical review unavailable</small>' : ""}</td><td>${escapeHtml(trade.ticker)}</td><td>${marketSideLabel(trade.side)}</td>
    <td>${cents(trade.entry_price ?? trade.price)}</td><td>${trade.contracts}</td><td>${String(trade.strategy || trade.source || "automatic").replaceAll("_", " ").toUpperCase()}${(trade.entries || []).some((entry) => entry.stop_status === "active") ? " · STOP ACTIVE" : ""}</td><td>${mode === "PAPER" ? points(trade.edge) : "—"}</td>
    <td><span class="status-pill ${String(trade.display_status || trade.status || "filled").toLowerCase()}">${String(trade.display_status || trade.status || trade.action || "FILLED").toUpperCase()}</span></td>
    <td class="${Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : ""}">${trade.realized_pnl == null ? "--" : money(trade.realized_pnl)}</td>
    <td>${trade.available_cash_after == null ? "—" : money(trade.available_cash_after)}</td></tr>
  `; }).join("") : `<tr><td colspan="10" class="empty-state">No ${mode === "PAPER" ? "paper trades" : "confirmed fills"} yet.</td></tr>`;
  if (mode !== "PAPER") {
    const profitTake = data.profit_take_state || {};
    const stopState = data.stop_loss_state || {};
    $("#protection-warning").textContent = `${stopState.warning || "Stop-loss execution requires the Kalshi Model to remain running and connected."} ${profitTake.warning || "Profit taking requires an armed, reconciled connection."}`;
    $("#position-table").innerHTML = (data.positions || []).length ? data.positions.map((position) => `
      <tr><td>${position.ticker}</td><td>${marketSideLabel(position.side)}</td><td>${position.contracts}</td>
      <td>${money(position.market_exposure)}</td><td>${position.stop_loss_price == null ? "Off" : cents(position.stop_loss_price)}</td>
      <td>${profitTake.enabled === false ? "Off" : cents(profitTake.trigger_price ?? .99)}</td><td>${String(position.display_status || position.status).toUpperCase()}</td></tr>
    `).join("") : '<tr><td colspan="7" class="empty-state">No unsettled positions.</td></tr>';
    $("#exchange-order-table").innerHTML = (data.orders || []).length ? data.orders.map((order) => `
      <tr><td>${shortDate(order.updated_at)}</td><td>${order.ticker}</td><td>${marketSideLabel(order.side)}</td><td>${order.action}</td>
      <td>${order.filled_contracts}</td><td>${order.remaining_contracts}</td><td>${cents(order.limit_price)}</td><td>${order.status}</td>
      <td>${["ACKNOWLEDGED", "RESTING", "PARTIALLY_FILLED"].includes(order.status) ? `<button class="table-action" data-cancel-exchange-order="${order.exchange_order_id}">Cancel</button>` : ""}</td></tr>
    `).join("") : '<tr><td colspan="9" class="empty-state">No exchange orders.</td></tr>';
  }
}

function setPaperResetState({ confirming = false, resetting = false } = {}) {
  state.paperReset.confirming = confirming;
  state.paperReset.resetting = resetting;
  const button = $("#reset-paper-round");
  button.classList.toggle("confirming", confirming);
  button.disabled = resetting;
  button.querySelector("[data-reset-label]").textContent = resetting
    ? "Resetting"
    : confirming ? "Confirm reset" : "Reset round";
}

async function resetPaperRound() {
  if (state.paperReset.resetting) return;
  if (!state.paperReset.confirming) {
    clearTimeout(state.paperReset.timer);
    setPaperResetState({ confirming: true });
    showToast(
      "Reset paper round?",
      "Click Confirm reset within 6 seconds to clear simulated orders, positions, and trade history.",
    );
    state.paperReset.timer = setTimeout(() => setPaperResetState(), 6000);
    return;
  }

  clearTimeout(state.paperReset.timer);
  setPaperResetState({ confirming: true, resetting: true });
  try {
    const result = await api("/api/paper/reset", { method: "POST" });
    if (state.dashboard) state.dashboard.paper = result.portfolio;
    await refreshDashboard();
    await loadPaper();
    showToast(
      "New paper round started",
      `${result.reset.cleared_trades} trades and ${result.reset.cleared_orders} orders cleared.`,
    );
  } catch (error) {
    showToast("Paper round not reset", error.message);
  } finally {
    setPaperResetState();
  }
}

const percentSettingIds = ["min_edge", "fractional_kelly", "max_position_pct", "max_risk_per_trade_pct", "max_session_drawdown_pct"];

async function loadSettings() {
  const [database, credentials, demoCredentials, liveCredentials, mobileMonitor] = await Promise.all([
    api("/api/database"), api("/api/credentials"),
    api("/api/trading/credentials/DEMO"), api("/api/trading/credentials/LIVE"),
    api("/api/mobile-monitor"),
  ]);
  $("#database-path").textContent = database.path;
  $("#database-counts").innerHTML = Object.entries(database.counts).map(([key, value]) => `<span>${key.replaceAll("_", " ")}: ${value}</span>`).join("");
  renderCredentialStatus(credentials);
  renderTradingCredentialStatus(demoCredentials);
  renderTradingCredentialStatus(liveCredentials);
  renderMobileMonitor(mobileMonitor);
  syncThemeButtons();
}

function renderMobileMonitor(monitor) {
  const enabled = Boolean(monitor?.enabled);
  $("#mobile-monitor-enabled").checked = enabled;
  $("#mobile-monitor-status").textContent = enabled ? "Enabled" : "Disabled";
  $("#mobile-monitor-status").classList.toggle("configured", enabled);
  $("#mobile-monitor-port").textContent = monitor?.port ?? "--";
  $("#mobile-monitor-local-status").textContent = enabled
    ? monitor?.tailscale_ready ? "Running · Tailscale ready" : "Running · Tailscale update required"
    : "Disabled";
  $("#mobile-monitor-local-url").value = monitor?.local_url || "";
  $("#mobile-monitor-private-url").value = monitor?.private_url || monitor?.detected_private_url || "";
  $("#mobile-monitor-tailscale-command").textContent = monitor?.tailscale_command || "--";
  $("#copy-mobile-monitor-url").disabled = !monitor?.tailscale_ready;
  const result = $("#mobile-monitor-result");
  if (enabled && monitor?.tailscale_issue) {
    result.hidden = false;
    result.textContent = monitor.tailscale_issue;
  }
}

async function updateMobileMonitor(event) {
  const input = event.currentTarget;
  const result = $("#mobile-monitor-result");
  input.disabled = true;
  result.hidden = false;
  result.textContent = input.checked ? "Enabling the read-only monitor..." : "Disabling the mobile monitor...";
  try {
    const monitor = await api("/api/mobile-monitor", {
      method: "PUT", body: JSON.stringify({ enabled: input.checked }),
    });
    renderMobileMonitor(monitor);
    result.textContent = monitor.enabled
      ? monitor.tailscale_ready
        ? "Mobile Monitor and Tailscale Serve are ready."
        : monitor.tailscale_issue || "Configure Tailscale Serve for private iPhone access."
      : "Mobile Monitor is disabled.";
  } catch (error) {
    input.checked = !input.checked;
    result.textContent = error.message;
  } finally {
    input.disabled = false;
  }
}

async function copyMobileMonitorUrl() {
  const value = $("#mobile-monitor-private-url").value;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showToast("Private URL copied", "Open it in Safari on your Tailscale-connected iPhone.");
  } catch (_) {
    $("#mobile-monitor-private-url").select();
    showToast("Select and copy the URL", "Clipboard access was unavailable.");
  }
}

async function copyMobileMonitorCommand() {
  const value = $("#mobile-monitor-tailscale-command").textContent.trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showToast("Tailscale command copied", "Paste it into Terminal and press Return.");
  } catch (_) {
    showToast("Unable to copy command", "Select the command and copy it manually.");
  }
}

async function refreshMobileMonitorUrl() {
  const button = $("#refresh-mobile-monitor-url");
  const result = $("#mobile-monitor-result");
  button.disabled = true;
  result.hidden = false;
  result.textContent = "Checking Tailscale Serve...";
  try {
    const monitor = await api("/api/mobile-monitor");
    renderMobileMonitor(monitor);
    result.textContent = monitor.tailscale_ready && monitor.private_url
      ? "Private Tailscale URL found. Open it on your iPhone."
      : monitor.tailscale_issue || "No private URL found yet. Run the copied command, then retry Refresh.";
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderTradingCredentialStatus(credentials) {
  const mode = credentials.mode;
  const stateLabel = $(`[data-trading-credential-state="${mode}"]`);
  stateLabel.textContent = credentials.configured
    ? credentials.source === "environment" ? "Configured in .env" : "Configured"
    : "Not configured";
  stateLabel.classList.toggle("configured", credentials.configured);
  const keyInput = $(`[data-trading-key-id="${mode}"]`);
  keyInput.value = "";
  keyInput.placeholder = credentials.key_id_hint ? `Saved as ${credentials.key_id_hint}` : `${mode} API Key ID`;
  const remove = $(`[data-remove-trading-credentials="${mode}"]`);
  remove.disabled = !credentials.local_credentials_saved;
}

async function saveTradingCredentials(event) {
  event.preventDefault();
  const mode = event.currentTarget.dataset.tradingCredentialForm;
  const keyId = $(`[data-trading-key-id="${mode}"]`).value.trim();
  const fileInput = $(`[data-trading-key-file="${mode}"]`);
  const file = fileInput.files?.[0];
  const panel = $(`[data-trading-credential-result="${mode}"]`);
  panel.hidden = false;
  if (!keyId || !file) {
    panel.textContent = `Enter the ${mode} Key ID and choose its private key file.`;
    return;
  }
  try {
    panel.textContent = "Validating, saving, and reconciling…";
    const credentials = await api(`/api/trading/credentials/${mode}`, {
      method: "POST", body: JSON.stringify({ key_id: keyId, private_key: await file.text() }),
    });
    fileInput.value = "";
    renderTradingCredentialStatus(credentials);
    panel.textContent = credentials.readiness?.reconciled
      ? `${modeLabel(mode)} credentials saved and reconciled.`
      : `${modeLabel(mode)} credentials saved. ${credentials.readiness?.last_error || "Reconcile before arming."}`;
    await refreshDashboard();
  } catch (error) { panel.textContent = error.message; }
}

async function removeTradingCredentials(mode) {
  const panel = $(`[data-trading-credential-result="${mode}"]`);
  panel.hidden = false;
  try {
    const credentials = await api(`/api/trading/credentials/${mode}`, { method: "DELETE" });
    renderTradingCredentialStatus(credentials);
    panel.textContent = `${modeLabel(mode)} credentials removed and the session disarmed.`;
    await refreshDashboard();
  } catch (error) { panel.textContent = error.message; }
}

async function verifyDemoTrading() {
  const phrase = window.prompt('Type VERIFY DEMO TRADING to create, acknowledge, cancel, and reconcile a one-contract Demo order.');
  if (phrase == null) return;
  const panel = $('[data-trading-credential-result="DEMO"]');
  panel.hidden = false;
  panel.textContent = "Running Demo verification…";
  try {
    const result = await api("/api/trading/demo/verify", {
      method: "POST", body: JSON.stringify({ confirmation: phrase }),
    });
    panel.textContent = result.verified
      ? "Demo verification passed. Live arming is now eligible after reviewing Live limits."
      : "Demo verification did not complete.";
    await refreshDashboard();
  } catch (error) { panel.textContent = error.message; }
}

async function markLiveLimitsReviewed() {
  try {
    await api("/api/trading/live/limits-reviewed", { method: "POST" });
    showToast("Live limits reviewed", "Live arming still requires successful Demo verification and deliberate session confirmation.");
    await refreshDashboard();
  } catch (error) { showToast("Limits not marked reviewed", error.message); }
}

function renderCredentialStatus(credentials) {
  const stateLabel = $("#credential-state");
  stateLabel.textContent = credentials.configured
    ? credentials.source === "environment" ? "Configured in .env" : "Configured"
    : "Not configured";
  stateLabel.classList.toggle("configured", credentials.configured);
  $("#credential-storage-path").textContent = credentials.storage_directory;
  const keyInput = $("#kalshi-key-id");
  keyInput.value = "";
  keyInput.placeholder = credentials.key_id_hint
    ? `Saved as ${credentials.key_id_hint}`
    : "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx";
  const removeButton = $("#remove-credentials");
  removeButton.disabled = !credentials.local_credentials_saved;
  removeButton.title = credentials.local_credentials_saved
    ? "Remove the credentials saved by this form"
    : credentials.source === "environment"
      ? "These credentials are managed in .env"
      : "No locally saved credentials";
}

async function saveKalshiCredentials(event) {
  event.preventDefault();
  const keyId = $("#kalshi-key-id").value.trim();
  const fileInput = $("#kalshi-private-key");
  const file = fileInput.files?.[0];
  const resultPanel = $("#credential-result");
  resultPanel.hidden = false;
  if (!keyId || !file) {
    resultPanel.textContent = "Enter the API Key ID and choose its downloaded private key file.";
    return;
  }

  const button = $("#save-credentials");
  button.disabled = true;
  resultPanel.textContent = "Validating and saving credentials on this Mac...";
  try {
    const credentials = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({ key_id: keyId, private_key: await file.text() }),
    });
    fileInput.value = "";
    $("#credential-file-name").textContent = "Choose the file downloaded when the key was created.";
    renderCredentialStatus(credentials);
    resultPanel.textContent = "Credentials saved securely. The Kalshi WebSocket is connecting now.";
    showToast("Kalshi credentials saved", "Live market-data streaming is connecting with the new key.");
    await refreshDashboard();
  } catch (error) {
    resultPanel.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function removeKalshiCredentials() {
  const button = $("#remove-credentials");
  const resultPanel = $("#credential-result");
  button.disabled = true;
  resultPanel.hidden = false;
  resultPanel.textContent = "Removing locally saved credentials...";
  try {
    const credentials = await api("/api/credentials", { method: "DELETE" });
    renderCredentialStatus(credentials);
    resultPanel.textContent = credentials.configured
      ? "Local credentials removed. Credentials from .env are still active."
      : "Local credentials removed. Kalshi is using REST fallback.";
    showToast("Saved credentials removed", "The private key copy was deleted from local app storage.");
    await refreshDashboard();
  } catch (error) {
    resultPanel.textContent = error.message;
    button.disabled = false;
  }
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
  $$("[data-page]").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  $$(".page").forEach((section) => section.classList.toggle("active", section.id === `page-${page}`));
  window.scrollTo({ top: 0, behavior: "auto" });
  try {
    if (page === "calibration") await loadCalibration();
    if (page === "paper") await loadPaper();
    if (page === "settings") await loadSettings();
  } catch (error) { showToast("Unable to load view", error.message); }
}

function bindEvents() {
  $$("[data-page]").forEach((button) => button.addEventListener("click", () => switchPage(button.dataset.page)));
  $$("[data-trading-mode]").forEach((button) => button.addEventListener("click", () => selectTradingMode(button.dataset.tradingMode)));
  $$("[data-window]").forEach((button) => button.addEventListener("click", async () => {
    state.chartWindow = Number(button.dataset.window);
    resetChartAxis();
    $$("[data-window]").forEach((item) => item.classList.toggle("active", item === button));
    const chart = await api(`/api/chart?minutes=${state.chartWindow}`);
    state.chartPoints = chart.points || [];
    state.volatilityPoints = chart.volatility_points || [];
    state.maximumMvi = Number(chart.maximum_margin_volatility || 0);
    drawChart();
  }));
  $$('[data-chart-mode]').forEach((button) => button.addEventListener("click", () => {
    state.chartMode = button.dataset.chartMode === "volatility" ? "volatility" : "btc";
    $$('[data-chart-mode]').forEach((item) => item.classList.toggle("active", item === button));
    resetChartAxis();
    drawChart();
  }));
  $("#credential-form").addEventListener("submit", saveKalshiCredentials);
  $("#remove-credentials").addEventListener("click", removeKalshiCredentials);
  $$("[data-trading-credential-form]").forEach((form) => form.addEventListener("submit", saveTradingCredentials));
  $$("[data-remove-trading-credentials]").forEach((button) => button.addEventListener("click", () => removeTradingCredentials(button.dataset.removeTradingCredentials)));
  $("[data-verify-demo]").addEventListener("click", verifyDemoTrading);
  $("[data-review-live-limits]").addEventListener("click", markLiveLimitsReviewed);
  $("#kalshi-private-key").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    $("#credential-file-name").textContent = file
      ? `${file.name} selected`
      : "Choose the file downloaded when the key was created.";
  });
  $("#run-backtest").addEventListener("click", runBacktest);
  $("#reset-paper-round").addEventListener("click", resetPaperRound);
  $("#run-bootstrap").addEventListener("click", runBootstrap);
  $("#backup-database").addEventListener("click", backupDatabase);
  $("#mobile-monitor-enabled").addEventListener("change", updateMobileMonitor);
  $("#copy-mobile-monitor-url").addEventListener("click", copyMobileMonitorUrl);
  $("#copy-mobile-monitor-command").addEventListener("click", copyMobileMonitorCommand);
  $("#refresh-mobile-monitor-url").addEventListener("click", refreshMobileMonitorUrl);
  $$('[data-paper-action]').forEach((button) => button.addEventListener("click", () => {
    state.paperOrder.action = button.dataset.paperAction;
    renderPaperController();
    renderTradeAssessment();
  }));
  $$('[data-paper-side]').forEach((button) => button.addEventListener("click", () => {
    handlePaperSide(button.dataset.paperSide);
  }));
  $("#manual-order-toggle").addEventListener("click", () => toggleManualOrder());
  $("#paper-limit-toggle").addEventListener("change", (event) => {
    state.paperOrder.limit = event.target.checked;
    $("#paper-dollars").value = "";
    $("#paper-contracts").value = "";
    $("#paper-limit-price").value = "";
    renderPaperController();
  });
  ["#paper-dollars", "#paper-contracts", "#paper-limit-price", "#paper-stop-loss"].forEach((selector) => {
    $(selector).addEventListener("input", renderPaperController);
  });
  $("#paper-submit").addEventListener("click", submitPaperOrder);
  $("#confirm-exchange-order").addEventListener("click", confirmExchangeOrder);
  $("#trade-confirmation").addEventListener("close", () => { state.trading.pendingConfirmation = null; });
  $("#reconcile-trading").addEventListener("click", reconcileSelectedTrading);
  $$('[data-arm-session]').forEach((button) => button.addEventListener("click", armSelectedTrading));
  $("#kill-trading").addEventListener("click", killSelectedTrading);
  $("#automatic-trading-toggle").addEventListener("change", toggleAutomaticTrading);
  $("#open-order-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-cancel-paper-order]");
    if (button) cancelPaperOrder(Number(button.dataset.cancelPaperOrder));
  });
  $("#exchange-order-table").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel-exchange-order]");
    if (!button) return;
    const { mode } = selectedTrading();
    try {
      await api(`/api/trading/${mode}/orders/${button.dataset.cancelExchangeOrder}`, { method: "DELETE" });
      await refreshDashboard();
      await loadPaper();
    } catch (error) { showToast("Unable to cancel order", error.message); }
  });
  $("#trade-table").addEventListener("click", (event) => {
    const row = event.target.closest(".trade-ledger-row.review-available");
    if (row) toggleHistoricalTradeReview(row);
  });
  $("#trade-table").addEventListener("keydown", (event) => {
    const row = event.target.closest(".trade-ledger-row.review-available");
    if (row && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      toggleHistoricalTradeReview(row);
    }
  });
  $("#calibration-controls").addEventListener("input", markCalibrationDirty);
  $("#calibration-controls").addEventListener("change", markCalibrationDirty);
  $("#apply-calibration").addEventListener("click", applyCalibration);
  $("#discard-calibration").addEventListener("click", () => setCalibrationValues(state.calibration.saved));
  $("#restore-defaults").addEventListener("click", () => {
    setCalibrationValues(state.calibration.defaults);
    markCalibrationDirty();
  });
  $("#configuration-snapshots").addEventListener("click", (event) => {
    const button = event.target.closest("[data-restore-snapshot]");
    if (button) restoreConfiguration(Number(button.dataset.restoreSnapshot));
  });
  $$('[data-theme-choice]').forEach((button) => button.addEventListener("click", () => {
    applyTheme(button.dataset.themeChoice);
  }));
  themeMedia.addEventListener("change", () => {
    if (state.themePreference === "system") applyTheme("system");
  });
  window.addEventListener("resize", () => {
    drawChart();
    drawHistoricalTradeReview();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  applyTheme(state.themePreference);
  bindEvents();
  window.requestAnimationFrame(animateChart);
  await refreshDashboard();
  connectLive();
  setInterval(refreshDashboard, 15000);
  setInterval(updateCountdown, 1000);
});
