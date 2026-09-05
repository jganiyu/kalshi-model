from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig
from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.engine import AnalysisEngine
from app.services.decision import Decision
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "available-cash.db")
    db.initialize()
    db.update_settings({"risk_controls_enabled": False})
    return db


def add_market(db: Database, ticker: str) -> None:
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
        "model_probability": 0.75,
        "market_probability": 0.50,
    }
    values.update(updates)
    return values


def assert_available_after(
    row: dict[str, object] | None, expected: float
) -> None:
    assert row is not None
    assert row["available_cash_after"] == pytest.approx(expected)


def test_migration_preserves_legacy_history_without_inventing_balance(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "legacy.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:7]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            """
            INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at)
            VALUES (?,?,?,?,?)
            """,
            ("LEGACY", "finalized", "{}", now, now),
        )
        order_id = connection.execute(
            """
            INSERT INTO paper_orders(
                ticker,side,action,order_type,status,created_at,requested_contracts,
                filled_price,filled_contracts,fees,filled_at,source,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "LEGACY", "YES", "BUY", "MARKET", "filled", now, 10,
                0.40, 10, 0.10, now, "manual", "MANUAL",
            ),
        ).lastrowid
        trade_id = connection.execute(
            """
            INSERT INTO paper_trades(
                ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                model_probability,market_probability,edge,expected_value,
                confidence,model_version,status,source,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "LEGACY", "YES", now, 0.40, 10, 4.0, 0.10, 0.60, 0.40,
                0.20, 0.19, "Moderate", "test", "settled", "manual", "MANUAL",
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO paper_entries(
                trade_id,order_id,ticker,side,opened_at,entry_price,
                initial_contracts,remaining_contracts,entry_cost,entry_fees,
                source,status,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade_id, order_id, "LEGACY", "YES", now, 0.40,
                10, 0, 4.0, 0.10, "manual", "settled", "MANUAL",
            ),
        )

    db.initialize()

    assert db.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == MIGRATIONS[-1][0]
    assert db.fetch_one("SELECT available_cash_after FROM paper_orders")["available_cash_after"] is None
    assert db.fetch_one("SELECT available_cash_after FROM paper_entries")["available_cash_after"] is None
    assert db.fetch_one("SELECT available_cash_after FROM paper_trades")["available_cash_after"] is None
    portfolio = PaperTradingService(db).portfolio()
    assert portfolio["trades"][0]["available_cash_after"] is None


def test_entry_and_close_record_available_cash_and_expose_it_to_apis(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "MANUAL")
    service = PaperTradingService(db)

    buy = service.place_order(
        ticker="MANUAL", side="YES", action="BUY", order_type="MARKET",
        market=market(), dollars=10,
    )
    after_entry = service.portfolio()["available_cash"]
    trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='MANUAL'")
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='MANUAL'")
    assert_available_after(buy, after_entry)
    assert_available_after(trade, after_entry)
    assert_available_after(entry, after_entry)

    paper_api = service.portfolio()
    assert paper_api["trades"][0]["available_cash_after"] == pytest.approx(after_entry)
    dashboard_api = AnalysisEngine(
        AppConfig(database_path=db.path), db
    )._portfolio_summary()
    assert dashboard_api["recent_paper_trades"][0]["available_cash_after"] == pytest.approx(
        after_entry
    )

    sell = service.place_order(
        ticker="MANUAL", side="YES", action="SELL", order_type="LIMIT",
        market=market(), contracts=int(buy["filled_contracts"]), limit_price=0.30,
    )
    after_close = service.portfolio()["available_cash"]
    trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='MANUAL'")
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='MANUAL'")
    assert trade and trade["status"] == "closed"
    assert entry and entry["status"] == "closed"
    assert_available_after(sell, after_close)
    assert_available_after(trade, after_close)
    assert_available_after(entry, after_close)


def test_stop_loss_and_settlement_record_available_cash(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "STOP")
    service = PaperTradingService(db)
    service.place_order(
        ticker="STOP", side="YES", action="BUY", order_type="MARKET",
        market=market(), dollars=10, stop_loss_price=0.30,
    )
    assert service.process_stop_losses("STOP", market(yes_bid=0.24)) == 1
    after_stop = service.portfolio()["available_cash"]
    assert_available_after(
        db.fetch_one("SELECT * FROM paper_orders WHERE source='stop_loss'"), after_stop
    )
    assert_available_after(
        db.fetch_one("SELECT * FROM paper_trades WHERE ticker='STOP'"), after_stop
    )
    assert_available_after(
        db.fetch_one("SELECT * FROM paper_entries WHERE ticker='STOP'"), after_stop
    )

    add_market(db, "SETTLE")
    service.place_order(
        ticker="SETTLE", side="YES", action="BUY", order_type="MARKET",
        market=market(), dollars=10,
    )
    settled_at = iso_now()
    assert service.settle("SETTLE", 1, settled_at) == 1
    db.execute(
        """
        INSERT INTO settlements(
            ticker,settled_at,result,settlement_value,raw_json,processed_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "SETTLE",
            settled_at,
            1,
            1.0,
            '{"expiration_value":"123.45"}',
            settled_at,
        ),
    )
    after_settlement = service.portfolio()["available_cash"]
    settled_trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='SETTLE'")
    settled_entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SETTLE'")
    assert settled_trade and settled_trade["status"] == "settled"
    assert settled_entry and settled_entry["status"] == "settled"
    assert service.portfolio()["trades"][0]["settlement_margin"] == pytest.approx(23.45)
    dashboard = AnalysisEngine(AppConfig(database_path=db.path), db)._portfolio_summary()
    assert dashboard["recent_paper_trades"][0]["settlement_margin"] == pytest.approx(23.45)
    assert_available_after(settled_trade, after_settlement)
    assert_available_after(settled_entry, after_settlement)

    db.execute(
        "UPDATE settlements SET settlement_value=?,raw_json=? WHERE ticker=?",
        (123.45, "{}", "SETTLE"),
    )
    assert service.portfolio()["trades"][0]["settlement_margin"] is None


