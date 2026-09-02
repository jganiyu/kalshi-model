from __future__ import annotations

from pathlib import Path

import pytest

from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.services.decision import make_decision
from app.services.forecast import make_forecast


@pytest.mark.parametrize(
    ("probability", "signal"),
    [
        (0.60, "LIKELY_UP"),
        (0.40, "LIKELY_DOWN"),
        (0.400001, "UNCERTAIN"),
        (0.50, "UNCERTAIN"),
        (0.599999, "UNCERTAIN"),
        (0.15, "LIKELY_DOWN"),
    ],
)
def test_forecast_thresholds(probability: float, signal: str) -> None:
    forecast = make_forecast(probability)

    assert forecast.signal == signal
    assert forecast.up_probability == pytest.approx(probability)
    assert forecast.down_probability == pytest.approx(1 - probability)


def test_selected_trade_side_cannot_change_forecast() -> None:
    up_probability = 0.72
    market = {
        "yes_bid": 0.59,
        "yes_ask": 0.60,
        "no_bid": 0.40,
        "no_ask": 0.41,
        "spread": 0.01,
        "yes_ask_size": 100,
        "no_ask_size": 100,
    }
    arguments = {
        "model_probability": up_probability,
        "market": market,
        "settings": {},
        "bankroll": 1_000,
        "data_quality": {"reliable": True},
        "calibration": {"sample_size": 0, "calibration_error": None},
        "model_variant_spread": 0.01,
    }
    up_trade = make_decision(**arguments, selected_side="YES")
    down_trade = make_decision(**arguments, selected_side="NO")
    forecast = make_forecast(up_probability)

    assert up_trade.model_probability == pytest.approx(0.72)
    assert down_trade.model_probability == pytest.approx(0.28)
    assert forecast.signal == "LIKELY_UP"
    assert forecast.up_probability == pytest.approx(0.72)


def test_low_probability_positive_edge_is_not_likely_up() -> None:
    settings = {
        "slippage_cents": 0.5,
        "buy_edge": 0.05,
        "sell_edge": 0.03,
        "hold_buffer": 0.005,
        "minimum_buy_probability": 0.55,
        "fractional_kelly": 0.25,
        "max_position_pct": 0.05,
        "max_risk_per_trade_pct": 0.02,
    }
    market = {
        "yes_bid": 0.061,
        "yes_ask": 0.062,
        "no_bid": 0.938,
        "no_ask": 0.939,
        "spread": 0.001,
        "yes_ask_size": 100,
        "no_ask_size": 100,
    }
    trade = make_decision(
        model_probability=0.15,
        market=market,
        settings=settings,
        bankroll=1_000,
        data_quality={"reliable": True},
        calibration={"sample_size": 0, "calibration_error": None},
        model_variant_spread=0.01,
        selected_side="YES",
    )
    forecast = make_forecast(0.15)

    assert trade.signal == "SPECULATIVE"
    assert trade.edge and trade.edge > 0
    assert forecast.signal == "LIKELY_DOWN"


def test_forecast_migration_preserves_existing_signal_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "forecast-upgrade.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:5]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            "INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at) VALUES (?,?,?,?,?)",
            ("LEGACY", "finalized", "{}", now, now),
        )
        connection.execute(
            """
            INSERT INTO signal_snapshots(
                observed_at,ticker,signal,reason_code,confidence,explanation,
                model_probability,market_probability,edge,expected_value,
                suggested_fraction,suggested_dollars,suggested_contracts,model_version,
                input_json,btc_state_json,kalshi_state_json,material_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now, "LEGACY", "SPECULATIVE", "LOW_WIN_PROBABILITY", "Low",
                "legacy signal", 0.15, 0.06, 0.09, 0.08, 0, 0, 0,
                "baseline-1.1", "{}", "{}", "{}", "initial signal",
            ),
        )

    db.initialize()
    row = db.fetch_one("SELECT * FROM signal_snapshots WHERE ticker='LEGACY'")

    assert row and row["signal"] == "SPECULATIVE"
    assert row["model_probability"] == pytest.approx(0.15)
    assert row["forecast_signal"] is None
    assert row["forecast_explanation"] is None
    assert db.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 21
