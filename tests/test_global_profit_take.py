from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.domain import iso_now, kalshi_fee
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path, **settings: object) -> Database:
    db = Database(tmp_path / "profit-take.db")
    db.initialize()
    db.update_settings({"risk_controls_enabled": False, **settings})
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
            "PROFIT", "PROFIT", "active", "test", 100.0, now, now, now,
            None, "", "", "{}", now, now,
        ),
    )
    return db


def market(**updates: float) -> dict[str, float]:
    values = {
        "yes_bid": 0.39,
        "yes_ask": 0.40,
        "no_bid": 0.59,
        "no_ask": 0.60,
        "yes_bid_size": 1_000,
        "no_bid_size": 1_000,
        "model_probability": 0.75,
        "market_probability": 0.40,
    }
    values.update(updates)
    return values


def open_position(service: PaperTradingService, contracts: int = 10) -> None:
    order = service.place_order(
        ticker="PROFIT",
        side="YES",
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=contracts,
        limit_price=0.40,
    )
    assert order["status"] == "filled"


def test_exact_profit_take_bid_closes_with_costs_and_available_cash(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path, paper_trading_enabled=False)
    service = PaperTradingService(db)
    open_position(service)

    assert service.process_open_orders(
        "PROFIT", market(yes_bid=0.99, yes_ask=0.995)
    ) == 1

    order = db.fetch_one("SELECT * FROM paper_orders WHERE source='profit_take'")
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='PROFIT'")
    trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='PROFIT'")
    assert order and order["filled_price"] == pytest.approx(0.985)
    assert order["fees"] == pytest.approx(kalshi_fee(0.985, 10))
    expected_pnl = (
        0.985 * 10
        - kalshi_fee(0.985, 10)
        - 0.40 * 10
        - kalshi_fee(0.40, 10)
    )
    assert order["realized_pnl"] == pytest.approx(expected_pnl)
    assert order["available_cash_after"] == pytest.approx(
        service.portfolio()["available_cash"]
    )
    assert entry and entry["status"] == "closed"
    assert entry["exit_reason"] == "PROFIT_TAKE"
    assert trade and trade["status"] == "closed"


def test_profit_take_uses_bid_and_can_be_disabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    open_position(service)

    assert service.process_profit_takes(
        "PROFIT", market(yes_bid=0.989, yes_ask=0.999)
    ) == 0
    assert db.fetch_one("SELECT status FROM paper_trades")["status"] == "open"

    db.update_settings({"global_profit_take_enabled": False})
    assert service.process_profit_takes(
        "PROFIT", market(yes_bid=0.99, yes_ask=0.995)
    ) == 0
    assert db.fetch_one("SELECT status FROM paper_trades")["status"] == "open"


def test_profit_take_respects_resting_sells_and_partial_bid_liquidity(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path, global_profit_take_price=0.98)
    service = PaperTradingService(db)
    open_position(service)
    reserved = service.place_order(
        ticker="PROFIT",
        side="YES",
        action="SELL",
        order_type="LIMIT",
        market=market(),
        contracts=4,
        limit_price=0.99,
    )
    assert reserved["status"] == "open"

    assert service.process_profit_takes(
        "PROFIT", market(yes_bid=0.98, yes_ask=0.99, yes_bid_size=10)
    ) == 1
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='PROFIT'")
    assert entry and entry["remaining_contracts"] == 4
    assert service.available_contracts("PROFIT", "YES") == 0

    assert service.cancel_order(int(reserved["id"])) is True
    assert service.process_profit_takes(
        "PROFIT", market(yes_bid=0.98, yes_ask=0.99, yes_bid_size=2)
    ) == 1
    assert db.fetch_one("SELECT remaining_contracts FROM paper_entries")[
        "remaining_contracts"
    ] == 2
    assert service.process_profit_takes(
        "PROFIT", market(yes_bid=0.98, yes_ask=0.99, yes_bid_size=2)
    ) == 1
    assert db.fetch_one("SELECT status FROM paper_trades")["status"] == "closed"


def test_global_profit_take_precedes_swing_exit(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    open_position(service)
    db.execute(
        "UPDATE paper_entries SET strategy='SWING',target_exit_price=0.10"
    )
    db.execute("UPDATE paper_trades SET strategy='SWING'")

    assert service.process_open_orders(
        "PROFIT", market(yes_bid=0.99, yes_ask=0.995)
    ) == 1
    exits = db.fetch_all(
        "SELECT * FROM paper_orders WHERE action='SELL' AND status='filled'"
    )
    assert len(exits) == 1
    assert exits[0]["source"] == "profit_take"
    assert db.fetch_one("SELECT exit_reason FROM paper_entries")[
        "exit_reason"
    ] == "PROFIT_TAKE"