def test_every_automatic_strategy_records_available_cash(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    add_market(db, "STANDARD")
    decision = Decision(
        "BUY", "BUY_EDGE", "Moderate", "test", 0.75, 0.40, 0.35,
        0.34, 0.40, 0.01, 0.02, 10.0, 10, "YES",
    )
    assert service.open_from_decision("STANDARD", decision, "test") is True

    for ticker, strategy in (
        ("EARLY", "EARLY_THRESHOLD"),
        ("LATE", "LATE_CONVICTION"),
        ("SWING", "SWING"),
    ):
        add_market(db, ticker)
        entered, _ = service.open_fixed_strategy(
            ticker=ticker,
            strategy=strategy,
            assessment={
                "side": "YES",
                "model_probability": 0.75,
                "market_probability": 0.40,
                "ask_size": 100,
                "buy": {
                    "executable_price": 0.40,
                    "expected_value": 0.34,
                    "net_edge": 0.34,
                },
            },
            bankroll_fraction=0.03,
            model_version="test",
            reason="test",
            target_exit_price=0.10 if strategy == "SWING" else None,
            fallback_exit_mode="Exit" if strategy == "SWING" else None,
            fallback_exit_seconds=120 if strategy == "SWING" else None,
        )
        assert entered is True

    for strategy in (
        "STANDARD_EDGE", "EARLY_THRESHOLD", "LATE_CONVICTION", "SWING"
    ):
        order = db.fetch_one(
            "SELECT * FROM paper_orders WHERE strategy=?", (strategy,)
        )
        trade = db.fetch_one(
            "SELECT * FROM paper_trades WHERE strategy=?", (strategy,)
        )
        entry = db.fetch_one(
            "SELECT * FROM paper_entries WHERE strategy=?", (strategy,)
        )
        assert order and order["available_cash_after"] is not None
        assert trade and trade["available_cash_after"] is not None
        assert entry and entry["available_cash_after"] is not None


def test_available_after_columns_are_rendered_on_both_pages() -> None:
    root = Path(__file__).resolve().parents[1]
    markup = (root / "app/templates/index.html").read_text()
    script = (root / "app/static/app.js").read_text()

    assert markup.count("<th>Available after</th>") == 2
    assert 'colspan="8">No paper trades yet' in markup
    assert 'colspan="10" class="empty-state">No paper trades yet.' in markup
    assert script.count("trade.available_cash_after") >= 2
    assert 'trade.available_cash_after == null ? "—"' in script
