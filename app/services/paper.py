from __future__ import annotations

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
        settled = [trade for trade in trades if trade["status"] == "settled"]
        open_trades = [trade for trade in trades if trade["status"] == "open"]
        realized = sum(float(trade["realized_pnl"] or 0) for trade in settled)
        open_capital = sum(float(trade["entry_cost"] + trade["fees"]) for trade in open_trades)
        available_cash = starting + realized - open_capital
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
        wins = sum(1 for trade in settled if float(trade["realized_pnl"] or 0) > 0)
        losses = sum(1 for trade in settled if float(trade["realized_pnl"] or 0) <= 0)
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
        session_peak = max(starting + realized - session_pnl, starting + realized)
        session_drawdown = max(0.0, -session_pnl / session_peak) if session_peak else 0.0
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
            "average_edge": average_edge,
            "expected_value": expected_total,
            "max_drawdown_pct": max_drawdown,
            "session_drawdown_pct": session_drawdown,
            "trades": list(reversed(trades[-100:])),
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
        entry_cost = decision.executable_price * contracts
        fees = kalshi_fee(decision.executable_price, contracts)
        self.db.execute(
            """
            INSERT INTO paper_trades(
                ticker, side, opened_at, entry_price, contracts, entry_cost, fees,
                model_probability, market_probability, edge, expected_value,
                confidence, model_version, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return True

    def settle(self, ticker: str, result: int, settled_at: str) -> int:
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
