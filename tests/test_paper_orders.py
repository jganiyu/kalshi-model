from __future__ import annotations

from pathlib import Path

import pytest

from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.services.decision import Decision
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "paper-orders.db")
    db.initialize()
    db.update_settings({"risk_controls_enabled": False})
    return db


def add_market(db: Database, ticker: str = "TEST-MARKET") -> None:
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
            ticker, ticker, "open", "test", 100.0, now, now, now, None,
            "", "", "{}", now, now,
        ),
    )


def market(**updates: float) -> dict[str, float]:
    values = {
        "yes_bid": 0.38,
        "yes_ask": 0.40,
        "no_bid": 0.59,
        "no_ask": 0.61,
        "model_probability": 0.55,
    }
    values.update(updates)
    return values


def test_order_migration_preserves_existing_paper_trades(tmp_path: Path) -> None:
    db = Database(tmp_path / "upgrade.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:2]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            """
            INSERT INTO markets(
                ticker,status,raw_json,first_seen_at,updated_at
            ) VALUES (?,?,?,?,?)
            """,
            ("LEGACY", "open", "{}", now, now),
        )
        connection.execute(
            """
            INSERT INTO paper_trades(
                ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                model_probability,market_probability,edge,expected_value,
                confidence,model_version,status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "LEGACY", "YES", now, 0.45, 5, 2.25, 0.05, 0.60, 0.45,
                0.15, 0.10, "Low", "baseline-1.0", "open",
            ),
        )

    db.initialize()
    preserved = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='LEGACY'")
    assert preserved and preserved["contracts"] == 5
    assert preserved["source"] == "automatic"
    assert db.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 14


def test_market_orders_use_best_prices_and_enforce_position(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = PaperTradingService(db)

    buy = service.place_order(
        ticker="TEST-MARKET",
        side="YES",
        action="BUY",
        order_type="MARKET",
        market=market(),
        dollars=10,
    )
    assert buy["status"] == "filled"
    assert buy["filled_price"] == 0.40
    assert buy["filled_contracts"] == 23
    position = db.fetch_one(
        "SELECT * FROM paper_trades WHERE ticker='TEST-MARKET' AND status='open'"
    )
    assert position and position["source"] == "manual"
    assert position["contracts"] == 23

    sell = service.place_order(
        ticker="TEST-MARKET",
        side="YES",
        action="SELL",
        order_type="MARKET",
        market=market(),
        dollars=4,
    )
    assert sell["status"] == "filled"
    assert sell["filled_price"] == 0.38
    assert sell["filled_contracts"] == 10
    assert service.available_contracts("TEST-MARKET", "YES") == 13
    assert service.portfolio()["realized_pnl"] == pytest.approx(sell["realized_pnl"])

    with pytest.raises(ValueError, match="available to sell"):
        service.place_order(
            ticker="TEST-MARKET",
            side="YES",
            action="SELL",
            order_type="MARKET",
            market=market(),
            dollars=100,
        )


def test_limit_order_reserves_fills_and_cancels(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = PaperTradingService(db)
    starting_cash = service.portfolio()["available_cash"]

    resting = service.place_order(
        ticker="TEST-MARKET",
        side="YES",
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=10,
        limit_price=0.35,
    )
    assert resting["status"] == "open"
    assert service.portfolio()["available_cash"] < starting_cash
    assert service.process_open_orders("TEST-MARKET", market(yes_ask=0.34)) == 1
    filled = db.fetch_one("SELECT * FROM paper_orders WHERE id=?", (resting["id"],))
    assert filled and filled["status"] == "filled"
    assert filled["filled_price"] == 0.34

    canceled = service.place_order(
        ticker="TEST-MARKET",
        side="NO",
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=5,
        limit_price=0.40,
    )
    reserved_cash = service.portfolio()["available_cash"]
    assert canceled["status"] == "open"
    assert service.cancel_order(canceled["id"]) is True
    assert service.cancel_order(canceled["id"]) is False
    assert service.portfolio()["available_cash"] > reserved_cash


def test_limit_validation_and_bankroll_controls(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = PaperTradingService(db)

    with pytest.raises(ValueError, match="whole number"):
        service.place_order(
            ticker="TEST-MARKET",
            side="YES",
            action="BUY",
            order_type="LIMIT",
            market=market(),
            contracts=1.5,
            limit_price=0.30,
        )
    with pytest.raises(ValueError, match="between 1 and 99 cents"):
        service.place_order(
            ticker="TEST-MARKET",
            side="YES",
            action="BUY",
            order_type="LIMIT",
            market=market(),
            contracts=1,
            limit_price=1.0,
        )
    with pytest.raises(ValueError, match="remaining paper bankroll"):
        service.place_order(
            ticker="TEST-MARKET",
            side="YES",
            action="BUY",
            order_type="MARKET",
            market=market(),
            dollars=2_000,
        )

    db.update_settings({"risk_controls_enabled": True, "max_risk_per_trade_pct": 0.03})
    with pytest.raises(ValueError, match="maximum risk per trade"):
        service.place_order(
            ticker="TEST-MARKET",
            side="YES",
            action="BUY",
            order_type="MARKET",
            market=market(),
            dollars=50,
        )


def test_drawdown_blocks_execution_without_replacing_model_signal(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    db.update_settings({
        "paper_trading_enabled": True,
        "risk_controls_enabled": True,
        "max_session_drawdown_pct": 0.0,
    })
    service = PaperTradingService(db)
    decision = Decision(
        "TRADE YES", "POSITIVE_EV", "Moderate", "model still favors Up",
        0.72, 0.60, 0.12, 0.10, 0.40, 0.01, 0.01, 10.0, 10, "YES",
    )

    portfolio = service.portfolio()
    assert decision.signal == "TRADE YES"
    assert portfolio["automatic_trade_allowed"] is False
    assert portfolio["automatic_trade_block_reason"] == "Session drawdown limit reached."
    assert service.open_from_decision("TEST-MARKET", decision) is False
    assert db.fetch_one("SELECT id FROM paper_trades") is None


def test_reset_round_restores_starting_paper_state(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = PaperTradingService(db)
    service.place_order(
        ticker="TEST-MARKET",
        side="YES",
        action="BUY",
        order_type="MARKET",
        market=market(),
        dollars=10,
    )
    service.place_order(
        ticker="TEST-MARKET",
        side="NO",
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=5,
        limit_price=0.40,
    )

    reset = service.reset_round()
    portfolio = service.portfolio()

    assert reset == {"cleared_trades": 1, "cleared_orders": 2}
    assert portfolio["current_bankroll"] == portfolio["starting_bankroll"]
    assert portfolio["available_cash"] == portfolio["starting_bankroll"]
    assert portfolio["realized_pnl"] == 0
    assert portfolio["session_drawdown_pct"] == 0
    assert portfolio["trades"] == []
    assert portfolio["orders"] == []
