from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.domain import iso_now
from app.services.decision import Decision
from app.services.paper import PaperTradingService
from app.services.training import ModelManager


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.initialize()
    return db


def add_market(db: Database, ticker: str) -> None:
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (ticker, ticker, "finalized", "test", 100.0, iso_now(), iso_now(), iso_now(), "", "", "", "{}", iso_now(), iso_now()),
    )


def test_database_migrations_and_settings_persist(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({"starting_bankroll": 2_500.0, "unknown": "ignored"})
    reopened = Database(tmp_path / "test.db")
    reopened.initialize()
    assert reopened.settings()["starting_bankroll"] == 2_500.0
    assert "unknown" not in reopened.settings()
    assert reopened.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 4
    assert ModelManager(reopened).active()["version"] == "baseline-1.1"


def test_new_database_starts_with_clean_manual_paper_account(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    settings = db.settings()
    portfolio = PaperTradingService(db).portfolio()

    assert settings["starting_bankroll"] == 1_000.0
    assert settings["paper_trading_enabled"] is False
    assert portfolio["starting_bankroll"] == 1_000.0
    assert portfolio["current_bankroll"] == 1_000.0
    assert portfolio["automatic_trading_enabled"] is False
    assert portfolio["trades"] == []
    assert portfolio["orders"] == []


def test_paper_trade_settlement_uses_actual_binary_outcome(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({"paper_trading_enabled": True})
    add_market(db, "TEST-YES")
    service = PaperTradingService(db)
    decision = Decision(
        "TRADE YES", "POSITIVE_EV", "Moderate", "test", 0.75, 0.60, 0.15,
        0.12, 0.61, 0.01, 0.02, 10.0, 16, "YES",
    )
    assert service.open_from_decision("TEST-YES", decision)
    assert not service.open_from_decision("TEST-YES", decision)
    opposite = Decision(
        "TRADE NO", "POSITIVE_EV", "Moderate", "test", 0.25, 0.40, 0.15,
        0.12, 0.41, 0.01, 0.02, 10.0, 16, "NO",
    )
    assert not service.open_from_decision("TEST-YES", opposite)
    open_portfolio = service.portfolio()
    assert open_portfolio["available_cash"] < open_portfolio["current_bankroll"]
    assert open_portfolio["current_bankroll"] < open_portfolio["starting_bankroll"]
    assert service.settle("TEST-YES", 1, iso_now()) == 1
    trade = db.fetch_one("SELECT * FROM paper_trades WHERE ticker='TEST-YES'")
    assert trade["status"] == "settled"
    assert trade["payout"] == 16
    assert trade["realized_pnl"] > 0


def test_model_promotion_requires_forward_validation_and_minimum_sample(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    manager = ModelManager(db)

    def add_observation(index: int) -> None:
        ticker = f"TEST-{index:02d}"
        add_market(db, ticker)
        outcome = index % 2
        z_distance = 3.0 if outcome else -3.0
        wrong_probability = 0.15 if outcome else 0.85
        features = {
            "z_distance": z_distance,
            "time_fraction": 0.33,
            "volatility_5m": 0.5,
            "volatility_15m": 0.5,
            "momentum_1m": z_distance / 100,
            "momentum_5m": z_distance / 50,
            "dispersion_pct": 0.02,
            "orderbook_imbalance": z_distance / 4,
            "market_probability": 0.5,
        }
        db.execute(
            "INSERT INTO settlements(ticker,settled_at,result,settlement_value,raw_json,processed_at) VALUES (?,?,?,?,?,?)",
            (ticker, iso_now(), outcome, None, "{}", iso_now()),
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
                f"2026-01-{index // 17 + 1:02d}T00:{index % 17:02d}:00+00:00", ticker,
                "NO TRADE", "TEST", "Low", "fixture", wrong_probability, 0.5,
                wrong_probability - 0.5, None, 0, 0, 0, "baseline-1.0",
                json.dumps({"features": features}), "{}", "{}", "fixture",
            ),
        )

    for index in range(119):
        add_observation(index)

    early_report = manager.evaluate_and_retrain("test-before-minimum")
    assert early_report["training_distinct_utc_days"] == 7
    assert early_report["promotion_data_eligible"] is False
    assert "requires at least 120 contracts" in early_report["tldr"]
    assert early_report["promoted"] is False

    add_observation(119)
    report = manager.evaluate_and_retrain("test")
    assert report["validation"].startswith("Rolling-window expanding")
    assert report["training_sample_size"] == 120
    assert report["training_distinct_utc_days"] == 8
    assert report["promotion_data_eligible"] is True
    assert report["candidate"]["sample_size"] < 120
    assert report["promoted"] is True
    assert manager.active()["model_type"] == "regularized-logistic"


def test_benchmark_calibration_learns_from_final_minute_proxy_ticks(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    close = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
    official = 100.01
    for index in range(20):
        ticker = f"BASIS-{index:02d}"
        raw = json.dumps({"expiration_value": f"{official:.2f}"})
        db.execute(
            """
            INSERT INTO markets(
                ticker,event_ticker,status,title,strike,open_time,close_time,
                expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
                first_seen_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker, ticker, "finalized", "test", 100.0,
                (close - timedelta(minutes=15)).isoformat(), close.isoformat(),
                close.isoformat(), "yes", "", "", raw, iso_now(), iso_now(),
            ),
        )
        db.execute(
            """
            INSERT INTO settlements(
                ticker,settled_at,result,settlement_value,raw_json,processed_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (ticker, close.isoformat(), 1, 1.0, raw, iso_now()),
        )
    db.executemany(
        """
        INSERT INTO btc_ticks(
            observed_at,composite_price,dispersion_pct,exchange_count,source_json
        ) VALUES (?,?,?,?,?)
        """,
        [
            (
                (close - timedelta(seconds=second)).isoformat(),
                100.0,
                0.01,
                3,
                "{}",
            )
            for second in range(1, 31)
        ],
    )

    summary = ModelManager(db).benchmark_calibration()

    assert summary["sample_size"] == 20
    assert summary["calibrated"] is True
    assert summary["bias_pct"] == pytest.approx(math.log(official / 100.0))
    assert summary["uncertainty_pct"] == pytest.approx(0.00015)
    assert summary["average_window_coverage"] == pytest.approx(0.5)
