from __future__ import annotations

import math
from datetime import date
from typing import Any

from app.db import Database
from app.domain import iso_now, kalshi_fee
from app.services.decision import Decision


class PaperTradingService:
    def __init__(self, db: Database):
        self.db = db

    def portfolio(self) -> dict[str, Any]:
        settings = self.db.settings()
        starting = float(settings["starting_bankroll"])
        trades = self.db.fetch_all("SELECT * FROM paper_trades ORDER BY opened_at ASC")
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
        automatic_enabled = bool(settings.get("paper_trading_enabled", True))
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
                settings.get("max_risk_per_trade_pct", 0.02)
            ),
            "max_session_drawdown_pct": float(
                settings.get("max_session_drawdown_pct", 0.10)
            ),
            "trades": list(reversed(trades[-100:])),
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
                }
                for trade in open_trades
            ],
        }

    @staticmethod
    def _order_commitment(price: float, contracts: int) -> float:
        return price * contracts + kalshi_fee(price, contracts)

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
                requested_contracts,limit_price,source
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, side, action, order_type, "open", iso_now(), requested_dollars,
                contracts, order_price if order_type == "LIMIT" else None, "manual",
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
        max_trade = bankroll * float(settings.get("max_risk_per_trade_pct", 0.02))
        if commitment > max_trade + 1e-9:
            raise ValueError("This order exceeds the maximum risk per trade.")
        position = self.db.fetch_one(
            "SELECT entry_cost,fees FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
            (ticker, side),
        )
        existing = float(position["entry_cost"] + position["fees"]) if position else 0.0
        pending = self.db.fetch_all(
            """
            SELECT limit_price,requested_contracts FROM paper_orders
            WHERE ticker=? AND side=? AND action='BUY' AND status='open'
            """,
            (ticker, side),
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
        with self.db.transaction() as connection:
            position_row = connection.execute(
                "SELECT * FROM paper_trades WHERE ticker=? AND side=? AND status='open'",
                (order["ticker"], order["side"]),
            ).fetchone()
            position = dict(position_row) if position_row else None
            realized_pnl = None
            if order["action"] == "BUY":
                entry_cost = price * contracts
                if position:
                    total_contracts = int(position["contracts"]) + contracts
                    total_cost = float(position["entry_cost"]) + entry_cost
                    old_source = position["source"] or "automatic"
                    source = "manual" if old_source == "manual" else "mixed"
                    connection.execute(
                        """
                        UPDATE paper_trades SET contracts=?,entry_price=?,entry_cost=?,
                            fees=?,source=? WHERE id=?
                        """,
                        (
                            total_contracts, total_cost / total_contracts, total_cost,
                            float(position["fees"]) + fees, source, position["id"],
                        ),
                    )
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
                    connection.execute(
                        """
                        INSERT INTO paper_trades(
                            ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                            model_probability,market_probability,edge,expected_value,
                            confidence,model_version,status,source
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            order["ticker"], order["side"], filled_at, price, contracts,
                            entry_cost, fees, float(market.get("model_probability") or 0.5),
                            float(market_probability), 0.0, 0.0, "Manual", "manual",
                            "open", "manual",
                        ),
                    )
            else:
                if not position or int(position["contracts"]) < contracts:
                    raise ValueError("The paper position no longer has enough contracts to sell.")
                held = int(position["contracts"])
                fraction = contracts / held
                allocated_cost = float(position["entry_cost"]) * fraction
                allocated_entry_fees = float(position["fees"]) * fraction
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
                    connection.execute(
                        """
                        UPDATE paper_trades SET contracts=?,entry_cost=?,fees=?,
                            realized_pnl=?,source=? WHERE id=?
                        """,
                        (
                            held - contracts,
                            float(position["entry_cost"]) - allocated_cost,
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
        return filled

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
            connection.execute("DELETE FROM paper_orders")
            connection.execute("DELETE FROM paper_trades")
        return {
            "cleared_trades": int(trades["count"] if trades else 0),
            "cleared_orders": int(orders["count"] if orders else 0),
        }

    def open_from_decision(
        self, ticker: str, decision: Decision, model_version: str = "baseline-1.0"
    ) -> bool:
        if not decision.signal.startswith("TRADE") or not decision.side:
            return False
        if decision.suggested_contracts < 1 or decision.executable_price is None:
            return False
        existing = self.db.fetch_one(
            "SELECT id FROM paper_trades WHERE ticker = ?",
            (ticker,),
        )
        if existing:
            return False
        contracts = decision.suggested_contracts
        try:
            self._validate_buy(ticker, decision.side, decision.executable_price, contracts)
        except ValueError:
            return False
        entry_cost = decision.executable_price * contracts
        fees = kalshi_fee(decision.executable_price, contracts)
        self.db.execute(
            """
            INSERT INTO paper_trades(
                ticker, side, opened_at, entry_price, contracts, entry_cost, fees,
                model_probability, market_probability, edge, expected_value,
                confidence, model_version, status, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                decision.side,
                iso_now(),
                decision.executable_price,
                contracts,
                entry_cost,
                fees,
                decision.model_probability,
                decision.market_probability,
                decision.edge,
                decision.expected_value,
                decision.confidence,
                model_version,
                "open",
                "automatic",
            ),
        )
        return True

    def settle(self, ticker: str, result: int, settled_at: str) -> int:
        self.db.execute(
            """
            UPDATE paper_orders SET status='canceled',canceled_at=?,error='Market settled'
            WHERE ticker=? AND status='open'
            """,
            (settled_at, ticker),
        )
        trades = self.db.fetch_all(
            "SELECT * FROM paper_trades WHERE ticker = ? AND status = 'open'", (ticker,)
        )
        for trade in trades:
            wins = (trade["side"] == "YES" and result == 1) or (
                trade["side"] == "NO" and result == 0
            )
            payout = float(trade["contracts"]) if wins else 0.0
            pnl = payout - float(trade["entry_cost"]) - float(trade["fees"])
            self.db.execute(
                """
                UPDATE paper_trades SET status='settled', settled_at=?, outcome=?,
                    payout=?, realized_pnl=? WHERE id=?
                """,
                (settled_at, int(wins), payout, pnl, trade["id"]),
            )
        return len(trades)
