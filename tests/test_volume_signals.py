from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.main as main

from app.db import Database
from app.domain import iso_now
from app.services.market_data import CompositeQuote, ExchangeQuote, ExchangeTrade
from app.services.training import ModelManager, feature_vector
from app.services.volume_signals import (
    VolumeSignalService,
    normalized_flow,
    relative_volume,
    volume_confirmation,
    weighted_vwap,
)


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "volume.db")
    db.initialize()
    return db


def test_volume_math_is_directional_normalized_and_missing_safe() -> None:
    assert normalized_flow(8, 2) == pytest.approx(0.6)
    assert normalized_flow(0, 0) is None
    assert relative_volume(20, [10, 10, 12]) == pytest.approx(2.0)
    assert relative_volume(20, [10, 0]) is None
    assert volume_confirmation(0.01, 2.0) > 0
    assert volume_confirmation(-0.01, 2.0) < 0
    assert feature_vector({"z_distance": None})[0] == 0
    vwap, deviation = weighted_vwap(
        [{"price": 100, "size": 1}, {"price": 102, "size": 3}]
    )
    assert vwap == pytest.approx(101.5)
    assert deviation and deviation > 0


def test_rolling_totals_are_audit_only_not_interval_volume(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = VolumeSignalService(db)
    observed = datetime(2026, 8, 30, 1, tzinfo=UTC).isoformat()
    service.audit_cumulative(
        CompositeQuote(
            100.0, 0.01,
            [
                ExchangeQuote("Coinbase", 100, 99, 101, 10_000, 1),
                ExchangeQuote("Kraken", 100, 99, 101, 9_000, 1),
                ExchangeQuote("Bitstamp", 100, 99, 101, None, 1),
            ],
            {},
        ),
        observed,
    )
    rows = db.fetch_all("SELECT * FROM btc_volume_observations ORDER BY exchange")
    assert len(rows) == 3
    assert all(row["source_window"] == "rolling_24h" for row in rows)
    assert next(row for row in rows if row["exchange"] == "Bitstamp")["valid"] == 0
    assert db.fetch_one("SELECT COUNT(*) count FROM btc_trade_ticks")["count"] == 0


def seed_trade_history(service: VolumeSignalService, now: datetime) -> None:
    for minutes_ago in range(64, -1, -1):
        for exchange in ("Coinbase", "Kraken"):
            timestamp = now - timedelta(minutes=minutes_ago, seconds=5)
            service.add_trade(
                ExchangeTrade(
                    exchange=exchange,
                    trade_id=f"{exchange}-{minutes_ago}",
                    observed_at=timestamp.isoformat(),
                    price=100 + (64 - minutes_ago) * 0.02,
                    size=2 if minutes_ago <= 1 else 1,
                    taker_side="BUY" if minutes_ago % 3 else "SELL",
                    raw={},
                )
            )
    service.flush()


def test_actual_trade_features_use_past_data_and_kalshi_flow(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = VolumeSignalService(db)
    now = datetime(2026, 8, 30, 2, tzinfo=UTC)
    seed_trade_history(service, now)
    service.record_kalshi_trades(
        "TEST",
        [
            {
                "trade_id": "k1", "created_time": (now - timedelta(seconds=10)).isoformat(),
                "count_fp": "20", "yes_price_dollars": "0.60",
                "taker_outcome_side": "yes", "is_block_trade": False,
            },
            {
                "trade_id": "k2", "created_time": (now - timedelta(seconds=20)).isoformat(),
                "count_fp": "5", "yes_price_dollars": "0.59",
                "taker_outcome_side": "no", "is_block_trade": False,
            },
        ],
    )
    snapshot = service.snapshot(
        observed_at=now.isoformat(), ticker="TEST", btc_price=101.5,
        momentum_1m=.001, momentum_5m=.003, open_interest=1000,
        seconds_remaining=300, threshold_margin=25,
        annualized_volatility=.5, settlement_window_fraction=0,
    )
    metrics = snapshot["metrics"]
    assert snapshot["status"] == "ACTIVE"
    assert snapshot["data_completeness"] == 1
    assert metrics["btc_rvol_1m"] is not None
    assert metrics["btc_rvol_5m"] is not None
    assert metrics["btc_vwap_distance_1m"] > 0
    assert metrics["btc_volume_confirmation_5m"] > 0
    assert metrics["kalshi_flow_imbalance_1m"] == pytest.approx(0.6)
    assert metrics["kalshi_turnover_5m"] == pytest.approx(.025)

    service.add_trade(
        ExchangeTrade(
            "Coinbase", "future", (now + timedelta(minutes=1)).isoformat(),
            50, 100_000, "SELL", {},
        )
    )
    second = service.snapshot(
        observed_at=now.isoformat(), ticker="TEST", btc_price=101.5,
        momentum_1m=.001, momentum_5m=.003, open_interest=1000,
        seconds_remaining=300, threshold_margin=25,
        annualized_volatility=.5, settlement_window_fraction=0, persist=False,
    )
    assert second["metrics"]["btc_flow_imbalance_1m"] == pytest.approx(
        metrics["btc_flow_imbalance_1m"]
    )


def test_missing_or_stale_source_does_not_become_zero_volume(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = VolumeSignalService(db)
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)
    service.add_trade(
        ExchangeTrade(
            "Coinbase", "old", (now - timedelta(minutes=2)).isoformat(),
            100, 1, "BUY", {},
        )
    )
    snapshot = service.snapshot(
        observed_at=now.isoformat(), ticker=None, btc_price=100,
        momentum_1m=0, momentum_5m=0, open_interest=None,
        seconds_remaining=900, threshold_margin=0,
        annualized_volatility=.5, settlement_window_fraction=0,
    )
    assert snapshot["status"] != "ACTIVE"
    assert snapshot["data_completeness"] == 0
    assert snapshot["metrics"]["btc_rvol_1m"] is None
    assert snapshot["features"]["btc_volume_missing"] == 1


def test_active_model_uses_its_stored_feature_schema(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.execute("UPDATE model_versions SET status='retired' WHERE status='active'")
    parameters = {
        "feature_names": ["z_distance"],
        "mean": [0.0], "scale": [1.0], "intercept": 0.0,
        "coefficients": [2.0], "regularization": .2,
    }
    db.execute(
        """
        INSERT INTO model_versions(
            version,created_at,model_type,status,training_samples,
            validation_json,parameters_json,promoted_at,parent_version
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        ("stored-schema", iso_now(), "regularized-logistic", "active", 10,
         "{}", json.dumps(parameters), iso_now(), "baseline-1.1"),
    )
    probability, version = ModelManager(db).predict({"z_distance": 1}, .5)
    assert version == "stored-schema"
    assert probability > .8


def test_volume_candidate_remains_shadow_and_reports_ablations(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    manager = ModelManager(db)
    for index in range(12):
        ticker = f"VOL-{index}"
        outcome = index % 2
        observed = datetime(2026, 8, 1 + index, tzinfo=UTC).isoformat()
        db.execute(
            "INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at) VALUES (?,?,?,?,?)",
            (ticker, "finalized", "{}", observed, observed),
        )
        db.execute(
            "INSERT INTO settlements(ticker,settled_at,result,raw_json,processed_at) VALUES (?,?,?,?,?)",
            (ticker, observed, outcome, "{}", observed),
        )
        features = {
            "z_distance": 1 if outcome else -1,
            "time_fraction": .5, "volatility_5m": .5, "volatility_15m": .5,
            "momentum_1m": .001 if outcome else -.001,
            "momentum_5m": .002 if outcome else -.002,
            "market_probability": .5, "btc_volume_missing": 0,
            "kalshi_volume_missing": 0,
            "btc_rvol_1m": 2, "btc_rvol_5m": 1.5,
            "btc_flow_imbalance_1m": .5 if outcome else -.5,
            "btc_flow_imbalance_5m": .4 if outcome else -.4,
            "btc_volume_confirmation_1m": .01 if outcome else -.01,
            "btc_volume_confirmation_5m": .02 if outcome else -.02,
            "btc_vwap_distance_1m": .001 if outcome else -.001,
            "btc_vwap_distance_5m": .002 if outcome else -.002,
            "threshold_margin_dollars": 30 if outcome else -30,
        }
        db.execute(
            """
            INSERT INTO signal_snapshots(
                observed_at,ticker,signal,reason_code,confidence,explanation,
                model_probability,model_version,input_json,btc_state_json,
                kalshi_state_json,material_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (observed, ticker, "HOLD", "TEST", "Low", "test", .5,
             "baseline-1.1", json.dumps({"features": features}), "{}", "{}", "test"),
        )
    report = manager.evaluate_and_retrain("volume-test")
    assert report["volume_shadow"]["status"] == "validated-shadow"
    assert report["volume_shadow"]["promoted"] is False
    assert "btc_relative_volume" in report["volume_shadow"]["ablations"]
    assert db.fetch_one("SELECT COUNT(*) count FROM model_versions WHERE status='shadow'")["count"] == 1
    assert manager.active()["version"] == "baseline-1.1"


def test_calibration_api_exposes_structured_volume_diagnostics(
    tmp_path: Path, monkeypatch,
) -> None:
    db = make_db(tmp_path)
    service = VolumeSignalService(db)

    class Trading:
        selected_mode = "PAPER"

        @staticmethod
        def broker(_mode):
            return type("Broker", (), {"portfolio": lambda self: {"strategy_results": {}}})()

    fake_engine = type(
        "Engine", (),
        {
            "trading": Trading(),
            "calibration_summary": lambda self: {"sample_size": 0},
            "margin_volatility": type("MVI", (), {"report": lambda self, mode: {}})(),
            "volume_signals": service,
            "models": ModelManager(db),
        },
    )()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "engine", fake_engine)
    payload = asyncio.run(main.calibration())
    report = payload["volume_signal_report"]
    assert "current" in report
    assert "audit" in report
    assert "actual_trade_sources" in report["audit"]
