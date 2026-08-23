const state = {
  dashboard: null,
  chartPoints: [],
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
  paperOrder: { side: "YES", action: "BUY", limit: false, submitting: false },
  paperReset: { confirming: false, resetting: false, timer: null },
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

function signalPosition(decision) {
  if (decision?.signal === "TRADE YES") return "up";
  if (decision?.signal === "TRADE NO") return "down";
  return "hold";
}

function signalTitle(decision) {
  if (!decision || signalPosition(decision) === "hold") return "";
  return `${decision.confidence} Confidence`;
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

function renderOrderBook(outcome, orderbook) {
  const prefix = outcome === "YES" ? "yes" : "no";
  const target = $(`#${outcome === "YES" ? "up" : "down"}-orderbook-rows`);
  if (!target) return;
  const bids = normalizeBookLevels(orderbook?.[`${prefix}_bids`]);
  const asks = normalizeBookLevels(orderbook?.[`${prefix}_asks`]);
  if (!bids.length && !asks.length) {
    target.innerHTML = '<tr><td class="book-empty" colspan="3">Waiting for live depth</td></tr>';
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

function renderOrderBooks(orderbook) {
  renderOrderBook("YES", orderbook);
  renderOrderBook("NO", orderbook);
}

function renderDashboard(data) {
  state.dashboard = data;
  const system = data.system || {};
  const current = data.current;
  const btc = data.btc || {};
  const decision = current?.decision;
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

  const position = signalPosition(decision);
  const pill = $("#signal-pill");
  pill.dataset.position = position;
  pill.setAttribute("aria-label", `Current signal: ${position === "up" ? "Buy Up" : position === "down" ? "Buy Down" : "Hold"}`);
  pill.querySelectorAll("[data-signal-option]").forEach((option) => {
    option.classList.toggle("active", option.dataset.signalOption === position);
  });
  const confidence = signalTitle(decision);
  const signalTitleElement = $("#signal-title");
  signalTitleElement.textContent = confidence;
  signalTitleElement.hidden = !confidence;
  const paperPermission = $("#paper-permission");
  const paper = data.paper || {};
  const automaticTradingEnabled = Boolean(paper.automatic_trading_enabled);
  const paperBlocked = automaticTradingEnabled && paper.automatic_trade_allowed === false;
  const automaticTradingStatus = paperBlocked
    ? `Automatic trading paused · ${paper.automatic_trade_block_reason || "Risk control active."}`
    : automaticTradingEnabled
      ? "Automatic trading on"
      : "Automatic trading off";
  paperPermission.hidden = false;
  paperPermission.classList.toggle("on", automaticTradingEnabled && !paperBlocked);
  paperPermission.classList.toggle("off", !automaticTradingEnabled);
  paperPermission.classList.toggle("paused", paperBlocked);
  paperPermission.textContent = automaticTradingStatus;
  paperPermission.title = automaticTradingStatus;
  $("#signal-explanation").textContent = formatMarketLanguage(decision?.explanation || system.message || "Connecting to public feeds.");
  $("#model-probability").textContent = percent(decision?.model_probability, 1);
  $("#market-probability").textContent = percent(decision?.market_probability, 1);
  $("#edge").textContent = points(decision?.edge);
  $("#ev").textContent = decision?.expected_value === null || decision?.expected_value === undefined ? "--" : money(decision.expected_value, 3);
  $("#edge").className = Number(decision?.edge) > 0 ? "positive" : Number(decision?.edge) < 0 ? "negative" : "";
  $("#ev").className = Number(decision?.expected_value) > 0 ? "positive" : Number(decision?.expected_value) < 0 ? "negative" : "";

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

  $("#contract-ticker").textContent = current?.ticker || "No active contract";
  state.closeTime = current?.close_time ? new Date(current.close_time) : null;
  updateCountdown();
  $("#threshold").textContent = money(threshold);
  $("#distance-to-strike").textContent = threshold === null
    ? "Waiting for threshold"
    : distance === null ? "--" : `${distance >= 0 ? "+" : ""}${money(distance)} from threshold`;
  $("#yes-market").textContent = `${percent(current?.yes_bid, 1)} / ${percent(current?.yes_ask, 1)}`;
  $("#no-market").textContent = `${percent(current?.no_bid, 1)} / ${percent(current?.no_ask, 1)}`;
  $("#spread").textContent = points(current?.spread);
  $("#open-interest").textContent = compact(current?.open_interest);
  $("#volume").textContent = compact(current?.volume);
  const positions = (data.paper?.positions || []).filter((item) => item.ticker === current?.ticker);
  $("#position-size").textContent = positions.length
    ? positions.map((item) => `${item.contracts} ${marketSideLabel(item.side)}`).join(" · ")
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

  renderOrderBooks(current?.orderbook || {});
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
  const paper = state.dashboard?.paper || {};
  const ticker = state.dashboard?.current?.ticker;
  const held = (paper.positions || [])
    .filter((position) => position.ticker === ticker && position.side === side)
    .reduce((total, position) => total + Number(position.contracts || 0), 0);
  const reserved = (paper.open_orders || [])
    .filter((order) => order.ticker === ticker && order.side === side && order.action === "SELL")
    .reduce((total, order) => total + Number(order.requested_contracts || 0), 0);
  return Math.max(0, held - reserved);
}

function paperOrderDraft() {
  const paper = state.dashboard?.paper || {};
  const current = state.dashboard?.current;
  const bestPrice = paperQuote();
  const available = Number(paper.available_cash || 0);
  let contracts = 0;
  let referencePrice = bestPrice;
  let requestedValue = 0;
  let error = "";

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
  if (!error && state.paperOrder.action === "BUY" && paper.risk_controls_enabled) {
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
  return { available, bestPrice, contracts, orderValue, requestedValue, error };
}

function renderOpenPaperOrders() {
  const orders = state.dashboard?.paper?.open_orders || [];
  const panel = $("#open-paper-orders");
  panel.hidden = orders.length === 0;
  $("#open-order-count").textContent = orders.length;
  $("#open-order-list").innerHTML = orders.map((order) => `
    <div class="open-order-row">
      <span><strong>${order.action} ${marketSideLabel(order.side).toUpperCase()}</strong><small>${order.requested_contracts} at ${cents(order.limit_price)}</small></span>
      <button type="button" data-cancel-paper-order="${order.id}" aria-label="Cancel ${marketSideLabel(order.side)} limit order">Cancel</button>
    </div>
  `).join("");
}

function renderPaperController() {
  const current = state.dashboard?.current;
  const action = state.paperOrder.action;
  $$('[data-paper-action]').forEach((button) => button.classList.toggle("active", button.dataset.paperAction === action));
  $$('[data-paper-side]').forEach((button) => button.classList.toggle("active", button.dataset.paperSide === state.paperOrder.side));
  $("#paper-limit-toggle").checked = state.paperOrder.limit;
  $("#paper-market-fields").hidden = state.paperOrder.limit;
  $("#paper-limit-fields").hidden = !state.paperOrder.limit;
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
  try {
    const result = await api("/api/paper/orders", { method: "POST", body: JSON.stringify(payload) });
    const status = result.order?.status === "filled" ? "filled" : "placed";
    showToast(`Paper order ${status}`, `${result.order.action} ${result.order.requested_contracts} ${marketSideLabel(result.order.side)} contract${result.order.requested_contracts === 1 ? "" : "s"}.`);
    $("#paper-dollars").value = "";
    $("#paper-contracts").value = "";
    $("#paper-limit-price").value = "";
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

async function cancelPaperOrder(orderId) {
  try {
    await api(`/api/paper/orders/${orderId}`, { method: "DELETE" });
    showToast("Limit order canceled", "Reserved paper bankroll or contracts are available again.");
    await refreshDashboard();
  } catch (error) { showToast("Unable to cancel order", error.message); }
}

function updateCountdown() {
  const value = state.closeTime
    ? countdown((state.closeTime.getTime() - Date.now()) / 1000)
    : "--:--";
  $("#countdown").textContent = value;
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
      <div class="bucket-bars">
        <i style="height:${Math.max(2, bucket.predicted * 100)}%" title="Predicted ${percent(bucket.predicted)}"></i>
        <i class="actual" style="height:${Math.max(2, bucket.actual * 100)}%" title="Actual ${percent(bucket.actual)}"></i>
      </div>
      <label>${bucket.label}</label>
      <small>n=${bucket.count}</small>
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
    <tr><td>${shortDate(trade.opened_at)}</td><td>${trade.ticker}</td><td>${marketSideLabel(trade.side)}</td>
    <td>${percent(trade.entry_price, 1)}</td><td>${trade.contracts}</td><td>${(trade.source || "automatic").toUpperCase()}</td><td>${points(trade.edge)}</td>
    <td><span class="status-pill ${trade.status}">${trade.status.toUpperCase()}</span></td>
    <td class="${Number(trade.realized_pnl) > 0 ? "positive" : Number(trade.realized_pnl) < 0 ? "negative" : ""}">${trade.realized_pnl == null ? "--" : money(trade.realized_pnl)}</td></tr>
  `).join("") : '<tr><td colspan="9" class="empty-state">No paper trades yet.</td></tr>';
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
  const [settings, database, credentials] = await Promise.all([
    api("/api/settings"), api("/api/database"), api("/api/credentials"),
  ]);
  for (const [key, value] of Object.entries(settings)) {
    const input = document.getElementById(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = percentSettingIds.includes(key) ? Number(value) * 100 : value;
  }
  $("#database-path").textContent = database.path;
  $("#database-counts").innerHTML = Object.entries(database.counts).map(([key, value]) => `<span>${key.replaceAll("_", " ")}: ${value}</span>`).join("");
  renderCredentialStatus(credentials);
  syncThemeButtons();
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
  try {
    if (page === "calibration") await loadCalibration();
    if (page === "paper") await loadPaper();
    if (page === "settings") await loadSettings();
  } catch (error) { showToast("Unable to load view", error.message); }
}

function bindEvents() {
  $$("[data-page]").forEach((button) => button.addEventListener("click", () => switchPage(button.dataset.page)));
  $$("[data-window]").forEach((button) => button.addEventListener("click", async () => {
    state.chartWindow = Number(button.dataset.window);
    resetChartAxis();
    $$("[data-window]").forEach((item) => item.classList.toggle("active", item === button));
    const chart = await api(`/api/chart?minutes=${state.chartWindow}`);
    state.chartPoints = chart.points || []; drawChart();
  }));
  $("#save-settings").addEventListener("click", saveSettings);
  $("#credential-form").addEventListener("submit", saveKalshiCredentials);
  $("#remove-credentials").addEventListener("click", removeKalshiCredentials);
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
  $$('[data-paper-action]').forEach((button) => button.addEventListener("click", () => {
    state.paperOrder.action = button.dataset.paperAction;
    renderPaperController();
  }));
  $$('[data-paper-side]').forEach((button) => button.addEventListener("click", () => {
    state.paperOrder.side = button.dataset.paperSide;
    renderPaperController();
  }));
  $("#paper-limit-toggle").addEventListener("change", (event) => {
    state.paperOrder.limit = event.target.checked;
    $("#paper-dollars").value = "";
    $("#paper-contracts").value = "";
    $("#paper-limit-price").value = "";
    renderPaperController();
  });
  ["#paper-dollars", "#paper-contracts", "#paper-limit-price"].forEach((selector) => {
    $(selector).addEventListener("input", renderPaperController);
  });
  $("#paper-submit").addEventListener("click", submitPaperOrder);
  $("#open-order-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-cancel-paper-order]");
    if (button) cancelPaperOrder(Number(button.dataset.cancelPaperOrder));
  });
  $$('[data-theme-choice]').forEach((button) => button.addEventListener("click", () => {
    applyTheme(button.dataset.themeChoice);
  }));
  themeMedia.addEventListener("change", () => {
    if (state.themePreference === "system") applyTheme("system");
  });
  window.addEventListener("resize", drawChart);
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
