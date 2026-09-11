"""Read-only causal replay of Texas entries against stored Coinbase candles.

This is deliberately an analysis tool, not a backtest that can alter trading.
It never fetches candles, writes SQLite, or treats a missing observation as a
negative outcome.  In particular, it cannot establish historical order-book
liquidity or that a sampled proxy did not cross between samples.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any, Iterable

from app.db import Database
from app.domain import kalshi_fee, texas_threshold_breached, texas_unfavorable_distance
from app.services.historical_realized_volatility import (
    GRANULARITY_SECONDS, PRODUCT, SOURCE, realized_volatility,
)


BUCKETS = ("<0.20%", "0.20–<0.40%", "0.40–<0.80%", "≥0.80%")
EPSILON = 1e-9
SWEEP_CHECKPOINT_SECONDS = (180, 300, 420, 600)
SWEEP_UNFAVORABLE_DISTANCE_DOLLARS = (0, 35, 50, 75)
SWEEP_MAX_POINT_GAP_SECONDS = 20.0


def _time_epoch(value: object) -> float | None:
    """Parse an ISO instant without discarding sub-second event ordering."""
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).timestamp()
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value < .20:
        return BUCKETS[0]
    if value < .40:
        return BUCKETS[1]
    if value < .80:
        return BUCKETS[2]
    return BUCKETS[3]


def rv15_at(
    candles: dict[int, float], observed_at: object, *, closed_minute_lag: int = 0,
) -> dict[str, Any]:
    """Rebuild RV15 using only the 16 closes available before ``observed_at``.

    ``closed_minute_lag=1`` is supplied for publication-delay sensitivity;
    neither mode may read a still-forming minute or a candle after the choice.
    """
    observed_epoch = _time_epoch(observed_at)
    if observed_epoch is None or closed_minute_lag < 0:
        return {"available": False, "reason": "invalid decision timestamp"}
    end = int(observed_epoch // GRANULARITY_SECONDS) * GRANULARITY_SECONDS - GRANULARITY_SECONDS
    end -= closed_minute_lag * GRANULARITY_SECONDS
    epochs = [end - (15 - index) * GRANULARITY_SECONDS for index in range(16)]
    missing = [epoch for epoch in epochs if epoch not in candles]
    if missing:
        return {"available": False, "reason": "missing candle window", "missing_minutes": missing,
                "window_end_epoch": end}
    value = realized_volatility([(epoch, candles[epoch]) for epoch in epochs], 15)
    if value is None:
        return {"available": False, "reason": "invalid candle window", "window_end_epoch": end}
    return {"available": True, "rv15_pct": value, "bucket": bucket(value),
            "window_start_epoch": epochs[0], "window_end_epoch": end}


def _json(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _strategy_version(value: object) -> str:
    normalized = str(value or "").upper()
    if normalized == "TEXAS_HOLDEM_2_1":
        return "V21"
    if normalized == "TEXAS_HOLDEM_3_0":
        return "V3"
    return "V2" if normalized == "TEXAS_HOLDEM_2_0" else "LEGACY"


def _is_texas(value: object) -> bool:
    return str(value or "").upper() in {
        "TEXAS_HOLDEM", "TEXAS_HOLDEM_2_0", "TEXAS_HOLDEM_2_1", "TEXAS_HOLDEM_3_0",
    }


def _fill_key(row: dict[str, Any]) -> tuple[float, int]:
    """A stable event order.  A sell never gets to consume a later buy."""
    filled_at = _time_epoch(row.get("filled_at"))
    return (filled_at if filled_at is not None else -1.0, int(row.get("id") or 0))


def _fractional_fill(row: dict[str, Any], contracts: float) -> dict[str, Any]:
    """Return an auditable partial fill, allocating its fee proportionally."""
    total = _number(row.get("contracts")) or 0.0
    copy = dict(row)
    copy["contracts"] = contracts
    if total > EPSILON:
        copy["fee"] = (_number(row.get("fee")) or 0.0) * contracts / total
    return copy


def _fill_pnl(
    buys: Iterable[dict[str, Any]], sells: Iterable[dict[str, Any]], settlement: dict[str, Any] | None,
    side: str,
) -> dict[str, Any]:
    """Chronological FIFO actual P/L.  Intent limits are never used here.

    Fill fees are exact execution cash flows.  ``broker_settlements.fees`` is
    an account-level reconciliation value, not a distinct settlement-fill fee,
    so it is deliberately *not* deducted again here.  A confirmed winning
    settlement pays gross $1 for each remaining contract; a confirmed loser
    pays $0.  This prevents a settlement summary from double-counting fees
    already represented by the fills.
    """
    lots: deque[list[float]] = deque()  # quantity, all-in buy cost
    buy_qty = sell_qty = 0.0
    realized_net = 0.0
    unmatched_sell = 0.0
    events = [(fill, "BUY") for fill in buys] + [(fill, "SELL") for fill in sells]
    for fill, action in sorted(events, key=lambda event: _fill_key(event[0])):
        qty, price, fee = _number(fill.get("contracts")), _number(fill.get("price")), _number(fill.get("fee"))
        if qty is None or price is None or fee is None or qty <= 0:
            continue
        if action == "BUY":
            cost = qty * price + fee
            lots.append([qty, cost])
            buy_qty += qty
            continue
        sell_qty += qty
        matched = min(qty, sum(lot[0] for lot in lots))
        # All sale proceeds/fees are included only to confirmed purchased qty.
        if matched > 0:
            proceeds = matched * price - fee * (matched / qty)
            remaining = matched
            matched_cost = 0.0
            while remaining > EPSILON and lots:
                lot_qty, lot_cost = lots[0]
                take = min(remaining, lot_qty)
                allocated_cost = lot_cost * (take / lot_qty)
                lots[0][0] -= take
                lots[0][1] -= allocated_cost
                matched_cost += allocated_cost
                remaining -= take
                if lots[0][0] <= EPSILON:
                    lots.popleft()
            realized_net += proceeds - matched_cost
        unmatched_sell += max(0.0, qty - matched)
    remaining_qty = sum(lot[0] for lot in lots)
    settlement_payout = 0.0
    settlement_fee = 0.0
    result = str((settlement or {}).get("market_result") or (settlement or {}).get("result") or "").upper()
    valid_results = {"YES", "NO", "0", "1"}
    settlement_state = "NOT_NEEDED" if remaining_qty <= EPSILON else "UNRESOLVED"
    if remaining_qty > EPSILON and result in valid_results:
        won = result in {side.upper(), "1", "YES" if side.upper() == "YES" else "NO"}
        # Explicit YES/NO is authoritative; integer 1 is only YES settlement.
        if result == "1":
            won = side.upper() == "YES"
        if result == "0":
            won = side.upper() == "NO"
        settlement_payout = remaining_qty if won else 0.0
        # See function docstring: this value is intentionally always zero.
        settlement_fee = 0.0
        # The final lots' all-in acquisition cost has not yet been recognized.
        realized_net += settlement_payout - settlement_fee - sum(lot[1] for lot in lots)
        remaining_qty = 0.0
        settlement_state = "CONFIRMED"
    return {"buy_contracts": buy_qty, "sell_contracts": sell_qty,
            "remaining_contracts": remaining_qty, "unmatched_sell_contracts": unmatched_sell,
            "net_pnl": realized_net if buy_qty > EPSILON and remaining_qty <= EPSILON and unmatched_sell <= EPSILON else None,
            "realized_pnl": realized_net if buy_qty > EPSILON else None,
            "open_cost_basis": sum(lot[1] for lot in lots),
            "entry_cost": sum((_number(row.get("contracts")) or 0) * (_number(row.get("price")) or 0) + (_number(row.get("fee")) or 0) for row in buys),
            "settlement_payout": settlement_payout, "settlement_fee": settlement_fee,
            "settlement_state": settlement_state,
            "settlement_fee_semantics": "not applied; per-fill execution fees are authoritative",
            "accounting_ambiguous": unmatched_sell > EPSILON}


def _point_crossing(
    points: Iterable[dict[str, Any]], side: str, threshold: float | None,
    first_fill_at: object, market_close_time: object,
) -> dict[str, Any]:
    first_fill_epoch = _time_epoch(first_fill_at)
    close_epoch = _time_epoch(market_close_time)
    if threshold is None or first_fill_epoch is None or close_epoch is None:
        return {"reliable_points": 0, "observed_touch": None, "observed_strict_breach": None,
                "no_recorded_crossing": None, "window_bounded": False,
                "reason": "missing threshold, first fill, or market close time"}
    reliable = []
    for point in points:
        epoch, price = _time_epoch(point.get("observed_at")), _number(point.get("btc_proxy"))
        if (epoch is not None and first_fill_epoch <= epoch <= close_epoch
                and int(point.get("data_reliable") or 0) and price is not None):
            reliable.append(price)
    if not reliable:
        return {"reliable_points": 0, "observed_touch": None, "observed_strict_breach": None,
                "no_recorded_crossing": None, "window_bounded": True}
    up = side.upper() == "YES"
    touch = any(price >= threshold if up else price <= threshold for price in reliable)
    strict = any(price > threshold if up else price < threshold for price in reliable)
    return {"reliable_points": len(reliable), "observed_touch": touch,
            "observed_strict_breach": strict, "no_recorded_crossing": not touch,
            "window_bounded": True,
            "absence_disclaimer": "No recorded crossing is not proof of no crossing between samples or gaps."}


def _attributed_sells(
    exact_buys: list[dict[str, Any]], fills: list[dict[str, Any]],
    position: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return trusted Texas sells plus explicit ambiguity records.

    Texas-tagged sell fills are trusted as Texas execution evidence.  A manual
    or unclassified sell is only attributed up to earlier *exactly linked*
    Texas buys, and never if another strategy could have owned that position.
    """
    exact_ids = {str(row.get("id")) for row in exact_buys}
    exact_open = 0.0
    selected: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    other_buy_seen = False
    position_ambiguous = bool(
        position and _number(position.get("contracts")) and (_number(position.get("contracts")) or 0) > EPSILON
        and not _is_texas(position.get("strategy"))
    )
    if position_ambiguous:
        ambiguities.append({"reason": "open non-Texas or unclassified position"})
    for fill in sorted(fills, key=_fill_key):
        action = str(fill.get("action") or "").upper()
        if action == "BUY":
            if str(fill.get("id")) in exact_ids:
                exact_open += _number(fill.get("contracts")) or 0.0
            else:
                other_buy_seen = True
            continue
        if action != "SELL":
            continue
        qty = _number(fill.get("contracts")) or 0.0
        if qty <= EPSILON:
            continue
        if _is_texas(fill.get("strategy")):
            selected.append(fill)
            exact_open = max(0.0, exact_open - qty)
            continue
        if other_buy_seen or position_ambiguous:
            ambiguities.append({"fill_id": fill.get("fill_id"), "reason": "manual sell ownership ambiguous"})
            continue
        attributable = min(qty, exact_open)
        if attributable > EPSILON:
            selected.append(_fractional_fill(fill, attributable))
            exact_open -= attributable
        if qty - attributable > EPSILON:
            ambiguities.append({"fill_id": fill.get("fill_id"), "reason": "manual sell exceeds earlier exact Texas buys",
                                "unattributed_contracts": qty - attributable})
    return selected, ambiguities


