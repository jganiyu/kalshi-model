from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import AppConfig, DEFAULT_SETTINGS
from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.engine import AnalysisEngine
from app.services.decision import Decision
from app.services.forecast import make_forecast
from app.services.margin_volatility import (
    CALCULATION_VERSION,
    MarginVolatilityService,
    cushion_metrics,
    historical_percentile_index,
    volatility_components,
)
from app.services.paper import PaperTradingService
from app.services.decision import make_trade_assessment


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "mvi.db")
    db.initialize()
    db.update_settings({"directional_momentum_gate_enabled": False})
    return db


def observations(changes: list[float], *, rollover_at: int | None = None) -> list[dict]:
    start = datetime(2026, 8, 28, 12, tzinfo=UTC)
    margin = 0.0
    result = [{"observed_at": start.isoformat(), "ticker": "A", "margin": margin}]
    for index, change in enumerate(changes, start=1):
        ticker = "B" if rollover_at is not None and index >= rollover_at else "A"
        if rollover_at is not None and index == rollover_at:
            margin = -500.0
        else:
            margin += change
        result.append(
            {
                "observed_at": (start + timedelta(seconds=index * 5)).isoformat(),
                "ticker": ticker,
                "margin": margin,
            }
        )
    return result


def test_mvi_scale_normalization_and_movement_shape() -> None:
    baseline = [float(value) for value in range(1, 100)]
    assert historical_percentile_index(50.0, baseline) == pytest.approx(5.0)
    assert 0 <= historical_percentile_index(-100.0, baseline) <= 10
    assert 0 <= historical_percentile_index(1000.0, baseline) <= 10

    calm = volatility_components(observations([0.05, -0.05] * 180))
    directional = volatility_components(observations([1.0] * 360))
    choppy = volatility_components(observations([1.0, -1.0] * 180))
    assert float(calm["raw_score"] or 0) < float(choppy["raw_score"] or 0)
    assert float(choppy["raw_score"] or 0) > float(directional["raw_score"] or 0)
    assert float(choppy["reversal_component"] or 0) > 0.9


def test_contract_rollover_does_not_create_false_spike() -> None:
    steady = volatility_components(observations([0.5, -0.5] * 180))
    rollover = volatility_components(
        observations([0.5, -0.5] * 180, rollover_at=180)
    )
    assert rollover["raw_score"] == pytest.approx(steady["raw_score"], rel=0.02)
    assert int(rollover["change_count"] or 0) == int(steady["change_count"] or 0) - 1


def test_cushion_uses_raw_volatility_and_square_root_of_time() -> None:
    expected, cushion = cushion_metrics(50.0, 2.0, 100.0)
    assert expected == pytest.approx(20.0)
    assert cushion == pytest.approx(2.5)
    expected_longer, cushion_longer = cushion_metrics(50.0, 2.0, 400.0)
    assert expected_longer == pytest.approx(40.0)
    assert cushion_longer == pytest.approx(1.25)
    assert cushion_metrics(50.0, 0.0, 100.0) == (None, None)


def test_volatility_gate_off_exact_limit_low_and_learning() -> None:
    off = MarginVolatilityService.gate(
        {"maximum_margin_volatility": 0}, None
    )
    assert off["passed"] is True and off["status"] == "OFF"
    settings = {"maximum_margin_volatility": 7.5}
    exact = MarginVolatilityService.gate(
        settings, {"mvi": 7.5, "reliable": True, "cushion_ratio": 1.4}
    )
    low = MarginVolatilityService.gate(
        settings, {"mvi": 0.1, "reliable": True}
    )
    high = MarginVolatilityService.gate(
        settings, {"mvi": 7.5001, "reliable": True}
    )
    learning = MarginVolatilityService.gate(
        settings, {"mvi": None, "reliable": False, "reliability_state": "LEARNING"}
    )
    assert exact["passed"] is True
    assert low["passed"] is True
    assert high["passed"] is False
    assert learning["passed"] is False and learning["status"] == "LEARNING"


def add_market(db: Database, ticker: str) -> None:
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, "active", "test", 100.0,
            "2026-08-28T12:00:00+00:00", "2026-08-28T12:15:00+00:00",
            "2026-08-28T12:15:00+00:00", None, "", "", "{}", iso_now(), iso_now(),
        ),
    )


