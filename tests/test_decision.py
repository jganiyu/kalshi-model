from __future__ import annotations

import pytest

from app.services.decision import make_decision, material_change


SETTINGS = {
    "risk_controls_enabled": True,
    "max_session_drawdown_pct": 0.10,
    "slippage_cents": 0.5,
    "min_edge": 0.05,
    "fractional_kelly": 0.25,
    "max_position_pct": 0.05,
    "max_risk_per_trade_pct": 0.02,
}

MARKET = {
    "yes_bid": 0.59,
    "yes_ask": 0.60,
    "no_bid": 0.40,
    "no_ask": 0.41,
    "spread": 0.01,
    "yes_ask_size": 100,
    "no_ask_size": 100,
}


def decide(**overrides):
    values = {
        "model_probability": 0.72,
        "market": MARKET,
        "settings": SETTINGS,
        "bankroll": 1_000,
        "data_quality": {"reliable": True},
        "calibration": {"sample_size": 60, "calibration_error": 0.05},
        "model_variant_spread": 0.02,
    }
    values.update(overrides)
    return make_decision(**values)


def test_data_quality_blocks_trade() -> None:
    result = decide(data_quality={"reliable": False, "reason": "stale feed"})
    assert result.signal == "HOLD"
    assert result.reason_code == "DATA_UNRELIABLE"


def test_benchmark_uncertainty_forces_hold_without_degrading_live_data() -> None:
    result = decide(
        data_quality={
            "reliable": True,
            "trade_allowed": False,
            "reason_code": "BENCHMARK_UNCERTAINTY",
            "reason": "Hold: proxy is inside the benchmark band.",
        }
    )

    assert result.signal == "HOLD"
    assert result.reason_code == "BENCHMARK_UNCERTAINTY"
    assert result.edge == pytest.approx(0.125)


def test_positive_ev_produces_yes_signal_and_conservative_size() -> None:
    result = decide()
    assert result.signal == "BUY"
    assert result.expected_value > 0
    assert result.suggested_dollars <= 20
    assert result.confidence in {"Moderate", "High"}


def test_selected_side_is_not_replaced_by_a_more_profitable_outcome() -> None:
    expensive = {**MARKET, "yes_bid": 0.78, "yes_ask": 0.80, "no_ask": 0.22}
    result = decide(model_probability=0.72, market=expensive)
    assert result.signal == "SELL"
    assert result.side == "YES"


def test_no_side_is_evaluated_symmetrically() -> None:
    result = decide(model_probability=0.25, selected_side="NO")
    assert result.signal == "BUY"
    assert result.side == "NO"
    assert result.model_probability == pytest.approx(0.75)


def test_material_signal_transition_is_logged() -> None:
    result = decide()
    reason = material_change(
        {"signal": "NO TRADE", "confidence": "Low", "edge": 0.01, "reason_code": "NO_EDGE"},
        result,
        0.05,
    )
    assert reason == "signal changed: NO TRADE -> BUY"


@pytest.mark.parametrize(
    ("side", "up_probability", "expected"),
    [
        ("YES", 0.80, "BUY"),
        ("YES", 0.60, "HOLD"),
        ("YES", 0.40, "SELL"),
        ("NO", 0.20, "BUY"),
        ("NO", 0.60, "HOLD"),
        ("NO", 0.80, "SELL"),
    ],
)
def test_buy_hold_and_sell_are_symmetric(
    side: str, up_probability: float, expected: str
) -> None:
    result = decide(model_probability=up_probability, selected_side=side)
    assert result.signal == expected
    assert result.side == side


def test_sell_is_informational_without_a_position() -> None:
    result = decide(model_probability=0.40, selected_side="YES", held_contracts=0)
    assert result.signal == "SELL"
    assert result.suggested_contracts == 0
    assert "informational" in result.explanation