def _summary(rows: list[dict[str, Any]], *, attempt_key: str = "first_attempt") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label in BUCKETS:
        # The default is the first submitted attempt, including unfilled ones.
        # A separately named first-funded view is supplied below for comparison.
        selected = [row for row in rows if (row.get(attempt_key) or {}).get("rv", {}).get("bucket") == label]
        filled = [row for row in selected if row.get("pnl", {}).get("buy_contracts", 0) > EPSILON]
        resolved = [row for row in filled if row.get("pnl", {}).get("net_pnl") is not None]
        crossings = [row for row in filled if row.get("crossing", {}).get("observed_touch") is not None]
        result[label] = {
            "attempt_rounds": len(selected), "filled_rounds": len(filled),
            "paper_broker_fill_unavailable_rounds": sum(
                row.get("accounting_status") == "UNAVAILABLE_PAPER_NO_BROKER_FILLS" for row in selected
            ),
            "gate_retained_at_0_20_pct": sum(label != BUCKETS[0] for _ in selected),
            "boost_eligible_at_0_80_pct": sum(label == BUCKETS[3] for _ in selected),
            "observed_touch_rate_denominator": len(crossings),
            "observed_touch_rate": (sum(bool(row["crossing"]["observed_touch"]) for row in crossings) / len(crossings)) if crossings else None,
            "actual_target_exits": sum(
                str(row.get("actual_exit_reason") or "").endswith("_TARGET")
                and row.get("confirmed_texas_exit_sell_contracts", 0) > EPSILON for row in filled
            ),
            "manual_or_unclassified_sell_rounds": sum(bool(row.get("manual_or_unclassified_sells")) for row in filled),
            "resolved_net_pnl_rounds": len(resolved),
            "unresolved_net_pnl_rounds": len(filled) - len(resolved),
            "net_pnl": sum(float(row["pnl"]["net_pnl"]) for row in resolved),
        }
    return result


