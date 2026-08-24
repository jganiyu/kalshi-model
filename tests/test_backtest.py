from __future__ import annotations

import json
from pathlib import Path

from app.db import Database
from app.domain import iso_now
from app.services.backtest import BacktestService


def add_settled_signal(
    db: Database,
    *,
    ticker: str,
    probability: float,
    market_probability: float,
    result: int,
) -> None:
    now = iso_now()
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, "finalized", "test", 100.0, now, now, now,
            "yes" if result else "no", "", "", "{}", now, now,
        ),
    )
    db.execute(
        """
        INSERT INTO settlements(
            ticker,settled_at,result,settlement_value,raw_json,processed_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (ticker, now, result, None, "{}", now),
    )
    db.execute(
        """
        INSERT INTO signal_snapshots(
            observed_at,ticker,signal,reason_code,confidence,explanation,
            model_probability,market_probability,edge,expected_value,
            suggested_fraction,suggested_dollars,suggested_contracts,model_version,
            input_json,btc_state_json,kalshi_state_json,material_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now, ticker, "BUY", "BUY_EDGE", "Low", "fixture",
            probability, market_probability, probability - market_probability, None,
            0, 0, 0, "test", json.dumps({"features": {}}), "{}", "{}", "fixture",
        ),
    )


def test_backtest_excludes_low_probability_value_bets(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    add_settled_signal(
        db,
        ticker="LONG-SHOT",
        probability=0.15,
        market_probability=0.05,
        result=1,
    )
    add_settled_signal(
        db,
        ticker="LIKELY-UP",
        probability=0.70,
        market_probability=0.55,
        result=1,
    )

    result = BacktestService(db).run(0.05)

    assert result["minimum_buy_probability"] == 0.55
    assert result["trades"] == 1
    assert result["trade_log"][0]["ticker"] == "LIKELY-UP"
