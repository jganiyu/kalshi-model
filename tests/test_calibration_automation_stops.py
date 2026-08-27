from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.db import Database
from app.domain import iso_now
from app.main import clean_settings_payload
from app.services.decision import Decision
from app.services.forecast import make_forecast
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "calibration.db")
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
        (ticker, ticker, "open", "test", 100, now, now, now, None, "", "", "{}", now, now),
    )


def market(**changes: float) -> dict[str, float]:
    values = {
        "yes_bid": 0.38,
        "yes_ask": 0.40,
        "no_bid": 0.59,
        "no_ask": 0.61,
        "model_probability": 0.75,
        "market_probability": 0.39,
    }
    values.update(changes)
    return values


def decision(signal: str = "BUY", side: str = "YES", confidence: str = "High") -> Decision:
    return Decision(
        signal,
        "BUY_EDGE" if signal == "BUY" else "NO_EDGE",
        confidence,
        "fixture",
        0.80,
        0.40,
        0.20,
        0.18,
        0.40 if signal == "BUY" else None,
        0.02 if signal == "BUY" else None,
        0.02,
        4.0,
        10,
        side,
    )


def test_selected_side_and_configuration_snapshots_persist(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({"selected_side": "NO", "buy_edge": 0.08})
    first = db.configuration_snapshots()[0]
    db.update_settings({"sell_edge": 0.07})

    reopened = Database(db.path)
    reopened.initialize()
    assert reopened.settings()["selected_side"] == "NO"
    assert reopened.settings()["buy_edge"] == pytest.approx(0.08)

    reopened.restore_configuration(first["id"])
    assert reopened.settings()["sell_edge"] == first["settings"]["sell_edge"]
    restored = reopened.configuration_snapshots()[0]
    assert restored["restored_from_id"] == first["id"]


def test_calibration_validation_accepts_nullable_stop_and_rejects_unsafe_values() -> None:
    assert clean_settings_payload({"default_stop_loss_cents": ""})[
        "default_stop_loss_cents"
    ] is None
    assert clean_settings_payload({"default_stop_loss_cents": 0})[
        "default_stop_loss_cents"
    ] is None
    assert clean_settings_payload({"automatic_buy_duration_pct": 0.70})[
        "automatic_buy_duration_pct"
    ] == pytest.approx(0.70)
    assert clean_settings_payload({"minimum_buy_probability": 0.55})[
        "minimum_buy_probability"
    ] == pytest.approx(0.55)
    assert clean_settings_payload({"global_profit_take_enabled": False})[
        "global_profit_take_enabled"
    ] is False
    assert clean_settings_payload({"global_profit_take_price": 0.99})[
        "global_profit_take_price"
    ] == pytest.approx(0.99)
    with pytest.raises(HTTPException):
        clean_settings_payload({"default_stop_loss_cents": 100})
    with pytest.raises(HTTPException):
        clean_settings_payload({"default_stop_loss_cents": -1})
    with pytest.raises(HTTPException):
        clean_settings_payload({"automatic_confirmation_seconds": 0})
    with pytest.raises(HTTPException):
        clean_settings_payload({"minimum_buy_probability": 0.49})
    with pytest.raises(HTTPException):
        clean_settings_payload({"training_min_samples": 12.5})
    with pytest.raises(HTTPException):
        clean_settings_payload({"risk_controls_enabled": "false"})
    with pytest.raises(HTTPException):
        clean_settings_payload({"global_profit_take_enabled": "true"})
    with pytest.raises(HTTPException):
        clean_settings_payload({"global_profit_take_price": 1.0})


def test_automatic_entry_uses_elapsed_buy_duration_and_current_signal(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "AUTO")
    db.update_settings({
        "paper_trading_enabled": True,
        "automatic_confirmation_seconds": 10,
        "automatic_buy_duration_pct": 0.70,
        "automatic_min_confidence": "High",
    })
    service = PaperTradingService(db)

    assert not service.consider_automatic_entry(
        ticker="AUTO", decision=decision("BUY"), seconds_remaining=200,
        model_version="test", now=0,
    )["entered"]
    service.consider_automatic_entry(
        ticker="AUTO", decision=decision("HOLD"), seconds_remaining=197,
        model_version="test", now=3,
    )
    service.consider_automatic_entry(
        ticker="AUTO", decision=decision("BUY"), seconds_remaining=194,
        model_version="test", now=6,
    )
    result = service.consider_automatic_entry(
        ticker="AUTO", decision=decision("BUY"), seconds_remaining=190,
        model_version="test", now=10,
    )

    assert result["buy_duration_pct"] == pytest.approx(0.70)
    assert result["entered"] is True
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_entries")["count"] == 1


def test_automatic_confirmation_resets_and_requires_buy_at_completion(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "A")
    add_market(db, "B")
    db.update_settings({
        "paper_trading_enabled": True,
        "automatic_entry_window_minutes": 5,
        "automatic_min_confidence": "High",
    })
    service = PaperTradingService(db)

    service.consider_automatic_entry(
        ticker="A", decision=decision(), seconds_remaining=200, model_version="test", now=0,
    )
    service.consider_automatic_entry(
        ticker="A", decision=decision(), seconds_remaining=195, model_version="test", now=5,
    )
    switched = service.consider_automatic_entry(
        ticker="B", decision=decision(), seconds_remaining=190, model_version="test", now=10,
    )
    assert switched["progress"] == 0

    outside = service.consider_automatic_entry(
        ticker="B", decision=decision(), seconds_remaining=400, model_version="test", now=20,
    )
    assert outside["armed"] is False

    service.consider_automatic_entry(
        ticker="A", decision=decision(), seconds_remaining=200, model_version="test", now=30,
    )
    service.consider_automatic_entry(
        ticker="A", decision=decision(), seconds_remaining=193, model_version="test", now=37,
    )
    complete_hold = service.consider_automatic_entry(
        ticker="A", decision=decision("HOLD"), seconds_remaining=190,
        model_version="test", now=40,
    )
    assert complete_hold["buy_duration_pct"] == pytest.approx(1.0)
    assert complete_hold["entered"] is False

    db.update_settings({"selected_side": "NO"})
    side_reset = service.consider_automatic_entry(
        ticker="A", decision=decision(side="NO"), seconds_remaining=180,
        model_version="test", now=50,
    )
    assert side_reset["progress"] == 0


def test_speculative_signal_never_enters_automatically(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "SPECULATIVE")
    db.update_settings({"paper_trading_enabled": True, "automatic_min_confidence": "Low"})
    service = PaperTradingService(db)

    service.consider_automatic_entry(
        ticker="SPECULATIVE", decision=decision("SPECULATIVE"),
        seconds_remaining=200, model_version="test", now=0,
    )
    result = service.consider_automatic_entry(
        ticker="SPECULATIVE", decision=decision("SPECULATIVE"),
        seconds_remaining=190, model_version="test", now=10,
    )

    assert result["entered"] is False
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_entries")["count"] == 0


def test_forecast_direction_alone_cannot_trigger_automatic_entry(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "FORECAST-ONLY")
    db.update_settings({"paper_trading_enabled": True, "automatic_min_confidence": "Low"})
    service = PaperTradingService(db)
    forecast = make_forecast(0.80)

    service.consider_automatic_entry(
        ticker="FORECAST-ONLY", decision=decision("HOLD"),
        seconds_remaining=200, model_version="test", now=0,
    )
    result = service.consider_automatic_entry(
        ticker="FORECAST-ONLY", decision=decision("HOLD"),
        seconds_remaining=190, model_version="test", now=10,
    )

    assert forecast.signal == "LIKELY_UP"
    assert result["entered"] is False
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_entries")["count"] == 0


def test_automatic_position_ignores_later_model_sell(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "AUTO-HOLD")
    db.update_settings({"paper_trading_enabled": True, "automatic_min_confidence": "High"})
    service = PaperTradingService(db)
    service.consider_automatic_entry(
        ticker="AUTO-HOLD", decision=decision(), seconds_remaining=200,
        model_version="test", now=0,
    )
    entered = service.consider_automatic_entry(
        ticker="AUTO-HOLD", decision=decision(), seconds_remaining=190,
        model_version="test", now=10,
    )
    assert entered["entered"] is True

    service.consider_automatic_entry(
        ticker="AUTO-HOLD", decision=decision("SELL"), seconds_remaining=180,
        model_version="test", now=20,
    )
    trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='AUTO-HOLD'")
    assert trade and trade["status"] == "open"


def test_market_stop_triggers_at_gap_bid_and_cancels_when_sold(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "STOP")
    service = PaperTradingService(db)
    buy = service.place_order(
        ticker="STOP", side="YES", action="BUY", order_type="MARKET",
        market=market(), dollars=10, stop_loss_price=0.30,
    )
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE order_id=?", (buy["id"],))
    assert entry and entry["stop_status"] == "active"

    assert service.process_stop_losses("STOP", market(yes_bid=0.24)) == 1
    stopped = db.fetch_one("SELECT * FROM paper_entries WHERE id=?", (entry["id"],))
    stop_order = db.fetch_one("SELECT * FROM paper_orders WHERE source='stop_loss'")
    assert stopped and stopped["stop_status"] == "triggered"
    assert stopped["remaining_contracts"] == 0
    assert stop_order and stop_order["filled_price"] == pytest.approx(0.24)


def test_limit_buy_attaches_stop_and_manual_sale_cancels_it(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "LIMIT-STOP")
    service = PaperTradingService(db)
    order = service.place_order(
        ticker="LIMIT-STOP", side="YES", action="BUY", order_type="LIMIT",
        market=market(), contracts=10, limit_price=0.35, stop_loss_price=0.25,
    )
    assert order["status"] == "open"
    assert db.fetch_one("SELECT id FROM paper_entries") is None
    service.process_open_orders("LIMIT-STOP", market(yes_ask=0.34))
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE order_id=?", (order["id"],))
    assert entry and entry["stop_loss_price"] == pytest.approx(0.25)

    service.place_order(
        ticker="LIMIT-STOP", side="YES", action="SELL", order_type="LIMIT",
        market=market(), contracts=10, limit_price=0.20,
    )
    closed = db.fetch_one("SELECT * FROM paper_entries WHERE id=?", (entry["id"],))
    assert closed and closed["status"] == "closed"
    assert closed["stop_status"] == "canceled"


def test_settlement_cancels_active_stop_and_global_changes_do_not_rewrite_it(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "SETTLE-STOP")
    service = PaperTradingService(db)
    db.update_settings({"default_stop_loss_cents": 30})
    assert service.open_from_decision("SETTLE-STOP", decision(), "test")
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SETTLE-STOP'")
    assert entry and entry["stop_loss_price"] == pytest.approx(0.30)

    db.update_settings({"default_stop_loss_cents": None})
    unchanged = db.fetch_one("SELECT * FROM paper_entries WHERE id=?", (entry["id"],))
    assert unchanged and unchanged["stop_loss_price"] == pytest.approx(0.30)
    service.settle("SETTLE-STOP", 1, iso_now())
    settled = db.fetch_one("SELECT * FROM paper_entries WHERE id=?", (entry["id"],))
    assert settled and settled["status"] == "settled"
    assert settled["stop_status"] == "settled"


def test_zero_disables_manual_and_automatic_stop_losses(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "MANUAL-OFF")
    add_market(db, "AUTO-OFF")
    service = PaperTradingService(db)

    manual = service.place_order(
        ticker="MANUAL-OFF", side="YES", action="BUY", order_type="MARKET",
        market=market(), dollars=10, stop_loss_price=0,
    )
    assert manual["stop_loss_price"] is None
    manual_entry = db.fetch_one(
        "SELECT * FROM paper_entries WHERE ticker='MANUAL-OFF'"
    )
    assert manual_entry and manual_entry["stop_loss_price"] is None

    db.update_settings({"default_stop_loss_cents": 0})
    assert service.open_from_decision("AUTO-OFF", decision(), "test")
    automatic_entry = db.fetch_one(
        "SELECT * FROM paper_entries WHERE ticker='AUTO-OFF'"
    )
    assert automatic_entry and automatic_entry["stop_loss_price"] is None


def test_sell_execution_is_blocked_without_holdings(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "NO-HOLDING")
    with pytest.raises(ValueError, match="available to sell"):
        PaperTradingService(db).place_order(
            ticker="NO-HOLDING", side="YES", action="SELL", order_type="MARKET",
            market=market(), dollars=10,
        )