def _sweep_loss_minimization(
    inputs: Iterable[dict[str, Any]],
    *,
    checkpoints: Iterable[int] = SWEEP_CHECKPOINT_SECONDS,
    buffers: Iterable[int] = SWEEP_UNFAVORABLE_DISTANCE_DOLLARS,
    minimum_rv15_pct: float = 0.0,
) -> dict[str, Any]:
    """Causally mark the proposed no-touch, adverse-distance exits.

    This intentionally reports a *mark-to-recorded-bid* counterfactual, not a
    fill backtest.  It needs a continuous reliable review-point sequence through
    the checkpoint, and excludes a row rather than treating missing data as a
    no-touch.  Actual fills before the checkpoint remain actual; only the
    conservatively established remaining position is marked at the checkpoint.
    """
    result: dict[str, Any] = {}
    for checkpoint_seconds in checkpoints:
        for buffer_dollars in buffers:
            key = f"{checkpoint_seconds}s/${buffer_dollars}"
            eligible: list[dict[str, Any]] = []
            excluded: defaultdict[str, int] = defaultdict(int)
            for item in inputs:
                rv15_pct = _number(item.get("rv15_pct"))
                if rv15_pct is None:
                    excluded["missing_causal_rv15"] += 1
                    continue
                if rv15_pct + EPSILON < minimum_rv15_pct:
                    excluded["below_rv15_gate"] += 1
                    continue
                first_fill_epoch = _time_epoch(item.get("first_fill_at"))
                close_epoch = _time_epoch(item.get("market_close_time"))
                threshold = _number(item.get("threshold"))
                side = str(item.get("side") or "").upper()
                if first_fill_epoch is None or close_epoch is None or threshold is None or side not in {"YES", "NO"}:
                    excluded["missing_round_timing_or_side"] += 1
                    continue
                checkpoint_epoch = first_fill_epoch + checkpoint_seconds
                if checkpoint_epoch > close_epoch:
                    excluded["checkpoint_after_market_close"] += 1
                    continue
                if item.get("sell_attribution_ambiguous"):
                    excluded["ambiguous_sell_ownership"] += 1
                    continue
                points = [point for point in item.get("points", []) if (
                    (epoch := _time_epoch(point.get("observed_at"))) is not None
                    and first_fill_epoch <= epoch <= checkpoint_epoch
                    and int(point.get("data_reliable") or 0)
                    and _number(point.get("btc_proxy")) is not None
                )]
                if not points:
                    excluded["no_reliable_points"] += 1
                    continue
                points.sort(key=lambda point: (_time_epoch(point.get("observed_at")) or -1, int(point.get("id") or 0)))
                point_epochs = [_time_epoch(point.get("observed_at")) or 0 for point in points]
                # A sampled 'no touch' is unsafe when observations skip a long
                # interval.  This includes the fill-to-first and last-to-checkpoint
                # edges rather than only gaps between stored points.
                gaps = [point_epochs[0] - first_fill_epoch, checkpoint_epoch - point_epochs[-1]]
                gaps.extend(right - left for left, right in zip(point_epochs, point_epochs[1:]))
                if max(gaps) > SWEEP_MAX_POINT_GAP_SECONDS:
                    excluded["review_gap_exceeds_20s"] += 1
                    continue
                if any(texas_threshold_breached(side, point.get("btc_proxy"), threshold) for point in points):
                    excluded["recorded_threshold_touch"] += 1
                    continue
                latest = points[-1]
                distance = texas_unfavorable_distance(side, latest.get("btc_proxy"), threshold)
                if distance is None or distance + EPSILON < buffer_dollars:
                    excluded["distance_below_buffer"] += 1
                    continue
                bid = _number(latest.get("yes_bid") if side == "YES" else latest.get("no_bid"))
                if bid is None or bid <= 0:
                    excluded["missing_executable_bid"] += 1
                    continue
                before_buys = [fill for fill in item["buy_fills"] if (_time_epoch(fill.get("filled_at")) or float("inf")) <= checkpoint_epoch]
                before_sells = [fill for fill in item["sells"] if (_time_epoch(fill.get("filled_at")) or float("inf")) <= checkpoint_epoch]
                before = _fill_pnl(before_buys, before_sells, None, side)
                remaining = float(before.get("remaining_contracts") or 0)
                if before.get("accounting_ambiguous") or before.get("unmatched_sell_contracts", 0) > EPSILON:
                    excluded["ambiguous_checkpoint_position"] += 1
                    continue
                if remaining <= EPSILON:
                    excluded["already_closed_at_checkpoint"] += 1
                    continue
                estimated_exit_fee = kalshi_fee(bid, remaining)
                estimated_net = float(before.get("realized_pnl") or 0) + remaining * bid - estimated_exit_fee - float(before.get("open_cost_basis") or 0)
                eligible.append({
                    "round_id": item["round_id"], "checkpoint_at": latest.get("observed_at"),
                    "remaining_contracts": remaining, "unfavorable_distance": distance,
                    "recorded_bid": bid, "estimated_exit_fee": estimated_exit_fee,
                    "estimated_net_pnl": estimated_net, "actual_net_pnl": item.get("actual_net_pnl"),
                })
            comparable = [row for row in eligible if row["actual_net_pnl"] is not None]
            result[key] = {
                "checkpoint_seconds": checkpoint_seconds,
                "unfavorable_distance_at_least_dollars": buffer_dollars,
                "qualified_rounds": len(eligible),
                "comparable_resolved_rounds": len(comparable),
                "synthetic_mark_to_bid_net_pnl": sum(row["estimated_net_pnl"] for row in comparable),
                "actual_net_pnl_for_same_rounds": sum(float(row["actual_net_pnl"]) for row in comparable),
                "synthetic_minus_actual_net_pnl": sum(row["estimated_net_pnl"] - float(row["actual_net_pnl"]) for row in comparable),
                "excluded": dict(sorted(excluded.items())),
                "rounds": eligible,
            }
    return {
        "kind": "read_only_texas_no_touch_loss_minimization_sweep",
        "method": {
            "trigger": "At checkpoint: no recorded threshold touch since first fill, adverse-side distance at least buffer, and a reliable point sequence with no gap above 20 seconds.",
            "price": "Latest recorded executable bid at or before checkpoint; hypothetical full IOC fill is not inferred.",
            "fees": "Current Kalshi taker-fee formula at the recorded bid; actual pre-checkpoint fills retain their recorded fees.",
            "zero_buffer": "0 means no extra adverse-distance buffer beyond no recorded touch.",
        },
        "minimum_rv15_pct": minimum_rv15_pct,
        "limitations": [
            "This is a mark-to-recorded-bid counterfactual, not evidence that the displayed bid had enough size for a full fill.",
            "No recorded touch is not proof of no intragap or intrasecond threshold touch.",
            "Only rounds with complete enough stored review points are included; exclusions are reported rather than guessed.",
        ],
        "variants": result,
    }


