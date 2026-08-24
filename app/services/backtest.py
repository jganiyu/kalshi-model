from __future__ import annotations

import json
from typing import Any

from app.db import Database
from app.domain import iso_now, kalshi_fee


class BacktestService:
    def __init__(self, db: Database):
        self.db = db

    def run(self, min_edge: float, starting_bankroll: float = 1000.0) -> dict[str, Any]:
        minimum_buy_probability = float(
            self.db.settings().get("minimum_buy_probability", 0.55)
        )
        rows = self.db.fetch_all(
            """
            SELECT s.ticker, s.observed_at, s.model_probability, s.market_probability,
                   z.result
            FROM signal_snapshots s
            JOIN settlements z ON z.ticker=s.ticker
            JOIN (SELECT ticker, MAX(id) max_id FROM signal_snapshots GROUP BY ticker) latest
              ON latest.max_id=s.id
            WHERE s.model_probability IS NOT NULL AND s.market_probability IS NOT NULL
            ORDER BY s.observed_at ASC
            """
        )
        bankroll = starting_bankroll
        peak = bankroll
        max_drawdown = 0.0
        trades = []
        for row in rows:
            probability = float(row["model_probability"])
            market_probability = float(row["market_probability"])
            yes_edge = probability - market_probability
            side = "YES" if yes_edge >= 0 else "NO"
            edge = abs(yes_edge)
            selected_probability = probability if side == "YES" else 1.0 - probability
            if edge < min_edge or selected_probability < minimum_buy_probability:
                continue
            price = market_probability if side == "YES" else 1 - market_probability
            price = min(0.99, price + 0.005)
            quantity = max(1, int((bankroll * 0.01) // (price + kalshi_fee(price))))
            fee = kalshi_fee(price, quantity)
            won = (side == "YES" and row["result"] == 1) or (
                side == "NO" and row["result"] == 0
            )
            pnl = (quantity if won else 0) - price * quantity - fee
            bankroll += pnl
            peak = max(peak, bankroll)
            max_drawdown = max(max_drawdown, (peak - bankroll) / peak if peak else 0.0)
            trades.append({**row, "side": side, "edge": edge, "pnl": pnl, "quantity": quantity})
        wins = sum(1 for trade in trades if trade["pnl"] > 0)
        result = {
            "method": "Stored point-in-time predictions; each market used once at its final saved signal.",
            "look_ahead_guard": "Rows are ordered by observation time and no settlement is used as an input.",
            "min_edge": min_edge,
            "minimum_buy_probability": minimum_buy_probability,
            "sample_size": len(rows),
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "ending_bankroll": bankroll,
            "return_pct": (bankroll - starting_bankroll) / starting_bankroll,
            "max_drawdown_pct": max_drawdown,
            "trade_log": trades[-100:],
        }
        self.db.execute(
            "INSERT INTO backtest_runs(created_at, parameters_json, results_json) VALUES (?, ?, ?)",
            (
                iso_now(),
                json.dumps(
                    {
                        "min_edge": min_edge,
                        "minimum_buy_probability": minimum_buy_probability,
                    }
                ),
                json.dumps(result),
            ),
        )
        return result
