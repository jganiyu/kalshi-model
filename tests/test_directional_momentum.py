from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS
from app.db import Database
from app.main import clean_settings_payload
from app.services.decision import Decision, make_trade_assessment
from app.services.directional_momentum import directional_gate, regression_momentum
from app.services.paper import PaperTradingService


def samples(*, slope: float, seconds: int = 15) -> list[tuple[float, float]]:
    return [(float(second), 100.0 + slope * second) for second in range(seconds + 1)]


def test_regression_momentum_uses_full_window_and_reports_fitted_move() -> None:
    rising = regression_momentum(samples(slope=0.2), lookback_seconds=15)
    falling = regression_momentum(samples(slope=-0.2), lookback_seconds=15)

    assert rising["reliable"] is True
    assert rising["direction"] == "UP"
    assert rising["slope_dollars_per_second"] == pytest.approx(0.2)
    assert rising["regression_movement_dollars"] == pytest.approx(3.0)
    assert falling["direction"] == "DOWN"
    assert falling["regression_movement_dollars"] == pytest.approx(-3.0)


def test_regression_requires_continuous_lookback_coverage() -> None:
    incomplete = regression_momentum(
        [(10.0, 100.0), (11.0, 101.0), (15.0, 105.0)],
        lookback_seconds=15,
        now=15,
    )
    gapped = regression_momentum(
        [(0.0, 100.0), (1.0, 101.0), (14.0, 114.0), (15.0, 115.0)],
        lookback_seconds=15,
        now=15,
    )
    assert incomplete["reliable"] is False
    assert gapped["reliable"] is False


def test_direction_gate_is_side_aware_and_honors_minimum_movement() -> None:
    settings = {
        "directional_momentum_gate_enabled": True,
        "directional_momentum_lookback_seconds": 15,
        "directional_momentum_minimum_movement_dollars": 1,
    }
    rising = regression_momentum(samples(slope=0.1), lookback_seconds=15)
    weak = regression_momentum(samples(slope=0.05), lookback_seconds=15)
    falling = regression_momentum(samples(slope=-0.1), lookback_seconds=15)

    assert directional_gate(settings, rising, side="YES")["passed"] is True
    assert directional_gate(settings, rising, side="NO")["passed"] is False
    assert directional_gate(settings, falling, side="NO")["passed"] is True
    assert directional_gate(settings, falling, side="YES")["passed"] is False
    assert directional_gate(settings, weak, side="YES")["passed"] is False


def make_service(tmp_path: Path) -> tuple[Database, PaperTradingService]:
    db = Database(tmp_path / "direction.db")
    db.initialize()
    db.update_settings(
        {
            "paper_trading_enabled": True,
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "swing_enabled": False,
            "threshold_margin_gate_dollars": 0,
            "directional_momentum_gate_enabled": True,
            "directional_momentum_lookback_seconds": 15,
            "directional_momentum_minimum_movement_dollars": 1,
            "automatic_confirmation_seconds": 5,
            "automatic_min_confidence": "Moderate",
        }
    )
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,raw_json,first_seen_at,updated_at
        ) VALUES ('DIRECTION','DIRECTION','active','test',100,?,?,?,?,?,?)
        """,
        (
            "2026-08-31T12:00:00+00:00",
            "2026-08-31T12:15:00+00:00",
            "2026-08-31T12:15:00+00:00",
            "{}",
            "2026-08-31T12:00:00+00:00",
            "2026-08-31T12:00:00+00:00",
        ),
    )
    return db, PaperTradingService(db)


def assessment_and_decisions() -> tuple[dict[str, dict], dict[str, Decision]]:
    market = {
        "yes_bid": 0.38,
        "yes_ask": 0.40,
        "no_bid": 0.60,
        "no_ask": 0.62,
        "yes_ask_size": 1_000,
        "no_ask_size": 1_000,
    }
    quality = {"reliable": True, "trade_allowed": True, "reason": "current"}
    assessments = {
        side: make_trade_assessment(
            up_probability=0.75,
            market=market,
            settings={"slippage_cents": 0.5},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }
    buy = assessments["YES"]["buy"]
    decisions = {
        "YES": Decision(
            "BUY", "BUY_EDGE", "Moderate", "fixture", 0.75, 0.39,
            float(buy["net_edge"]), float(buy["expected_value"]),
            float(buy["executable_price"]), float(buy["fee_per_contract"]),
            0.02, 20.0, 20, "YES",
        ),
        "NO": Decision(
            "HOLD", "NO_EDGE", "Low", "fixture", 0.25, 0.61, 0.0,
            None, None, None, 0.0, 0.0, 0, "NO",
        ),
    }
    return assessments, decisions


@pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE"])
def test_direction_gate_blocks_and_resets_confirmation_in_every_mode(
    tmp_path: Path, mode: str
) -> None:
    _db, service = make_service(tmp_path)
    assessments, decisions = assessment_and_decisions()

    def run(now: float, movement: float) -> dict:
        return service.consider_strategies(
            ticker="DIRECTION",
            assessments=assessments,
            standard_decisions=decisions,
            seconds_remaining=300,
            market_status="active",
            market_open_time="2026-08-31T12:00:00+00:00",
            market_observed_at="2026-08-31T12:10:00+00:00",
            threshold_state=None,
            settlement_window={"coverage": 1.0},
            z_distance=3.0,
            threshold_margin_dollars=100,
            directional_momentum={
                "reliable": True,
                "regression_movement_dollars": movement,
                "slope_dollars_per_second": movement / 15,
                "direction": "UP" if movement > 0 else "DOWN",
                "sample_count": 16,
                "coverage_seconds": 15,
            },
            model_version="test",
            now=now,
            execution_mode=mode,
            automatic_enabled=True,
            execution_risk_by_side={"YES": {"passed": True}},
            portfolio={
                "automatic_trade_allowed": True,
                "automatic_trade_block_reason": None,
                "available_cash": 1_000,
            },
        )

    run(0, 2.0)
    progressing = run(3, 2.0)["standard_edge_readiness"]
    assert progressing["gates"]["directional_momentum"]["passed"] is True
    assert progressing["metrics"]["confirmation"]["progress"] > 0

    reversed_state = run(4, -2.0)["standard_edge_readiness"]
    assert reversed_state["gates"]["directional_momentum"]["passed"] is False
    assert reversed_state["metrics"]["confirmation"]["progress"] == 0
    assert "regression rise" in reversed_state["blocker"]


def test_direction_defaults_and_settings_validation() -> None:
    assert DEFAULT_SETTINGS["directional_momentum_gate_enabled"] is True
    assert DEFAULT_SETTINGS["directional_momentum_lookback_seconds"] == 15
    assert DEFAULT_SETTINGS[
        "directional_momentum_minimum_movement_dollars"
    ] == pytest.approx(1.0)
    cleaned = clean_settings_payload(
        {
            "directional_momentum_gate_enabled": True,
            "directional_momentum_lookback_seconds": 20,
            "directional_momentum_minimum_movement_dollars": 2.5,
        }
    )
    assert cleaned == {
        "directional_momentum_gate_enabled": True,
        "directional_momentum_lookback_seconds": 20.0,
        "directional_momentum_minimum_movement_dollars": 2.5,
    }