def replay_texas_rv(db: Database) -> dict[str, Any]:
    """Return a frozen, JSON-serializable analysis report without DB mutation."""
    connection = db.connect()
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        def rows(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        candles = {int(row["minute_epoch"]): float(row["close"]) for row in rows(
            "SELECT minute_epoch,close FROM coinbase_realized_volatility_candles WHERE source=? AND product=? AND granularity_seconds=60",
            (SOURCE, PRODUCT),
        )}
        rounds = rows("SELECT * FROM texas_holdem_rounds ORDER BY created_at,id")
        attempts = rows("SELECT * FROM texas_holdem_attempts ORDER BY round_id,attempt_number,id")
        intents = rows("SELECT * FROM broker_order_intents ORDER BY created_at,id")
        fills = rows("SELECT * FROM broker_fills ORDER BY filled_at,id")
        settlements = rows("SELECT * FROM broker_settlements")
        positions = rows("SELECT * FROM broker_positions")
        sessions = rows("SELECT * FROM trade_review_sessions")
        points = rows("SELECT p.* FROM trade_review_points p JOIN trade_review_sessions s ON s.id=p.session_id ORDER BY p.observed_at,p.id")
    finally:
        connection.close()
    attempts_by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_round[int(attempt["round_id"])].append(attempt)
    intent_by_client = {(str(row["mode"]), str(row["client_order_id"])): row for row in intents}
    fills_by_client: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    fills_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        fills_by_key[(str(fill["mode"]), str(fill["ticker"]), str(fill["side"]))].append(fill)
        if fill.get("client_order_id"):
            fills_by_client[(str(fill["mode"]), str(fill["client_order_id"]))].append(fill)
    settlement_by_key = {(str(row["mode"]), str(row["ticker"])): row for row in settlements}
    position_by_key = {(str(row["mode"]), str(row["ticker"]), str(row["side"])): row for row in positions}
    session_by_key = {(str(row["environment"]), str(row["ticker"])): row for row in sessions}
    points_by_session: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        points_by_session[int(point["session_id"])].append(point)

    report_rows: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    sweep_inputs: list[dict[str, Any]] = []
    for round_row in rounds:
        mode, ticker, side = str(round_row["environment"]), str(round_row["ticker"]), str(round_row.get("side") or "")
        unit_attempts = attempts_by_round.get(int(round_row["id"]), [])
        if not unit_attempts:
            continue
        linked_attempts = []
        for attempt in unit_attempts:
            client_id = attempt.get("broker_client_order_id")
            intent = intent_by_client.get((mode, str(client_id))) if client_id else None
            decision_at = (intent or {}).get("created_at") or attempt.get("observed_at")
            rv = rv15_at(candles, decision_at)
            record = {"round_id": round_row["id"], "environment": mode, "ticker": ticker, "side": side,
                      "strategy_version": _strategy_version(round_row.get("strategy")),
                      "attempt_number": attempt.get("attempt_number"), "decision_at": decision_at,
                      "intent_linked": bool(intent), "client_order_id": client_id, "rv": rv}
            attempt_rows.append(record)
            linked_attempts.append((attempt, intent, record))
        first_attempt, first_intent, first_record = linked_attempts[0]
        buy_fills: list[dict[str, Any]] = []
        for _, intent, _ in linked_attempts:
            if intent and str(intent.get("action")) == "BUY":
                buy_fills.extend(fills_by_client.get((mode, str(intent["client_order_id"])), []))
        # Exact intent links first; de-duplicate partial fills by normalized fill id.
        seen = set()
        buy_fills = [fill for fill in buy_fills if str(fill.get("id")) not in seen and not seen.add(str(fill.get("id")))]
        first_fill = min(buy_fills, key=_fill_key, default=None)
        first_fill_at = first_fill.get("filled_at") if first_fill else None
        relevant_fills = fills_by_key[(mode, ticker, side)]
        attributed_sells, sell_ambiguities = _attributed_sells(
            buy_fills, relevant_fills, position_by_key.get((mode, ticker, side)),
        )
        pnl = _fill_pnl(buy_fills, attributed_sells, settlement_by_key.get((mode, ticker)), side)
        if sell_ambiguities:
            pnl["accounting_ambiguous"] = True
            pnl["net_pnl"] = None
        session = session_by_key.get((mode, ticker))
        crossing = _point_crossing(
            points_by_session.get(int(session["id"]), []) if session else [], side,
            _number(round_row.get("threshold")), first_fill_at,
            session.get("market_close_time") if session else None,
        )
        funded_records = [record for _, intent, record in linked_attempts
                          if intent and str(intent.get("action")) == "BUY"
                          and fills_by_client.get((mode, str(intent.get("client_order_id"))))]
        paper_unavailable = mode == "PAPER" and not buy_fills
        report_rows.append({
            "round_id": round_row["id"], "environment": mode, "ticker": ticker, "side": side,
            "strategy_version": _strategy_version(round_row.get("strategy")), "first_attempt": first_record,
            "first_funded_attempt": funded_records[0] if funded_records else None,
            "attempt_count": len(unit_attempts), "pnl": pnl, "first_fill_at": first_fill_at,
            "actual_exit_reason": round_row.get("exit_reason"),
            "confirmed_texas_exit_sell_contracts": sum(
                _number(fill.get("contracts")) or 0.0 for fill in attributed_sells if _is_texas(fill.get("strategy"))
            ),
            "manual_or_unclassified_sells": any(not _is_texas(fill.get("strategy")) for fill in attributed_sells)
                or bool(sell_ambiguities),
            "sell_attribution_ambiguities": sell_ambiguities,
            "accounting_status": "UNAVAILABLE_PAPER_NO_BROKER_FILLS" if paper_unavailable else "RESOLVED_OR_PARTIAL",
            "crossing": crossing, "review": {"available": bool(session), "coverage": session.get("coverage") if session else None,
                "gap_count": session.get("gap_count") if session else None, "status": session.get("status") if session else "UNAVAILABLE"},
        })
        sweep_inputs.append({
            "round_id": round_row["id"], "side": side, "threshold": round_row.get("threshold"),
            "first_fill_at": first_fill_at, "market_close_time": session.get("market_close_time") if session else None,
            "points": points_by_session.get(int(session["id"]), []) if session else [],
            "buy_fills": buy_fills, "sells": attributed_sells,
            "sell_attribution_ambiguous": bool(sell_ambiguities), "actual_net_pnl": pnl.get("net_pnl"),
            # The exact funded order's decision-time RV is preferred.  A round
            # that never funded cannot inform an after-fill exit variant.
            "rv15_pct": _number((funded_records[0] if funded_records else first_record).get("rv", {}).get("rv15_pct")),
        })
    # Keep chronology explicit; all per-bucket outputs have exact denominators.
    early = report_rows[:len(report_rows) // 2]
    late = report_rows[len(report_rows) // 2:]
    return {
        "kind": "retrospective_coinbase_rv15_replay", "read_only": True,
        "method": {"source": SOURCE, "product": PRODUCT, "granularity_seconds": 60,
                   "formula": "100*sqrt(sum(log-return^2)) over 15 returns", "candle_count": 16,
                   "no_lookahead": "Ends at last closed minute before decision; no current percentile.",
                   "publication_delay_sensitivity": "Use rv15_at(..., closed_minute_lag=1) separately."},
        "limitations": ["Retrospective candle reconstruction is not proof Coinbase/API delivered the candle then.",
                        "No recorded breach is not proof of no intraminute or gap crossing.",
                        "No historical Kalshi executable liquidity, candidate P/L, or 1.5x fillability is inferred."],
        "denominators": {"all_rounds": len(rounds), "attempted_rounds": len(report_rows), "attempts": len(attempt_rows),
                           "available_rv_attempts": sum(bool(row["rv"].get("available")) for row in attempt_rows),
                           "missing_rv_attempts": sum(not bool(row["rv"].get("available")) for row in attempt_rows)},
        "rounds": report_rows, "attempts": attempt_rows,
        "bucket_basis": {
            "by_bucket": "first submitted Texas attempt (includes attempts that did not fund)",
            "by_first_funded_attempt_bucket": "first exact-linked Texas BUY attempt with a confirmed broker fill",
        },
        "by_bucket": _summary(report_rows),
        "by_first_funded_attempt_bucket": _summary(report_rows, attempt_key="first_funded_attempt"),
        "by_environment": {
            environment: _summary([row for row in report_rows if row["environment"] == environment])
            for environment in ("PAPER", "DEMO", "LIVE")
        }, "chronological": {"early": _summary(early), "late": _summary(late)},
        "by_strategy": {
            version: _summary(
                [row for row in report_rows if row["strategy_version"] == version]
            )
            for version in ("LEGACY", "V2", "V21", "V3")
        },
        "loss_minimization_sweep": _sweep_loss_minimization(sweep_inputs),
        "loss_minimization_by_rv15_gate": {
            f"≥{gate:.2f}%": _sweep_loss_minimization(sweep_inputs, minimum_rv15_pct=gate)
            for gate in (0.15, 0.20, 0.25, 0.30)
        },
    }


def replay_markdown(report: dict[str, Any]) -> str:
    """Small human-readable companion; JSON remains the authoritative output."""
    lines = ["# Texas Coinbase RV15 retrospective replay", "", "Read-only; no look-ahead and no inferred execution.", "",
             f"Attempts: {report['denominators']['attempts']} ({report['denominators']['available_rv_attempts']} RV available)", "",
             "| RV bucket | Attempt rounds | Filled rounds | Resolved P/L rounds | Net P/L |", "|---|---:|---:|---:|---:|"]
    for label, value in report["by_bucket"].items():
        lines.append(f"| {label} | {value['attempt_rounds']} | {value['filled_rounds']} | {value['resolved_net_pnl_rounds']} | {value['net_pnl']:.4f} |")
    return "\n".join(lines)
