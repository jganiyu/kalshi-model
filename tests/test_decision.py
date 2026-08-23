from __future__ import annotations

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
        "drawdown_pct": 0.0,
        "data_quality": {"reliable": True},
        "calibration": {"sample_size": 60, "calibration_error": 0.05},
        "model_variant_spread": 0.02,
    }
    values.update(overrides)
    return make_decision(**values)


def test_data_quality_blocks_trade() -> None:
    result = decide(data_quality={"reliable": False, "reason": "stale feed"})
    assert result.signal == "NO TRADE"
    assert result.reason_code == "DATA_UNRELIABLE"


def test_drawdown_risk_control_blocks_trade() -> None:
    result = decide(drawdown_pct=0.11)
    assert result.signal == "NO TRADE"
    assert result.reason_code == "RISK_LIMIT"


def test_positive_ev_produces_yes_signal_and_conservative_size() -> None:
    result = decide()
    assert result.signal == "TRADE YES"
    assert result.expected_value > 0
    assert result.suggested_dollars <= 20
    assert result.confidence in {"Moderate", "High"}


def test_high_probability_does_not_force_a_yes_trade_at_bad_price() -> None:
    expensive = {**MARKET, "yes_bid": 0.78, "yes_ask": 0.80, "no_ask": 0.22}
    result = decide(model_probability=0.72, market=expensive)
    assert result.signal != "TRADE YES"
    assert result.side == "NO"


def test_no_side_is_evaluated_symmetrically() -> None:
    result = decide(model_probability=0.25)
    assert result.signal == "TRADE NO"
    assert result.side == "NO"


def test_material_signal_transition_is_logged() -> None:
    result = decide()
    reason = material_change(
        {"signal": "NO TRADE", "confidence": "Low", "edge": 0.01, "reason_code": "NO_EDGE"},
        result,
        0.05,
    )
    assert reason == "signal changed: NO TRADE -> TRADE YES"