def test_entry_and_signal_evidence_persist(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "MVI-EVIDENCE")
    service = PaperTradingService(db)
    service._entry_volatility = {"mvi": 6.2, "cushion_ratio": 1.4}
    decision = Decision(
        "BUY", "BUY_EDGE", "Moderate", "test", 0.75, 0.50, 0.20,
        0.18, 0.51, 0.01, 0.02, 10.0, 10, "YES",
    )
    assert service.open_from_decision("MVI-EVIDENCE", decision)
    entry = db.fetch_one(
        "SELECT margin_volatility_index,margin_cushion_ratio FROM paper_entries"
    )
    assert entry == {
        "margin_volatility_index": pytest.approx(6.2),
        "margin_cushion_ratio": pytest.approx(1.4),
    }

    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    engine._save_signal(
        "MVI-EVIDENCE", make_forecast(0.75), decision, "test", {}, {}, {},
        "test", iso_now(), 0.75, "YES",
        {"mvi": 6.2, "cushion_ratio": 1.4},
    )
    signal = db.fetch_one(
        "SELECT margin_volatility_index,margin_cushion_ratio,margin_volatility_max FROM signal_snapshots"
    )
    assert signal["margin_volatility_index"] == pytest.approx(6.2)
    assert signal["margin_cushion_ratio"] == pytest.approx(1.4)
    assert signal["margin_volatility_max"] == pytest.approx(0.0)


def test_additive_migration_preserves_history_and_settings(tmp_path: Path) -> None:
    legacy = Database(tmp_path / "legacy.db")
    with legacy.transaction() as connection:
        for version, sql in MIGRATIONS[:12]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        connection.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("starting_bankroll", "4321.0", iso_now()),
        )
    legacy.initialize()
    assert legacy.settings()["starting_bankroll"] == pytest.approx(4321.0)
    assert legacy.settings()["maximum_margin_volatility"] == pytest.approx(0.0)
    assert legacy.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 19
    assert legacy.fetch_one("SELECT COUNT(*) count FROM margin_volatility_observations")["count"] == 0
    assert "margin_volatility_index" in {
        row["name"] for row in legacy.fetch_all("PRAGMA table_info(signal_snapshots)")
    }


def test_default_gate_is_off() -> None:
    assert DEFAULT_SETTINGS["maximum_margin_volatility"] == pytest.approx(0.0)
    assert CALCULATION_VERSION == "mvi-1"


@pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE"])
def test_every_execution_mode_uses_same_gate_and_resets_confirmation(
    tmp_path: Path, mode: str
) -> None:
    db = make_db(tmp_path)
    db.update_settings(
        {
            "paper_trading_enabled": True,
            "maximum_margin_volatility": 7.5,
            "threshold_margin_gate_dollars": 0,
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "swing_enabled": False,
            "automatic_confirmation_seconds": 5,
            "automatic_min_confidence": "Moderate",
        }
    )
    market = {
        "yes_bid": 0.38, "yes_ask": 0.40, "no_bid": 0.60, "no_ask": 0.62,
        "yes_ask_size": 1000, "no_ask_size": 1000,
    }
    quality = {"reliable": True, "trade_allowed": True, "reason": "current"}
    side_assessments = {
        side: make_trade_assessment(
            up_probability=0.75,
            market=market,
            settings={"slippage_cents": 0.5},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }
    decision = Decision(
        "BUY", "BUY_EDGE", "Moderate", "fixture", 0.75, 0.39, 0.20,
        0.18, 0.405, 0.01, 0.02, 10.0, 10, "YES",
    )
    service = PaperTradingService(db)

    def run(now: float, mvi: float) -> dict:
        return service.consider_strategies(
            ticker="MVI-MODE", assessments=side_assessments,
            standard_decisions={"YES": decision, "NO": decision.__class__(
                "HOLD", "NO_EDGE", "Low", "fixture", 0.25, 0.61, -0.1,
                None, None, None, 0, 0, 0, "NO"
            )},
            seconds_remaining=300, market_status="active",
            market_open_time="2026-08-28T12:00:00+00:00",
            market_observed_at="2026-08-28T12:10:00+00:00",
            threshold_state=None, settlement_window={"coverage": 1.0},
            z_distance=3.0, threshold_margin_dollars=100,
            margin_volatility={
                "mvi": mvi, "reliable": True, "reliability_state": "RELIABLE",
                "cushion_ratio": 1.5,
            },
            model_version="test", now=now, execution_mode=mode,
            automatic_enabled=True,
            execution_risk_by_side={
                "YES": {"passed": True}, "NO": {"passed": True}
            },
            portfolio={
                "automatic_trade_allowed": True,
                "automatic_trade_block_reason": None,
                "available_cash": 1000,
            },
        )

    run(0, 6.0)
    progressing = run(3, 6.0)["standard_edge_readiness"]
    assert progressing["metrics"]["confirmation"]["progress"] > 0
    blocked = run(4, 8.0)["standard_edge_readiness"]
    assert blocked["mode"] == mode
    assert blocked["gates"]["volatility"]["passed"] is False
    assert blocked["metrics"]["confirmation"]["progress"] == 0
    assert "volatility" in blocked["blocker"].lower()
