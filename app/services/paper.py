from __future__ import annotations

import json
import math
import time
from collections import deque
from datetime import date
from typing import Any, Callable

from app.db import Database
from app.domain import iso_now, kalshi_fee, parse_time, threshold_breach_exit_state
from app.services.decision import Decision
from app.services.margin_volatility import MarginVolatilityService
from app.services.trade_review import paper_trade_ref, review_metadata


_USE_DEFAULT_STOP = object()


def _stop_price_from_cents(value: Any) -> float | None:
    if value in (None, ""):
        return None
    cents = float(value)
    return cents / 100 if cents > 0 else None


def _strategy_metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class PaperTradingService:
    def __init__(self, db: Database):
        self.db = db
        self._automatic_key: tuple[str, str] | None = None
        self._automatic_started_at: float | None = None
        self._automatic_last_at: float | None = None
        self._automatic_last_buy = False
        self._automatic_segments: deque[tuple[float, float, bool]] = deque()
        self._strategy_states: dict[str, dict[str, Any]] = {}

    def portfolio(self) -> dict[str, Any]:
        settings = self.db.settings()
        starting = float(settings["starting_bankroll"])
        trades = self.db.fetch_all("SELECT * FROM paper_trades ORDER BY opened_at ASC")
        settlement_rows = self.db.fetch_all(
            """
            SELECT s.ticker,
                   COALESCE(
                     json_extract(s.raw_json,'$.expiration_value'),
                     json_extract(m.raw_json,'$.expiration_value')
                   ) AS settlement_price,
                   m.strike
            FROM settlements s LEFT JOIN markets m ON m.ticker=s.ticker
            """
        )
        settlement_margins = {
            str(row["ticker"]): float(row["settlement_price"]) - float(row["strike"])
            for row in settlement_rows
            if row.get("settlement_price") is not None and row.get("strike") is not None
        }
        for trade in trades:
            trade["settlement_margin"] = settlement_margins.get(str(trade["ticker"]))
        orders = self.db.fetch_all("SELECT * FROM paper_orders ORDER BY created_at ASC")
        settled = [trade for trade in trades if trade["status"] == "settled"]
        open_trades = [trade for trade in trades if trade["status"] == "open"]
        filled_sales = [
            order
            for order in orders
            if order["status"] == "filled" and order["action"] == "SELL"
        ]
        realized = sum(float(trade["realized_pnl"] or 0) for trade in settled)
        realized += sum(float(order["realized_pnl"] or 0) for order in filled_sales)
        open_capital = sum(float(trade["entry_cost"] + trade["fees"]) for trade in open_trades)
        open_orders = [order for order in orders if order["status"] == "open"]
        reserved_cash = sum(
            self._order_commitment(float(order["limit_price"]), int(order["requested_contracts"]))
            for order in open_orders
            if order["action"] == "BUY"
        )
        available_cash = starting + realized - open_capital - reserved_cash
        unrealized = 0.0
        for trade in open_trades:
            snapshot = self.db.fetch_one(
                """
                SELECT yes_bid,no_bid FROM kalshi_snapshots
                WHERE ticker=? ORDER BY id DESC LIMIT 1
                """,
                (trade["ticker"],),
            )
            mark = None
            if snapshot:
                mark = snapshot["yes_bid"] if trade["side"] == "YES" else snapshot["no_bid"]
            mark = float(mark if mark is not None else trade["entry_price"])
            exit_fee = kalshi_fee(mark, float(trade["contracts"]))
            unrealized += (
                mark * float(trade["contracts"])
                - float(trade["entry_cost"])
                - float(trade["fees"])
                - exit_fee
            )
        current = starting + realized + unrealized
        realized_results = [float(trade["realized_pnl"] or 0) for trade in settled]
        realized_results.extend(float(order["realized_pnl"] or 0) for order in filled_sales)
        wins = sum(1 for value in realized_results if value > 0)
        losses = sum(1 for value in realized_results if value <= 0)
        equity = starting
        peak = starting
        max_drawdown = 0.0
        for trade in settled:
            equity += float(trade["realized_pnl"] or 0)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
        session_pnl = sum(
            float(trade["realized_pnl"] or 0)
            for trade in settled
            if str(trade.get("settled_at") or "").startswith(date.today().isoformat())
        )
        session_pnl += sum(
            float(order["realized_pnl"] or 0)
            for order in filled_sales
            if str(order.get("filled_at") or "").startswith(date.today().isoformat())
        )
        session_peak = max(starting + realized - session_pnl, starting + realized)
        session_drawdown = max(0.0, -session_pnl / session_peak) if session_peak else 0.0
        automatic_enabled = bool(settings.get("paper_trading_enabled", False))
        automatic_block_reason = None
        if not automatic_enabled:
            automatic_block_reason = "Automatic paper trading is off."
        elif (
            settings.get("risk_controls_enabled", True)
            and session_drawdown >= float(settings.get("max_session_drawdown_pct", 0.10))
        ):
            automatic_block_reason = "Session drawdown limit reached."
        elif available_cash <= 0:
            automatic_block_reason = "No paper bankroll is available."
        average_edge = (
            sum(float(trade["edge"]) for trade in trades) / len(trades) if trades else 0.0
        )
        expected_total = sum(float(trade["expected_value"]) * int(trade["contracts"]) for trade in trades)
        entries = self.db.fetch_all(
            "SELECT * FROM paper_entries ORDER BY opened_at ASC, id ASC"
        )
        active_stops = [
            entry for entry in entries
            if entry["status"] == "open" and entry.get("stop_status") == "active"
        ]
        return {
            "starting_bankroll": starting,
            "current_bankroll": current,
            "available_cash": available_cash,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "return_pct": (current - starting) / starting if starting else 0.0,
            "realized_return_pct": realized / starting if starting else 0.0,
            "wins": wins,
            "losses": losses,
            "open_positions": len(open_trades),
            "open_order_count": len(open_orders),
            "reserved_cash": reserved_cash,
            "average_edge": average_edge,
            "expected_value": expected_total,
            "max_drawdown_pct": max_drawdown,
            "session_drawdown_pct": session_drawdown,
            "automatic_trading_enabled": automatic_enabled,
            "automatic_trade_allowed": automatic_block_reason is None,
            "automatic_trade_block_reason": automatic_block_reason,
            "risk_controls_enabled": bool(settings.get("risk_controls_enabled", True)),
            "max_position_pct": float(settings.get("max_position_pct", 0.05)),
            "max_risk_per_trade_pct": float(
                settings.get("max_risk_per_trade_pct", 0.03)
            ),
            "max_session_drawdown_pct": float(
                settings.get("max_session_drawdown_pct", 0.10)
            ),
            "selected_side": settings.get("selected_side", "YES"),
            "default_stop_loss_cents": settings.get("default_stop_loss_cents"),
            "global_profit_take_enabled": bool(
                settings.get("global_profit_take_enabled", True)
            ),
            "global_profit_take_price": float(
                settings.get("global_profit_take_price", 0.99)
            ),
            "threshold_breach_exit_state": {
                "enabled": bool(
                    settings.get("threshold_breach_exit_enabled", True)
                ),
                "buffer_dollars": float(
                    settings.get("threshold_breach_exit_buffer_dollars", 0.0)
                ),
                "warning": (
                    "This is a side-aware exit based on the BTC proxy versus To Beat. "
                    "It does not use contract price as the trigger."
                ),
            },
            "active_stop_losses": active_stops,
            "strategy_results": self.strategy_results(),
            "trades": [
                {
                    **trade,
                    **review_metadata(
                        self.db,
                        "PAPER",
                        paper_trade_ref(trade["id"]),
                        trade.get("status"),
                    ),
                    "entries": [
                        {
                            **entry,
                            "strategy_metadata": _strategy_metadata(
                                entry.get("strategy_metadata_json")
                            ),
                            "threshold_breach_enabled": entry.get(
                                "threshold_breach_enabled"
                            ),
                            "threshold_exit_buffer": entry.get(
                                "threshold_exit_buffer"
                            ),
                            "threshold_exit_level": entry.get(
                                "threshold_exit_level"
                            ),
                            "threshold_trigger_btc_proxy": entry.get(
                                "threshold_trigger_btc_proxy"
                            ),
                            "threshold_trigger_threshold": entry.get(
                                "threshold_trigger_threshold"
                            ),
                            "threshold_triggered_at": entry.get(
                                "threshold_triggered_at"
                            ),
                            "threshold_exit_status": entry.get(
                                "threshold_exit_status"
                            ),
                            "threshold_exit_block_reason": entry.get(
                                "threshold_exit_block_reason"
                            ),
                        }
                        for entry in entries if entry["trade_id"] == trade["id"]
                    ],
                }
                for trade in reversed(trades[-100:])
            ],
            "orders": list(reversed(orders[-100:])),
            "open_orders": list(reversed(open_orders)),
            "positions": [
                {
                    "ticker": trade["ticker"],
                    "side": trade["side"],
                    "contracts": int(trade["contracts"]),
                    "entry_price": float(trade["entry_price"]),
                    "committed_dollars": float(trade["entry_cost"] + trade["fees"]),
                    "source": trade.get("source", "automatic"),
                    "entries": [
                        {
                            "id": entry["id"],
                            "contracts": int(entry["remaining_contracts"]),
                            "entry_price": float(entry["entry_price"]),
                            "stop_loss_price": entry.get("stop_loss_price"),
                            "stop_status": entry.get("stop_status"),
                            "source": entry["source"],
                            "strategy": entry.get("strategy"),
                            "target_exit_price": entry.get("target_exit_price"),
                            "fallback_exit_mode": entry.get("fallback_exit_mode"),
                            "fallback_exit_seconds": entry.get("fallback_exit_seconds"),
                            "strategy_metadata": _strategy_metadata(
                                entry.get("strategy_metadata_json")
                            ),
                        }
                        for entry in entries
                        if entry["trade_id"] == trade["id"]
                        and entry["status"] == "open"
                    ],
                }
                for trade in open_trades
            ],
        }

    def strategy_results(self) -> dict[str, dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT e.*,t.outcome,t.status AS trade_status
            FROM paper_entries e
            JOIN paper_trades t ON t.id=e.trade_id
            WHERE e.strategy IN ('STANDARD_EDGE','EARLY_THRESHOLD','LATE_CONVICTION','SWING')
            ORDER BY e.id ASC
            """
        )
        results: dict[str, dict[str, Any]] = {}
        for strategy in (
            "STANDARD_EDGE", "EARLY_THRESHOLD", "LATE_CONVICTION", "SWING"
        ):
            entries = [row for row in rows if row.get("strategy") == strategy]
            settled = [row for row in entries if row.get("status") == "settled"]
            wins = sum(
                1
                for row in settled
                if int(row.get("outcome") or 0) == 1
            )
            realized = 0.0
            for row in settled:
                won = int(row.get("outcome") or 0) == 1
                payout = float(row.get("initial_contracts") or 0) if won else 0.0
                realized += payout - float(row.get("entry_cost") or 0) - float(
                    row.get("entry_fees") or 0
                )
            closed_ids = [int(row["id"]) for row in entries if row.get("status") == "closed"]
            if closed_ids:
                placeholders = ",".join("?" for _ in closed_ids)
                exits = self.db.fetch_all(
                    f"SELECT realized_pnl FROM paper_orders WHERE entry_id IN ({placeholders}) AND status='filled'",
                    closed_ids,
                )
                realized += sum(float(exit_row.get("realized_pnl") or 0) for exit_row in exits)

            def average(key: str) -> float | None:
                values = [float(row[key]) for row in entries if row.get(key) is not None]
                return sum(values) / len(values) if values else None

            results[strategy] = {
                "entries": len(entries),
                "settled_trades": len(settled),
                "win_rate": wins / len(settled) if settled else None,
                "realized_pnl": realized,
                "average_entry_probability": average("model_probability"),
                "average_entry_price": average("entry_price"),
                "average_entry_ev": average("expected_value"),
            }
            if strategy == "SWING":
                completed = [
                    row for row in entries if row.get("status") in {"closed", "settled"}
                ]
                pnl_by_entry: dict[int, float] = {}
                exit_orders_by_entry: dict[int, list[dict[str, Any]]] = {}
                for row in completed:
                    if row.get("status") == "settled":
                        won = int(row.get("outcome") or 0) == 1
                        payout = float(row.get("initial_contracts") or 0) if won else 0.0
                        pnl_by_entry[int(row["id"])] = (
                            payout
                            - float(row.get("entry_cost") or 0)
                            - float(row.get("entry_fees") or 0)
                        )
                        continue
                    exits = self.db.fetch_all(
                        """
                        SELECT * FROM paper_orders
                        WHERE entry_id=? AND action='SELL' AND status='filled'
                        """,
                        (row["id"],),
                    )
                    exit_orders_by_entry[int(row["id"])] = exits
                    pnl_by_entry[int(row["id"])] = sum(
                        float(exit_row.get("realized_pnl") or 0) for exit_row in exits
                    )

                def swing_average(values: list[float]) -> float | None:
                    return sum(values) / len(values) if values else None

                hold_seconds = []
                for row in completed:
                    opened_at = parse_time(row.get("opened_at"))
                    closed_at = parse_time(row.get("closed_at"))
                    if opened_at and closed_at:
                        hold_seconds.append(max(0.0, (closed_at - opened_at).total_seconds()))
                exit_prices = []
                target_count = 0
                fallback_count = 0
                stop_count = 0
                risk_count = 0
                settlement_count = 0
                for row in completed:
                    orders = exit_orders_by_entry.get(int(row["id"]), [])
                    sources = {str(order.get("source") or "") for order in orders}
                    target_count += int("swing_target" in sources)
                    fallback_count += int("swing_fallback" in sources)
                    stop_count += int("stop_loss" in sources)
                    risk_count += int("risk_exit" in sources)
                    settlement_count += int(row.get("status") == "settled")
                    filled = [
                        order for order in orders
                        if order.get("filled_price") is not None
                        and int(order.get("filled_contracts") or 0) > 0
                    ]
                    filled_contracts = sum(
                        int(order.get("filled_contracts") or 0) for order in filled
                    )
                    if filled_contracts:
                        exit_prices.append(
                            sum(
                                float(order["filled_price"])
                                * int(order["filled_contracts"])
                                for order in filled
                            ) / filled_contracts
                        )
                    elif row.get("exit_price") is not None:
                        exit_prices.append(float(row["exit_price"]))
                realized_values = list(pnl_by_entry.values())
                deployed = sum(
                    float(row.get("entry_cost") or 0) + float(row.get("entry_fees") or 0)
                    for row in completed
                )
                favorable = [
                    float(row["max_favorable_bid"]) - float(row["entry_price"])
                    for row in completed if row.get("max_favorable_bid") is not None
                ]
                adverse = [
                    float(row["min_adverse_bid"]) - float(row["entry_price"])
                    for row in completed if row.get("min_adverse_bid") is not None
                ]
                results[strategy].update(
                    {
                        "completed_trades": len(completed),
                        "wins": sum(1 for value in realized_values if value > 0),
                        "losses": sum(1 for value in realized_values if value <= 0),
                        "win_rate": (
                            sum(1 for value in realized_values if value > 0)
                            / len(realized_values)
                            if realized_values else None
                        ),
                        "target_hit_rate": (
                            target_count / len(completed) if completed else None
                        ),
                        "average_exit_price": swing_average(exit_prices),
                        "average_holding_seconds": swing_average(hold_seconds),
                        "return_on_deployed_capital": (
                            sum(realized_values) / deployed if deployed else None
                        ),
                        "target_exits": target_count,
                        "fallback_exits": fallback_count,
                        "stop_exits": stop_count,
                        "risk_exits": risk_count,
                        "settlement_exits": settlement_count,
                        "average_favorable_move": swing_average(favorable),
                        "average_adverse_move": swing_average(adverse),
                    }
                )
        return results

    @staticmethod
    def _order_commitment(price: float, contracts: int) -> float:
        return price * contracts + kalshi_fee(price, contracts)

    def _available_cash_in_transaction(
        self, connection: Any, starting_bankroll: float
    ) -> float:
        settled_row = connection.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) AS realized
            FROM paper_trades WHERE status='settled'
            """
        ).fetchone()
        sales_row = connection.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) AS realized
            FROM paper_orders WHERE status='filled' AND action='SELL'
            """
        ).fetchone()
        capital_row = connection.execute(
            """
            SELECT COALESCE(SUM(entry_cost + fees), 0) AS committed
            FROM paper_trades WHERE status='open'
            """
        ).fetchone()
        open_buys = connection.execute(
            """
            SELECT limit_price,requested_contracts FROM paper_orders
            WHERE status='open' AND action='BUY'
            """
        ).fetchall()
        reserved = sum(
            self._order_commitment(float(row["limit_price"]), int(row["requested_contracts"]))
            for row in open_buys
            if row["limit_price"] is not None
        )
        return (
            float(starting_bankroll)
            + float(settled_row["realized"] or 0)
            + float(sales_row["realized"] or 0)
            - float(capital_row["committed"] or 0)
            - reserved
        )

    @staticmethod
    def executable_price(market: dict[str, Any], side: str, action: str) -> float | None:
        key = f"{side.lower()}_{'ask' if action == 'BUY' else 'bid'}"
        value = market.get(key)
        if value is None:
            return None
        price = float(value)
        return price if 0 < price < 1 else None

    def available_contracts(self, ticker: str, side: str) -> int:
        position = self.db.fetch_one(
            "SELECT contracts FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        )
        held = int(position["contracts"]) if position else 0
        reserved = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(requested_contracts), 0) AS contracts
            FROM paper_orders
            WHERE ticker=? AND side=? AND action='SELL' AND status='open'
            """,
            (ticker, side),
        )
        return max(0, held - int(reserved["contracts"] if reserved else 0))

    def place_order(
        self,
        *,
        ticker: str,
        side: str,
        action: str,
        order_type: str,
        market: dict[str, Any],
        dollars: float | None = None,
        contracts: int | None = None,
        limit_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> dict[str, Any]:
        side = str(side).upper()
        action = str(action).upper()
        order_type = str(order_type).upper()
        if side not in {"YES", "NO"}:
            raise ValueError("Choose Up or Down.")
        if action not in {"BUY", "SELL"}:
            raise ValueError("Choose Buy or Sell.")
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Order type must be market or limit.")
        if action == "SELL" and stop_loss_price is not None:
            raise ValueError("Stop-losses can only be attached to Buy orders.")
        if stop_loss_price is not None:
            try:
                stop_loss_price = float(stop_loss_price)
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a valid stop-loss price.") from exc
            if stop_loss_price == 0:
                stop_loss_price = None
            elif not 0.01 <= stop_loss_price <= 0.99:
                raise ValueError("Stop-loss must be between 1 and 99 cents.")

        best_price = self.executable_price(market, side, action)
        requested_dollars = None
        if order_type == "MARKET":
            if best_price is None:
                raise ValueError("No executable Kalshi price is currently available.")
            try:
                requested_dollars = float(dollars or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a valid dollar amount.") from exc
            if requested_dollars <= 0:
                raise ValueError("Enter a dollar amount greater than zero.")
            unit_value = (
                best_price + kalshi_fee(best_price)
                if action == "BUY"
                else best_price
            )
            contracts = math.floor(requested_dollars / unit_value)
            if contracts < 1:
                raise ValueError("The amount is too small for one contract at the current price.")
            order_price = best_price
        else:
            if isinstance(contracts, bool):
                raise ValueError("Contracts must be a positive whole number.")
            try:
                contract_value = float(contracts or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Contracts must be a positive whole number.") from exc
            if not contract_value.is_integer():
                raise ValueError("Contracts must be a positive whole number.")
            contracts = int(contract_value)
            if contracts < 1:
                raise ValueError("Contracts must be a positive whole number.")
            try:
                order_price = float(limit_price or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a valid limit price.") from exc
            if not 0.01 <= order_price <= 0.99:
                raise ValueError("Limit price must be between 1 and 99 cents.")

        assert contracts is not None
        if action == "BUY":
            self._validate_buy(ticker, side, order_price, contracts)
        elif contracts > self.available_contracts(ticker, side):
            side_label = "Up" if side == "YES" else "Down"
            raise ValueError(
                f"Only {self.available_contracts(ticker, side)} {side_label} "
                "contracts are available to sell."
            )

        order_id = self.db.execute(
            """
                INSERT INTO paper_orders(
                    ticker,side,action,order_type,status,created_at,requested_dollars,
                    requested_contracts,limit_price,source,stop_loss_price,strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, side, action, order_type, "open", iso_now(), requested_dollars,
                contracts, order_price if order_type == "LIMIT" else None, "manual",
                stop_loss_price, "MANUAL",
            ),
        )
        order = self.db.fetch_one("SELECT * FROM paper_orders WHERE id=?", (order_id,))
        assert order
        marketable = order_type == "MARKET" or (
            best_price is not None
            and (
                (action == "BUY" and best_price <= order_price)
                or (action == "SELL" and best_price >= order_price)
            )
        )
        if marketable and best_price is not None:
            self._fill_order(order, best_price, market)
        return self.db.fetch_one("SELECT * FROM paper_orders WHERE id=?", (order_id,)) or order

    def _validate_buy(self, ticker: str, side: str, price: float, contracts: int) -> None:
        settings = self.db.settings()
        portfolio = self.portfolio()
        commitment = self._order_commitment(price, contracts)
        if commitment > float(portfolio["available_cash"]) + 1e-9:
            raise ValueError("This order exceeds the remaining paper bankroll.")
        if not settings.get("risk_controls_enabled", True):
            return
        if float(portfolio["session_drawdown_pct"]) >= float(
            settings.get("max_session_drawdown_pct", 0.10)
        ):
            raise ValueError("The session drawdown limit is active.")
        bankroll = max(0.0, float(portfolio["current_bankroll"]))
        max_trade = bankroll * float(settings.get("max_risk_per_trade_pct", 0.03))
        if commitment > max_trade + 1e-9:
            raise ValueError("This order exceeds the maximum risk per trade.")
        position = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(entry_cost + fees), 0) AS committed
            FROM paper_trades WHERE ticker=? AND status='open'
            """,
            (ticker,),
        )
        existing = float(position["committed"] or 0) if position else 0.0
        pending = self.db.fetch_all(
            """
            SELECT limit_price,requested_contracts FROM paper_orders
            WHERE ticker=? AND action='BUY' AND status='open'
            """,
            (ticker,),
        )
        existing += sum(
            self._order_commitment(float(order["limit_price"]), int(order["requested_contracts"]))
            for order in pending
        )
        max_position = bankroll * float(settings.get("max_position_pct", 0.05))
        if existing + commitment > max_position + 1e-9:
            raise ValueError("This order exceeds the maximum position size.")

    def _fill_order(
        self, order: dict[str, Any], price: float, market: dict[str, Any]
    ) -> None:
        contracts = int(order["requested_contracts"])
        fees = kalshi_fee(price, contracts)
        filled_at = iso_now()
        starting_bankroll = float(self.db.settings()["starting_bankroll"])
        with self.db.transaction() as connection:
            position_row = connection.execute(
                "SELECT * FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
                (order["ticker"], order["side"]),
            ).fetchone()
            position = dict(position_row) if position_row else None
            trade_id: int | None = int(position["id"]) if position else None
            realized_pnl = None
            affected_entry_ids: list[int] = []
            if order["action"] == "BUY":
                entry_cost = price * contracts
                if position:
                    total_contracts = int(position["contracts"]) + contracts
                    total_cost = float(position["entry_cost"]) + entry_cost
                    old_source = position["source"] or "automatic"
                    source = old_source if old_source == order["source"] else "mixed"
                    order_strategy = order.get("strategy") or (
                        "MANUAL" if order["source"] == "manual" else "STANDARD_EDGE"
                    )
                    old_strategy = position.get("strategy") or order_strategy
                    strategy = (
                        old_strategy if old_strategy == order_strategy else "MIXED"
                    )
                    connection.execute(
                        """
                        UPDATE paper_trades SET contracts=?,entry_price=?,entry_cost=?,
                            fees=?,source=?,strategy=? WHERE id=?
                        """,
                        (
                            total_contracts, total_cost / total_contracts, total_cost,
                            float(position["fees"]) + fees, source, strategy,
                            position["id"],
                        ),
                    )
                    trade_id = int(position["id"])
                else:
                    market_probability = market.get("market_probability")
                    if market_probability is None:
                        yes_bid = market.get("yes_bid")
                        yes_ask = market.get("yes_ask")
                        market_probability = (
                            (float(yes_bid) + float(yes_ask)) / 2
                            if yes_bid is not None and yes_ask is not None
                            else 0.5
                        )
                    cursor = connection.execute(
                        """
                        INSERT INTO paper_trades(
                            ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                            model_probability,market_probability,edge,expected_value,
                            confidence,model_version,status,source,strategy
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            order["ticker"], order["side"], filled_at, price, contracts,
                            entry_cost, fees, float(market.get("model_probability") or 0.5),
                            float(market_probability), 0.0, 0.0, "Manual", "manual",
                            "open", order["source"], order.get("strategy") or "MANUAL",
                        ),
                    )
                    trade_id = int(cursor.lastrowid)
                stop_loss = order.get("stop_loss_price")
                entry_cursor = connection.execute(
                    """
                    INSERT INTO paper_entries(
                        trade_id,order_id,ticker,side,opened_at,entry_price,
                        initial_contracts,remaining_contracts,entry_cost,entry_fees,
                        stop_loss_price,stop_status,source,status,strategy,
                        model_probability,expected_value,entry_reason,
                        margin_volatility_index,margin_cushion_ratio
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trade_id, order["id"], order["ticker"], order["side"],
                        filled_at, price, contracts, contracts, entry_cost, fees,
                        stop_loss, "active" if stop_loss is not None else None,
                        order["source"], "open", order.get("strategy") or "MANUAL",
                        market.get("model_probability"), market.get("expected_value"),
                        market.get("entry_reason"),
                        market.get("margin_volatility_index"),
                        market.get("margin_cushion_ratio"),
                    ),
                )
                affected_entry_ids.append(int(entry_cursor.lastrowid))
            else:
                if not position or int(position["contracts"]) < contracts:
                    raise ValueError("The paper position no longer has enough contracts to sell.")
                held = int(position["contracts"])
                exit_reason = {
                    "stop_loss": "STOP_LOSS",
                    "profit_take": "PROFIT_TAKE",
                    "threshold_breach_exit": "THRESHOLD_BREACH_EXIT",
                    "swing_target": "TARGET",
                    "swing_fallback": "FALLBACK",
                    "risk_exit": "RISK",
                }.get(str(order.get("source") or ""), "MANUAL")
                allocated_cost, allocated_entry_fees, affected_entry_ids = self._consume_entries(
                    connection,
                    ticker=str(order["ticker"]),
                    side=str(order["side"]),
                    contracts=contracts,
                    closed_at=filled_at,
                    target_entry_id=order.get("entry_id"),
                    stop_triggered=order.get("source") == "stop_loss",
                    exit_reason=exit_reason,
                    exit_price=price,
                    exit_fees=fees,
                )
                proceeds = price * contracts
                realized_pnl = proceeds - fees - allocated_cost - allocated_entry_fees
                previous_realized = float(position["realized_pnl"] or 0)
                if contracts == held:
                    connection.execute(
                        """
                        UPDATE paper_trades SET status='closed',settled_at=?,payout=?,
                            realized_pnl=?,source=? WHERE id=?
                        """,
                        (
                            filled_at, proceeds, previous_realized + realized_pnl,
                            "manual" if position["source"] == "manual" else "mixed",
                            position["id"],
                        ),
                    )
                else:
                    remaining_cost = float(position["entry_cost"]) - allocated_cost
                    remaining_contracts = held - contracts
                    connection.execute(
                        """
                        UPDATE paper_trades SET contracts=?,entry_price=?,entry_cost=?,fees=?,
                            realized_pnl=?,source=? WHERE id=?
                        """,
                        (
                            remaining_contracts,
                            remaining_cost / remaining_contracts,
                            remaining_cost,
                            float(position["fees"]) - allocated_entry_fees,
                            previous_realized + realized_pnl,
                            "manual" if position["source"] == "manual" else "mixed",
                            position["id"],
                        ),
                    )
            connection.execute(
                """
                UPDATE paper_orders SET status='filled',filled_price=?,filled_contracts=?,
                    fees=?,realized_pnl=?,filled_at=? WHERE id=?
                """,
                (price, contracts, fees, realized_pnl, filled_at, order["id"]),
            )
            available_cash_after = self._available_cash_in_transaction(
                connection, starting_bankroll
            )
            connection.execute(
                "UPDATE paper_orders SET available_cash_after=? WHERE id=?",
                (available_cash_after, order["id"]),
            )
            assert trade_id is not None
            connection.execute(
                "UPDATE paper_trades SET available_cash_after=? WHERE id=?",
                (available_cash_after, trade_id),
            )
            if affected_entry_ids:
                placeholders = ",".join("?" for _ in affected_entry_ids)
                connection.execute(
                    f"UPDATE paper_entries SET available_cash_after=? WHERE id IN ({placeholders})",
                    (available_cash_after, *affected_entry_ids),
                )

    @staticmethod
    def _consume_entries(
        connection: Any,
        *,
        ticker: str,
        side: str,
        contracts: int,
        closed_at: str,
        target_entry_id: int | None = None,
        stop_triggered: bool = False,
        exit_reason: str = "MANUAL",
        exit_price: float | None = None,
        exit_fees: float = 0.0,
    ) -> tuple[float, float, list[int]]:
        params: list[Any] = [ticker, side]
        where = "ticker=? AND side=? AND status='open' AND remaining_contracts>0"
        if target_entry_id is not None:
            where += " AND id=?"
            params.append(int(target_entry_id))
        rows = connection.execute(
            f"SELECT * FROM paper_entries WHERE {where} ORDER BY opened_at ASC,id ASC",
            params,
        ).fetchall()
        available = sum(int(row["remaining_contracts"]) for row in rows)
        if available < contracts:
            raise ValueError("The paper entry no longer has enough contracts to sell.")
        remaining_to_sell = contracts
        allocated_cost = 0.0
        allocated_fees = 0.0
        affected_entry_ids: list[int] = []
        for raw in rows:
            if remaining_to_sell <= 0:
                break
            entry = dict(raw)
            affected_entry_ids.append(int(entry["id"]))
            take = min(remaining_to_sell, int(entry["remaining_contracts"]))
            fraction = take / int(entry["initial_contracts"])
            allocated_cost += float(entry["entry_cost"]) * fraction
            allocated_fees += float(entry["entry_fees"]) * fraction
            allocated_exit_fee = exit_fees * (take / contracts)
            left = int(entry["remaining_contracts"]) - take
            if left == 0:
                stop_status = (
                    "triggered"
                    if stop_triggered and target_entry_id == entry["id"]
                    else "canceled" if entry.get("stop_status") == "active" else entry.get("stop_status")
                )
                connection.execute(
                    """
                    UPDATE paper_entries SET remaining_contracts=0,status='closed',
                        closed_at=?,stop_status=?,exit_reason=?,exit_price=?,
                        exit_fees=COALESCE(exit_fees,0)+? WHERE id=?
                    """,
                    (
                        closed_at, stop_status, exit_reason, exit_price,
                        allocated_exit_fee, entry["id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE paper_entries SET remaining_contracts=?,exit_reason=?,
                        exit_price=?,exit_fees=COALESCE(exit_fees,0)+? WHERE id=?
                    """,
                    (
                        left, exit_reason, exit_price, allocated_exit_fee,
                        entry["id"],
                    ),
                )
            remaining_to_sell -= take
        return allocated_cost, allocated_fees, affected_entry_ids

    def process_open_orders(self, ticker: str, market: dict[str, Any]) -> int:
        orders = self.db.fetch_all(
            "SELECT * FROM paper_orders WHERE ticker=? AND status='open' ORDER BY id ASC",
            (ticker,),
        )
        filled = 0
        for order in orders:
            best_price = self.executable_price(market, order["side"], order["action"])
            if best_price is None:
                continue
            limit_price = float(order["limit_price"])
            marketable = (
                order["action"] == "BUY" and best_price <= limit_price
            ) or (
                order["action"] == "SELL" and best_price >= limit_price
            )
            if marketable:
                try:
                    self._fill_order(order, best_price, market)
                    filled += 1
                except ValueError as exc:
                    self.db.execute(
                        """
                        UPDATE paper_orders SET status='canceled',canceled_at=?,error=?
                        WHERE id=?
                        """,
                        (iso_now(), str(exc), order["id"]),
                    )
        filled += self.process_profit_takes(ticker, market)
        filled += self.process_threshold_breach_exits(ticker, market)
        filled += self.process_stop_losses(ticker, market)
        return filled + self.process_swing_exits(ticker, market)

    def process_profit_takes(self, ticker: str, market: dict[str, Any]) -> int:
        settings = self.db.settings()
        if not settings.get("global_profit_take_enabled", True):
            return 0
        target = float(settings.get("global_profit_take_price", 0.99))
        slippage = float(settings.get("slippage_cents", 0.5)) / 100
        entries = self.db.fetch_all(
            """
            SELECT * FROM paper_entries
            WHERE ticker=? AND status='open' AND remaining_contracts>0
            ORDER BY id ASC
            """,
            (ticker,),
        )
        liquidity: dict[str, int | None] = {}
        closed = 0
        for entry in entries:
            side = str(entry["side"])
            bid = self.executable_price(market, side, "SELL")
            if bid is None or bid + 1e-12 < target:
                continue
            if side not in liquidity:
                bid_size = market.get(f"{side.lower()}_bid_size")
                liquidity[side] = (
                    max(0, int(float(bid_size))) if bid_size is not None else None
                )
            available = self.available_contracts(ticker, side)
            quoted = liquidity[side]
            contracts = min(int(entry["remaining_contracts"]), available)
            if quoted is not None:
                contracts = min(contracts, quoted)
            if contracts < 1:
                continue
            order_id = self.db.execute(
                """
                INSERT INTO paper_orders(
                    ticker,side,action,order_type,status,created_at,
                    requested_contracts,source,entry_id,strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticker, side, "SELL", "MARKET", "open", iso_now(),
                    contracts, "profit_take", entry["id"],
                    entry.get("strategy") or "MANUAL",
                ),
            )
            order = self.db.fetch_one(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,)
            )
            if not order:
                continue
            try:
                self._fill_order(order, max(0.001, bid - slippage), market)
                closed += 1
                if quoted is not None:
                    liquidity[side] = quoted - contracts
            except ValueError as exc:
                self.db.execute(
                    """
                    UPDATE paper_orders SET status='canceled',canceled_at=?,error=?
                    WHERE id=?
                    """,
                    (iso_now(), str(exc), order_id),
                )
        return closed

    def process_threshold_breach_exits(
        self, ticker: str, market: dict[str, Any]
    ) -> int:
        settings = self.db.settings()
        enabled = bool(settings.get("threshold_breach_exit_enabled", True))
        buffer_dollars = float(
            settings.get("threshold_breach_exit_buffer_dollars", 0.0)
        )
        btc_proxy = market.get("btc_proxy")
        threshold = market.get("strike")
        quality = market.get("data_quality")
        data_reliable = (
            bool(quality.get("reliable"))
            if isinstance(quality, dict)
            else bool(btc_proxy is not None and threshold is not None)
        )
        entries = self.db.fetch_all(
            """
            SELECT * FROM paper_entries
            WHERE ticker=? AND status='open' AND remaining_contracts>0
            ORDER BY id ASC
            """,
            (ticker,),
        )
        if not entries:
            return 0
        slippage = float(settings.get("slippage_cents", 0.5)) / 100
        closed = 0
        liquidity: dict[str, int | None] = {}
        for entry in entries:
            side = str(entry["side"])
            state = threshold_breach_exit_state(
                side,
                float(btc_proxy) if btc_proxy is not None else None,
                float(threshold) if threshold is not None else None,
                enabled=enabled,
                buffer_dollars=buffer_dollars,
                data_reliable=data_reliable,
            )
            self.db.execute(
                """
                UPDATE paper_entries SET threshold_breach_enabled=?,
                    threshold_exit_buffer=?,threshold_exit_level=?,
                    threshold_exit_status=?,threshold_exit_block_reason=?
                WHERE id=?
                """,
                (
                    int(enabled),
                    buffer_dollars,
                    state["exit_level"],
                    state["status"],
                    state["reason"],
                    entry["id"],
                ),
            )
            if state["status"] != "Breached":
                continue
            bid = self.executable_price(market, side, "SELL")
            if bid is None:
                self.db.execute(
                    """
                    UPDATE paper_entries SET threshold_exit_status='Blocked',
                        threshold_exit_block_reason='No executable bid is available.'
                    WHERE id=?
                    """,
                    (entry["id"],),
                )
                continue
            if side not in liquidity:
                bid_size = market.get(f"{side.lower()}_bid_size")
                liquidity[side] = (
                    max(0, int(float(bid_size))) if bid_size is not None else None
                )
            available = self.available_contracts(ticker, side)
            quoted = liquidity[side]
            contracts = min(int(entry["remaining_contracts"]), available)
            if quoted is not None:
                contracts = min(contracts, quoted)
            if contracts < 1:
                self.db.execute(
                    """
                    UPDATE paper_entries SET threshold_exit_status='Blocked',
                        threshold_exit_block_reason='No executable bid liquidity is available.'
                    WHERE id=?
                    """,
                    (entry["id"],),
                )
                continue
            triggered_at = iso_now()
            self.db.execute(
                """
                UPDATE paper_entries SET threshold_trigger_btc_proxy=?,
                    threshold_trigger_threshold=?,threshold_triggered_at=COALESCE(
                        threshold_triggered_at,?),threshold_exit_status='Exit pending',
                    threshold_exit_block_reason=NULL WHERE id=?
                """,
                (btc_proxy, threshold, triggered_at, entry["id"]),
            )
            order_id = self.db.execute(
                """
                INSERT INTO paper_orders(
                    ticker,side,action,order_type,status,created_at,limit_price,
                    requested_contracts,source,entry_id,strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticker,
                    side,
                    "SELL",
                    "MARKET",
                    "open",
                    triggered_at,
                    max(0.001, float(bid) - slippage),
                    contracts,
                    "threshold_breach_exit",
                    entry["id"],
                    entry.get("strategy") or "MANUAL",
                ),
            )
            order = self.db.fetch_one(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,)
            )
            if not order:
                continue
            try:
                self._fill_order(
                    order, max(0.001, float(bid) - slippage), market
                )
                closed += 1
                if quoted is not None:
                    liquidity[side] = quoted - contracts
                remaining = self.db.fetch_one(
                    "SELECT remaining_contracts,status FROM paper_entries WHERE id=?",
                    (entry["id"],),
                ) or {}
                self.db.execute(
                    """
                    UPDATE paper_entries SET threshold_exit_status=?,
                        threshold_exit_block_reason=NULL WHERE id=?
                    """,
                    (
                        "Exited"
                        if str(remaining.get("status")) == "closed"
                        else "Exit pending",
                        entry["id"],
                    ),
                )
            except ValueError as exc:
                self.db.execute(
                    """
                    UPDATE paper_orders SET status='canceled',canceled_at=?,error=?
                    WHERE id=?
                    """,
                    (iso_now(), str(exc), order_id),
                )
                self.db.execute(
                    """
                    UPDATE paper_entries SET threshold_exit_status='Blocked',
                        threshold_exit_block_reason=? WHERE id=?
                    """,
                    (str(exc), entry["id"]),
                )
        return closed

    def process_stop_losses(self, ticker: str, market: dict[str, Any]) -> int:
        entries = self.db.fetch_all(
            """
            SELECT * FROM paper_entries
            WHERE ticker=? AND status='open' AND stop_status='active'
              AND remaining_contracts>0
            ORDER BY id ASC
            """,
            (ticker,),
        )
        triggered = 0
        for entry in entries:
            bid = self.executable_price(market, str(entry["side"]), "SELL")
            stop = entry.get("stop_loss_price")
            if bid is None or stop is None or bid > float(stop):
                continue
            order_id = self.db.execute(
                """
                INSERT INTO paper_orders(
                    ticker,side,action,order_type,status,created_at,
                    requested_contracts,source,entry_id,strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticker, entry["side"], "SELL", "MARKET", "open", iso_now(),
                    int(entry["remaining_contracts"]), "stop_loss", entry["id"],
                    entry.get("strategy") or "STOP_LOSS",
                ),
            )
            order = self.db.fetch_one(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,)
            )
            if not order:
                continue
            try:
                self._fill_order(order, bid, market)
                triggered += 1
            except ValueError as exc:
                self.db.execute(
                    """
                    UPDATE paper_orders SET status='canceled',canceled_at=?,error=?
                    WHERE id=?
                    """,
                    (iso_now(), str(exc), order_id),
                )
        return triggered

    def process_swing_exits(self, ticker: str, market: dict[str, Any]) -> int:
        entries = self.db.fetch_all(
            """
            SELECT * FROM paper_entries
            WHERE ticker=? AND strategy='SWING' AND status='open'
              AND remaining_contracts>0
            ORDER BY id ASC
            """,
            (ticker,),
        )
        if not entries:
            return 0
        market_row = self.db.fetch_one(
            "SELECT close_time FROM markets WHERE ticker=?", (ticker,)
        )
        observed = parse_time(market.get("observed_at")) or parse_time(iso_now())
        close = parse_time(market_row.get("close_time")) if market_row else None
        seconds_remaining = (
            max(0.0, (close - observed).total_seconds())
            if close is not None and observed is not None else None
        )
        slippage = float(self.db.settings().get("slippage_cents", 0.5)) / 100
        closed = 0
        for entry in entries:
            side = str(entry["side"])
            bid = self.executable_price(market, side, "SELL")
            if bid is None:
                continue
            self.db.execute(
                """
                UPDATE paper_entries SET
                    max_favorable_bid=CASE
                        WHEN max_favorable_bid IS NULL OR max_favorable_bid<? THEN ?
                        ELSE max_favorable_bid END,
                    min_adverse_bid=CASE
                        WHEN min_adverse_bid IS NULL OR min_adverse_bid>? THEN ?
                        ELSE min_adverse_bid END
                WHERE id=?
                """,
                (bid, bid, bid, bid, entry["id"]),
            )
            target = float(entry.get("target_exit_price") or 0.10)
            reason = None
            source = None
            if bid + 1e-12 >= target:
                reason, source = "TARGET", "swing_target"
            elif (
                str(entry.get("fallback_exit_mode") or "Exit") == "Exit"
                and seconds_remaining is not None
                and seconds_remaining
                <= float(entry.get("fallback_exit_seconds") or 120)
            ):
                reason, source = "FALLBACK", "swing_fallback"
            if not reason or not source:
                continue
            bid_size = market.get(f"{side.lower()}_bid_size")
            available = int(float(bid_size)) if bid_size is not None else int(
                entry["remaining_contracts"]
            )
            contracts = min(int(entry["remaining_contracts"]), max(0, available))
            if contracts < 1:
                continue
            fill_price = max(0.001, bid - slippage)
            order_id = self.db.execute(
                """
                INSERT INTO paper_orders(
                    ticker,side,action,order_type,status,created_at,
                    requested_contracts,source,entry_id,strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticker, side, "SELL", "MARKET", "open", iso_now(),
                    contracts, source, entry["id"], "SWING",
                ),
            )
            order = self.db.fetch_one(
                "SELECT * FROM paper_orders WHERE id=?", (order_id,)
            )
            if not order:
                continue
            try:
                self._fill_order(order, fill_price, market)
                closed += 1
            except ValueError as exc:
                self.db.execute(
                    """
                    UPDATE paper_orders SET status='canceled',canceled_at=?,error=?
                    WHERE id=?
                    """,
                    (iso_now(), str(exc), order_id),
                )
        return closed

    def cancel_order(self, order_id: int) -> bool:
        order = self.db.fetch_one(
            "SELECT id FROM paper_orders WHERE id=? AND status='open'", (order_id,)
        )
        if not order:
            return False
        self.db.execute(
            "UPDATE paper_orders SET status='canceled',canceled_at=? WHERE id=?",
            (iso_now(), order_id),
        )
        return True

    def reset_round(self) -> dict[str, int]:
        trades = self.db.fetch_one("SELECT COUNT(*) AS count FROM paper_trades")
        orders = self.db.fetch_one("SELECT COUNT(*) AS count FROM paper_orders")
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM paper_entries")
            connection.execute("DELETE FROM paper_orders")
            connection.execute("DELETE FROM paper_trades")
        self.reset_automatic_confirmation()
        return {
            "cleared_trades": int(trades["count"] if trades else 0),
            "cleared_orders": int(orders["count"] if orders else 0),
        }

    def open_from_decision(
        self, ticker: str, decision: Decision, model_version: str = "baseline-1.0"
    ) -> bool:
        if decision.signal != "BUY" or not decision.side:
            return False
        if decision.suggested_contracts < 1 or decision.executable_price is None:
            return False
        existing = self.db.fetch_one(
            """
            SELECT id FROM paper_entries
            WHERE ticker=? AND source='automatic'
            """,
            (ticker,),
        )
        if existing:
            return False
        contracts = decision.suggested_contracts
        try:
            self._validate_buy(ticker, decision.side, decision.executable_price, contracts)
        except ValueError:
            return False
        stop_price = _stop_price_from_cents(
            self.db.settings().get("default_stop_loss_cents")
        )
        order_id = self.db.execute(
            """
            INSERT INTO paper_orders(
                ticker,side,action,order_type,status,created_at,requested_contracts,
                source,stop_loss_price,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, decision.side, "BUY", "MARKET", "open", iso_now(),
                contracts, "automatic", stop_price, "STANDARD_EDGE",
            ),
        )
        order = self.db.fetch_one("SELECT * FROM paper_orders WHERE id=?", (order_id,))
        if not order:
            return False
        self._fill_order(
            order,
            decision.executable_price,
            {
                "model_probability": decision.model_probability,
                "market_probability": decision.market_probability,
                "expected_value": decision.expected_value,
                "entry_reason": decision.explanation,
                "margin_volatility_index": getattr(
                    self, "_entry_volatility", {}
                ).get("mvi"),
                "margin_cushion_ratio": getattr(
                    self, "_entry_volatility", {}
                ).get("cushion_ratio"),
            },
        )
        trade = self.db.fetch_one(
            "SELECT id FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
            (ticker, decision.side),
        )
        if trade:
            self.db.execute(
                """
                UPDATE paper_trades SET edge=?,expected_value=?,confidence=?,model_version=?
                WHERE id=?
                """,
                (
                    decision.edge, decision.expected_value, decision.confidence,
                    model_version, trade["id"],
                ),
            )
        return True

    def _reset_standard_confirmation(self) -> None:
        self._automatic_key = None
        self._automatic_started_at = None
        self._automatic_last_at = None
        self._automatic_last_buy = False
        self._automatic_segments.clear()

    def reset_automatic_confirmation(self) -> None:
        self._reset_standard_confirmation()
        self._strategy_states.clear()

    def consider_automatic_entry(
        self,
        *,
        ticker: str,
        decision: Decision,
        seconds_remaining: float,
        model_version: str,
        now: float | None = None,
        automatic_enabled: bool | None = None,
        open_handler: Callable[[str, Decision, str], bool] | None = None,
    ) -> dict[str, Any]:
        settings = self.db.settings()
        key = (ticker, str(decision.side or ""))
        current_time = time.monotonic() if now is None else float(now)
        window = float(settings.get("automatic_confirmation_seconds", 10))
        entry_window = float(settings.get("automatic_entry_window_minutes", 5)) * 60
        enabled = (
            bool(settings.get("paper_trading_enabled", False))
            if automatic_enabled is None else bool(automatic_enabled)
        )
        inside_window = 0 < seconds_remaining <= entry_window
        if not enabled or not inside_window or not decision.side:
            self.reset_automatic_confirmation()
            return {
                "armed": False, "progress": 0.0, "ready": False,
                "entered": False,
            }
        if self._automatic_key != key:
            self.reset_automatic_confirmation()
            self._automatic_key = key
            self._automatic_started_at = current_time
            self._automatic_last_at = current_time
            self._automatic_last_buy = decision.signal == "BUY"
            return {
                "armed": True, "progress": 0.0, "ready": False,
                "entered": False,
            }
        assert self._automatic_started_at is not None
        assert self._automatic_last_at is not None
        if current_time < self._automatic_last_at:
            self.reset_automatic_confirmation()
            return {
                "armed": False, "progress": 0.0, "ready": False,
                "entered": False,
            }
        if current_time > self._automatic_last_at:
            self._automatic_segments.append(
                (self._automatic_last_at, current_time, self._automatic_last_buy)
            )
        self._automatic_last_at = current_time
        self._automatic_last_buy = decision.signal == "BUY"
        cutoff = current_time - window
        while self._automatic_segments and self._automatic_segments[0][1] <= cutoff:
            self._automatic_segments.popleft()
        observed = min(window, current_time - self._automatic_started_at)
        buy_seconds = sum(
            max(0.0, end - max(start, cutoff))
            for start, end, is_buy in self._automatic_segments
            if is_buy
        )
        ratio = buy_seconds / window if window > 0 else 0.0
        confidence_rank = {"Low": 0, "Moderate": 1, "High": 2}
        required_confidence = str(settings.get("automatic_min_confidence", "High"))
        confidence_ok = confidence_rank.get(decision.confidence, 0) >= confidence_rank.get(
            required_confidence, 2
        )
        complete = observed >= window
        required_ratio = float(settings.get("automatic_buy_duration_pct", 0.70))
        ready = bool(
            complete
            and decision.signal == "BUY"
            and ratio + 1e-9 >= required_ratio
            and confidence_ok
        )
        entered = False
        if ready:
            entered = (
                open_handler(ticker, decision, model_version)
                if open_handler is not None
                else self.open_from_decision(ticker, decision, model_version)
            )
            if entered:
                self.reset_automatic_confirmation()
        return {
            "armed": True,
            "progress": min(1.0, observed / window) if window > 0 else 1.0,
            "buy_duration_pct": ratio,
            "confidence_ok": confidence_ok,
            "ready": ready,
            "entered": entered,
        }

    def _automatic_entry_exists(self, ticker: str) -> bool:
        return self.db.fetch_one(
            "SELECT id FROM paper_entries WHERE ticker=? AND source='automatic' LIMIT 1",
            (ticker,),
        ) is not None

    @staticmethod
    def _confidence_meets(decision: Decision, required: str) -> bool:
        rank = {"Low": 0, "Moderate": 1, "High": 2}
        return rank.get(decision.confidence, 0) >= rank.get(required, 2)

    def _standard_candidates(
        self,
        decisions: dict[str, Decision],
        required_confidence: str,
    ) -> list[Decision]:
        return [
            decision
            for decision in decisions.values()
            if decision.signal == "BUY"
            and decision.side in {"YES", "NO"}
            and self._confidence_meets(decision, required_confidence)
        ]

    @staticmethod
    def _threshold_margin_gate(
        settings: dict[str, Any],
        *,
        side: str,
        margin_dollars: float | None,
    ) -> dict[str, Any]:
        required = max(
            0.0, float(settings.get("threshold_margin_gate_dollars", 50.0))
        )
        normalized_side = str(side or "YES").upper()
        signed_required = required if normalized_side == "YES" else -required
        if required <= 0:
            return {
                "enabled": False,
                "passed": True,
                "current": margin_dollars,
                "required": signed_required,
                "detail": "The threshold-margin gate is off.",
            }
        if margin_dollars is None or not math.isfinite(float(margin_dollars)):
            return {
                "enabled": True,
                "passed": False,
                "current": None,
                "required": signed_required,
                "detail": "Waiting for the BTC-proxy distance from the threshold.",
            }
        current = float(margin_dollars)
        passed = (
            current + 1e-12 >= required
            if normalized_side == "YES"
            else current - 1e-12 <= -required
        )
        direction = "above" if normalized_side == "YES" else "below"
        label = "Up" if normalized_side == "YES" else "Down"
        article = "an" if normalized_side == "YES" else "a"
        detail = (
            f"The BTC proxy is outside the ${required:,.2f} {label} threshold band."
            if passed
            else (
                f"The BTC proxy must be at least ${required:,.2f} {direction} "
                f"the threshold for {article} {label} entry."
            )
        )
        return {
            "enabled": True,
            "passed": passed,
            "current": current,
            "required": signed_required,
            "detail": detail,
        }

    def _standard_entry_readiness(
        self,
        *,
        ticker: str,
        assessments: dict[str, dict[str, Any]],
        standard_decisions: dict[str, Decision],
        standard: Decision | None,
        confirmation: dict[str, Any],
        seconds_remaining: float,
        status_open: bool,
        priority_strategy: str | None,
        blocked_reason: str | None,
        entry_exists: bool,
        portfolio: dict[str, Any],
        automatic_enabled: bool | None = None,
        execution_mode: str = "PAPER",
        execution_block_reason: str | None = None,
        execution_risk_by_side: dict[str, dict[str, Any]] | None = None,
        threshold_margin_dollars: float | None = None,
        margin_volatility: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Describe Standard Edge using the same inputs that drive execution."""
        settings = self.db.settings()
        probability_required = float(settings.get("minimum_buy_probability", 0.55))
        ev_required = float(settings.get("buy_edge", settings.get("min_edge", 0.05)))
        ev_required += float(settings.get("hold_buffer", 0.005))
        confirmation_required = float(
            settings.get("automatic_confirmation_seconds", 10)
        )
        entry_window = float(settings.get("automatic_entry_window_minutes", 5)) * 60
        required_confidence = str(settings.get("automatic_min_confidence", "High"))
        spread_required = (
            float(settings.get("confidence_high_max_spread", 0.02))
            if required_confidence == "High"
            else float(settings.get("confidence_moderate_max_spread", 0.03))
            if required_confidence == "Moderate"
            else None
        )
        liquidity_required = int(settings.get("minimum_liquidity_contracts", 1))

        def side_score(side: str) -> tuple[float, float, float]:
            assessment = assessments.get(side) or {}
            probability = float(assessment.get("model_probability") or 0.0)
            raw_expected_value = (assessment.get("buy") or {}).get("expected_value")
            expected_value = (
                float(raw_expected_value) if raw_expected_value is not None else -1.0
            )
            probability_progress = probability / max(probability_required, 1e-9)
            ev_progress = (
                1.0
                if ev_required <= 0 and expected_value >= ev_required
                else max(0.0, expected_value) / max(ev_required, 1e-9)
            )
            return min(probability_progress, ev_progress), expected_value, probability

        if standard is not None and standard.side in {"YES", "NO"}:
            side = str(standard.side)
        else:
            available_sides = [
                side
                for side in ("YES", "NO")
                if (assessments.get(side) or {}).get("model_probability") is not None
            ]
            side = max(available_sides, key=side_score, default="YES")
        assessment = assessments.get(side) or {}
        decision = standard_decisions.get(side)
        buy = assessment.get("buy") or {}
        probability = assessment.get("model_probability")
        expected_value = buy.get("expected_value")
        spread = assessment.get("spread")
        liquidity = assessment.get("ask_size")
        probability_value = float(probability) if probability is not None else None
        ev_value = float(expected_value) if expected_value is not None else None
        spread_value = float(spread) if spread is not None else None
        liquidity_value = float(liquidity) if liquidity is not None else None
        threshold_gate = self._threshold_margin_gate(
            settings, side=side, margin_dollars=threshold_margin_dollars
        )
        volatility_gate = MarginVolatilityService.gate(settings, margin_volatility)

        probability_passed = bool(
            probability_value is not None
            and probability_value + 1e-12 >= probability_required
        )
        ev_passed = bool(
            ev_value is not None and ev_value + 1e-12 >= ev_required
        )
        spread_passed = bool(
            spread_value is not None
            and (spread_required is None or spread_value <= spread_required + 1e-12)
        )
        liquidity_passed = bool(
            liquidity_value is not None
            and liquidity_value + 1e-12 >= liquidity_required
        )
        confidence_ok = bool(
            decision is not None
            and self._confidence_meets(decision, required_confidence)
        )
        data_reliable = bool(assessment.get("data_reliable"))
        trade_allowed = bool(assessment.get("trade_allowed"))
        raw_data_passed = data_reliable and trade_allowed and status_open
        data_passed = raw_data_passed
        data_detail = str(assessment.get("quality_reason") or "Market data unavailable.")
        quality_detail = "Entry quality is unavailable."
        if decision is not None:
            quality_detail = (
                f"Entry quality is {decision.confidence}; {required_confidence} is required."
            )
        if not status_open:
            data_detail = "The market is not active."

        entered = bool(confirmation.get("entered"))
        risk_passed = bool(portfolio.get("automatic_trade_allowed", False))
        risk_detail = str(
            portfolio.get("automatic_trade_block_reason") or "Risk controls are clear."
        )
        if entry_exists:
            risk_passed = False
            risk_detail = "An automatic entry already exists for this market."
        elif decision is not None and decision.reason_code == "SIZE_TOO_SMALL":
            risk_passed = False
            risk_detail = decision.explanation
        elif (
            execution_mode == "PAPER"
            and
            not entered
            and risk_passed
            and decision is not None
            and decision.signal == "BUY"
            and decision.executable_price is not None
            and decision.suggested_contracts > 0
        ):
            try:
                self._validate_buy(
                    ticker,
                    str(decision.side),
                    float(decision.executable_price),
                    int(decision.suggested_contracts),
                )
            except ValueError as exc:
                risk_passed = False
                risk_detail = str(exc)

        enabled = (
            bool(settings.get("paper_trading_enabled", False))
            if automatic_enabled is None else bool(automatic_enabled)
        )
        if execution_block_reason:
            risk_passed = False
            risk_detail = execution_block_reason
        elif execution_mode != "PAPER" and standard is not None:
            execution_risk = (execution_risk_by_side or {}).get(side) or {}
            if not execution_risk.get("passed", False):
                risk_passed = False
                risk_detail = str(
                    execution_risk.get("primary_blocker")
                    or "The selected account cannot accept this entry."
                )
        inside_window = 0 < seconds_remaining <= entry_window
        priority_blocked = bool(
            priority_strategy and priority_strategy != "STANDARD_EDGE"
        )
        prerequisites_passed = bool(
            enabled
            and inside_window
            and not priority_blocked
            and blocked_reason is None
            and standard is not None
            and probability_passed
            and ev_passed
            and spread_passed
            and liquidity_passed
            and data_passed
            and confidence_ok
            and threshold_gate["passed"]
            and volatility_gate["passed"]
            and risk_passed
        )
        confirmation_progress = (
            min(1.0, max(0.0, float(confirmation.get("progress") or 0.0)))
            if prerequisites_passed
            else 0.0
        )
        confirmation_ready = bool(
            prerequisites_passed
            and (confirmation.get("ready") or confirmation.get("entered"))
        )
        if entered:
            status = "ENTERED"
        elif confirmation_ready:
            status = "READY"
        elif prerequisites_passed:
            status = "CONFIRMING"
        elif (
            blocked_reason
            or priority_blocked
            or not enabled
            or not inside_window
            or not spread_passed
            or not liquidity_passed
            or not data_passed
            or not confidence_ok
            or not threshold_gate["passed"]
            or not volatility_gate["passed"]
            or not risk_passed
        ):
            status = "BLOCKED"
        else:
            status = "WATCHING"

        if priority_blocked:
            blocker = f"{str(priority_strategy).replace('_', ' ').title()} has priority."
        elif blocked_reason:
            blocker = blocked_reason
        elif not enabled:
            blocker = f"Automatic {execution_mode.title()} trading is off."
        elif not inside_window:
            blocker = "Outside the Standard Edge entry window."
        elif not raw_data_passed:
            blocker = data_detail
        elif not threshold_gate["passed"]:
            blocker = str(threshold_gate["detail"])
        elif not volatility_gate["passed"]:
            blocker = str(volatility_gate["detail"])
        elif not risk_passed:
            blocker = risk_detail
        elif not spread_passed:
            blocker = "Spread exceeds the Standard Edge limit."
        elif not liquidity_passed:
            blocker = "Not enough contracts are available at the ask."
        elif not confidence_ok:
            blocker = quality_detail
        elif not probability_passed:
            shortfall = probability_required - float(probability_value or 0.0)
            blocker = f"Waiting for {shortfall * 100:.1f} points more win chance."
        elif not ev_passed:
            shortfall = ev_required - float(ev_value or 0.0)
            blocker = f"Waiting for {shortfall * 100:.1f} cents more EV."
        elif confirmation_ready:
            blocker = "Entry is ready."
        else:
            remaining = max(
                0.0, confirmation_required * (1.0 - confirmation_progress)
            )
            blocker = f"Confirming for {remaining:.1f} more seconds."

        return {
            "strategy": "STANDARD_EDGE",
            "mode": execution_mode,
            "side": side,
            "status": status,
            "ready": confirmation_ready,
            "entered": entered,
            "priority_strategy": priority_strategy,
            "blocker": blocker,
            "metrics": {
                "probability": {
                    "current": probability_value,
                    "required": probability_required,
                    "progress": min(
                        1.0,
                        max(0.0, float(probability_value or 0.0))
                        / max(probability_required, 1e-9),
                    ),
                    "passed": probability_passed,
                },
                "net_ev": {
                    "current": ev_value,
                    "required": ev_required,
                    "progress": min(
                        1.0,
                        1.0
                        if ev_required <= 0 and ev_passed
                        else max(0.0, float(ev_value or 0.0))
                        / max(ev_required, 1e-9),
                    ),
                    "passed": ev_passed,
                },
                "confirmation": {
                    "current_seconds": confirmation_progress * confirmation_required,
                    "required_seconds": confirmation_required,
                    "progress": confirmation_progress,
                    "passed": confirmation_ready,
                    "locked": not prerequisites_passed,
                },
            },
            "gates": {
                "spread": {
                    "passed": spread_passed,
                    "current": spread_value,
                    "required": spread_required,
                    "detail": "Spread is within the entry-quality limit."
                    if spread_passed else "Spread exceeds the entry-quality limit.",
                },
                "liquidity": {
                    "passed": liquidity_passed,
                    "current": liquidity_value,
                    "required": liquidity_required,
                    "detail": "Enough contracts are available at the ask."
                    if liquidity_passed else "Not enough contracts are available at the ask.",
                },
                "data": {
                    "passed": data_passed,
                    "detail": data_detail,
                },
                "quality": {
                    "passed": confidence_ok,
                    "current": decision.confidence if decision is not None else None,
                    "required": required_confidence,
                    "detail": quality_detail,
                },
                "threshold_margin": threshold_gate,
                "volatility": volatility_gate,
                "risk": {
                    "passed": risk_passed,
                    "detail": risk_detail,
                },
            },
        }

    def _fixed_strategy_size(
        self,
        *,
        ticker: str,
        price: float,
        requested_fraction: float,
        available_contracts: float | None,
    ) -> tuple[int, float]:
        settings = self.db.settings()
        portfolio = self.portfolio()
        effective_fraction = self._effective_strategy_fraction(settings, requested_fraction)
        bankroll = max(0.0, float(portfolio.get("current_bankroll") or 0.0))
        cash = max(0.0, float(portfolio.get("available_cash") or 0.0))
        existing = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(entry_cost + fees), 0) AS committed
            FROM paper_trades WHERE ticker=? AND status='open'
            """,
            (ticker,),
        )
        pending = self.db.fetch_all(
            """
            SELECT limit_price,requested_contracts FROM paper_orders
            WHERE ticker=? AND action='BUY' AND status='open'
            """,
            (ticker,),
        )
        existing_commitment = float(existing["committed"] or 0) if existing else 0.0
        existing_commitment += sum(
            self._order_commitment(
                float(order["limit_price"]), int(order["requested_contracts"])
            )
            for order in pending
        )
        position_capacity = (
            max(
                0.0,
                bankroll * float(settings.get("max_position_pct", 0.05))
                - existing_commitment,
            )
            if settings.get("risk_controls_enabled", True)
            else cash
        )
        target = min(cash, bankroll * effective_fraction, position_capacity)
        unit_cost = price + kalshi_fee(price)
        contracts = math.floor(target / unit_cost) if unit_cost > 0 else 0
        if available_contracts is not None:
            contracts = min(contracts, math.floor(max(0.0, float(available_contracts))))
        return max(0, contracts), effective_fraction

    @staticmethod
    def _effective_strategy_fraction(
        settings: dict[str, Any], requested_fraction: float
    ) -> float:
        effective = max(0.0, float(requested_fraction))
        if settings.get("risk_controls_enabled", True):
            effective = min(
                effective,
                float(settings.get("max_risk_per_trade_pct", 0.03)),
                float(settings.get("max_position_pct", 0.05)),
            )
        return effective

    def open_fixed_strategy(
        self,
        *,
        ticker: str,
        strategy: str,
        assessment: dict[str, Any],
        bankroll_fraction: float,
        model_version: str,
        reason: str,
        stop_loss_cents: Any = _USE_DEFAULT_STOP,
        target_exit_price: float | None = None,
        fallback_exit_mode: str | None = None,
        fallback_exit_seconds: float | None = None,
        strategy_metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, float]:
        side = str(assessment.get("side") or "")
        buy = assessment.get("buy") or {}
        price = buy.get("executable_price")
        probability = assessment.get("model_probability")
        expected_value = buy.get("expected_value")
        if (
            side not in {"YES", "NO"}
            or price is None
            or probability is None
            or expected_value is None
            or self._automatic_entry_exists(ticker)
        ):
            return False, 0.0
        contracts, effective_fraction = self._fixed_strategy_size(
            ticker=ticker,
            price=float(price),
            requested_fraction=bankroll_fraction,
            available_contracts=assessment.get("ask_size"),
        )
        if contracts < 1:
            return False, effective_fraction
        try:
            self._validate_buy(ticker, side, float(price), contracts)
        except ValueError:
            return False, effective_fraction
        configured_stop = (
            self.db.settings().get("default_stop_loss_cents")
            if stop_loss_cents is _USE_DEFAULT_STOP else stop_loss_cents
        )
        stop_price = _stop_price_from_cents(configured_stop)
        order_id = self.db.execute(
            """
            INSERT INTO paper_orders(
                ticker,side,action,order_type,status,created_at,requested_contracts,
                source,stop_loss_price,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, side, "BUY", "MARKET", "open", iso_now(), contracts,
                "automatic", stop_price, strategy,
            ),
        )
        order = self.db.fetch_one("SELECT * FROM paper_orders WHERE id=?", (order_id,))
        if not order:
            return False, effective_fraction
        self._fill_order(
            order,
            float(price),
            {
                "model_probability": probability,
                "market_probability": assessment.get("market_probability"),
                "expected_value": expected_value,
                "entry_reason": reason,
                "margin_volatility_index": getattr(
                    self, "_entry_volatility", {}
                ).get("mvi"),
                "margin_cushion_ratio": getattr(
                    self, "_entry_volatility", {}
                ).get("cushion_ratio"),
            },
        )
        if strategy == "SWING":
            initial_bid = (assessment.get("sell") or {}).get("raw_price")
            self.db.execute(
                """
                UPDATE paper_entries SET target_exit_price=?,fallback_exit_mode=?,
                    fallback_exit_seconds=?,max_favorable_bid=?,min_adverse_bid=?,
                    strategy_metadata_json=?
                WHERE order_id=?
                """,
                (
                    target_exit_price, fallback_exit_mode, fallback_exit_seconds,
                    initial_bid, initial_bid,
                    json.dumps(strategy_metadata or {}, sort_keys=True), order_id,
                ),
            )
        trade = self.db.fetch_one(
            "SELECT id FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        )
        if trade:
            self.db.execute(
                """
                UPDATE paper_trades SET edge=?,expected_value=?,confidence=?,model_version=?
                WHERE id=?
                """,
                (
                    buy.get("net_edge"), expected_value, strategy.replace("_", " ").title(),
                    model_version, trade["id"],
                ),
            )
        return True, effective_fraction

    @staticmethod
    def _best_buy_assessment(
        assessments: dict[str, dict[str, Any]],
        *,
        minimum_probability: float,
        minimum_ev: float,
        maximum_spread: float,
        minimum_liquidity: int,
    ) -> dict[str, Any] | None:
        candidates = []
        for assessment in assessments.values():
            buy = assessment.get("buy") or {}
            probability = assessment.get("model_probability")
            expected_value = buy.get("expected_value")
            spread = assessment.get("spread")
            liquidity = assessment.get("ask_size")
            if (
                assessment.get("trade_allowed")
                and probability is not None
                and float(probability) >= minimum_probability
                and expected_value is not None
                and float(expected_value) + 1e-12 >= minimum_ev
                and spread is not None
                and float(spread) <= maximum_spread
                and liquidity is not None
                and float(liquidity) >= minimum_liquidity
                and buy.get("executable_price") is not None
            ):
                candidates.append(assessment)
        return max(
            candidates,
            key=lambda item: (
                float((item.get("buy") or {}).get("expected_value") or 0.0),
                float(item.get("model_probability") or 0.0),
            ),
            default=None,
        )

    @staticmethod
    def _swing_candidates(
        assessments: dict[str, dict[str, Any]], settings: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        available = [
            assessment for assessment in assessments.values()
            if (assessment.get("buy") or {}).get("raw_price") is not None
        ]
        monitored = max(
            available,
            key=lambda item: (
                float((item.get("buy") or {}).get("expected_value") or -1.0),
                -float((item.get("buy") or {}).get("raw_price") or 1.0),
            ),
            default=None,
        )
        maximum_price = float(settings.get("swing_max_entry_price", 0.05))
        minimum_advantage = float(settings.get("swing_min_model_advantage", 0.03))
        maximum_spread = float(settings.get("swing_max_spread", 0.03))
        minimum_liquidity = int(settings.get("swing_min_liquidity_contracts", 1))
        eligible = []
        for assessment in available:
            buy = assessment.get("buy") or {}
            raw_ask = buy.get("raw_price")
            advantage = buy.get("expected_value")
            spread = assessment.get("spread")
            liquidity = assessment.get("ask_size")
            if (
                assessment.get("trade_allowed")
                and raw_ask is not None
                and float(raw_ask) <= maximum_price + 1e-12
                and advantage is not None
                and float(advantage) + 1e-12 >= minimum_advantage
                and spread is not None
                and float(spread) <= maximum_spread + 1e-12
                and liquidity is not None
                and float(liquidity) >= minimum_liquidity
                and buy.get("executable_price") is not None
            ):
                eligible.append(assessment)
        candidate = max(
            eligible,
            key=lambda item: (
                float((item.get("buy") or {}).get("expected_value") or -1.0),
                float(item.get("model_probability") or 0.0),
            ),
            default=None,
        )
        return monitored, candidate

    def _swing_entry_readiness(
        self,
        *,
        ticker: str,
        assessments: dict[str, dict[str, Any]],
        opening_elapsed: float | None,
        status_open: bool,
        confirmation: dict[str, Any],
        priority_strategy: str | None,
        blocked_reason: str | None,
        entry_exists: bool,
        portfolio: dict[str, Any],
        entered: bool,
    ) -> dict[str, Any]:
        settings = self.db.settings()
        monitored, candidate = self._swing_candidates(assessments, settings)
        selected = candidate or monitored
        buy = (selected or {}).get("buy") or {}
        raw_ask = buy.get("raw_price")
        executable = buy.get("executable_price")
        fee = buy.get("fee_per_contract")
        break_even = (
            float(executable) + float(fee or 0.0)
            if executable is not None else None
        )
        advantage = buy.get("expected_value")
        spread = (selected or {}).get("spread")
        liquidity = (selected or {}).get("ask_size")
        maximum_price = float(settings.get("swing_max_entry_price", 0.05))
        required_advantage = float(settings.get("swing_min_model_advantage", 0.03))
        maximum_spread = float(settings.get("swing_max_spread", 0.03))
        minimum_liquidity = int(settings.get("swing_min_liquidity_contracts", 1))
        entry_window = float(settings.get("swing_entry_window_seconds", 300))
        inside_window = bool(
            opening_elapsed is not None and 0 <= opening_elapsed < entry_window
        )
        data_passed = bool(selected and selected.get("trade_allowed"))
        price_passed = bool(
            raw_ask is not None and float(raw_ask) <= maximum_price + 1e-12
        )
        advantage_passed = bool(
            advantage is not None and float(advantage) + 1e-12 >= required_advantage
        )
        spread_passed = bool(
            spread is not None and float(spread) <= maximum_spread + 1e-12
        )
        liquidity_passed = bool(
            liquidity is not None and float(liquidity) >= minimum_liquidity
        )
        risk_passed = bool(portfolio.get("automatic_trade_allowed", True))
        confirmation_ready = bool(confirmation.get("ready"))
        open_swing = self.db.fetch_one(
            """
            SELECT * FROM paper_entries
            WHERE ticker=? AND strategy='SWING' AND status='open' LIMIT 1
            """,
            (ticker,),
        )
        blocker = None
        status = "WATCHING"
        if entered:
            status, blocker = "ENTERED", "Swing position opened."
        elif open_swing:
            status = "OPEN"
            blocker = (
                f"Waiting for {float(open_swing.get('target_exit_price') or 0.10) * 100:.0f}¢ target."
            )
        elif not settings.get("swing_enabled", False):
            status, blocker = "DISABLED", "Swing Trade is off."
        elif blocked_reason:
            status, blocker = "BLOCKED", blocked_reason
        elif entry_exists:
            status, blocker = "BLOCKED", "An automatic entry already exists for this market."
        elif not status_open:
            status, blocker = "BLOCKED", "The market is not active."
        elif priority_strategy and priority_strategy != "SWING":
            status = "BLOCKED"
            blocker = f"{priority_strategy.replace('_', ' ').title()} has priority."
        elif not inside_window:
            status, blocker = "BLOCKED", "Swing entry window closed."
        elif not data_passed:
            status, blocker = "BLOCKED", str(
                (selected or {}).get("quality_reason") or "Market data is not tradeable."
            )
        elif not price_passed:
            blocker = f"Watching for an ask at or below {maximum_price * 100:.0f}¢."
        elif not advantage_passed:
            blocker = "Model advantage is below the Swing requirement."
        elif not spread_passed:
            status, blocker = "BLOCKED", "Spread exceeds the Swing limit."
        elif not liquidity_passed:
            status, blocker = "BLOCKED", "Swing liquidity is below the minimum."
        elif not risk_passed:
            status = "BLOCKED"
            blocker = str(
                portfolio.get("automatic_trade_block_reason") or "Risk controls block entry."
            )
        elif not confirmation_ready:
            status, blocker = "CONFIRMING", "Swing conditions are confirming."
        else:
            status, blocker = "READY", "Swing entry is ready."
        return {
            "strategy": "SWING",
            "side": (selected or {}).get("side"),
            "status": status,
            "ready": status in {"READY", "ENTERED"},
            "entered": entered,
            "opening_elapsed_seconds": opening_elapsed,
            "entry_window_seconds": entry_window,
            "executable_ask": raw_ask,
            "simulated_fill_price": executable,
            "maximum_entry_price": maximum_price,
            "model_probability": (selected or {}).get("model_probability"),
            "all_in_break_even_probability": break_even,
            "model_advantage": advantage,
            "required_model_advantage": required_advantage,
            "target_exit_price": float(settings.get("swing_target_exit_price", 0.10)),
            "confirmation": confirmation,
            "priority_strategy": priority_strategy,
            "blocker": blocker,
            "gates": {
                "entry_window": {"passed": inside_window},
                "price": {"passed": price_passed, "current": raw_ask, "required": maximum_price},
                "model_advantage": {"passed": advantage_passed, "current": advantage, "required": required_advantage},
                "spread": {"passed": spread_passed, "current": spread, "required": maximum_spread},
                "liquidity": {"passed": liquidity_passed, "current": liquidity, "required": minimum_liquidity},
                "data": {"passed": data_passed},
                "risk": {"passed": risk_passed},
            },
        }

    def _confirm_strategy(
        self,
        *,
        strategy: str,
        ticker: str,
        side: str,
        duration: float,
        quote_marker: str | None,
        require_next_quote: bool,
        now: float,
    ) -> dict[str, Any]:
        key = (ticker, side)
        state = self._strategy_states.get(strategy)
        if not state or state.get("key") != key:
            state = {
                "key": key,
                "started_at": now,
                "initial_quote": quote_marker,
                "next_quote_seen": False,
            }
            self._strategy_states[strategy] = state
        elif quote_marker and quote_marker != state.get("initial_quote"):
            state["next_quote_seen"] = True
        elapsed = max(0.0, now - float(state["started_at"]))
        ready = elapsed + 1e-9 >= duration and (
            not require_next_quote or bool(state.get("next_quote_seen"))
        )
        return {
            "armed": True,
            "progress": 1.0 if duration <= 0 else min(1.0, elapsed / duration),
            "next_quote_seen": bool(state.get("next_quote_seen")),
            "ready": ready,
        }

    def consider_strategies(
        self,
        *,
        ticker: str,
        assessments: dict[str, dict[str, Any]],
        standard_decisions: dict[str, Decision],
        seconds_remaining: float,
        market_status: str | None,
        market_open_time: str | None,
        market_observed_at: str | None,
        threshold_state: dict[str, Any] | None,
        settlement_window: dict[str, Any],
        z_distance: float,
        threshold_margin_dollars: float | None = None,
        margin_volatility: dict[str, Any] | None = None,
        model_version: str,
        portfolio: dict[str, Any] | None = None,
        now: float | None = None,
        execution_mode: str = "PAPER",
        automatic_enabled: bool | None = None,
        execution_block_reason: str | None = None,
        execution_risk_by_side: dict[str, dict[str, Any]] | None = None,
        entry_exists_override: bool | None = None,
        standard_entry_handler: Callable[[str, Decision, str], bool] | None = None,
        fixed_entry_handler: Callable[..., tuple[bool, float]] | None = None,
    ) -> dict[str, Any]:
        settings = self.db.settings()
        self._entry_volatility = dict(margin_volatility or {})
        current_time = time.monotonic() if now is None else float(now)
        empty = {"armed": False, "progress": 0.0, "entered": False}
        result: dict[str, Any] = {
            "active_strategy": None,
            "entered": False,
            "effective_bankroll_allocation": 0.0,
            "early_threshold": dict(empty),
            "standard_edge": dict(empty),
            "late_conviction": dict(empty),
            "swing": dict(empty),
        }
        portfolio_state = portfolio or self.portfolio()
        required_confidence = str(settings.get("automatic_min_confidence", "High"))
        standard_candidates = self._standard_candidates(
            standard_decisions, required_confidence
        )
        standard = max(
            standard_candidates,
            key=lambda item: float(item.expected_value or float("-inf")),
            default=None,
        )
        status_open = str(market_status or "").lower() in {"active", "open"}
        entry_exists = (
            self._automatic_entry_exists(ticker)
            if entry_exists_override is None else bool(entry_exists_override)
        )
        observed = parse_time(market_observed_at)
        opened = parse_time(market_open_time)
        opening_elapsed = (
            (observed - opened).total_seconds()
            if observed is not None and opened is not None
            else None
        )

        def finish(
            *,
            priority_strategy: str | None = None,
            blocked_reason: str | None = None,
        ) -> dict[str, Any]:
            result["standard_edge_readiness"] = self._standard_entry_readiness(
                ticker=ticker,
                assessments=assessments,
                standard_decisions=standard_decisions,
                standard=standard,
                confirmation=result["standard_edge"],
                seconds_remaining=seconds_remaining,
                status_open=status_open,
                priority_strategy=priority_strategy,
                blocked_reason=blocked_reason,
                entry_exists=entry_exists,
                portfolio=portfolio_state,
                automatic_enabled=automatic_enabled,
                execution_mode=execution_mode,
                execution_block_reason=execution_block_reason,
                execution_risk_by_side=execution_risk_by_side,
                threshold_margin_dollars=threshold_margin_dollars,
                margin_volatility=margin_volatility,
            )
            result["swing_readiness"] = self._swing_entry_readiness(
                ticker=ticker,
                assessments=assessments,
                opening_elapsed=opening_elapsed,
                status_open=status_open,
                confirmation=result["swing"],
                priority_strategy=priority_strategy,
                blocked_reason=blocked_reason,
                entry_exists=entry_exists,
                portfolio=portfolio_state,
                entered=bool(result["swing"].get("entered")),
            )
            return result

        enabled = (
            bool(settings.get("paper_trading_enabled", False))
            if automatic_enabled is None else bool(automatic_enabled)
        )
        if not enabled:
            self.reset_automatic_confirmation()
            return finish(
                blocked_reason=f"Automatic {execution_mode.title()} trading is off."
            )
        if execution_block_reason:
            self.reset_automatic_confirmation()
            return finish(blocked_reason=execution_block_reason)
        if entry_exists:
            self.reset_automatic_confirmation()
            result["blocked_reason"] = "An automatic entry already exists for this market."
            return finish(blocked_reason=result["blocked_reason"])

        if not status_open:
            self.reset_automatic_confirmation()
            result["blocked_reason"] = "The market is not active."
            return finish(blocked_reason=result["blocked_reason"])

        volatility_gate = MarginVolatilityService.gate(settings, margin_volatility)
        if not volatility_gate["passed"]:
            self.reset_automatic_confirmation()
            result["blocked_reason"] = str(volatility_gate["detail"])
            return finish(blocked_reason=result["blocked_reason"])

        def margin_gate(side: str) -> dict[str, Any]:
            return self._threshold_margin_gate(
                settings,
                side=side,
                margin_dollars=threshold_margin_dollars,
            )
        early_candidate = None
        threshold_ready = False
        if threshold_state and observed is not None and opened is not None:
            first_seen = parse_time(threshold_state.get("first_observed_at"))
            latest_seen = parse_time(threshold_state.get("latest_observed_at"))
            threshold_ready = bool(
                first_seen
                and latest_seen
                and first_seen < opened
                and (observed - latest_seen).total_seconds()
                >= float(settings.get("early_threshold_stability_seconds", 1))
            )
        if (
            settings.get("early_threshold_enabled", True)
            and status_open
            and opening_elapsed is not None
            and 0 <= opening_elapsed <= float(settings.get("early_entry_window_seconds", 30))
            and threshold_ready
        ):
            early_candidate = self._best_buy_assessment(
                assessments,
                minimum_probability=float(settings.get("early_min_probability", 0.65)),
                minimum_ev=float(settings.get("early_min_net_ev", 0.005)),
                maximum_spread=float(settings.get("early_max_spread", 0.05)),
                minimum_liquidity=int(settings.get("early_min_liquidity_contracts", 1)),
            )
        if early_candidate:
            threshold_gate = margin_gate(str(early_candidate["side"]))
            if not threshold_gate["passed"]:
                self.reset_automatic_confirmation()
                result["active_strategy"] = "EARLY_THRESHOLD"
                result["blocked_reason"] = str(threshold_gate["detail"])
                return finish(
                    priority_strategy="EARLY_THRESHOLD",
                    blocked_reason=result["blocked_reason"],
                )
            effective = self._effective_strategy_fraction(
                settings, float(settings.get("early_bankroll_pct", 0.03))
            )
            confirmation = self._confirm_strategy(
                strategy="EARLY_THRESHOLD", ticker=ticker,
                side=str(early_candidate["side"]),
                duration=float(settings.get("early_confirmation_seconds", 2)),
                quote_marker=market_observed_at, require_next_quote=True,
                now=current_time,
            )
            result["active_strategy"] = "EARLY_THRESHOLD"
            result["effective_bankroll_allocation"] = effective
            result["early_threshold"] = {**confirmation, "entered": False}
            self._reset_standard_confirmation()
            self._strategy_states.pop("LATE_CONVICTION", None)
            self._strategy_states.pop("SWING", None)
            if confirmation["ready"]:
                opener = fixed_entry_handler or self.open_fixed_strategy
                entered, effective = opener(
                    ticker=ticker, strategy="EARLY_THRESHOLD",
                    assessment=early_candidate,
                    bankroll_fraction=float(settings.get("early_bankroll_pct", 0.03)),
                    model_version=model_version,
                    reason="Stable pre-open threshold remained underpriced after activation.",
                    strategy_metadata={
                        "margin_volatility_index": self._entry_volatility.get("mvi"),
                        "margin_cushion_ratio": self._entry_volatility.get("cushion_ratio"),
                        "margin_volatility_version": self._entry_volatility.get(
                            "calculation_version"
                        ),
                    },
                )
                result["entered"] = entered
                result["effective_bankroll_allocation"] = effective
                result["early_threshold"]["entered"] = entered
                if entered:
                    self.reset_automatic_confirmation()
            return finish(priority_strategy="EARLY_THRESHOLD")
        self._strategy_states.pop("EARLY_THRESHOLD", None)

        if standard is not None:
            threshold_gate = margin_gate(str(standard.side))
            if not threshold_gate["passed"]:
                self.reset_automatic_confirmation()
                result["active_strategy"] = "STANDARD_EDGE"
                result["blocked_reason"] = str(threshold_gate["detail"])
                return finish(
                    priority_strategy="STANDARD_EDGE",
                    blocked_reason=result["blocked_reason"],
                )
            standard_result = self.consider_automatic_entry(
                ticker=ticker, decision=standard, seconds_remaining=seconds_remaining,
                model_version=model_version, now=current_time,
                automatic_enabled=enabled, open_handler=standard_entry_handler,
            )
            result["active_strategy"] = "STANDARD_EDGE"
            result["standard_edge"] = standard_result
            result["entered"] = bool(standard_result.get("entered"))
            result["effective_bankroll_allocation"] = float(
                standard.suggested_fraction or 0.0
            )
            self._strategy_states.pop("LATE_CONVICTION", None)
            self._strategy_states.pop("SWING", None)
            return finish(priority_strategy="STANDARD_EDGE")
        self._reset_standard_confirmation()

        late_candidate = None
        if (
            settings.get("late_conviction_enabled", True)
            and 0 < seconds_remaining <= float(settings.get("late_max_seconds_remaining", 90))
            and abs(float(z_distance)) >= float(settings.get("late_min_z_distance", 2.0))
            and float(settlement_window.get("coverage") or 0.0)
            >= float(settings.get("late_min_settlement_coverage", 0.80))
        ):
            late_candidate = self._best_buy_assessment(
                assessments,
                minimum_probability=float(settings.get("late_min_probability", 0.85)),
                minimum_ev=float(settings.get("late_min_net_ev", 0.005)),
                maximum_spread=float(settings.get("late_max_spread", 0.03)),
                minimum_liquidity=int(settings.get("late_min_liquidity_contracts", 1)),
            )
        if late_candidate:
            threshold_gate = margin_gate(str(late_candidate["side"]))
            if not threshold_gate["passed"]:
                self.reset_automatic_confirmation()
                result["active_strategy"] = "LATE_CONVICTION"
                result["blocked_reason"] = str(threshold_gate["detail"])
                return finish(
                    priority_strategy="LATE_CONVICTION",
                    blocked_reason=result["blocked_reason"],
                )
            effective = self._effective_strategy_fraction(
                settings, float(settings.get("late_bankroll_pct", 0.03))
            )
            confirmation = self._confirm_strategy(
                strategy="LATE_CONVICTION", ticker=ticker,
                side=str(late_candidate["side"]),
                duration=float(settings.get("late_confirmation_seconds", 3)),
                quote_marker=market_observed_at, require_next_quote=False,
                now=current_time,
            )
            result["active_strategy"] = "LATE_CONVICTION"
            result["effective_bankroll_allocation"] = effective
            result["late_conviction"] = {**confirmation, "entered": False}
            self._strategy_states.pop("SWING", None)
            if confirmation["ready"]:
                opener = fixed_entry_handler or self.open_fixed_strategy
                entered, effective = opener(
                    ticker=ticker, strategy="LATE_CONVICTION",
                    assessment=late_candidate,
                    bankroll_fraction=float(settings.get("late_bankroll_pct", 0.03)),
                    model_version=model_version,
                    reason="High-probability late outcome retained positive Buy EV.",
                    strategy_metadata={
                        "margin_volatility_index": self._entry_volatility.get("mvi"),
                        "margin_cushion_ratio": self._entry_volatility.get("cushion_ratio"),
                        "margin_volatility_version": self._entry_volatility.get(
                            "calculation_version"
                        ),
                    },
                )
                result["entered"] = entered
                result["effective_bankroll_allocation"] = effective
                result["late_conviction"]["entered"] = entered
                if entered:
                    self.reset_automatic_confirmation()
            return finish(priority_strategy="LATE_CONVICTION")
        self._strategy_states.pop("LATE_CONVICTION", None)

        _, swing_candidate = self._swing_candidates(assessments, settings)
        swing_inside_window = bool(
            opening_elapsed is not None
            and 0 <= opening_elapsed
            < float(settings.get("swing_entry_window_seconds", 300))
        )
        if (
            settings.get("swing_enabled", False)
            and swing_inside_window
            and swing_candidate
            and portfolio_state.get("automatic_trade_allowed", True)
        ):
            threshold_gate = margin_gate(str(swing_candidate["side"]))
            if not threshold_gate["passed"]:
                self.reset_automatic_confirmation()
                result["active_strategy"] = "SWING"
                result["blocked_reason"] = str(threshold_gate["detail"])
                return finish(
                    priority_strategy="SWING",
                    blocked_reason=result["blocked_reason"],
                )
            effective = self._effective_strategy_fraction(
                settings, float(settings.get("swing_bankroll_pct", 0.01))
            )
            confirmation = self._confirm_strategy(
                strategy="SWING", ticker=ticker,
                side=str(swing_candidate["side"]),
                duration=float(settings.get("swing_confirmation_seconds", 0)),
                quote_marker=market_observed_at, require_next_quote=False,
                now=current_time,
            )
            result["active_strategy"] = "SWING"
            result["effective_bankroll_allocation"] = effective
            result["swing"] = {**confirmation, "entered": False}
            if confirmation["ready"]:
                opener = fixed_entry_handler or self.open_fixed_strategy
                entered, effective = opener(
                    ticker=ticker,
                    strategy="SWING",
                    assessment=swing_candidate,
                    bankroll_fraction=float(settings.get("swing_bankroll_pct", 0.01)),
                    model_version=model_version,
                    reason=(
                        "Early ask was below the Swing entry limit with sufficient "
                        "model advantage after fees and slippage."
                    ),
                    stop_loss_cents=settings.get("swing_stop_loss_cents"),
                    target_exit_price=float(
                        settings.get("swing_target_exit_price", 0.10)
                    ),
                    fallback_exit_mode=str(
                        settings.get("swing_fallback_mode", "Exit")
                    ),
                    fallback_exit_seconds=float(
                        settings.get("swing_fallback_seconds_remaining", 120)
                    ),
                    strategy_metadata={
                        "entry_window_seconds": float(
                            settings.get("swing_entry_window_seconds", 300)
                        ),
                        "maximum_entry_ask": float(
                            settings.get("swing_max_entry_price", 0.05)
                        ),
                        "minimum_model_advantage": float(
                            settings.get("swing_min_model_advantage", 0.03)
                        ),
                        "maximum_spread": float(
                            settings.get("swing_max_spread", 0.03)
                        ),
                        "minimum_liquidity_contracts": int(
                            settings.get("swing_min_liquidity_contracts", 1)
                        ),
                        "bankroll_fraction": float(
                            settings.get("swing_bankroll_pct", 0.01)
                        ),
                        "confirmation_seconds": float(
                            settings.get("swing_confirmation_seconds", 0)
                        ),
                        "margin_volatility_index": self._entry_volatility.get("mvi"),
                        "margin_cushion_ratio": self._entry_volatility.get(
                            "cushion_ratio"
                        ),
                        "margin_volatility_version": self._entry_volatility.get(
                            "calculation_version"
                        ),
                    },
                )
                result["entered"] = entered
                result["effective_bankroll_allocation"] = effective
                result["swing"]["entered"] = entered
                if entered:
                    self.reset_automatic_confirmation()
                    result["swing"] = {
                        "armed": True, "progress": 1.0,
                        "ready": True, "entered": True,
                    }
            return finish(priority_strategy="SWING")
        self._strategy_states.pop("SWING", None)
        return finish()

    def settle(self, ticker: str, result: int, settled_at: str) -> int:
        starting_bankroll = float(self.db.settings()["starting_bankroll"])
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE paper_orders SET status='canceled',canceled_at=?,error='Market settled'
                WHERE ticker=? AND status='open'
                """,
                (settled_at, ticker),
            )
            trade_rows = connection.execute(
                "SELECT * FROM paper_trades WHERE ticker=? AND status='open'",
                (ticker,),
            ).fetchall()
            trades = [dict(row) for row in trade_rows]
            connection.execute(
                """
                UPDATE paper_entries SET status='settled',remaining_contracts=0,
                    closed_at=?,stop_status=CASE
                        WHEN stop_status='active' THEN 'settled' ELSE stop_status END,
                    exit_reason=COALESCE(exit_reason,'SETTLEMENT'),
                    exit_price=CASE
                        WHEN (side='YES' AND ?=1) OR (side='NO' AND ?=0) THEN 1.0
                        ELSE 0.0 END,
                    exit_fees=COALESCE(exit_fees,0)
                WHERE ticker=? AND status='open'
                """,
                (settled_at, result, result, ticker),
            )
            for trade in trades:
                wins = (trade["side"] == "YES" and result == 1) or (
                    trade["side"] == "NO" and result == 0
                )
                payout = float(trade["contracts"]) if wins else 0.0
                pnl = payout - float(trade["entry_cost"]) - float(trade["fees"])
                connection.execute(
                    """
                    UPDATE paper_trades SET status='settled',settled_at=?,outcome=?,
                        payout=?,realized_pnl=? WHERE id=?
                    """,
                    (settled_at, int(wins), payout, pnl, trade["id"]),
                )
            if trades:
                available_cash_after = self._available_cash_in_transaction(
                    connection, starting_bankroll
                )
                trade_ids = [int(trade["id"]) for trade in trades]
                placeholders = ",".join("?" for _ in trade_ids)
                connection.execute(
                    f"UPDATE paper_trades SET available_cash_after=? WHERE id IN ({placeholders})",
                    (available_cash_after, *trade_ids),
                )
                connection.execute(
                    f"UPDATE paper_entries SET available_cash_after=? WHERE trade_id IN ({placeholders})",
                    (available_cash_after, *trade_ids),
                )
        return len(trades)
